# gift-notifier — ПРОФИТ-нотификатор NFT-подарков (Telethon userbot, 24/7 GitHub Actions)

Юзербот работает под твоим аккаунтом (**@Gifts_Replayer**) и мгновенно сообщает,
когда на аккаунт передан NFT-подарок (или пришёл оффер на покупку).

## Что делает

1. **Реальное время** — слушает MTProto-события (`events.Raw`): сервис-сообщения
   `messageActionStarGift` / `messageActionStarGiftUnique` /
   `messageActionStarGiftPurchaseOffer` с объектом `starGiftUnique`.
2. **Страховка** — раз в минуту опрашивает `payments.getSavedStarGifts` («Избранное»
   подарки профиля) и уведомляет о новых. Это же закрывает паузы между рестартами
   воркеров GitHub Actions.
3. По каждому событию отправляет:
   - в **Избранное** (Saved Messages);
   - в **приватный канал** `https://t.me/c/3747516067/39` (реплай в пост/топик 39);
   - под каждой карточкой — **нативная анимация подарка** (`.tgs`, gzip-Lottie
     с CDN Fragment, MIME `application/x-tgsticker`).

## Формат карточки

```
ПРОФИТ!            ← blockquote + bold

Тип: «Фейк Офферы»
Подарки: «XmasStocking-223543»
Ссылка на подарок: https://t.me/nft/XmasStocking-223543
```

## Как работает 24/7 на GitHub Actions

- Workflow `gifts-notify` стартует по cron `*/5 * * * *`.
- Каждый воркер живёт `RUN_SECONDS` ≈ 5.3 часа (меньше `timeout-minutes: 352`),
  поэтому соединение с Telegram почти всегда активное.
- GitHub сам пропускает запуск, пока предыдущий воркер жив; новый стартует в
  ближайшую 5-минутную границу после завершения → простой между воркерами
  в среднем 0–3 минуты, максимум ~8. Подарки, пришедшие в этот простой,
  уведомлением закрывает стартовый опрос (`RECENT_GRACE=900` сек).
- **Public-репозиторий обязателен**: на бесплатном тарифе приватные репы имеют
  ~2000 Actions-минут/мес, а 24/7 требует ~43 000. В коде секретов нет — всё в
  GitHub Secrets; `state/state.json` хранит только хэши id подарков.

## Секреты (Settings → Secrets and variables → Actions)

| Secret | Что это |
|---|---|
| `API_ID`, `API_HASH` | с my.telegram.org (уже есть — берутся из `gift-market-tracker/.env`) |
| `SESSION_STRING` | строка сессии Telethon (генерируется `gen_session.py`) |
| `CHANNEL_TARGET` | `https://t.me/c/3747516067/39` |

Необязательные env (можно добавить как переменные/секреты):
`POLL_SECONDS=60`, `RECENT_GRACE=900`, `NOTIFY_ON_GIFT=1`, `NOTIFY_ON_OFFER=1`,
`REPLY_TO_ID` (если надо слать без реплая — поставь `0` и убери `/39` из ссылки).

## Локальная генерация сессии (один раз)

```bat
cd gift-notifier
.venv\Scripts\python gen_session.py
```
- введёшь телефон @Gifts_Replayer, код из Telegram, пароль 2FA;
- появится `config.local.json` со строкой `session` (в git не попадает);
- локальное подключение автоматически идёт через твой MTProto-прокси
  (TgWsProxy, `127.0.0.1:1443`) — он подтягивается из `gift-market-tracker/.env`.

Файловый режим (если вводить руками неудобно): `python gen_session.py --poll`
и заполнять файлы `.phone`, `.code`, `.password`.

## Локальный смоук-тест

```bat
.venv\Scripts\python test_send.py
```
Отправит тестовую карточку XmasStocking-223543 + анимацию в Избранное и канал.

## Запуск на GitHub

1. Создай **public** репозиторий и запушь папку.
2. Добавь 4 секрета из таблицы выше.
3. Actions → `gifts-notify` → Run workflow (первый прогон), дальше cron сам.

## Запасной вариант анимации

Основной путь — готовый `.tgs` с `nft.fragment.com/gift/<slug>.tgs` (это уже
gzip-Lottie). Если CDN недоступен, скрипт качает `nft.fragment.com/gift/<slug>.lottie.json`
и жмёт gzip сам (вариант из ТЗ). Если клиент не принял режим «стикер», отправка
повторяется как «анимированный документ», затем — как обычный файл.

## Файлы

- `bot.py` — основной воркер (события + поллер + отправка).
- `gen_session.py` — генерация SESSION_STRING (локально).
- `test_send.py` — смоук-тест.
- `.github/workflows/notify.yml` — хостинг 24/7.
- `state/state.json` — дедуп (хэши id), коммитится github-actions-ботом.

## Важно про безопасность

`SESSION_STRING` = полный доступ к аккаунту. Хранится только в GitHub Secrets
и в локальном `config.local.json`. Не коммитить, не пересылать.
