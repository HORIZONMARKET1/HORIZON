# HORIZON MARKET bot (Railway)

1. Railway → New Project → Deploy from GitHub (папка horizon-bot) или загрузите файлы.
2. Variables: BOT_TOKEN, FIREBASE_CREDENTIALS_JSON (ключ сервисного аккаунта Firebase целиком), ADMIN_IDS (ваш Telegram ID).
3. Firestore Rules: добавьте
   match /tgSessions/{id} { allow get: if true; allow list, write: if false; }
4. Остановите любые другие запуски этого бота (два getUpdates одновременно не работают).
