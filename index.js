/**
 * HORIZON MARKET — серверные push-уведомления.
 *
 * Эти функции реагируют на изменения в Firestore и реально отправляют
 * push через Firebase Cloud Messaging (FCM) — они доходят до клиента,
 * даже если у него закрыт и сайт, и браузер (пока телефон/компьютер онлайн).
 *
 * Деплой (один раз настроить, дальше просто `firebase deploy --only functions`):
 *   1) npm i -g firebase-tools
 *   2) firebase login
 *   3) firebase init functions   (в папке проекта, выбрать существующий проект horizon-bdbf0,
 *                                  язык JavaScript, но при этом просто заменить сгенерированный
 *                                  functions/index.js и functions/package.json этими файлами)
 *   4) firebase deploy --only functions
 *
 * Требуется тарифный план Firebase "Blaze" (pay-as-you-go) — без него Cloud Functions
 * и исходящие вызовы FCM из функций не работают. Free-квоты Blaze обычно с запасом
 * покрывают такой магазин.
 */

const { onDocumentCreated, onDocumentUpdated } = require('firebase-functions/v2/firestore');
const admin = require('firebase-admin');
admin.initializeApp();

const db = admin.firestore();
const messaging = admin.messaging();

/* ---------- Вспомогательное: отправить пуш по списку токенов, удалить нерабочие ---------- */
async function sendToTokens(tokens, notification, data) {
  const uniqueTokens = [...new Set(tokens)].filter(Boolean);
  if (uniqueTokens.length === 0) return;

  // FCM принимает максимум 500 токенов за раз
  const chunks = [];
  for (let i = 0; i < uniqueTokens.length; i += 500) chunks.push(uniqueTokens.slice(i, i + 500));

  for (const chunk of chunks) {
    const resp = await messaging.sendEachForMulticast({
      tokens: chunk,
      notification,
      data: data || {},
      webpush: {
        notification: { icon: 'https://raw.githubusercontent.com/gauravghongde/logos/main/logo192.png' },
        fcmOptions: { link: (data && data.click_action) || '/' }
      }
    });
    // Чистим токены, которые больше не действительны (юзер отписался/удалил браузер и т.п.)
    const toDelete = [];
    resp.responses.forEach((r, i) => {
      if (!r.success) {
        const code = r.error && r.error.code;
        if (code === 'messaging/registration-token-not-registered' || code === 'messaging/invalid-registration-token') {
          toDelete.push(chunk[i]);
        }
      }
    });
    await Promise.all(toDelete.map(t => db.collection('pushTokens').doc(t).delete().catch(() => {})));
  }
}

/* ---------- Получить токены по списку userId (учитывая лимит Firestore 'in' = 30) ---------- */
async function getTokensForUserIds(userIds) {
  const ids = [...new Set(userIds)].filter(Boolean);
  if (ids.length === 0) return [];
  const tokens = [];
  for (let i = 0; i < ids.length; i += 30) {
    const batch = ids.slice(i, i + 30);
    const snap = await db.collection('pushTokens').where('userId', 'in', batch).get();
    snap.forEach(d => tokens.push(d.data().token));
  }
  return tokens;
}

/* ---------- Все токены (для широковещательных уведомлений типа "новый товар") ---------- */
async function getAllTokens() {
  const snap = await db.collection('pushTokens').get();
  return snap.docs.map(d => d.data().token);
}

function money(n) {
  try { return Number(n).toLocaleString('ru-RU') + ' ₽'; } catch (e) { return String(n); }
}

/* ================= НОВЫЙ ТОВАР ================= */
exports.pushOnNewProduct = onDocumentCreated('products/{productId}', async (event) => {
  const p = event.data.data();
  const tokens = await getAllTokens();
  if (tokens.length === 0) return;
  await sendToTokens(
    tokens,
    { title: '✨ Новинка в HORIZON MARKET!', body: `${p.name} — ${money(p.price)}` },
    { click_action: `/?product=${event.params.productId}`, tag: 'new-product' }
  );
});

/* ================= ТОВАР СНОВА В НАЛИЧИИ / ЦЕНА СНИЗИЛАСЬ ================= */
exports.pushOnProductUpdate = onDocumentUpdated('products/{productId}', async (event) => {
  const before = event.data.before.data();
  const after = event.data.after.data();
  const productId = event.params.productId;

  const backInStock = before.inStock === false && after.inStock !== false;
  const priceDropped = Number(after.price) < Number(before.price);

  if (!backInStock && !priceDropped) return;

  // Находим клиентов, которые подписаны на этот товар (stockNotify / priceWatch — массивы productId в документе users/{id})
  const jobs = [];

  if (backInStock) {
    jobs.push(
      db.collection('users').where('stockNotify', 'array-contains', productId).get().then(async (snap) => {
        const userIds = snap.docs.map(d => d.id);
        const tokens = await getTokensForUserIds(userIds);
        await sendToTokens(
          tokens,
          { title: '📦 Товар снова в наличии!', body: `${after.name} — ${money(after.price)}` },
          { click_action: `/?product=${productId}`, tag: 'back-in-stock' }
        );
        // снимаем подписку у тех, кому отправили
        await Promise.all(snap.docs.map(d =>
          d.ref.update({ stockNotify: admin.firestore.FieldValue.arrayRemove(productId) }).catch(() => {})
        ));
      })
    );
  }

  if (priceDropped) {
    jobs.push(
      db.collection('users').where('priceWatch', 'array-contains', productId).get().then(async (snap) => {
        const userIds = snap.docs.map(d => d.id);
        const tokens = await getTokensForUserIds(userIds);
        await sendToTokens(
          tokens,
          { title: '💸 Цена снизилась!', body: `${after.name}: ${money(before.price)} → ${money(after.price)}` },
          { click_action: `/?product=${productId}`, tag: 'price-drop' }
        );
        await Promise.all(snap.docs.map(d =>
          d.ref.update({ priceWatch: admin.firestore.FieldValue.arrayRemove(productId) }).catch(() => {})
        ));
      })
    );
  }

  await Promise.all(jobs);
});

/* ================= СТАТУС ЗАКАЗА ИЗМЕНИЛСЯ ================= */
exports.pushOnOrderStatusChange = onDocumentUpdated('orders/{orderId}', async (event) => {
  const before = event.data.before.data();
  const after = event.data.after.data();
  if (before.status === after.status || !after.clientId) return;

  const tokens = await getTokensForUserIds([after.clientId]);
  await sendToTokens(
    tokens,
    { title: '📦 Статус заказа обновлён', body: `Заказ теперь: ${after.status}` },
    { click_action: '/', tag: 'order-status' }
  );
});

/* ================= НОВОЕ СООБЩЕНИЕ В ЧАТЕ ================= */
exports.pushOnNewChatMessage = onDocumentCreated('chats/{chatId}/messages/{messageId}', async (event) => {
  const msg = event.data.data();
  const chatSnap = await db.collection('chats').doc(event.params.chatId).get();
  if (!chatSnap.exists) return;
  const chat = chatSnap.data();
  const recipientId = msg.senderId === chat.clientId ? chat.sellerId : chat.clientId;
  if (!recipientId) return;

  const tokens = await getTokensForUserIds([recipientId]);
  await sendToTokens(
    tokens,
    { title: '💬 Новое сообщение', body: msg.text ? msg.text.slice(0, 120) : 'Вам написали в чат' },
    { click_action: '/', tag: 'chat-message' }
  );
});
