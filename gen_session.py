#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Генерация SESSION_STRING для @Gifts_Replayer (локально, на твоём ПК).

Нужно один раз:
  • api_id / api_hash — возьмутся из gift-market-tracker/.env (или впиши свои);
  • телефон аккаунта @Gifts_Replayer;
  • код входа (придёт в Telegram этого аккаунта) и пароль 2FA (если включён).

Режимы:
  1) интерактивный:        python gen_session.py
  2) файловый (для агента): python gen_session.py --poll
     - впиши номер в файл .phone (рядом со скриптом);
     - скрипт запросит код → впиши его в .code;
     - если включена 2FA — впиши пароль в .password;
     - результат: config.local.json (сессия хранится ТОЛЬКО локально).

Прокси: если найден gift-market-tracker/.env с MT_PROXY (t.me/proxy?server=...),
локальный вход автоматически идёт через этот MTProto-прокси (TgWsProxy).
На GitHub Actions прокси не нужен — там прямой доступ к Telegram.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

from telethon import TelegramClient, errors
from telethon.network.connection import (
    ConnectionTcpFull,
    ConnectionTcpMTProxyAbridged,
    ConnectionTcpMTProxyIntermediate,
    ConnectionTcpMTProxyRandomizedIntermediate,
)
from telethon.sessions import StringSession

BASE = Path(__file__).parent
CONFIG = BASE / "config.local.json"
TRACKER_ENV = BASE.parent / "gift-market-tracker" / ".env"
DEFAULT_CHANNEL = "https://t.me/c/3747516067/39"


def parse_env_text(txt: str) -> dict:
    out = {}
    for line in txt.splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def load_base_config() -> dict:
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    if TRACKER_ENV.exists():
        env = parse_env_text(TRACKER_ENV.read_text(encoding="utf-8", errors="ignore"))
        if env.get("API_ID") and env.get("API_HASH"):
            return {"api_id": int(env["API_ID"]), "api_hash": env["API_HASH"],
                    "mt_proxy": env.get("MT_PROXY", ""), "phone": env.get("USERBOT_PHONE", "")}
    return {}


def mtproxy_variants(mt_proxy_url: str):
    """Разбор t.me/proxy?server=&port=&secret= → варианты подключения."""
    if not mt_proxy_url:
        return []
    q = urllib.parse.parse_qs(urllib.parse.urlparse(mt_proxy_url).query)
    if not ("server" in q and "port" in q and "secret" in q):
        return []
    host, port, secret = q["server"][0], int(q["port"][0]), q["secret"][0]
    return [
        ("mtproxy-randomized", ConnectionTcpMTProxyRandomizedIntermediate, (host, port, secret)),
        ("mtproxy-intermediate", ConnectionTcpMTProxyIntermediate, (host, port, secret)),
        ("mtproxy-abridged", ConnectionTcpMTProxyAbridged, (host, port, secret)),
    ]


def save_config(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


async def pick_connection(cfg: dict):
    """Возвращаем первый рабочий клиент (подключение удалось)."""
    variants = mtproxy_variants(cfg.get("mt_proxy", "")) + [("direct", ConnectionTcpFull, None)]
    for label, conn_cls, proxy in variants:
        client = TelegramClient(
            StringSession(), int(cfg["api_id"]), cfg["api_hash"],
            connection=conn_cls, proxy=proxy,
            device_model="GiftNotifier", system_version="win", app_version="1.0",
        )
        try:
            await client.connect()
            print(f"[OK] подключение установлено: {label}")
            return client
        except Exception as e:  # noqa: BLE001
            print(f"[..] {label} не подошёл: {type(e).__name__}: {str(e)[:120]}")
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
    raise RuntimeError("ни прямое подключение, ни прокси не сработали")


async def finish_login(client: TelegramClient, cfg: dict) -> None:
    me = await client.get_me()
    session_str = StringSession.save(client.session)
    cfg.update({
        "api_id": int(cfg["api_id"]),
        "api_hash": cfg["api_hash"],
        "session": session_str,
        "channel_target": cfg.get("channel_target") or DEFAULT_CHANNEL,
    })
    save_config(cfg)
    print("=" * 60)
    print(f"Аккаунт: id={me.id} @{'(нет username)' if not me.username else me.username}"
          f" phone={getattr(me, 'phone', '?')}")
    print(f"Сессия сохранена: {CONFIG}")
    print("SESSION_STRING (для GitHub Secrets):")
    print(session_str)
    print("=" * 60)
    print("⚠️  Никому не показывай эту строку — это полный доступ к аккаунту.")


async def interactive(cfg: dict) -> int:
    client = await pick_connection(cfg)
    try:
        phone = (cfg.get("phone") or input("Телефон аккаунта @Gifts_Replayer (+7...): ")).strip()
        await client.send_code_request(phone)
        code = input("Код из Telegram: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except errors.SessionPasswordNeededError:
            pwd = getpass.getpass("Пароль 2FA: ")
            await client.sign_in(password=pwd)
        await finish_login(client, cfg)
        return 0
    finally:
        await client.disconnect()


async def poll_files(cfg: dict, timeout_min: int = 45) -> int:
    f_phone, f_code, f_pass = BASE / ".phone", BASE / ".code", BASE / ".password"
    deadline = time.time() + timeout_min * 60

    print(f"[poll] жду файл .phone (впиши номер, напр. +7952XXXXXXX) → {f_phone}")
    while not f_phone.exists() and time.time() < deadline:
        await asyncio.sleep(3)
    if not f_phone.exists():
        print("[poll] таймаут ожидания .phone")
        return 1
    phone = f_phone.read_text(encoding="utf-8", errors="ignore").strip()

    client = await pick_connection(cfg)
    try:
        await client.send_code_request(phone)
        print(f"[poll] код отправлен. Впиши его в файл .code → {f_code}")
        code = ""
        while time.time() < deadline:
            if f_code.exists():
                code = f_code.read_text(encoding="utf-8", errors="ignore").strip()
                if code:
                    break
            await asyncio.sleep(3)
        if not code:
            print("[poll] таймаут ожидания .code")
            return 1
        try:
            await client.sign_in(phone=phone, code=code)
        except errors.SessionPasswordNeededError:
            print(f"[poll] нужна 2FA. Впиши пароль в файл .password → {f_pass}")
            pwd = ""
            while time.time() < deadline:
                if f_pass.exists():
                    pwd = f_pass.read_text(encoding="utf-8", errors="ignore").strip()
                    if pwd:
                        break
                await asyncio.sleep(3)
            if not pwd:
                print("[poll] таймаут ожидания .password")
                return 1
            await client.sign_in(password=pwd)
        await finish_login(client, cfg)
        for f in (f_phone, f_code, f_pass):
            f.unlink(missing_ok=True)
        return 0
    finally:
        await client.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", action="store_true", help="файловый режим (.phone/.code/.password)")
    args = ap.parse_args()

    cfg = load_base_config()
    if not cfg.get("api_id") or not cfg.get("api_hash"):
        print("Нет api_id/api_hash: заполни config.local.json {\"api_id\":..., \"api_hash\":...} "
              "или положи рядом gift-market-tracker/.env")
        return 2
    return asyncio.run(poll_files(cfg) if args.poll else interactive(cfg))


if __name__ == "__main__":
    sys.exit(main())
