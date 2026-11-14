#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ПРОФИТ-нотификатор NFT-подарков Telegram.

Юзербот (Telethon, MTProto) под аккаунтом владельца SESSION_STRING:
  1) в реальном времени ловит события Raw-апдейтов, в которых появляются
     StarGiftUnique (передача NFT-подарка на аккаунт, покупка/офферы);
  2) параллельно раз в POLL_SECONDS опрашивает payments.getSavedStarGifts
     (страховка на случай пропуска апдейтов между рестартами воркера);
  3) по каждому новому подарку мгновенно отправляет:
       - текстовую карточку «ПРОФИТ!» в Избранное (Saved Messages);
       - текстовую карточку в приватный канал (CHANNEL_TARGET, реплай в топик/пост);
       - нативную анимацию подарка (.tgs = gzip-Lottie с CDN Fragment) ответом
         на карточку в обоих чатах.

Хостинг: GitHub Actions, cron каждые 5 минут, каждый воркер живёт RUN_SECONDS
секунд (меньше, чем timeout-minutes у job) — итого покрытие ≈24/7.

Все секреты приходят из переменных окружения (GitHub Secrets):
  API_ID, API_HASH, SESSION_STRING, CHANNEL_TARGET (опц.), RUN_SECONDS (опц.)
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
from telethon import TelegramClient, events, functions, types
from telethon.sessions import StringSession
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    InputStickerSetEmpty,
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityUrl,
)

# ────────────────────────── Конфигурация (окружение) ──────────────────────────

API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "").strip()
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()

# Куда дублировать карточки. Варианты:
#   https://t.me/c/3747516067/39   → приватный канал id=3747516067, реплай к посту/топику 39
#   https://t.me/c/3747516067      → без реплая
#   @username | -100XXXXXXXXXX     → произвольный чат
CHANNEL_TARGET = os.environ.get("CHANNEL_TARGET", "https://t.me/c/3747516067/39").strip()
REPLY_TO_ID = int(os.environ.get("REPLY_TO_ID", "0") or 0)

RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "19000"))
POLL_SECONDS = max(15, int(os.environ.get("POLL_SECONDS", "60")))
# Сколько секунд «назад» считать свежим подарком при сидировании после рестарта.
RECENT_GRACE = int(os.environ.get("RECENT_GRACE", "900"))

NOTIFY_ON_GIFT = os.environ.get("NOTIFY_ON_GIFT", "1") == "1"
NOTIFY_ON_OFFER = os.environ.get("NOTIFY_ON_OFFER", "1") == "1"

STATE_FILE = Path(os.environ.get("STATE_FILE", "state/state.json"))
STATE_COMMIT = os.environ.get("STATE_COMMIT", "1") == "1"

# ────────────────────────── Константы шаблона/анимации ────────────────────────

TGS_MIME = "application/x-tgsticker"
UA_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}

TITLE_TEXT = "ПРОФИТ!"
TYPE_LABEL = "Фейк Офферы"      # «Тип» из ТЗ — меняем при желании тут
GIFTS_LABEL = "Подарки"
LINK_LABEL = "Ссылка на подарок"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gift-notifier")


def utcnow() -> float:
    return time.time()


# ────────────────────────── Разбор ссылки на канал ────────────────────────────

def parse_channel_target(target: str):
    """→ (pepел_для_get_entity, channel_id_из_/c/ссылки, reply_to)."""
    m = re.match(r"^https?://t\.me/c/(\d+)(?:/(\d+))?", target)
    if m:
        channel_id = int(m.group(1))
        reply_to = REPLY_TO_ID or int(m.group(2) or 0)
        return f"https://t.me/c/{channel_id}", channel_id, reply_to
    return target, None, REPLY_TO_ID


async def resolve_channel(client: TelegramClient, target: str, channel_id: int | None):
    """Надёжно резолвим приватный канал: линк → кэш → скан диалогов."""
    if not target:
        return None
    if not target.startswith("https://t.me/c"):
        try:
            return await client.get_entity(target)
        except Exception as e:  # noqa: BLE001
            log.warning("get_entity(%s) failed: %s", target, e)
    if channel_id:
        try:
            return await client.get_entity(types.PeerChannel(channel_id))
        except Exception:  # noqa: BLE001
            pass
        wanted = -(10**12 + channel_id)  # marked id вида -100xxxxxxxxxx
        try:
            async for d in client.iter_dialogs():
                if d.id in (wanted, channel_id):
                    return d.entity
        except Exception as e:  # noqa: BLE001
            log.warning("iter_dialogs failed: %s", e)
    return None


# ────────────────────────── Шаблон сообщения + сущности ───────────────────────

def u16len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def build_notification(slug: str) -> tuple[str, list]:
    """Карточка строго по ТЗ + entities (blockquote, bold, url). Оффсеты в UTF-16."""
    link = f"https://t.me/nft/{slug}"
    line_type = f"Тип: «{TYPE_LABEL}»"
    line_gift = f"{GIFTS_LABEL}: «{slug}»"
    line_link = f"{LINK_LABEL}: {link}"

    text = "\n".join([TITLE_TEXT, "", line_type, line_gift, line_link])
    ents: list = []

    # Заголовок: blockquote + bold (первые u16len(TITLE_TEXT) юнитов).
    ents.append(MessageEntityBlockquote(offset=0, length=u16len(TITLE_TEXT)))
    ents.append(MessageEntityBold(offset=0, length=u16len(TITLE_TEXT)))

    pos = u16len(TITLE_TEXT) + 1 + 1  # +\n + пустая строка
    for line, label in ((line_type, "Тип:"), (line_gift, f"{GIFTS_LABEL}:"),
                        (line_link, f"{LINK_LABEL}:")):
        ents.append(MessageEntityBold(offset=pos, length=u16len(label)))
        if line is line_link:
            off = pos + u16len(f"{LINK_LABEL}: ")
            ents.append(MessageEntityUrl(offset=off, length=u16len(link)))
        pos += u16len(line) + 1
    return text, ents


# ────────────────────────── Анимация подарка (.tgs) ───────────────────────────

async def fetch_anim_bytes(http: aiohttp.ClientSession, slug: str) -> bytes | None:
    """Lottie-анимация с CDN Fragment.

    Первичен готовый .tgs (это уже gzip-Lottie). Запасной путь по ТЗ — скачать
    .lottie.json и сжать в gzip самому. Итог валидируем по gzip-магии 1f 8b.
    """
    urls = [
        f"https://nft.fragment.com/gift/{slug}.tgs",
        f"https://nft.fragment.com/gift/{slug.lower()}.tgs",
        f"https://nft.fragment.com/gift/{slug}.lottie.json",
        f"https://nft.fragment.com/gift/{slug.lower()}.lottie.json",
    ]
    for url in urls:
        try:
            async with http.get(url, headers=UA_HEADERS,
                                timeout=aiohttp.ClientTimeout(total=45)) as r:
                if r.status != 200:
                    continue
                data = await r.read()
        except Exception as e:  # noqa: BLE001
            log.warning("anim fetch %s: %s", url, e)
            continue
        if not data or len(data) < 64:
            continue
        if url.endswith(".lottie.json") and not data.startswith(b"\x1f\x8b"):
            if not data.lstrip().startswith(b"{"):
                continue  # это не Lottie
            data = gzip.compress(data)  # JSON → .tgs (gzip)
        elif not data.startswith(b"\x1f\x8b"):
            data = gzip.compress(data)  # перестраховка
        return data
    log.warning("анимация для %s не найдена ни по одному URL", slug)
    return None


def _anim_attrs_variants(slug: str):
    fname = DocumentAttributeFilename(f"{slug}.tgs")
    yield "sticker", [DocumentAttributeSticker(alt="", stickerset=InputStickerSetEmpty()), fname]
    yield "animated", [DocumentAttributeAnimated(), fname]
    yield "plain", [fname]


async def send_anim(client: TelegramClient, dest, slug: str, data: bytes, reply_to: int | None):
    for label, attrs in _anim_attrs_variants(slug):
        bio = io.BytesIO(data)
        bio.name = f"{slug}.tgs"
        try:
            msg = await client.send_file(
                dest, bio, mime_type=TGS_MIME, attributes=attrs,
                force_document=True, reply_to=reply_to, parse_mode=None,
            )
            log.info("анимация %s отправлена (режим %s)", slug, label)
            return msg
        except Exception as e:  # noqa: BLE001
            log.warning("анимация %s режим %s не принялся: %s", slug, label, e)
    return None


# ────────────────────────── Состояние (дедуп между воркерами) ─────────────────

def key_hash(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:20]


def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        st.setdefault("v", 1)
        st.setdefault("first_done", False)
        st.setdefault("seen", [])
        return st
    except Exception:  # noqa: BLE001
        return {"v": 1, "first_done": False, "seen": []}


def save_state(st: dict) -> None:
    st["seen"] = st["seen"][-2000:]
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("state write failed: %s", e)


def git_commit_state(force: bool = False) -> None:
    """Фиксируем state/state.json (только хэши id подарков, без приватных данных)."""
    if not STATE_COMMIT:
        return
    if not force and os.environ.get("GITHUB_ACTIONS") != "true":
        return
    try:
        subprocess.run(["git", "add", str(STATE_FILE.parent)],
                       check=False, capture_output=True, timeout=60)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"],
                              check=False, capture_output=True, timeout=60)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", "state: seen gifts"],
                           check=False, capture_output=True, timeout=60)
            subprocess.run(["git", "push"], check=False, capture_output=True, timeout=120)
            log.info("state закоммичен")
    except Exception as e:  # noqa: BLE001
        log.warning("state commit skipped: %s", e)


# ────────────────────────── Извлечение подарков из апдейтов ───────────────────

GIFT_ACTIONS = (
    types.MessageActionStarGift,                  # подарок (обычный/NFT) передан
    types.MessageActionStarGiftUnique,            # апгрейд/трансфер/резейл NFT
    types.MessageActionStarGiftPurchaseOffer,     # входящий оффер на покупку
    types.MessageActionStarGiftPurchaseOfferDeclined,
)


def extract_gift_event(update, me_id: int):
    """Из Raw-апдейта достаём (StarGiftUnique, kind) либо None."""
    if type(update) not in (types.UpdateNewMessage, types.UpdateEditMessage):
        return None
    msg = getattr(update, "message", None)
    action = getattr(msg, "action", None)
    if not isinstance(action, GIFT_ACTIONS):
        return None

    # Свои исходящие действия (мы отправили подарок/оффер) — не «профит».
    if not isinstance(action, types.MessageActionStarGiftUnique) and getattr(msg, "out", False):
        return None
    if isinstance(action, types.MessageActionStarGiftUnique):
        to_id = getattr(action, "to_id", None)
        if to_id is not None and getattr(to_id, "user_id", None) not in (None, me_id):
            return None

    gift = getattr(action, "gift", None)
    if not isinstance(gift, types.StarGiftUnique):
        return None  # обычный (не-NFT) подарок — мимо ТЗ

    if isinstance(action, types.MessageActionStarGiftPurchaseOfferDeclined):
        kind = "offer_declined"
    elif isinstance(action, types.MessageActionStarGiftPurchaseOffer):
        kind = "offer"
    else:
        kind = "gift"
    return gift, kind


# ────────────────────────── Опрос saved-подарков ──────────────────────────────

async def fetch_saved_gifts(client: TelegramClient) -> list:
    out, offset = [], ""
    for _ in range(10):
        res = await client(functions.payments.GetSavedStarGiftsRequest(
            peer="me", offset=offset, limit=100))
        out.extend(getattr(res, "gifts", None) or [])
        nxt = getattr(res, "next_offset", None)
        if not nxt:
            break
        offset = nxt
    return out


# ────────────────────────── Доставка уведомления ──────────────────────────────

async def deliver(client: TelegramClient, http: aiohttp.ClientSession,
                  channel_entity, reply_to: int,
                  gift: types.StarGiftUnique, kind: str, anim_cache: dict) -> None:
    slug = getattr(gift, "slug", None) or (
        f"{(getattr(gift, 'title', '') or 'Gift').replace(' ', '')}-{getattr(gift, 'num', 0)}")
    text, ents = build_notification(slug)

    m_saved = None
    m_chan = None
    try:
        m_saved = await client.send_message(
            "me", text, parse_mode=None, formatting_entities=ents, link_preview=False)
    except Exception as e:  # noqa: BLE001
        log.error("отправка в Избранное не удалась: %s", e)

    if channel_entity is not None:
        try:
            m_chan = await client.send_message(
                channel_entity, text, parse_mode=None, formatting_entities=ents,
                link_preview=False, reply_to=reply_to or None)
        except Exception as e:  # noqa: BLE001
            log.error("отправка в канал не удалась: %s", e)

    if slug not in anim_cache:
        anim_cache[slug] = await fetch_anim_bytes(http, slug)
    anim = anim_cache[slug]

    if anim:
        if m_saved is not None:
            await send_anim(client, "me", slug, anim, m_saved.id)
        if m_chan is not None:
            await send_anim(client, channel_entity, slug, anim, m_chan.id)

    log.info("ПРОФИТ-уведомление доставлено: %s (kind=%s, num=%s)",
             slug, kind, getattr(gift, "num", "?"))


async def notify_worker(client: TelegramClient, http: aiohttp.ClientSession,
                        channel_entity_holder: dict, reply_to: int,
                        queue: "asyncio.Queue", st: dict, anim_cache: dict) -> None:
    while True:
        gift, kind = await queue.get()
        try:
            await deliver(client, http, channel_entity_holder.get("entity"), reply_to,
                          gift, kind, anim_cache)
        except Exception:  # noqa: BLE001
            log.exception("deliver failed for %s", getattr(gift, "slug", "?"))
        finally:
            queue.task_done()


# ────────────────────────── main ──────────────────────────────────────────────

async def main() -> int:
    if not (API_ID and API_HASH and SESSION_STRING):
        log.error("Нужны API_ID, API_HASH и SESSION_STRING (GitHub Secrets / env).")
        return 2

    started = utcnow()
    client = TelegramClient(
        StringSession(SESSION_STRING), API_ID, API_HASH,
        auto_reconnect=True, connection_retries=None, retry_delay=5,
        flood_sleep_threshold=120,
    )
    await client.connect()
    if not await client.is_user_authorized():
        log.error("Сессия не авторизована — перегенерируй SESSION_STRING (gen_session.py).")
        return 3

    me = await client.get_me()
    log.info("онлайн: id=%s @%s (%s)", me.id, me.username or "-", (me.first_name or "").strip())

    st = load_state()
    seen: set[str] = set(st["seen"])

    queue: asyncio.Queue = asyncio.Queue()
    anim_cache: dict[str, bytes] = {}
    channel_holder: dict = {"entity": None}

    target, channel_id, reply_to = parse_channel_target(CHANNEL_TARGET)
    channel_holder["entity"] = await resolve_channel(client, target, channel_id)
    if channel_holder["entity"] is not None:
        log.info("канал: %s (id=%s)", getattr(channel_holder["entity"], "title", "?"),
                 channel_id)
    else:
        log.warning("канал не разрешился (%s) — уведомления пойдут только в Избранное; "
                    "проверь, что аккаунт участник канала", target)

    http = aiohttp.ClientSession(headers=UA_HEADERS)

    async def handle(gift: types.StarGiftUnique, kind: str) -> None:
        gkey = key_hash(f"gift:{gift.id}")
        if gkey in seen:
            return
        if (kind.startswith("offer") and not NOTIFY_ON_OFFER) or \
           (kind == "gift" and not NOTIFY_ON_GIFT):
            seen.add(gkey)
            return
        seen.add(gkey)
        st["seen"].append(gkey)
        await queue.put((gift, kind))

    # 1) Реальное время: Raw-апдейты с сервис-сообщениями о подарках/офферах.
    @client.on(events.Raw())
    async def on_raw(update) -> None:  # noqa: ANN001
        try:
            ev = extract_gift_event(update, me.id)
            if ev:
                await handle(*ev)
        except Exception:  # noqa: BLE001
            log.exception("raw handler failed")

    # 2) Сидирование: первый запуск помечает всё существующее как seen без уведомлений,
    #    последующие — уведомляют только свежее (упавшее в паузу между воркерами).
    saved = await fetch_saved_gifts(client)
    log.info("saved gifts: %d шт.", len(saved))
    for sg in saved:
        gift = getattr(sg, "gift", None)
        if not isinstance(gift, types.StarGiftUnique):
            continue
        gkey = key_hash(f"gift:{gift.id}")
        if gkey in seen:
            continue
        seen.add(gkey)
        st["seen"].append(gkey)
        if not st["first_done"]:
            continue  # первый запуск: тихо засеяли
        date = getattr(sg, "date", None) or 0
        if date and date >= started - RECENT_GRACE:
            await queue.put((gift, "gift"))

    if not st["first_done"]:
        st["first_done"] = True
    save_state(st)
    git_commit_state(force=True)

    worker = asyncio.create_task(notify_worker(
        client, http, channel_holder, reply_to, queue, st, anim_cache))

    # 3) Страховочный поллер раз в POLL_SECONDS.
    async def poll_loop() -> None:
        while utcnow() < started + RUN_SECONDS:
            await asyncio.sleep(POLL_SECONDS)
            try:
                for sg in await fetch_saved_gifts(client):
                    gift = getattr(sg, "gift", None)
                    if isinstance(gift, types.StarGiftUnique):
                        await handle(gift, "gift")
                if channel_holder.get("entity") is None:
                    channel_holder["entity"] = await resolve_channel(client, target, channel_id)
                if not queue.empty():
                    save_state(st)
            except Exception:  # noqa: BLE001
                log.exception("poll failed")

    poller = asyncio.create_task(poll_loop())

    # 4) Живём RUN_SECONDS, аккуратно гасимся (следующий cron-воркер подхватит).
    try:
        while utcnow() < started + RUN_SECONDS:
            await asyncio.sleep(min(60, started + RUN_SECONDS - utcnow()))
            if not queue.empty():
                await queue.join()
    finally:
        poller.cancel()
        try:
            await asyncio.wait_for(queue.join(), timeout=180)
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("очередь не опустела за 180с — гасимся")
        worker.cancel()
        save_state(st)
        git_commit_state(force=True)
        await http.close()
        await client.disconnect()
        log.info("воркер завершён штатно")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
