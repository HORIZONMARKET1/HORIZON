# HORIZON MARKET bot (Railway)

1. Railway → New Project → Deploy from GitHub (папка horizon-bot) или загрузите файлы.
2. Variables: BOT_TOKEN, FIREBASE_CREDENTIALS_JSON (ключ сервисного аккаунта Firebase целиком), ADMIN_IDS (ваш Telegram ID).
3. Firestore Rules: добавьте
   match /tgSessions/{id} { allow get: if true; allow list, write: if false; }
4. Остановите любые другие запуски этого бота (два getUpdates одновременно не работают).

## HORIZON кредит (верификация + чат HORIZON BANK)
Дополнительные переменные Railway:
- ANTHROPIC_API_KEY — ключ для ИИ-автоответчика (без него чат HORIZON BANK не отвечает).
- PUBLIC_URL — публичный адрес сервиса Railway (Settings → Networking → Generate Domain), например https://horizon-bot.up.railway.app. Нужен для камеры (селфи).
- KYC_ADMIN_IDS — Telegram ID тех, кому приходят заявки на верификацию (по умолчанию ADMIN_IDS).
- ANTHROPIC_MODEL — необязательно.
Иконку чата HORIZON BANK админ ставит в «Админка → Настройки».
Кредитный лимит клиента: «Админка → Пользователи → Лимит» (по умолчанию 500 TJS).
