# -*- coding: utf-8 -*-
"""Настройка GitHub-репозитория и секретов через REST API (без gh auth).

Использует токен из %TEMP%\\gh_token.txt (скоупы repo+workflow достаточно).
Секреты шифруются sealed box (libsodium) публичным ключом репозитория.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import requests
from nacl import encoding, public

TOKEN_FILE = Path(os.environ["TEMP"]) / "gh_token.txt"
TOKEN = TOKEN_FILE.read_text(encoding="ascii", errors="ignore").strip()
API = "https://api.github.com"
OWNER = "shame-h9w"
REPO = "gift-notifier"
S = requests.Session()
S.headers.update({"Authorization": f"Bearer {TOKEN}",
                  "Accept": "application/vnd.github+json",
                  "User-Agent": "gift-notifier-setup"})


def api(method: str, url: str, **kw):
    r = S.request(method, f"{API}{url}", timeout=30, **kw)
    return r


def ensure_repo() -> None:
    r = api("GET", f"/repos/{OWNER}/{REPO}")
    if r.status_code == 200:
        print(f"репо уже есть: {r.json().get('html_url')}")
        info = r.json()
        if info.get("private"):
            api("PATCH", f"/repos/{OWNER}/{REPO}", json={"private": False})
            print("переключён в public (нужен для 24/7 Actions)")
        return
    if r.status_code == 404:
        r2 = api("POST", "/user/repos", json={
            "name": REPO, "private": False, "description":
            "PROFIT-notifier: Telegram NFT gift alerts (Telethon userbot, 24/7 GitHub Actions)",
            "auto_init": False,
        })
        if r2.status_code == 201:
            print(f"репо создан: {r2.json().get('html_url')}")
            return
        print("create failed:", r2.status_code, r2.text[:300])
        sys.exit(1)
    print("get repo failed:", r.status_code, r.text[:300])
    sys.exit(1)


def set_secret(name: str, value: str) -> None:
    pk = api("GET", f"/repos/{OWNER}/{REPO}/actions/secrets/public-key").json()
    key = public.PublicKey(pk["key"].encode(), encoding.Base64Encoder())
    sealed = public.SealedBox(key).encrypt(value.encode("utf-8"))
    r = api("PUT", f"/repos/{OWNER}/{REPO}/actions/secrets/{name}",
            json={"encrypted_value": base64.b64encode(sealed).decode(),
                  "key_id": pk["key_id"]})
    print(f"secret {name}: HTTP {r.status_code} {'OK' if r.status_code in (201, 204) else r.text[:200]}")


if __name__ == "__main__":
    ensure_repo()
    # Секреты без сессии (SESSION_STRING добавим после генерации).
    tracker_env = Path(__file__).parent.parent / "gift-market-tracker" / ".env"
    env = {}
    for line in tracker_env.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    set_secret("API_ID", env["API_ID"])
    set_secret("API_HASH", env["API_HASH"])
    set_secret("CHANNEL_TARGET", "https://t.me/c/3747516067/39")
    if len(sys.argv) > 1 and sys.argv[1] == "--with-session":
        sess = json.loads((Path(__file__).parent / "config.local.json")
                          .read_text(encoding="utf-8"))["session"]
        set_secret("SESSION_STRING", sess)
