#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HORIZON MARKET — Telegram-бот (Railway).

1) Код подтверждения при регистрации: приложение открывает t.me/<бот>?start=v...,
   бот присылает 6-значный код и кладёт в Firestore tgSessions/<токен> {chatId, codeHash}.
   Приложение читает эту запись (браузер больше НЕ вызывает getUpdates).
   Токены n... (уведомления, курьер) — бот только записывает chatId, дальше всё делает приложение.
2) Оплата заказа: кнопка «Оплатить» → t.me/<бот>?start=pay_<orderId>.
   Бот присылает товары, итог и реквизиты → клиент шлёт чек → админу уходит чек
   с кнопками «Подтвердить / Отказ» → при подтверждении orders/<id>.paid = true, status = «Оплачено».

Переменные окружения (Railway → Variables):
  BOT_TOKEN                   токен бота (тот же, что «Бот регистрации» в админке приложения)
  FIREBASE_CREDENTIALS_JSON   JSON сервисного аккаунта целиком (или его base64)
  ADMIN_IDS                   Telegram ID админов через запятую (необязательно: иначе берётся adminBotChatId из settings/main)
  CARD_NUMBER, CARD_BANK, PAY_PHONE   (необязательно, есть значения по умолчанию)
"""
import base64, hashlib, html, json, logging, os, random, re, time
import requests
import firebase_admin
from firebase_admin import credentials, firestore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("horizon-bot")

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CARD = os.environ.get("CARD_NUMBER", "4444 8888 1227 1025").strip()
BANK = os.environ.get("CARD_BANK", "Alif").strip()
PHONE = os.environ.get("PAY_PHONE", "+992 978 11 78 11").strip()
CUR = os.environ.get("CURRENCY", "TJS")
if not TOKEN:
    raise SystemExit("Не задан BOT_TOKEN")

raw = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
if not raw:
    raise SystemExit("Не задан FIREBASE_CREDENTIALS_JSON")
if not raw.lstrip().startswith("{"):
    raw = base64.b64decode(raw).decode("utf-8")
firebase_admin.initialize_app(credentials.Certificate(json.loads(raw)))
db = firestore.client()

API = f"https://api.telegram.org/bot{TOKEN}/"
S = requests.Session()


def tg(method, _t=40, **p):
    try:
        r = S.post(API + method, json=p, timeout=_t)
        j = r.json()
    except Exception as e:
        log.warning("%s: %s", method, e)
        return None
    if j.get("ok"):
        return j["result"]
    if r.status_code == 429:
        time.sleep(min(int(j.get("parameters", {}).get("retry_after", 1)), 10))
        return tg(method, _t, **p)
    log.warning("%s -> %s %s", method, r.status_code, j.get("description"))
    return None


def say(chat, text, **kw):
    return tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True, **kw)


def esc(x):
    return html.escape(str(x if x is not None else ""))


def money(n):
    try:
        return f"{float(n):,.2f}".replace(",", " ").replace(".00", "") + f" {CUR}"
    except Exception:
        return f"{n} {CUR}"


def admin_ids():
    ids = [int(x) for x in re.split(r"[,\s]+", os.environ.get("ADMIN_IDS", "")) if re.fullmatch(r"-?\d+", x or "")]
    if not ids:
        try:
            v = (db.collection("settings").document("main").get().to_dict() or {}).get("adminBotChatId")
            if v and re.fullmatch(r"-?\d+", str(v)):
                ids = [int(v)]
        except Exception as e:
            log.warning("settings/main: %s", e)
    return ids


# ───────────── заказ ─────────────
def order_text(o):
    lines = []
    for i, it in enumerate(o.get("items") or [], 1):
        name = it.get("name") or it.get("title") or it.get("productName") or "Товар"
        qty = it.get("qty") or it.get("quantity") or 1
        extra = " ".join(str(it[k]) for k in ("size", "color") if it.get(k))
        price = it.get("price")
        lines.append(f"{i}. {esc(name)}{' (' + esc(extra) + ')' if extra else ''} × {qty}" + (f" — {money(float(price) * int(qty))}" if price is not None else ""))
    return "\n".join(lines) or "—"


def pay_instruction(o):
    return (
        f"🛒 <b>Ваш заказ</b>\n{order_text(o)}\n\n"
        f"💰 <b>Общий итог: {money(o.get('total', 0))}</b>\n\n"
        f"Оплатите на карту <code>{esc(CARD)}</code> <b>{esc(BANK)}</b> по номеру карты.\n"
        f"Алиф моби и DC City: <code>{esc(PHONE)}</code>\n\n"
        f"📸 После оплаты отправьте сюда <b>чек</b> (фото или файл)."
    )


# ───────────── /start ─────────────
def on_start(chat, user, arg):
    if arg.startswith("pay_"):
        return start_pay(chat, user, arg[4:])
    if arg and arg[0] in "vn":
        data = {"chatId": chat, "username": user.get("username") or "", "firstName": user.get("first_name") or "", "ts": int(time.time() * 1000)}
        if arg[0] == "v":
            code = str(random.SystemRandom().randint(100000, 999999))
            data["codeHash"] = hashlib.sha256(code.encode()).hexdigest()
            db.collection("tgSessions").document(arg).set(data)
            say(chat, f"Ваш код подтверждения регистрации HORIZON MARKET: <b>{code}</b>\n\nНикому не сообщайте этот код.")
        else:
            db.collection("tgSessions").document(arg).set(data)
        return
    say(chat, "👋 Добро пожаловать в <b>HORIZON MARKET</b>!\nЭтот бот присылает коды подтверждения, уведомления и принимает оплату заказов. Нажмите «Оплатить» в приложении, чтобы оплатить заказ.")


def start_pay(chat, user, order_id):
    if not re.fullmatch(r"[A-Za-z0-9]{10,40}", order_id):
        return say(chat, "Заказ не найден.")
    snap = db.collection("orders").document(order_id).get()
    if not snap.exists:
        return say(chat, "Заказ не найден.")
    o = snap.to_dict()
    if o.get("paid") or o.get("status") == "Оплачено":
        return say(chat, "✅ Этот заказ уже оплачен. Спасибо!")
    if o.get("status") == "Отменён":
        return say(chat, "Этот заказ отменён.")
    db.collection("botChats").document(str(chat)).set({"orderId": order_id, "ts": int(time.time() * 1000)})
    db.collection("botPayments").document(order_id).set({"chatId": chat, "status": "awaiting", "username": user.get("username") or ""}, merge=True)
    say(chat, pay_instruction(o))


# ───────────── чек ─────────────
def on_receipt(m):
    chat = m["chat"]["id"]
    st = db.collection("botChats").document(str(chat)).get().to_dict() or {}
    oid = st.get("orderId")
    if not oid:
        return say(chat, "Сначала нажмите «Оплатить» у заказа в приложении — я пришлю реквизиты.")
    o = (db.collection("orders").document(oid).get().to_dict()) or {}
    if o.get("paid"):
        return say(chat, "✅ Этот заказ уже оплачен.")
    pref = db.collection("botPayments").document(oid)
    cur = pref.get().to_dict() or {}
    if cur.get("status") == "review":
        return say(chat, "⏳ Чек уже на проверке. Пожалуйста, подождите.")
    if m.get("photo"):
        kind, fid = "photo", m["photo"][-1]["file_id"]
    elif m.get("document"):
        kind, fid = "document", m["document"]["file_id"]
    else:
        return say(chat, "Отправьте чек фото или файлом 📸")
    u = m.get("from") or {}
    who = f"@{u['username']}" if u.get("username") else (u.get("first_name") or "")
    caption = (
        f"💳 <b>Новый чек об оплате</b>\n"
        f"Заказ: <code>{esc(oid)}</code>\n"
        f"Клиент: {esc(o.get('clientName'))} {esc(o.get('clientPhone'))}\nTelegram: {esc(who)}\n\n"
        f"{order_text(o)}\n\n💰 <b>Итого: {money(o.get('total', 0))}</b>"
    )
    markup = {"inline_keyboard": [[{"text": "✅ Подтвердить", "callback_data": f"ok:{oid}"}, {"text": "❌ Отказ", "callback_data": f"no:{oid}"}]]}
    sent = []
    for a in admin_ids():
        r = tg("sendPhoto" if kind == "photo" else "sendDocument", chat_id=a, caption=caption, parse_mode="HTML", reply_markup=markup, **{kind: fid})
        if r:
            sent.append({"chat": a, "msg": r["message_id"]})
    if not sent:
        return say(chat, "Не удалось передать чек администратору. Попробуйте чуть позже.")
    pref.set({"status": "review", "chatId": chat, "fileId": fid, "fileType": kind, "adminMsgs": sent, "receiptAt": int(time.time() * 1000)}, merge=True)
    say(chat, "⏳ Пожалуйста, подождите — платёж в процессе.\nЭто займёт 1–5 минут.")


# ───────────── решение админа ─────────────
@firestore.transactional
def claim(tx, ref, new_status, admin):
    d = ref.get(transaction=tx).to_dict() or {}
    if d.get("status") != "review":
        return None
    tx.update(ref, {"status": new_status, "decidedBy": admin, "decidedAt": int(time.time() * 1000)})
    return d


def on_callback(cb):
    uid = cb["from"]["id"]
    if uid not in admin_ids():
        return tg("answerCallbackQuery", callback_query_id=cb["id"], text="Только для администратора", show_alert=True)
    act, _, oid = (cb.get("data") or "").partition(":")
    if act not in ("ok", "no") or not oid:
        return tg("answerCallbackQuery", callback_query_id=cb["id"])
    ref = db.collection("botPayments").document(oid)
    d = claim(db.transaction(), ref, "approved" if act == "ok" else "rejected", uid)
    if d is None:
        return tg("answerCallbackQuery", callback_query_id=cb["id"], text="Уже обработано", show_alert=True)
    client = d.get("chatId")
    if act == "ok":
        db.collection("orders").document(oid).update({"paid": True, "status": "Оплачено", "paidVia": "telegram", "paidAt": firestore.SERVER_TIMESTAMP})
        db.collection("botChats").document(str(client)).delete()
        say(client, "✅ <b>Покупка завершена!</b>\nОплата получена, ваш заказ оплачен. Спасибо за покупку в HORIZON MARKET! 🎉")
        mark = f"✅ Подтверждено ({esc(cb['from'].get('first_name'))})"
    else:
        say(client, "❌ <b>Платёж не подтверждён.</b>\nЕсли вы оплатили, отправьте чек ещё раз или свяжитесь с магазином.")
        ref.update({"status": "awaiting"})
        mark = f"❌ Отказано ({esc(cb['from'].get('first_name'))})"
    for a in d.get("adminMsgs") or []:
        tg("editMessageReplyMarkup", chat_id=a["chat"], message_id=a["msg"], reply_markup={"inline_keyboard": [[{"text": mark, "callback_data": "done"}]]})
    tg("answerCallbackQuery", callback_query_id=cb["id"], text="Готово")


# ───────────── цикл ─────────────
def handle(u):
    if "callback_query" in u:
        return on_callback(u["callback_query"])
    m = u.get("message")
    if not m or m["chat"]["type"] != "private":
        return
    chat = m["chat"]["id"]
    text = (m.get("text") or "").strip()
    if text.startswith("/start"):
        return on_start(chat, m.get("from") or {}, text[6:].strip())
    if m.get("photo") or m.get("document"):
        return on_receipt(m)
    if text:
        say(chat, "Для оплаты нажмите «Оплатить» у заказа в приложении. Если вы уже оплатили, отправьте чек фото или файлом.")


def main():
    tg("deleteWebhook")
    log.info("Бот запущен. Админы: %s", admin_ids())
    offset = None
    while True:
        body = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            body["offset"] = offset
        try:
            res = S.post(API + "getUpdates", json=body, timeout=45).json()
        except Exception as e:
            log.warning("getUpdates: %s", e)
            time.sleep(3)
            continue
        if not res.get("ok"):
            log.warning("getUpdates: %s", res.get("description"))
            time.sleep(5)
            continue
        for u in res["result"]:
            offset = u["update_id"] + 1
            try:
                handle(u)
            except Exception:
                log.exception("Ошибка обработки апдейта")


if __name__ == "__main__":
    main()
