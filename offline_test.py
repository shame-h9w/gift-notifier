# -*- coding: utf-8 -*-
"""Оффлайн-тест: fetch_anim_bytes (CDN Fragment) + entity-разметка шаблона."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import aiohttp
import bot


async def main():
    async with aiohttp.ClientSession(headers=bot.UA_HEADERS) as http:
        for slug in ("XmasStocking-223543", "PlushPepe-88"):
            data = await bot.fetch_anim_bytes(http, slug)
            ok = bool(data) and data.startswith(b"\x1f\x8b")
            print(f"anim {slug}: {'OK' if ok else 'FAIL'} size={len(data or b'')} gzip_magic={ok}")

    text, ents = bot.build_notification("XmasStocking-223543")
    print("\n--- TEXT ---")
    print(text)
    print("--- ENTITIES ---")
    for e in ents:
        print(f"  {type(e).__name__} offset={e.offset} length={e.length}")

    # проверка: utf16-оффсеты совпадают с нарезкой строки
    s = text.encode("utf-16-le").decode("utf-16-le")
    for e in ents:
        frag = s[e.offset:e.offset + e.length]
        print(f"  slice[{e.offset}:{e.offset+e.length}] = {frag!r}")


asyncio.run(main())
