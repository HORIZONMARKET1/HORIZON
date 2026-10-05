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
import base64, hashlib, hmac, html, json, logging, os, random, re, threading, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
AI_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
AI_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
BANK = "horizon_bank"
DEFAULT_LIMIT = 500
POOL = ThreadPoolExecutor(max_workers=6)
BOT_USERNAME = ""
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
    if arg.startswith("kyc_"):
        return kyc_start(chat, user, arg[4:])
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
    if act in ("kok", "kno"):
        return kyc_decide(cb, act == "kok", oid)
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



# ═══════════════ HORIZON КРЕДИТ: верификация, чат HORIZON BANK, ИИ-автоответчик ═══════════════
def safe(t):
    return str(t or "").replace("<", "‹").replace(">", "›")


def bank_post(client_id, text, actions=None):
    cid = "_".join(sorted([client_id, BANK]))
    ref = db.collection("chats").document(cid)
    msg = {"senderId": BANK, "text": safe(text), "read": False, "createdAt": firestore.SERVER_TIMESTAMP}
    if actions:
        msg["actions"] = actions
    ref.collection("messages").add(msg)
    ref.set({"clientId": client_id, "sellerId": BANK, "updatedAt": firestore.SERVER_TIMESTAMP,
             "lastMessageText": safe(text)[:120], "lastMessageHasImage": False,
             "lastMessageSenderId": BANK, "lastMessageAt": firestore.SERVER_TIMESTAMP}, merge=True)


def credit_summary(o):
    items = "\n".join("• %s × %s" % (safe(it.get("name") or "Товар"), it.get("qty") or 1) for it in (o.get("items") or [])) or "—"
    total = float(o.get("total") or 0)
    markup = float(o.get("installmentMarkup") or 0)
    rate = o.get("installmentRate") or 0
    parts = int(o.get("installmentParts") or 1)
    per = float(o.get("installmentPerPayment") or total)
    term = o.get("installmentTerm") or "—"
    pay = ("Платёж каждый месяц: %s (%d × %s)" % (money(per), parts, money(per))) if parts > 1 else ("Один платёж: %s" % money(total))
    return ("Выбранный товар:\n%s\n\nСрок: %s\nПроцент: %s%% (наценка %s)\nСумма товара: %s\nИтого с процентом: %s\n%s"
            % (items, term, rate, money(markup), money(total - markup), money(total), pay))


def user_info(uid):
    try:
        return db.collection("users").document(uid).get().to_dict() or {}
    except Exception:
        return {}


_seen_orders = set()


def process_credit_order(oid):
    if oid in _seen_orders:
        return
    _seen_orders.add(oid)
    ref = db.collection("orders").document(oid)
    o = ref.get().to_dict() or {}
    if o.get("bankNotified") or o.get("installmentProvider") != "HORIZON кредит":
        return
    cid = o.get("clientId")
    if not cid or cid == "guest":
        return
    ref.update({"bankNotified": True})
    u = user_info(cid)
    limit = float(u.get("creditLimit") or DEFAULT_LIMIT)
    base = float(o.get("total") or 0) - float(o.get("installmentMarkup") or 0)
    head = "🏦 HORIZON BANK\nЗдравствуйте! Ваша заявка на рассрочку HORIZON кредит получена.\n\n" + credit_summary(o)
    if base > limit:
        ref.update({"creditStatus": "limit_exceeded"})
        return bank_post(cid, head + "\n\n⚠️ Сумма заказа превышает ваш кредитный лимит (%s). Выберите другой способ оплаты или обратитесь к администратору для повышения лимита." % money(limit))
    if u.get("creditVerified"):
        ref.update({"creditStatus": "approved", "status": "Новый"})
        return bank_post(cid, head + "\n\n✅ Вы уже проходили верификацию — заявка одобрена. Продавец свяжется с вами для оформления.")
    ref.update({"creditStatus": "kyc_required"})
    link = "https://t.me/%s?start=kyc_%s" % (BOT_USERNAME, oid)
    bank_post(cid, head + "\n\nЭто ваша первая рассрочка, поэтому нужно пройти верификацию (около 5 минут). Нажмите кнопку ниже.",
              actions=[{"type": "kyc", "label": "Пройти верификацию", "url": link}])


KYC_ORDER = ["fio", "dob", "work", "phone1", "phone2", "pass_front", "pass_back", "selfie"]
KYC_Q = {
    "fio": "1️⃣ Введите ваше <b>ФИО</b> полностью:",
    "dob": "2️⃣ Введите <b>дату рождения</b> (ДД.ММ.ГГГГ):",
    "work": "3️⃣ Укажите <b>место работы</b>:",
    "phone1": "4️⃣ Введите <b>первый дополнительный номер телефона</b>:",
    "phone2": "5️⃣ Введите <b>второй дополнительный номер телефона</b>:",
    "pass_front": "6️⃣ Отправьте <b>фото паспорта — передняя сторона</b> (страница с фото):",
    "pass_back": "7️⃣ Отправьте <b>фото паспорта — обратная сторона</b>:",
}


def kyc_ask(chat, step):
    if step == "selfie":
        return say(chat, "8️⃣ Последний шаг — <b>селфи в реальном времени</b>.\nНажмите кнопку: откроется камера. Фото из галереи загрузить нельзя.",
                   reply_markup={"inline_keyboard": [[{"text": "📸 Сделать селфи", "web_app": {"url": PUBLIC_URL + "/selfie"}}]]})
    say(chat, KYC_Q[step])


def kyc_start(chat, user, oid):
    o = (db.collection("orders").document(oid).get().to_dict()) or {}
    if o.get("installmentProvider") != "HORIZON кредит":
        return say(chat, "Заявка не найдена.")
    if user_info(o.get("clientId")).get("creditVerified"):
        return say(chat, "✅ Вы уже верифицированы — повторно проходить не нужно.")
    if o.get("creditStatus") in ("review", "approved"):
        return say(chat, "Ваша заявка уже %s." % ("на проверке" if o.get("creditStatus") == "review" else "одобрена"))
    if not PUBLIC_URL:
        return say(chat, "Верификация временно недоступна. Попробуйте позже.")
    db.collection("kycSessions").document(str(chat)).set({"orderId": oid, "clientId": o.get("clientId"), "step": "fio", "data": {}, "username": user.get("username") or ""})
    say(chat, "🏦 <b>Верификация HORIZON BANK</b>\nОтвечайте на вопросы по одному. Отмена: /cancel")
    kyc_ask(chat, "fio")


def kyc_message(m, sess):
    chat = m["chat"]["id"]
    step = sess["step"]
    text = (m.get("text") or "").strip()
    data = sess.get("data") or {}
    ref = db.collection("kycSessions").document(str(chat))
    if step == "selfie":
        return say(chat, "Нужно селфи с камеры — нажмите кнопку ниже. Фото из галереи не принимается.",
                   reply_markup={"inline_keyboard": [[{"text": "📸 Сделать селфи", "web_app": {"url": PUBLIC_URL + "/selfie"}}]]})
    if step in ("pass_front", "pass_back"):
        fid = None
        if m.get("photo"):
            fid = m["photo"][-1]["file_id"]
        elif m.get("document") and str(m["document"].get("mime_type", "")).startswith("image/"):
            fid = m["document"]["file_id"]
        if not fid:
            return say(chat, "Отправьте именно фото паспорта 📷")
        data[step] = fid
    else:
        if not text:
            return say(chat, "Напишите ответ текстом.")
        if step == "fio" and len(text.split()) < 2:
            return say(chat, "Введите ФИО полностью (фамилия, имя, отчество).")
        if step == "dob":
            try:
                age = (datetime.now() - datetime.strptime(text, "%d.%m.%Y")).days / 365.25
            except ValueError:
                return say(chat, "Формат даты: ДД.ММ.ГГГГ, например 25.04.1995")
            if not (18 <= age <= 80):
                return say(chat, "Кредит доступен клиентам от 18 лет.")
        if step in ("phone1", "phone2"):
            digits = re.sub(r"\D", "", text)
            if not 9 <= len(digits) <= 15:
                return say(chat, "Введите номер телефона, например +992 900 00 00 00")
            if step == "phone2" and digits == re.sub(r"\D", "", data.get("phone1", "")):
                return say(chat, "Второй номер должен отличаться от первого.")
        data[step] = text
    nxt = KYC_ORDER[KYC_ORDER.index(step) + 1]
    ref.update({"data": data, "step": nxt})
    kyc_ask(chat, nxt)


def send_bytes_photo(chat, jpeg, caption, markup=None):
    p = {"chat_id": chat, "caption": caption, "parse_mode": "HTML"}
    if markup:
        p["reply_markup"] = json.dumps(markup)
    try:
        r = S.post(API + "sendPhoto", data=p, files={"photo": ("selfie.jpg", jpeg, "image/jpeg")}, timeout=60).json()
        return r.get("result")
    except Exception as e:
        log.warning("sendPhoto: %s", e)


def kyc_finalize(uid, jpeg):
    ref = db.collection("kycSessions").document(str(uid))
    sess = ref.get().to_dict() or {}
    if sess.get("step") != "selfie":
        return False
    oid, d = sess["orderId"], sess.get("data") or {}
    o = db.collection("orders").document(oid).get().to_dict() or {}
    ref.delete()
    db.collection("orders").document(oid).update({"creditStatus": "review"})
    db.collection("kycApplications").document(oid).set({"clientId": sess.get("clientId"), "chatId": uid, "status": "review", "data": {k: v for k, v in d.items() if not k.startswith("pass_")}, "ts": int(time.time() * 1000)})
    say(uid, "✅ Ваша заявка принята и проверяется модератором.\nЭто может занять около 2–3 часов. Мы сообщим результат здесь.")
    cap = ("🏦 <b>Заявка на кредит HORIZON</b>\nЗаказ: <code>%s</code>\nКлиент: %s %s\n\n"
           "ФИО: %s\nДата рождения: %s\nМесто работы: %s\nТелефон 1: %s\nТелефон 2: %s\n\n%s") % (
        esc(oid), esc(o.get("clientName")), esc(o.get("clientPhone")), esc(d.get("fio")), esc(d.get("dob")), esc(d.get("work")),
        esc(d.get("phone1")), esc(d.get("phone2")), esc(credit_summary(o)))
    markup = {"inline_keyboard": [[{"text": "✅ Подтвердить", "callback_data": "kok:" + oid}, {"text": "❌ Отказ", "callback_data": "kno:" + oid}]]}
    fid = None
    for a in (kyc_admins()):
        tg("sendMediaGroup", chat_id=a, media=[{"type": "photo", "media": d.get("pass_front")}, {"type": "photo", "media": d.get("pass_back")}])
        if fid:
            tg("sendPhoto", chat_id=a, photo=fid, caption=cap[:1024], parse_mode="HTML", reply_markup=markup)
        else:
            r = send_bytes_photo(a, jpeg, cap[:1024], markup)
            if r:
                fid = r["photo"][-1]["file_id"]
    return True


def kyc_admins():
    ids = [int(x) for x in re.split(r"[,\s]+", os.environ.get("KYC_ADMIN_IDS", "")) if re.fullmatch(r"-?\d+", x or "")]
    return ids or admin_ids()


def kyc_decide(cb, ok, oid):
    if cb["from"]["id"] not in kyc_admins():
        return tg("answerCallbackQuery", callback_query_id=cb["id"], text="Только для администратора", show_alert=True)
    app = db.collection("kycApplications").document(oid)
    d = app.get().to_dict() or {}
    if d.get("status") != "review":
        return tg("answerCallbackQuery", callback_query_id=cb["id"], text="Уже обработано", show_alert=True)
    app.update({"status": "approved" if ok else "rejected", "decidedBy": cb["from"]["id"]})
    o = db.collection("orders").document(oid).get().to_dict() or {}
    cid, chat = d.get("clientId"), d.get("chatId")
    if ok:
        upd = {"creditVerified": True}
        if not user_info(cid).get("creditLimit"):
            upd["creditLimit"] = DEFAULT_LIMIT
        db.collection("users").document(cid).update(upd)
        db.collection("orders").document(oid).update({"creditStatus": "approved", "status": "Новый"})
        txt = "✅ Ваша заявка на кредит успешно принята!\n\n" + credit_summary(o)
        say(chat, "✅ <b>Ваша заявка на кредит успешно принята!</b>\n\n" + esc(credit_summary(o)))
        bank_post(cid, txt)
    else:
        db.collection("orders").document(oid).update({"creditStatus": "rejected"})
        say(chat, "❌ К сожалению, заявка на кредит отклонена. Вы можете выбрать другой способ оплаты или обратиться в чат HORIZON BANK.")
        bank_post(cid, "❌ К сожалению, ваша заявка на кредит отклонена. Выберите другой способ оплаты или напишите нам в этот чат.")
    mark = "✅ Подтверждено" if ok else "❌ Отказано"
    tg("editMessageReplyMarkup", chat_id=cb["message"]["chat"]["id"], message_id=cb["message"]["message_id"], reply_markup={"inline_keyboard": [[{"text": mark, "callback_data": "done"}]]})
    tg("answerCallbackQuery", callback_query_id=cb["id"], text="Готово")


# ---------- ИИ-автоответчик HORIZON BANK в чате приложения ----------
AI_SYSTEM = """Ты — виртуальный ассистент HORIZON BANK, финансового сервиса магазина HORIZON MARKET (Таджикистан). Отвечай клиенту в чате вежливо, коротко и по делу, на языке клиента (русский, таджикский или узбекский). Без markdown и HTML, только обычный текст, можно эмодзи.
Условия рассрочки «HORIZON кредит»: оформляется только по паспорту. Срок 1 неделя — без процентов (для сумм до 500 TJS); 1 месяц — наценка 6%; 3 месяца — 10%; 6 месяцев — 15%. Кредитный лимит у нового клиента 500 TJS, администратор может его повысить. Верификация нужна только при первой рассрочке: ФИО, дата рождения, место работы, два дополнительных номера телефона, фото паспорта с двух сторон и селфи с камеры; проверка модератором занимает около 2–3 часов. После одобрения клиент платит равными ежемесячными платежами.
Отвечай на любые вопросы клиента: по кредиту используй факты выше и данные клиента ниже, на остальные — просто помоги по-человечески. Если не знаешь точного ответа (например, про товар, доставку или личные данные) — честно скажи об этом и предложи написать продавцу или администратору. Ничего не выдумывай про условия и не обещай одобрение кредита."""


def ai_reply(cid, history):
    u = user_info(cid)
    orders = []
    try:
        for d in db.collection("orders").where("clientId", "==", cid).where("installmentProvider", "==", "HORIZON кредит").limit(5).stream():
            o = d.to_dict()
            orders.append("- заказ %s: %s, срок %s, итого %s, статус кредита: %s" % (d.id[:6], ", ".join(str(i.get("name")) for i in o.get("items") or [])[:120], o.get("installmentTerm"), money(o.get("total", 0)), o.get("creditStatus")))
    except Exception as e:
        log.warning("orders ctx: %s", e)
    ctx = "\n\nДанные клиента: имя %s; кредитный лимит %s; верификация %s.\nЕго заявки:\n%s" % (
        u.get("fio") or "—", money(u.get("creditLimit") or DEFAULT_LIMIT), "пройдена" if u.get("creditVerified") else "не пройдена", "\n".join(orders) or "нет")
    msgs = []
    for role, text in history:
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + text
        else:
            msgs.append({"role": role, "content": text})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if not msgs:
        return None
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                      headers={"x-api-key": AI_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": AI_MODEL, "max_tokens": 500, "system": AI_SYSTEM + ctx, "messages": msgs})
    j = r.json()
    if r.status_code != 200:
        log.warning("AI error %s %s", r.status_code, j)
        return None
    return "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text").strip()


_answered = set()


def answer_chat(chat_id):
    ref = db.collection("chats").document(chat_id)
    docs = list(ref.collection("messages").order_by("createdAt", direction=firestore.Query.DESCENDING).limit(12).stream())
    if not docs:
        return
    last = docs[0].to_dict()
    if last.get("senderId") == BANK or docs[0].id in _answered:
        return
    ts = last.get("createdAt")
    if ts and time.time() - ts.timestamp() > 900:
        return
    _answered.add(docs[0].id)
    cid = (ref.get().to_dict() or {}).get("clientId")
    hist = [("assistant" if d.to_dict().get("senderId") == BANK else "user", d.to_dict().get("text") or "[клиент отправил фото]") for d in reversed(docs)]
    try:
        ans = ai_reply(cid, hist)
    except Exception:
        log.exception("ai_reply")
        ans = None
    bank_post(cid, ans or "Спасибо за сообщение! Сейчас я не могу ответить автоматически — оператор HORIZON BANK ответит вам в ближайшее время.")


def on_chats(docs, changes, read_time):
    for ch in changes:
        if ch.type.name == "REMOVED":
            continue
        if (ch.document.to_dict() or {}).get("lastMessageSenderId") not in (None, BANK):
            POOL.submit(answer_chat, ch.document.id)


def on_orders(docs, changes, read_time):
    for ch in changes:
        if ch.type.name != "REMOVED":
            d = ch.document.to_dict() or {}
            if d.get("installmentProvider") == "HORIZON кредит" and not d.get("bankNotified"):
                POOL.submit(process_credit_order, ch.document.id)


# ---------- веб-сервер: страница селфи (Telegram Mini App) ----------
SELFIE_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<script src="https://telegram.org/js/telegram-web-app.js"></script><title>Селфи</title>
<style>body{margin:0;background:#0f0b1f;color:#fff;font-family:system-ui,sans-serif;text-align:center;padding:16px}video{width:100%;max-width:420px;border-radius:18px;background:#000;transform:scaleX(-1)}
button{margin-top:16px;width:100%;max-width:420px;padding:16px;border:0;border-radius:14px;background:#7b2ff7;color:#fff;font-size:17px;font-weight:700}p{opacity:.8}</style></head>
<body><h3>Селфи для верификации</h3><p id="m">Разрешите доступ к камере и держите лицо в кадре</p><video id="v" autoplay playsinline muted></video><br><button id="b" disabled>Сделать фото</button>
<script>
const tgw=window.Telegram.WebApp;tgw.ready();tgw.expand();const v=document.getElementById('v'),b=document.getElementById('b'),m=document.getElementById('m');
navigator.mediaDevices&&navigator.mediaDevices.getUserMedia?navigator.mediaDevices.getUserMedia({video:{facingMode:'user'},audio:false}).then(s=>{v.srcObject=s;b.disabled=false}).catch(()=>{m.textContent='Нет доступа к камере. Разрешите камеру в настройках и откройте снова.'}):m.textContent='Камера недоступна. Обновите Telegram.';
b.onclick=async()=>{b.disabled=true;m.textContent='Отправка…';const c=document.createElement('canvas'),k=Math.min(1,900/Math.max(v.videoWidth,v.videoHeight));c.width=v.videoWidth*k;c.height=v.videoHeight*k;c.getContext('2d').drawImage(v,0,0,c.width,c.height);
try{const r=await fetch('/selfie',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({initData:tgw.initData,image:c.toDataURL('image/jpeg',.85)})});const j=await r.json();if(j.ok){tgw.close()}else{m.textContent=j.error||'Ошибка';b.disabled=false}}catch(e){m.textContent='Нет связи, попробуйте ещё раз';b.disabled=false}};
</script></body></html>"""


def init_user(init):
    d = dict(urllib.parse.parse_qsl(init or "", keep_blank_values=True))
    h = d.pop("hash", None)
    chk = "\n".join("%s=%s" % (k, d[k]) for k in sorted(d))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not h or not hmac.compare_digest(hmac.new(secret, chk.encode(), hashlib.sha256).hexdigest(), h):
        return None
    if time.time() - int(d.get("auth_date", 0)) > 86400:
        return None
    return json.loads(d["user"])["id"]


class Web(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.split("?")[0] == "/selfie":
            return self._send(200, SELFIE_HTML, "text/html")
        self._send(200, "ok", "text/plain")

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if self.path != "/selfie" or n > 8_000_000:
                return self._send(400, json.dumps({"ok": False, "error": "bad request"}))
            body = json.loads(self.rfile.read(n))
            uid = init_user(body.get("initData"))
            if not uid:
                return self._send(403, json.dumps({"ok": False, "error": "Ошибка авторизации"}))
            jpeg = base64.b64decode(body["image"].split(",", 1)[1])
            if not kyc_finalize(uid, jpeg):
                return self._send(409, json.dumps({"ok": False, "error": "Сначала ответьте на вопросы в боте"}))
            self._send(200, json.dumps({"ok": True}))
        except Exception:
            log.exception("web")
            self._send(500, json.dumps({"ok": False, "error": "Ошибка сервера"}))


def start_background():
    global BOT_USERNAME
    me = tg("getMe") or {}
    BOT_USERNAME = me.get("username", "")
    port = int(os.environ.get("PORT", "8080"))
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", port), Web).serve_forever, daemon=True).start()
    log.info("Web :%s, public=%s, bot=@%s, AI=%s", port, PUBLIC_URL or "—", BOT_USERNAME, "on" if AI_KEY else "off")
    global _w1, _w2
    _w1 = db.collection("orders").where("installmentProvider", "==", "HORIZON кредит").on_snapshot(on_orders)
    if AI_KEY:
        _w2 = db.collection("chats").where("sellerId", "==", BANK).on_snapshot(on_chats)


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
    if text == "/cancel" and db.collection("kycSessions").document(str(chat)).get().exists:
        db.collection("kycSessions").document(str(chat)).delete()
        return say(chat, "Верификация отменена. Вы можете начать заново кнопкой в чате HORIZON BANK.")
    ks = db.collection("kycSessions").document(str(chat)).get()
    if ks.exists and ks.to_dict().get("step") in KYC_ORDER:
        return kyc_message(m, ks.to_dict())
    if m.get("photo") or m.get("document"):
        return on_receipt(m)
    if text:
        say(chat, "Для оплаты нажмите «Оплатить» у заказа в приложении. Если вы уже оплатили, отправьте чек фото или файлом.")


def main():
    tg("deleteWebhook")
    start_background()
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
