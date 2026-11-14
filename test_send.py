#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Локальный смоук-тест: подключение, резолв канала, тестовые карточка + анимация.

Запуск:  python test_send.py            # карточка XmasStocking-223543 в Избранное и канал
         python test_send.py --no-anim  # только текст
Читает config.local.json (создаётся gen_session.py).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.parse
from pathlib import Path

from telethon import TelegramClient, functions, types
from telethon.network.connection import ConnectionTcpFull
from telethon.sessions import StringSession

import bot  # переиспользуем шаблон/отправку

BASE = Path(__file__).parent
CONFIG = BASE / "config.local.json"


def proxy_from_cfg(cfg: dict):
    """cfg['proxy'] = 'mtproto://host:port#secret' или пусто → direct."""
    raw = (cfg.get("proxy") or "").strip()
    if not raw:
        return ConnectionTcpFull, None
    u = urllib.parse.urlparse(raw)
    if u.scheme != "mtproto":
        return ConnectionTcpFull, None
    secret = urllib.parse.unquote(u.fragment or "")
    cls = ConnectionTcpMTProxyRandomizedIntermediate
    return cls, (u.hostname, u.port, secret)


async def main(no_anim: bool) -> int:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    conn_cls, proxy = proxy_from_cfg(cfg)

    client = TelegramClient(StringSession(cfg["session"]), cfg["api_id"], cfg["api_hash"],
                            connection=conn_cls, proxy=proxy)
    await client.connect()
    if not await client.is_user_authorized():
        print("сессия не авторизована")
        return 3
    me = await client.get_me()
    print(f"онлайн: id={me.id} @{me.username}")

    target, channel_id, reply_to = bot.parse_channel_target(cfg.get("channel_target", ""))
    entity = await bot.resolve_channel(client, target, channel_id)
    if entity is not None:
        try:
            full = await client(functions.channels.GetFullChannelRequest(entity))
            print(f"канал: {entity.title} | marked id={entity.id} | "
                  f"forum={getattr(entity, 'forum', False)} | "
                  f"full_id=-100{channel_id or ''}")
        except Exception:  # noqa: BLE001
            print(f"канал: {entity.title} | marked id={entity.id}")
    else:
        print("!! канал НЕ разрешился — проверь членство аккаунта в канале")

    slug = "XmasStocking-223543"
    text, ents = bot.build_notification(slug)

    m1 = await client.send_message("me", text, parse_mode=None, formatting_entities=ents,
                                   link_preview=False)
    print(f"текст → Избранное: message_id={m1.id}")

    m2 = None
    if entity is not None:
        m2 = await client.send_message(entity, text, parse_mode=None, formatting_entities=ents,
                                       link_preview=False, reply_to=reply_to or None)
        print(f"текст → канал: message_id={m2.id}")

    if not no_anim:
        http = bot.aiohttp.ClientSession(headers=bot.UA_HEADERS)
        try:
            anim = await bot.fetch_anim_bytes(http, slug)
            if anim:
                print(f"анимация скачана: {len(anim)} байт")
                await bot.send_anim(client, "me", slug, anim, m1.id)
                if entity is not None and m2 is not None:
                    await bot.send_anim(client, entity, slug, anim, m2.id)
        finally:
            await http.close()

    await client.disconnect()
    print("ГОТОВО — проверь Избранное и канал")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-anim", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args().no_anim)))
