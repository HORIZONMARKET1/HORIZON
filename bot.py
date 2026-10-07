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
  SITE_URL                    адрес сайта/приложения (https://...) для кнопки «Смотреть на сайте» в рассылке о новых товарах
                              (необязательно: если не задан, берётся settings/main.siteUrl — его сохраняет приложение, когда его открывает админ)
  PROMO_GAP_MIN (20), PROMO_DAILY_MAX (5)   пауза между рассылками о товарах и максимум рассылок в день (необязательно)
"""
import base64, calendar, hashlib, hmac, html, json, logging, os, random, re, threading, time, traceback, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests
import firebase_admin
from firebase_admin import credentials, firestore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("horizon-bot")

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CARD = os.environ.get("CARD_NUMBER", "4444 8888 1227 1025").strip()
CARD_BANK_NAME = os.environ.get("CARD_BANK", "Alif").strip()  # название банка для реквизитов (BANK ниже — id чата HORIZON BANK)
PHONE = os.environ.get("PAY_PHONE", "+992 978 11 78 11").strip()
CUR = os.environ.get("CURRENCY", "TJS")
AI_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
AI_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip()
PUBLIC_URL = (os.environ.get("PUBLIC_URL", "") or os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")).strip().rstrip("/")
if PUBLIC_URL:
    # Telegram принимает для кнопки Mini App только https://. Без схемы или с http:// кнопка отклоняется,
    # и бот после 7-го шага «молчит» — поэтому приводим адрес к https:// принудительно.
    PUBLIC_URL = "https://" + re.sub(r"^https?://", "", PUBLIC_URL, flags=re.I).rstrip("/")
BANK = "horizon_bank"
DEFAULT_LIMIT = 500
CHANNEL = os.environ.get("CHANNEL_USERNAME", "horizonmarkettj").strip().lstrip("@")  # публичный канал для проверки подписки
CHANNEL_URL = "https://t.me/" + CHANNEL
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
    LAST_TG_ERR["text"] = "%s: %s" % (method, j.get("description"))
    log.warning("%s -> %s %s", method, r.status_code, j.get("description"))
    return None


def say(chat, text, **kw):
    r = tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True, **kw)
    if r is None and kw.get("reply_markup"):
        # Telegram отклонил сообщение с кнопкой — сообщаем админам (кроме случаев, когда клиент заблокировал бота)
        err = LAST_TG_ERR["text"]
        if not re.search(r"blocked|chat not found|deactivated|kicked", err, re.I):
            notify_admins("Telegram отклонил сообщение с кнопкой (чат <code>%s</code>):\n<code>%s</code>" % (chat, esc(err)), key="btn:" + err)
    return r


# ───────────── оповещения админов об ошибках ─────────────
LAST_TG_ERR = {"text": ""}
_alert_last = {}


def notify_admins(text, key=None, every=600, icon="⚠️"):
    """Сообщение админам. Никогда не бросает исключений; одинаковые (по key) — не чаще раза в `every` секунд."""
    try:
        key = key or text[:80]
        now = time.time()
        if every and now - _alert_last.get(key, 0) < every:
            return
        _alert_last[key] = now
        for a in sorted(set(admin_ids()) | set(kyc_admins())):
            try:
                S.post(API + "sendMessage", json={"chat_id": a, "text": ("%s %s" % (icon, text))[:3900], "parse_mode": "HTML",
                                                  "disable_web_page_preview": True}, timeout=15)
            except Exception:
                pass
    except Exception:
        log.exception("notify_admins")


def report_exc(where):
    """Записать текущее исключение в лог и отправить админам (кратко)."""
    tb = traceback.format_exc().strip().splitlines()
    notify_admins("<b>Ошибка в боте</b> (%s):\n<code>%s</code>" % (esc(where), esc("\n".join(tb[-3:])[-900:])),
                  key="exc:%s:%s" % (where, tb[-1][:80]))


def _thread_hook(args):
    log.error("Упал поток %s: %s", getattr(args.thread, "name", "?"), args.exc_value)
    notify_admins("<b>Упал фоновый поток</b> <code>%s</code>:\n<code>%s</code>" % (esc(getattr(args.thread, "name", "?")), esc(str(args.exc_value)[:500])),
                  key="thr:%s" % args.exc_type)


threading.excepthook = _thread_hook


def bg(fn, *a):
    """Фоновая задача: ошибки не теряются, а попадают в лог и админам."""
    def run():
        try:
            fn(*a)
        except Exception:
            log.exception("bg %s", getattr(fn, "__name__", "?"))
            report_exc(getattr(fn, "__name__", "фоновая задача"))
    POOL.submit(run)


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
        f"Оплатите на карту <code>{esc(CARD)}</code> <b>{esc(CARD_BANK_NAME)}</b> по номеру карты.\n"
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
    if (cb.get("data") or "") == "subchk":
        return on_subcheck(cb)  # кнопка «Я подписался» доступна любому клиенту
    uid = cb["from"]["id"]
    act, _, oid = (cb.get("data") or "").partition(":")
    kyc_acts = ("kok", "kno", "kback", "kr", "ip")
    allowed = (set(admin_ids()) | set(kyc_admins())) if act in kyc_acts else set(admin_ids())
    if uid not in allowed:
        return tg("answerCallbackQuery", callback_query_id=cb["id"], text="Только для администратора", show_alert=True)
    if act == "kok":
        return kyc_decide(cb, True, oid)
    if act == "kno":
        return kyc_reasons(cb, oid)
    if act == "kback":
        return kyc_back(cb, oid)
    if act == "kr":
        code, _, oid2 = oid.partition(":")
        return kyc_decide(cb, False, oid2, code)
    if act == "ip":
        return installment_paid(cb, oid)
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
        sched = credit_approved(oid, o)
        txt = head + "\n\n✅ Вы уже проходили верификацию — заявка одобрена. Продавец свяжется с вами для оформления.\n\n📅 График платежей:\n" + schedule_text(sched)
        chat = client_chat(cid, oid)
        if chat:
            say(chat, "✅ <b>Рассрочка подтверждена!</b>\n\n" + esc(credit_summary(o)) + "\n\n📅 <b>График платежей:</b>\n" + esc(schedule_text(sched)) + "\n\nМы напомним о каждом платеже заранее.")
        return bank_post(cid, txt)
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


def selfie_markup():
    return {"inline_keyboard": [[{"text": "✅ Пройти аутентификацию", "web_app": {"url": PUBLIC_URL + "/selfie"}}]]}


def kyc_ask(chat, step):
    if step == "selfie":
        r = say(chat, "✅ Паспорт принят.\n\n8️⃣ Последний шаг — <b>аутентификация</b>.\n"
                      "Нажмите кнопку «Пройти аутентификацию»: откроется камера прямо в Telegram. "
                      "Сделайте фото лица, проверьте его и нажмите «Отправить» — фото сразу придёт в бот.\n"
                      "Фото из галереи загрузить нельзя.", reply_markup=selfie_markup())
        if not r:
            # Telegram не принял кнопку (чаще всего PUBLIC_URL пустой/не https/не открывается) — не молчим
            log.error("Не удалось отправить кнопку Mini App. PUBLIC_URL=%r — проверьте переменную PUBLIC_URL (https://ваш-домен) в Railway", PUBLIC_URL)
            say(chat, "⚠️ Не удалось открыть камеру для аутентификации. Мы уже сообщили администратору — попробуйте ещё раз чуть позже "
                      "(напишите любое сообщение, и кнопка отправится снова).")
            for a in kyc_admins():
                say(a, "⚠️ <b>Верификация не может открыть камеру</b>: Telegram отклонил кнопку Mini App.\nPUBLIC_URL = <code>%s</code>\n"
                       "Проверьте, что в Railway задан PUBLIC_URL вида https://ваш-домен.up.railway.app и у сервиса включён публичный домен." % esc(PUBLIC_URL or "не задан"))
        return r
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
    db.collection("kycSessions").document(str(chat)).set({"orderId": oid, "clientId": o.get("clientId"), "step": "fio", "data": {}, "username": user.get("username") or "",
                                                            "updatedAt": int(time.time() * 1000), "remind": 0})
    say(chat, "🏦 <b>Верификация HORIZON BANK</b>\nОтвечайте на вопросы по одному. Отмена: /cancel")
    kyc_ask(chat, "fio")


def kyc_message(m, sess):
    chat = m["chat"]["id"]
    step = sess["step"]
    text = (m.get("text") or "").strip()
    data = sess.get("data") or {}
    ref = db.collection("kycSessions").document(str(chat))
    if step == "selfie":
        return say(chat, "Остался последний шаг — нажмите «Пройти аутентификацию» ниже, откроется камера. Фото из галереи не принимается.",
                   reply_markup=selfie_markup())
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
    ref.update({"data": data, "step": nxt, "updatedAt": int(time.time() * 1000), "remind": 0})
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


REJECT_REASONS = {
    "photo": "Фото паспорта нечёткое или обрезано. Переснимите документ при хорошем освещении: должны быть видны все данные и края страницы.",
    "face": "Лицо на селфи не совпадает с фото в паспорте. Сделайте новое селфи при хорошем освещении, без очков и головного убора.",
    "expired": "Паспорт недействителен или срок его действия истёк. Нужен действующий паспорт.",
    "data": "Данные в анкете неверны или не совпадают с паспортом. Проверьте ФИО, дату рождения и номера телефонов.",
    "other": None,
}
REASON_BTN = {"photo": "📷 Плохое фото паспорта", "face": "🙂 Лицо не совпадает", "expired": "📕 Паспорт просрочен",
              "data": "✏️ Неверные данные", "other": "❌ Без причины"}
REASON_SHORT = {"photo": "фото паспорта", "face": "лицо", "expired": "паспорт", "data": "данные"}


def kyc_markup(oid):
    return {"inline_keyboard": [[{"text": "✅ Подтвердить", "callback_data": "kok:" + oid}, {"text": "❌ Отказ", "callback_data": "kno:" + oid}]]}


def _kyc_guard(cb):
    if cb["from"]["id"] not in kyc_admins():
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Только для администратора", show_alert=True)
        return False
    return True


def kyc_reasons(cb, oid):
    """Админ нажал «Отказ» — просим выбрать причину (клиент получит её в сообщении)."""
    if not _kyc_guard(cb):
        return
    d = db.collection("kycApplications").document(oid).get().to_dict() or {}
    if d.get("status") != "review":
        return tg("answerCallbackQuery", callback_query_id=cb["id"], text="Уже обработано", show_alert=True)
    rows = [[{"text": REASON_BTN[k], "callback_data": "kr:%s:%s" % (k, oid)}] for k in ("photo", "face", "expired", "data", "other")]
    rows.append([{"text": "↩️ Назад", "callback_data": "kback:" + oid}])
    tg("editMessageReplyMarkup", chat_id=cb["message"]["chat"]["id"], message_id=cb["message"]["message_id"], reply_markup={"inline_keyboard": rows})
    tg("answerCallbackQuery", callback_query_id=cb["id"], text="Выберите причину отказа")


def kyc_back(cb, oid):
    if not _kyc_guard(cb):
        return
    tg("editMessageReplyMarkup", chat_id=cb["message"]["chat"]["id"], message_id=cb["message"]["message_id"], reply_markup=kyc_markup(oid))
    tg("answerCallbackQuery", callback_query_id=cb["id"])


def kyc_decide(cb, ok, oid, reason=None):
    if not _kyc_guard(cb):
        return
    app = db.collection("kycApplications").document(oid)
    d = app.get().to_dict() or {}
    if d.get("status") != "review":
        return tg("answerCallbackQuery", callback_query_id=cb["id"], text="Уже обработано", show_alert=True)
    code = reason if reason in REJECT_REASONS else "other"
    upd = {"status": "approved" if ok else "rejected", "decidedBy": cb["from"]["id"]}
    if not ok:
        upd["rejectReason"] = code
    app.update(upd)
    o = db.collection("orders").document(oid).get().to_dict() or {}
    cid, chat = d.get("clientId"), d.get("chatId")
    if ok:
        u_upd = {"creditVerified": True}
        if not user_info(cid).get("creditLimit"):
            u_upd["creditLimit"] = DEFAULT_LIMIT
        db.collection("users").document(cid).update(u_upd)
        db.collection("orders").document(oid).update({"creditStatus": "approved", "status": "Новый"})
        sched = credit_approved(oid, o)
        sched_txt = schedule_text(sched)
        txt = "✅ Ваша заявка на кредит успешно принята!\n\n" + credit_summary(o) + "\n\n📅 График платежей:\n" + sched_txt + "\n\nМы напомним о каждом платеже заранее."
        say(chat, "✅ <b>Ваша заявка на кредит успешно принята!</b>\n\n" + esc(credit_summary(o)) + "\n\n📅 <b>График платежей:</b>\n" + esc(sched_txt) + "\n\nМы напомним о каждом платеже заранее.")
        bank_post(cid, txt)
        mark = "✅ Подтверждено"
    else:
        db.collection("orders").document(oid).update({"creditStatus": "rejected", "creditRejectReason": code})
        link = "https://t.me/%s?start=kyc_%s" % (BOT_USERNAME, oid)
        why = REJECT_REASONS[code]
        msg = "❌ К сожалению, заявка на кредит отклонена."
        plain = msg
        if why:
            msg += "\n\n<b>Причина:</b> " + esc(why)
            plain += "\n\nПричина: " + why
        msg += "\n\nВы можете пройти верификацию заново или выбрать другой способ оплаты."
        plain += "\n\nВы можете пройти верификацию заново или выбрать другой способ оплаты."
        say(chat, msg, reply_markup={"inline_keyboard": [[{"text": "🔁 Пройти верификацию заново", "url": link}]]})
        bank_post(cid, plain, actions=[{"type": "kyc", "label": "Пройти верификацию заново", "url": link}])
        mark = "❌ Отказано" + (" · " + REASON_SHORT[code] if code in REASON_SHORT else "")
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
        notify_admins("ИИ-ответчик HORIZON BANK не работает: HTTP %s\n<code>%s</code>" % (r.status_code, esc(str(j)[:300])), key="ai", every=1800)
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
        report_exc("ИИ-ответчик")
        ans = None
    bank_post(cid, ans or "Спасибо за сообщение! Сейчас я не могу ответить автоматически — оператор HORIZON BANK ответит вам в ближайшее время.")


def on_chats(docs, changes, read_time):
    for ch in changes:
        if ch.type.name == "REMOVED":
            continue
        if (ch.document.to_dict() or {}).get("lastMessageSenderId") not in (None, BANK):
            bg(answer_chat, ch.document.id)


def on_orders(docs, changes, read_time):
    for ch in changes:
        if ch.type.name != "REMOVED":
            d = ch.document.to_dict() or {}
            if d.get("installmentProvider") == "HORIZON кредит" and not d.get("bankNotified"):
                bg(process_credit_order, ch.document.id)


# ---------- веб-сервер: страница селфи (Telegram Mini App) ----------
SELFIE_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<script src="https://telegram.org/js/telegram-web-app.js"></script><title>Аутентификация</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#0f0b1f;color:#fff;font-family:system-ui,sans-serif;text-align:center;padding:16px}
h3{margin:4px 0 8px}p{opacity:.85;min-height:20px;margin:6px 0 12px}
.box{position:relative;width:100%;max-width:420px;margin:0 auto;aspect-ratio:3/4;border-radius:18px;overflow:hidden;background:#000}
.box video,.box img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;transform:scaleX(-1)}.box img{display:none}
.row{display:flex;gap:10px;max-width:420px;margin:16px auto 0}
button{flex:1;padding:16px;border:0;border-radius:14px;background:#7b2ff7;color:#fff;font-size:17px;font-weight:700}
button.sec{background:#2a2358}button:disabled{opacity:.5}
</style></head>
<body><h3>Аутентификация</h3><p id="m">Разрешите доступ к камере и держите лицо в кадре</p>
<div class="box"><video id="v" autoplay playsinline muted></video><img id="p" alt=""></div>
<div class="row"><button id="shot" disabled>Сделать фото</button><button id="retake" class="sec" style="display:none">Переснять</button><button id="send" style="display:none">Отправить</button></div>
<script>
const tgw=window.Telegram.WebApp;tgw.ready();tgw.expand();
const v=document.getElementById('v'),p=document.getElementById('p'),m=document.getElementById('m'),shot=document.getElementById('shot'),retake=document.getElementById('retake'),send=document.getElementById('send');
let data=null;
function mode(captured){v.style.display=captured?'none':'block';p.style.display=captured?'block':'none';shot.style.display=captured?'none':'';retake.style.display=captured?'':'none';send.style.display=captured?'':'none';}
if(navigator.mediaDevices&&navigator.mediaDevices.getUserMedia){
navigator.mediaDevices.getUserMedia({video:{facingMode:'user'},audio:false}).then(s=>{v.srcObject=s;shot.disabled=false;m.textContent='Держите лицо в кадре и нажмите «Сделать фото»'}).catch(()=>{m.textContent='Нет доступа к камере. Разрешите камеру в настройках Telegram и откройте снова.'});
}else{m.textContent='Камера недоступна. Обновите Telegram.'}
shot.onclick=()=>{if(!v.videoWidth){m.textContent='Камера ещё не готова, подождите секунду';return}
const c=document.createElement('canvas'),k=Math.min(1,900/Math.max(v.videoWidth,v.videoHeight));c.width=v.videoWidth*k;c.height=v.videoHeight*k;c.getContext('2d').drawImage(v,0,0,c.width,c.height);
data=c.toDataURL('image/jpeg',.85);p.src=data;mode(true);m.textContent='Проверьте фото и нажмите «Отправить»'};
retake.onclick=()=>{data=null;mode(false);m.textContent='Держите лицо в кадре и нажмите «Сделать фото»'};
send.onclick=async()=>{if(!data)return;send.disabled=true;retake.disabled=true;m.textContent='Отправка…';
try{const r=await fetch('/selfie',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({initData:tgw.initData,image:data})});const j=await r.json();
if(j.ok){m.textContent='✅ Отправлено';setTimeout(()=>tgw.close(),700)}else{m.textContent=j.error||'Ошибка';send.disabled=false;retake.disabled=false}}
catch(e){m.textContent='Нет связи, попробуйте ещё раз';send.disabled=false;retake.disabled=false}};
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
        self.send_header("Cache-Control", "no-store")
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
            report_exc("страница селфи /selfie")
            self._send(500, json.dumps({"ok": False, "error": "Ошибка сервера"}))


def start_background():
    global BOT_USERNAME
    me = tg("getMe") or {}
    BOT_USERNAME = me.get("username", "")
    port = int(os.environ.get("PORT", "8080"))
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", port), Web).serve_forever, daemon=True).start()
    log.info("Web :%s, public=%s, bot=@%s, AI=%s", port, PUBLIC_URL or "—", BOT_USERNAME, "on" if AI_KEY else "off")
    start_watchers()
    tg("setMyCommands", commands=[
        {"command": "status", "description": "Статус заявки и ближайший платёж"},
        {"command": "continue", "description": "Продолжить верификацию"},
        {"command": "cancel", "description": "Отменить верификацию или оплату"},
        {"command": "help", "description": "Помощь"}])
    threading.Thread(target=scheduler, name="scheduler", daemon=True).start()
    threading.Thread(target=promo_loop, name="promo", daemon=True).start()


# ═══════════════ УВЕДОМЛЕНИЯ О ЗАКАЗЕ, ГРАФИК И НАПОМИНАНИЯ РАССРОЧКИ, КОМАНДЫ ═══════════════
TZ = timezone(timedelta(hours=5))  # Таджикистан: UTC+5, без перехода на летнее время


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def fmt_date(iso):
    try:
        return date.fromisoformat(iso).strftime("%d.%m.%Y")
    except Exception:
        return str(iso)


def build_schedule(o, base_ms):
    """График платежей рассрочки: считаем от дня одобрения. Неделя — один платёж через 7 дней, иначе — ежемесячно."""
    base = datetime.fromtimestamp(base_ms / 1000, TZ).date()
    total = float(o.get("total") or 0)
    parts = max(int(o.get("installmentParts") or 1), 1)
    per = float(o.get("installmentPerPayment") or 0)
    if "недел" in str(o.get("installmentTerm") or "").lower():
        return [{"n": 1, "due": (base + timedelta(days=7)).isoformat(), "amount": per or total}]
    amt = per or round(total / parts, 2)
    return [{"n": i, "due": add_months(base, i).isoformat(), "amount": amt} for i in range(1, parts + 1)]


def schedule_text(sched):
    return "\n".join("%d) %s — %s" % (x["n"], fmt_date(x["due"]), money(x["amount"])) for x in sched)


def credit_approved(oid, o):
    base = int(time.time() * 1000)
    sched = build_schedule(o, base)
    db.collection("orders").document(oid).update({"creditApprovedAt": base, "installmentSchedule": sched, "installmentPaid": 0, "reminders": {}})
    return sched


def client_chat(cid, oid=None):
    """Telegram-чат клиента: из профиля (если уведомления включены) или из его заявки на кредит."""
    if not cid or cid == "guest":
        return None
    u = user_info(cid)
    if u.get("telegramChatId") and u.get("telegramNotifyEnabled") is not False:
        return u["telegramChatId"]
    if oid:
        try:
            return (db.collection("kycApplications").document(oid).get().to_dict() or {}).get("chatId")
        except Exception:
            return None
    return None


# ---------- статусы заказа → сообщение клиенту в Telegram ----------
NOTIFY_STATUS = {
    "Новый": "📥 Заказ №{n} принят! Мы уже начали его обработку.",
    "Ожидает оформления рассрочки": "🏦 Заказ №{n}: ждём оформления рассрочки. Мы напишем, как только она будет подтверждена.",
    "Оплачено": "✅ Заказ №{n}: оплата получена. Скоро соберём ваш заказ.",
    "Собран": "📦 Заказ №{n} собран и скоро отправится к вам.",
    "В пути": "🚚 Заказ №{n} в пути.",
    "У курьера": "🛵 Заказ №{n} у курьера — скоро будет у вас!",
    "Доставлен": "🎉 Заказ №{n} доставлен. Спасибо за покупку в HORIZON MARKET!",
    "Отменён": "❌ Заказ №{n} отменён. Если это ошибка — напишите нам в чат приложения.",
}
_ord_status = {}


def on_all_orders(docs, changes, read_time):
    first = not _ord_status and not getattr(on_all_orders, "ready", False)
    for ch in changes:
        if ch.type.name == "REMOVED":
            continue
        st = (ch.document.to_dict() or {}).get("status") or "Новый"
        prev = _ord_status.get(ch.document.id)
        _ord_status[ch.document.id] = st
        if first:
            continue  # при запуске не рассылаем про уже существующие заказы
        if st != prev and st in NOTIFY_STATUS:
            bg(notify_order_status, ch.document.id, st)
    on_all_orders.ready = True


def notify_order_status(oid, st):
    ref = db.collection("orders").document(oid)
    o = ref.get().to_dict() or {}
    if (o.get("status") or "Новый") != st or o.get("tgStatusSent") == st:
        return
    if st == "Оплачено" and o.get("paidVia") == "telegram":
        return  # об оплате через бота клиент уже получил сообщение «Покупка завершена»
    chat = client_chat(o.get("clientId"), oid)
    if not chat:
        return
    ref.update({"tgStatusSent": st})
    say(chat, NOTIFY_STATUS[st].format(n=oid[-5:].upper()))


# ---------- напоминания о платежах рассрочки ----------
def reminder_kind(days):
    """days — сколько дней до срока платежа (отрицательное — просрочка)."""
    if days > 3:
        return None
    if days > 0:
        return "pre"
    if days == 0:
        return "due"
    if days >= -2:
        return "late1"
    return "late3"


def _remind_one(ref, oid, o, today):
    sched = o.get("installmentSchedule") or []
    paid = int(o.get("installmentPaid") or 0)
    if not sched or paid >= len(sched) or o.get("status") == "Отменён":
        return
    x = sched[paid]
    days = (date.fromisoformat(x["due"]) - today).days
    kind = reminder_kind(days)
    if not kind:
        return
    key = "p%d_%s" % (x["n"], kind)
    if (o.get("reminders") or {}).get(key):
        return
    n, amount, due = oid[-5:].upper(), money(x["amount"]), fmt_date(x["due"])
    head = "Заказ №%s · платёж %d из %d\nСумма: %s\nСрок: %s" % (n, x["n"], len(sched), amount, due)
    pay = ("Оплатить можно на карту %s (%s) или через Алиф моби / DC City: %s.\n"
           "После оплаты напишите об этом в чат HORIZON BANK в приложении.") % (CARD, CARD_BANK_NAME, PHONE)
    if kind == "pre":
        txt = "⏰ Скоро платёж по рассрочке (через %d дн.)\n%s\n\n%s" % (days, head, pay)
    elif kind == "due":
        txt = "📅 Сегодня срок платежа по рассрочке\n%s\n\n%s" % (head, pay)
    elif kind == "late1":
        txt = "⚠️ Платёж по рассрочке просрочен\n%s\n\nПожалуйста, оплатите как можно скорее.\n%s" % (head, pay)
    else:
        txt = "⚠️ Платёж по рассрочке просрочен более 2 дней\n%s\n\nПожалуйста, срочно оплатите или напишите нам в чат HORIZON BANK.\n%s" % (head, pay)
    ref.update({"reminders.%s" % key: True})  # отмечаем до отправки, чтобы не продублировать
    cid = o.get("clientId")
    chat = client_chat(cid, oid)
    if chat:
        say(chat, esc(txt))
    try:
        bank_post(cid, txt)
    except Exception:
        log.exception("bank_post reminder")
    if kind in ("due", "late3"):
        atxt = ("💳 <b>Платёж по рассрочке%s</b>\nЗаказ <code>%s</code> · платёж %d из %d\nКлиент: %s %s\nСумма: %s · срок: %s") % (
            " — просрочка" if kind == "late3" else "", esc(oid), x["n"], len(sched), esc(o.get("clientName")), esc(o.get("clientPhone")), esc(amount), esc(due))
        markup = {"inline_keyboard": [[{"text": "✅ Платёж получен", "callback_data": "ip:%s:%d" % (oid, x["n"])}]]}
        for a in kyc_admins():
            say(a, atxt, reply_markup=markup)


def installment_reminders(now):
    today = now.date()
    for d in db.collection("orders").where("installmentProvider", "==", "HORIZON кредит").where("creditStatus", "==", "approved").stream():
        try:
            _remind_one(d.reference, d.id, d.to_dict() or {}, today)
        except Exception:
            log.exception("reminder %s", d.id)
            report_exc("напоминание о платеже")


def installment_paid(cb, rest):
    """Админ нажал «Платёж получен»: callback_data = ip:<orderId>:<номер платежа>."""
    oid, _, num = rest.rpartition(":")
    try:
        n = int(num)
    except ValueError:
        return tg("answerCallbackQuery", callback_query_id=cb["id"])
    ref = db.collection("orders").document(oid)
    o = ref.get().to_dict() or {}
    sched = o.get("installmentSchedule") or []
    paid = int(o.get("installmentPaid") or 0)
    if n > len(sched) or paid != n - 1:
        return tg("answerCallbackQuery", callback_query_id=cb["id"], text="Уже отмечено", show_alert=True)
    done = n >= len(sched)
    upd = {"installmentPaid": n}
    if done:
        upd["creditStatus"] = "closed"
    ref.update(upd)
    short = oid[-5:].upper()
    if done:
        msg = "🎉 Рассрочка по заказу №%s полностью погашена. Спасибо, что вы с HORIZON MARKET!" % short
    else:
        nx = sched[n]
        msg = "✅ Платёж %d из %d по заказу №%s получен. Спасибо!\nСледующий платёж: %s — %s" % (n, len(sched), short, fmt_date(nx["due"]), money(nx["amount"]))
    cid = o.get("clientId")
    chat = client_chat(cid, oid)
    if chat:
        say(chat, esc(msg))
    try:
        bank_post(cid, msg)
    except Exception:
        log.exception("bank_post paid")
    tg("editMessageReplyMarkup", chat_id=cb["message"]["chat"]["id"], message_id=cb["message"]["message_id"],
       reply_markup={"inline_keyboard": [[{"text": "✅ Платёж %d получен" % n, "callback_data": "done"}]]})
    tg("answerCallbackQuery", callback_query_id=cb["id"], text="Готово")


# ---------- напоминание о незавершённой верификации ----------
STEP_NAMES = {"fio": "ФИО", "dob": "дата рождения", "work": "место работы", "phone1": "первый доп. телефон", "phone2": "второй доп. телефон",
              "pass_front": "паспорт (лицевая сторона)", "pass_back": "паспорт (обратная сторона)", "selfie": "аутентификация (селфи)"}


def kyc_abandon_reminders():
    now = int(time.time() * 1000)
    for d in db.collection("kycSessions").stream():
        sess = d.to_dict() or {}
        step = sess.get("step")
        if step not in KYC_ORDER:
            continue
        up = sess.get("updatedAt")
        if not up:
            d.reference.update({"updatedAt": now, "remind": 0})  # старые сессии: отсчёт начинаем с сегодня
            continue
        chat = int(d.id)
        hours = (now - up) / 3600000.0
        done = int(sess.get("remind") or 0)
        if hours >= 168:
            d.reference.delete()
            say(chat, "Верификация отменена: прошло 7 дней без ответа. Начать заново можно кнопкой в чате HORIZON BANK в приложении.")
        elif hours >= 24 and done < 2:
            d.reference.update({"remind": 2})
            say(chat, "⏰ Вы так и не завершили верификацию HORIZON BANK (шаг %d из %d). Ваши ответы сохранены — продолжим с того же места. Отмена: /cancel" % (KYC_ORDER.index(step) + 1, len(KYC_ORDER)))
            kyc_ask(chat, step)
        elif hours >= 1 and done < 1:
            d.reference.update({"remind": 1})
            say(chat, "⏰ Вы не закончили верификацию HORIZON BANK (шаг %d из %d). Ваши ответы сохранены — продолжим с того же места. Отмена: /cancel" % (KYC_ORDER.index(step) + 1, len(KYC_ORDER)))
            kyc_ask(chat, step)


# ---------- подписки Firestore и планировщик ----------
_watchers = {}


def start_watchers():
    for w in list(_watchers.values()):
        try:
            w.unsubscribe()
        except Exception:
            pass
    _watchers.clear()
    _watchers["credit_orders"] = db.collection("orders").where("installmentProvider", "==", "HORIZON кредит").on_snapshot(on_orders)
    _watchers["all_orders"] = db.collection("orders").on_snapshot(on_all_orders)
    if AI_KEY:
        _watchers["chats"] = db.collection("chats").where("sellerId", "==", BANK).on_snapshot(on_chats)


def check_watchers():
    for name, w in list(_watchers.items()):
        if getattr(w, "is_active", True) is False:
            notify_admins("Подписка на Firestore «%s» остановилась — перезапускаю." % name, key="watch:" + name)
            start_watchers()
            return


def scheduler():
    time.sleep(60)
    while True:
        try:
            check_watchers()
            now = datetime.now(TZ)
            if 9 <= now.hour < 21:  # ночью клиентам не пишем
                kyc_abandon_reminders()
                installment_reminders(now)
                channel_reminders()
        except Exception:
            log.exception("scheduler")
            report_exc("планировщик напоминаний")
        time.sleep(1800)


# ---------- подписка на канал: проверка и напоминания каждые 4 часа ----------
def subscribe_markup():
    return {"inline_keyboard": [[{"text": "📢 Подписаться", "url": CHANNEL_URL}], [{"text": "✅ Я подписался", "callback_data": "subchk"}]]}


def channel_subscribed(user_id):
    """True/False — подписан ли пользователь на канал; None — проверить не удалось (бот не админ канала и т.п.)."""
    r = tg("getChatMember", chat_id="@" + CHANNEL, user_id=user_id)
    if r is None:
        return None
    st = r.get("status")
    return st in ("creator", "administrator", "member") or (st == "restricted" and bool(r.get("is_member")))


def on_subcheck(cb):
    uid = cb["from"]["id"]
    st = channel_subscribed(uid)
    if st is None:
        notify_admins("Бот не может проверить подписку на канал @%s: <code>%s</code>\nДобавьте бота администратором канала." % (esc(CHANNEL), esc(LAST_TG_ERR["text"])), key="chanperm", every=21600)
        return tg("answerCallbackQuery", callback_query_id=cb["id"], text="Не удалось проверить подписку. Попробуйте чуть позже.", show_alert=True)
    if not st:
        return tg("answerCallbackQuery", callback_query_id=cb["id"], text="Вы ещё не подписаны. Нажмите «Подписаться», затем снова «Я подписался».", show_alert=True)
    now = int(time.time() * 1000)
    try:
        for d in db.collection("users").where("telegramChatId", "==", uid).limit(3).stream():
            d.reference.update({"chSub": True, "chCheckedAt": now})
    except Exception:
        log.exception("subcheck users")
    tg("editMessageText", chat_id=cb["message"]["chat"]["id"], message_id=cb["message"]["message_id"], text="✅ Спасибо! Вы подписаны на канал HORIZON MARKET.")
    tg("answerCallbackQuery", callback_query_id=cb["id"], text="Спасибо за подписку!")


def channel_reminders():
    """Каждые ~4 часа проверяем клиентов с включёнными уведомлениями; не подписанным шлём сообщение с кнопкой «Подписаться»."""
    now = int(time.time() * 1000)
    for d in db.collection("users").where("telegramNotifyEnabled", "==", True).stream():
        u = d.to_dict() or {}
        chat = u.get("telegramChatId")
        if not chat or u.get("role") in ("admin", "seller"):
            continue
        gap = 24 * 3600000 if u.get("chSub") else 4 * 3600000 - 60000  # подписанных перепроверяем раз в сутки
        if now - int(u.get("chCheckedAt") or 0) < gap:
            continue
        st = channel_subscribed(chat)
        if st is None:
            notify_admins("Бот не может проверить подписку на канал @%s: <code>%s</code>\nДобавьте бота администратором канала (хватит права «просмотр участников»)." % (esc(CHANNEL), esc(LAST_TG_ERR["text"])), key="chanperm", every=21600)
            return
        d.reference.update({"chSub": st, "chCheckedAt": now})
        if not st:
            say(chat, "📢 Вы ещё не подписаны на наш канал — там все новинки и акции первыми. Подпишитесь, чтобы ничего не пропустить!", reply_markup=subscribe_markup())
        time.sleep(0.05)


# ═══════════════ РАССЫЛКА О НОВЫХ ТОВАРАХ: ИИ пишет текст под конкретный товар ═══════════════
PROMO_GAP = int(os.environ.get("PROMO_GAP_MIN", "20")) * 60      # пауза между рассылками о разных товарах
PROMO_DAILY_MAX = int(os.environ.get("PROMO_DAILY_MAX", "5"))    # не больше стольких рассылок о товарах в день (защита от спама)

PROMO_SYSTEM = """Ты — копирайтер интернет-магазина электроники HORIZON MARKET (Таджикистан). Напиши короткое сообщение для Telegram о том, что в каталоге появился новый товар.
Требования:
— 1–2 коротких предложения, не больше 200 символов;
— живой, привлекательный тон, подходящий именно этому типу товара: для смартфона — «Новый смартфон уже в каталоге…», для наушников — про звук, для клавиатуры и мыши — про удобство, для ноутбука — про работу и учёбу и т.п.;
— 1–2 уместных эмодзи в тексте;
— называй товар по названию (длинное название можно сократить до бренда и модели);
— опирайся ТОЛЬКО на переданные данные: не выдумывай характеристики, скидки, акции, гарантию, сроки, «хит продаж» и «ограниченную партию»;
— цену не указывай (её добавят отдельно);
— без хэштегов, без markdown и HTML, без кавычек вокруг текста, не начинай с «Привет»;
— пиши по-русски.
Ответ — только сам текст сообщения."""

PROMO_FALLBACK = [
    "🆕 Новинка в каталоге: {name}.",
    "✨ В HORIZON MARKET появился новый товар: {name}.",
    "🛍 Свежее поступление — {name}. Загляните в каталог!",
    "🔥 Встречайте новинку: {name} уже в каталоге.",
    "📦 В каталог добавили {name}. Посмотрите, пока интересно!",
]


def name_of(coll, doc_id):
    if not doc_id:
        return ""
    try:
        return (db.collection(coll).document(str(doc_id)).get().to_dict() or {}).get("name") or ""
    except Exception:
        return ""


def clean_promo(t):
    t = re.sub(r"<[^>]+>", "", t or "")
    t = re.sub(r"\s+", " ", t).strip().strip('"«»“”').strip()
    if len(t) > 260:
        cut = max(t.rfind(x, 0, 260) for x in ".!?…")
        t = t[:cut + 1] if cut > 80 else t[:257].rstrip() + "…"
    return t if len(t) >= 10 else None


def ai_promo_text(p, cat, sub):
    """Текст рассылки под товар от ИИ; None, если ИИ недоступен."""
    if not AI_KEY:
        return None
    chars = "; ".join("%s: %s" % (c.get("key"), c.get("value")) for c in (p.get("characteristics") or [])[:6] if isinstance(c, dict) and c.get("key"))
    info = "Название: %s\nКатегория: %s\nБренд: %s\nХарактеристики: %s\nОписание: %s\nМетка: %s" % (
        p.get("name"), " / ".join(x for x in (cat, sub) if x) or "—", p.get("brand") or "—", chars or "—",
        (p.get("desc") or "—")[:300], p.get("promoLabel") or "—")
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=40,
                          headers={"x-api-key": AI_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json={"model": AI_MODEL, "max_tokens": 200, "temperature": 0.9, "system": PROMO_SYSTEM,
                                "messages": [{"role": "user", "content": info}]})
        j = r.json()
        if r.status_code != 200:
            log.warning("AI promo error %s %s", r.status_code, j)
            notify_admins("ИИ не смог написать текст рассылки: HTTP %s\n<code>%s</code>\nОтправлен обычный шаблон." % (r.status_code, esc(str(j)[:300])), key="aipromo", every=1800)
            return None
        return "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    except Exception:
        log.exception("ai_promo_text")
        return None


def make_hook(p):
    """(текст, 'ai' | 'fallback')"""
    hook = clean_promo(ai_promo_text(p, name_of("catalogs", p.get("catalogId")), name_of("subcatalogs", p.get("subcatalogId"))))
    if hook:
        return hook, "ai"
    return random.choice(PROMO_FALLBACK).format(name=p.get("name") or "товар"), "fallback"


def promo_rates():
    st = {}
    try:
        st = db.collection("settings").document("main").get().to_dict() or {}
    except Exception:
        log.exception("promo_rates")
    site = (os.environ.get("SITE_URL", "") or st.get("siteUrl") or "").strip()
    if site and not site.lower().startswith("http"):
        site = "https://" + site
    return {"rub": float(st.get("rateRUB") or 8.23), "uzs": float(st.get("rateUZS") or 1304), "site": site}


def fmt_price(n, cc, rates):
    """Цена в валюте клиента, как в приложении: TJS — основная, +7 → RUB, +998 → UZS."""
    n = float(n or 0)
    nb = "\u00a0"
    if cc == "+7":
        return "{:,}".format(round(n * rates["rub"])).replace(",", nb) + " ₽"
    if cc == "+998":
        return "{:,}".format(round(n * rates["uzs"])).replace(",", nb) + " UZS"
    return "{:,.2f}".format(n).replace(",", nb).replace(".", ",") + " TJS"


def promo_message(hook, p, cc, rates):
    line = "💰 <b>%s</b>" % esc(fmt_price(p.get("price"), cc, rates))
    try:
        if p.get("oldPrice") and float(p["oldPrice"]) > float(p.get("price") or 0):
            line += "  <s>%s</s>" % esc(fmt_price(p["oldPrice"], cc, rates))
    except Exception:
        pass
    return esc(hook) + "\n\n" + line


def promo_markup(pid, rates):
    row = [{"text": "📢 Смотреть в Telegram", "url": CHANNEL_URL}]
    if rates.get("site"):
        base = rates["site"]
        row.append({"text": "🌐 Смотреть на сайте", "url": "%s%sproduct=%s" % (base, "&" if "?" in base else "?", pid)})
    return {"inline_keyboard": [row]}


def promo_photo(p):
    """Первое фото товара: data:-картинка из Firestore (декодируем) или ссылка."""
    img = (p.get("images") or [None])[0]
    try:
        if isinstance(img, str) and img.startswith("data:") and "," in img:
            return {"bytes": base64.b64decode(img.split(",", 1)[1])}
        if isinstance(img, str) and img.startswith("http"):
            return {"fid": img}
    except Exception:
        log.exception("promo_photo")
    return {}


def send_promo_to(chat, text, markup, photo):
    """Фото с подписью (первый раз загружаем, дальше по file_id) или просто текст. Возвращает результат Telegram либо None."""
    if photo.get("fid") or photo.get("bytes"):
        if photo.get("fid"):
            r = tg("sendPhoto", chat_id=chat, photo=photo["fid"], caption=text[:1024], parse_mode="HTML", reply_markup=markup)
        else:
            r = send_bytes_photo(chat, photo["bytes"], text[:1024], markup)
        if r:
            try:
                photo.clear()
                photo["fid"] = r["photo"][-1]["file_id"]
            except Exception:
                pass
            return r
        if re.search(r"blocked|chat not found|deactivated|kicked", LAST_TG_ERR["text"], re.I):
            return None
        photo.clear()  # фото не принято Telegram — дальше шлём только текст
    return say(chat, text, reply_markup=markup)


def promo_audience():
    seen, out = set(), []
    for d in db.collection("users").where("telegramNotifyEnabled", "==", True).select(["telegramChatId", "countryCode"]).stream():
        u = d.to_dict() or {}
        ch = u.get("telegramChatId")
        if ch and str(ch) not in seen:
            seen.add(str(ch))
            out.append((ch, u.get("countryCode")))
    return out


@firestore.transactional
def _claim_promo_tx(tx, ref):
    if ref.get(transaction=tx).exists:
        return False
    tx.set(ref, {"state": "sending", "at": int(time.time() * 1000)})
    return True


def process_new_product(pid, p):
    """Рассылка о новом товаре всем клиентам с включёнными уведомлениями. Возвращает True, если разослали."""
    log_ref = db.collection("promoLog").document(pid)  # отдельная коллекция, чтобы не трогать документ товара (он тяжёлый, с фото)
    if not _claim_promo_tx(db.transaction(), log_ref):
        return False
    try:
        if p.get("inStock") is False or float(p.get("price") or 0) <= 0 or not p.get("name"):
            log_ref.update({"state": "skipped"})
            return False
        hook, how = make_hook(p)
        rates = promo_rates()
        markup, photo = promo_markup(pid, rates), promo_photo(p)
        sent = failed = 0
        for chat, cc in promo_audience():
            if send_promo_to(chat, promo_message(hook, p, cc, rates), markup, photo):
                sent += 1
            else:
                failed += 1
            time.sleep(0.05)  # ~20 сообщений в секунду — в пределах лимитов Telegram
        log_ref.update({"state": "sent", "sent": sent, "failed": failed, "text": hook, "by": how})
        notify_admins("Рассылка о товаре «%s»: доставлено %d, не доставлено %d.\nТекст (%s): %s" % (
            esc(p.get("name")), sent, failed, "ИИ" if how == "ai" else "шаблон", esc(hook)), key=None, every=0, icon="📣")
        return True
    except Exception:
        log_ref.update({"state": "error"})
        raise


def promo_tick():
    """Раз в пару минут: берём самый старый ещё не обработанный новый товар и рассылаем (днём, не чаще раза в PROMO_GAP)."""
    if not 9 <= datetime.now(TZ).hour < 21:
        return  # ночью не рассылаем — товар уйдёт утром
    st_ref = db.collection("settings").document("botPromo")
    st = st_ref.get().to_dict() or {}
    if not st.get("cursor"):
        st_ref.set({"cursor": datetime.now(timezone.utc)}, merge=True)  # первый запуск: уже добавленные товары не трогаем
        return
    now_ms = int(time.time() * 1000)
    if now_ms - int(st.get("lastSentAt") or 0) < PROMO_GAP * 1000:
        return
    docs = list(db.collection("products").where("createdAt", ">", st["cursor"]).order_by("createdAt").limit(1).stream())
    if not docs:
        return
    d = docs[0]
    created = d.get("createdAt")
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    count = int(st.get("count") or 0) if st.get("day") == day else 0
    if count >= PROMO_DAILY_MAX:
        db.collection("promoLog").document(d.id).set({"state": "skipped_limit", "at": now_ms})
        st_ref.set({"cursor": created}, merge=True)
        return
    ok = False
    try:
        ok = process_new_product(d.id, d.to_dict() or {})
    finally:
        upd = {"cursor": created}
        if ok:
            upd.update({"lastSentAt": int(time.time() * 1000), "day": day, "count": count + 1})
        st_ref.set(upd, merge=True)


def promo_loop():
    time.sleep(90)
    while True:
        try:
            promo_tick()
        except Exception:
            log.exception("promo")
            report_exc("рассылка о новых товарах")
        time.sleep(120)


def promo_preview(chat, arg):
    """Для админа: /promo [id товара] — показывает, как будет выглядеть рассылка (отправляется только вам)."""
    if chat not in (set(admin_ids()) | set(kyc_admins())):
        return say(chat, "Команда только для администратора.")
    if arg:
        doc = db.collection("products").document(arg).get()
        if not doc.exists:
            return say(chat, "Товар с таким id не найден.")
        pid, p = doc.id, doc.to_dict() or {}
    else:
        docs = list(db.collection("products").order_by("createdAt", direction=firestore.Query.DESCENDING).limit(1).stream())
        if not docs:
            return say(chat, "Товаров пока нет.")
        pid, p = docs[0].id, docs[0].to_dict() or {}
    hook, how = make_hook(p)
    rates = promo_rates()
    send_promo_to(chat, promo_message(hook, p, "+992", rates), promo_markup(pid, rates), promo_photo(p))
    say(chat, "👆 Так клиенты увидят рассылку о товаре «%s». Текст: %s." % (
        esc(p.get("name")), "написал ИИ" if how == "ai" else "обычный шаблон (ИИ недоступен — проверьте ANTHROPIC_API_KEY)")
        + ("" if rates.get("site") else "\n⚠️ Кнопки «Смотреть на сайте» нет: не задан SITE_URL (Railway → Variables)."))


# ---------- команды клиента ----------
CREDIT_HUMAN = {"kyc_required": "нужно пройти верификацию", "review": "заявка на проверке (около 2–3 часов)", "rejected": "заявка отклонена",
                "limit_exceeded": "сумма превышает кредитный лимит", "closed": "рассрочка погашена ✅"}
HELP_TEXT = ("Команды:\n/status — на каком шаге заявка и когда ближайший платёж\n/continue — продолжить верификацию с того же места\n"
             "/cancel — отменить верификацию или оплату\n/help — эта подсказка")


def client_ids_for_chat(chat):
    ids = set()
    try:
        for d in db.collection("users").where("telegramChatId", "==", chat).limit(3).stream():
            ids.add(d.id)
        for d in db.collection("kycApplications").where("chatId", "==", chat).limit(5).stream():
            c = (d.to_dict() or {}).get("clientId")
            if c:
                ids.add(c)
    except Exception:
        log.exception("client_ids_for_chat")
    return ids


def status_text(chat):
    out = []
    ks = db.collection("kycSessions").document(str(chat)).get()
    if ks.exists:
        st = (ks.to_dict() or {}).get("step")
        if st in KYC_ORDER:
            out.append("🏦 <b>Верификация</b>: шаг %d из %d — %s.\nПродолжить: /continue · Отменить: /cancel" % (KYC_ORDER.index(st) + 1, len(KYC_ORDER), esc(STEP_NAMES.get(st, st))))
    lines = []
    for cid in client_ids_for_chat(chat):
        try:
            for d in db.collection("orders").where("clientId", "==", cid).where("installmentProvider", "==", "HORIZON кредит").limit(5).stream():
                o = d.to_dict() or {}
                cs = o.get("creditStatus")
                if not cs:
                    continue
                if cs == "approved":
                    sched, paid = o.get("installmentSchedule") or [], int(o.get("installmentPaid") or 0)
                    if sched and paid < len(sched):
                        x = sched[paid]
                        h = "рассрочка одобрена. Следующий платёж %d из %d: %s — %s" % (x["n"], len(sched), fmt_date(x["due"]), money(x["amount"]))
                    else:
                        h = "рассрочка одобрена"
                else:
                    h = CREDIT_HUMAN.get(cs, cs)
                lines.append("• Заказ №%s: %s" % (d.id[-5:].upper(), h))
        except Exception:
            log.exception("status orders")
    if lines:
        out.append("💳 <b>Рассрочка HORIZON кредит</b>\n" + esc("\n".join(lines)))
    pay = db.collection("botChats").document(str(chat)).get().to_dict() or {}
    if pay.get("orderId"):
        out.append("🧾 Ждём чек об оплате заказа №%s. Отправьте сюда фото чека. Отмена: /cancel" % esc(str(pay["orderId"])[-5:].upper()))
    return "\n\n".join(out) or "Активных заявок и платежей нет.\nЧтобы оформить заказ, откройте приложение HORIZON MARKET."


def on_command(chat, cmd, text=""):
    if cmd == "/promo":
        parts = text.split()
        return promo_preview(chat, parts[1] if len(parts) > 1 else "")
    if cmd == "/status":
        return say(chat, status_text(chat))
    if cmd == "/help":
        return say(chat, HELP_TEXT)
    kref = db.collection("kycSessions").document(str(chat))
    if cmd == "/continue":
        ks = kref.get()
        st = (ks.to_dict() or {}).get("step") if ks.exists else None
        if st not in KYC_ORDER:
            return say(chat, "Сейчас нет незавершённой верификации. Узнать статус: /status")
        kref.update({"updatedAt": int(time.time() * 1000), "remind": 0})
        say(chat, "▶️ Продолжаем верификацию — шаг %d из %d." % (KYC_ORDER.index(st) + 1, len(KYC_ORDER)))
        return kyc_ask(chat, st)
    if cmd == "/cancel":
        if kref.get().exists:
            kref.delete()
            return say(chat, "Верификация отменена. Вы можете начать заново кнопкой в чате HORIZON BANK.")
        bref = db.collection("botChats").document(str(chat))
        if bref.get().exists:
            bref.delete()
            return say(chat, "Оплата отменена. Чтобы вернуться к ней, нажмите «Оплатить» у заказа в приложении.")
        return say(chat, "Сейчас нечего отменять.")
    return say(chat, "Неизвестная команда. Список команд: /help")


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
    if text.startswith("/"):
        return on_command(chat, text.split()[0].split("@")[0].lower(), text)
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
    notify_admins("Бот запущен. Если вы его не обновляли — значит он перезапустился после сбоя (причину ищите в Logs на Railway).", key="start", every=0, icon="🔄")
    offset = None
    fails = 0
    while True:
        body = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            body["offset"] = offset
        try:
            res = S.post(API + "getUpdates", json=body, timeout=45).json()
        except Exception as e:
            log.warning("getUpdates: %s", e)
            fails += 1
            if fails == 6:
                notify_admins("Бот не может получить сообщения из Telegram: <code>%s</code>" % esc(str(e)[:300]), key="poll", every=1800)
            time.sleep(3)
            continue
        if not res.get("ok"):
            log.warning("getUpdates: %s", res.get("description"))
            fails += 1
            desc = str(res.get("description"))
            if "Conflict" in desc:
                notify_admins("Бот запущен в двух местах одновременно (Telegram: Conflict). Оставьте только один запуск, иначе сообщения будут теряться.", key="conflict", every=3600)
            elif fails == 6:
                notify_admins("Telegram вернул ошибку при получении сообщений: <code>%s</code>" % esc(desc[:300]), key="poll", every=1800)
            time.sleep(5)
            continue
        fails = 0
        for u in res["result"]:
            offset = u["update_id"] + 1
            try:
                handle(u)
            except Exception:
                log.exception("Ошибка обработки апдейта")
                report_exc("обработка сообщения клиента")


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:
        if not isinstance(e, (KeyboardInterrupt, SystemExit)):
            log.exception("Бот остановился из-за ошибки")
            report_exc("бот остановился")
        raise
