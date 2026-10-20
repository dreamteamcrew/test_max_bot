"""Резервный канал «пациент — врач» для Telegram на вебхуке.

Упрощённая часть ТЗ NV-Clinic: подключение врача по ссылке, анкета пациента
(согласие, ФИО, контакт), подтверждение врачом и переписка через бота.
"""
import hashlib
import json
import os
import secrets
import urllib.error
from datetime import datetime, timedelta, timezone
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import db

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"
# Секрет, по которому бот узнаёт запросы от Telegram (выводится из токена).
SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
WEBHOOK_PATH = "/webhook"
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
MAX_PENDING = 10  # не больше 10 неподтверждённых заявок на врача
BOT_USERNAME = ""

STRANGER_TEXT = (
    "Это резервный канал связи клиники с врачом. Подключиться можно только "
    "по ссылке, которую даёт ваш врач. Пожалуйста, не присылайте сюда "
    "медицинских подробностей."
)
CONSENT_TEXT = (
    "Чтобы врач мог переписываться с вами через этот канал, нужно согласие "
    "на обработку персональных данных: ФИО, номер телефона и ваш ID в Telegram. "
    "Текст сообщений бот не хранит, он только пересылает их врачу.\n\n"
    "Вы согласны?"
)
HELP = {
    "admin": "Команды администратора:\n/doctor_add ФИО — ссылка для подключения врача\n"
    "/doctors — список врачей\n/doctor_remove ID — отключить врача\n/status — сводка",
    "doctor": "Команды врача:\n/link — ваша ссылка для пациентов\n/newlink — новая ссылка, "
    "старая перестанет работать\n/patients — ваши пациенты\n\nЧтобы ответить пациенту, "
    "нажмите «Ответить» на его сообщении.",
}


class TelegramError(Exception):
    pass


def call(method, payload):
    req = urllib.request.Request(
        f"{API}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp).get("result")
    except urllib.error.HTTPError as exc:
        raise TelegramError(f"{method}: {exc.code} {exc.read()[:200]!r}") from exc


def send(chat_id, text, **extra):
    return call("sendMessage", {"chat_id": chat_id, "text": text, **extra})


def buttons(*rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def new_code():
    """Случайный код для ссылки и его хеш: в базе храним только хеш."""
    code = secrets.token_urlsafe(12)
    return code, hash_code(code)


def hash_code(code):
    return hashlib.sha256(code.encode()).hexdigest()


def deep_link(payload):
    return f"https://t.me/{BOT_USERNAME}?start={payload}"


def log_consent(tg_id, action):
    db.run("INSERT INTO consent_log (tg_id, action) VALUES (:t, :a)", t=tg_id, a=action)


def get_doctor(tg_id):
    return db.one("SELECT * FROM doctors WHERE tg_id = :t", t=tg_id)


def get_patient(tg_id):
    return db.one("SELECT * FROM patients WHERE tg_id = :t", t=tg_id)


# --- Обработка обновлений -------------------------------------------------


def handle_update(update):
    if "callback_query" in update:
        return handle_callback(update["callback_query"])
    message = update.get("message")
    if not message or message["chat"]["type"] != "private":
        return
    user_id = message["from"]["id"]
    text = message.get("text") or ""

    if text.startswith("/"):
        command, _, arg = text.partition(" ")
        return handle_command(user_id, command.split("@")[0], arg.strip(), message)

    doctor = get_doctor(user_id)
    if doctor:
        return doctor_message(doctor, message)
    patient = get_patient(user_id)
    if patient:
        return patient_message(patient, message)
    if user_id in ADMIN_IDS:
        return send(user_id, HELP["admin"])
    send(user_id, STRANGER_TEXT)


def handle_command(user_id, command, arg, message):
    if command == "/start":
        return start(user_id, arg, message)
    if command == "/myid":
        return send(user_id, f"Ваш ID: {user_id}")
    if command == "/stop":
        return stop_patient(user_id)
    if user_id in ADMIN_IDS and command in ADMIN_COMMANDS:
        return ADMIN_COMMANDS[command](user_id, arg)
    doctor = get_doctor(user_id)
    if doctor and command in DOCTOR_COMMANDS:
        return DOCTOR_COMMANDS[command](doctor)
    send(user_id, "Не знаю такой команды.")


def start(user_id, payload, message):
    if payload.startswith("d_"):
        return activate_doctor(user_id, payload[2:], message)
    if payload.startswith("p_"):
        return connect_patient(user_id, payload[2:])
    if user_id in ADMIN_IDS:
        return send(user_id, "Здравствуйте! Вы администратор.\n\n" + HELP["admin"])
    if get_doctor(user_id):
        return send(user_id, "Здравствуйте! Вы подключены как врач.\n\n" + HELP["doctor"])
    patient = get_patient(user_id)
    if patient and patient["state"] == "active":
        return send(user_id, "Вы подключены к врачу. Просто напишите сообщение.")
    send(user_id, STRANGER_TEXT)


# --- Врачи ----------------------------------------------------------------


def activate_doctor(user_id, code, message):
    invite = db.one(
        "SELECT * FROM doctor_invites WHERE code_hash = :h AND used_at IS NULL AND expires_at > now()",
        h=hash_code(code),
    )
    if not invite:
        return send(user_id, "Ссылка недействительна: её уже использовали или срок истёк.")
    if get_doctor(user_id):
        return send(user_id, "Вы уже подключены как врач.")
    if get_patient(user_id):
        return send(user_id, "Этот аккаунт подключён как пациент. Сначала отправьте /stop.")
    link, link_hash = new_code()
    db.run("UPDATE doctor_invites SET used_at = now() WHERE code_hash = :h", h=invite["code_hash"])
    db.run(
        "INSERT INTO doctors (tg_id, name, link_hash) VALUES (:t, :n, :l)",
        t=user_id, n=invite["name"], l=link_hash,
    )
    send(user_id, f"Готово, {invite['name']}, вы подключены как врач.\n\n"
         f"Ваша ссылка для пациентов:\n{deep_link('p_' + link)}\n\n" + HELP["doctor"])
    for admin in ADMIN_IDS:
        send(admin, f"Врач {invite['name']} активировал(а) ссылку.")


def cmd_link(doctor):
    send(doctor["tg_id"], "Старую ссылку бот не хранит в открытом виде. "
         "Отправьте /newlink, чтобы получить новую.")


def cmd_newlink(doctor):
    link, link_hash = new_code()
    db.run("UPDATE doctors SET link_hash = :l WHERE tg_id = :t", l=link_hash, t=doctor["tg_id"])
    send(doctor["tg_id"], f"Новая ссылка для пациентов:\n{deep_link('p_' + link)}\n\nСтарая больше не работает.")


def cmd_patients(doctor):
    rows = db.run(
        "SELECT full_name, state FROM patients WHERE doctor_id = :d ORDER BY created_at",
        d=doctor["tg_id"],
    )
    labels = {"active": "подключён", "pending": "ждёт подтверждения"}
    lines = [f"• {r['full_name'] or 'без имени'} ({labels.get(r['state'], 'заполняет анкету')})" for r in rows]
    send(doctor["tg_id"], "Ваши пациенты:\n" + "\n".join(lines) if lines else "Пациентов пока нет.")


def doctor_message(doctor, message):
    patients = db.run(
        "SELECT * FROM patients WHERE doctor_id = :d AND state = 'active'", d=doctor["tg_id"]
    )
    target = None
    reply_to = message.get("reply_to_message")
    if reply_to:
        row = db.one(
            "SELECT patient_id FROM msg_map WHERE doctor_id = :d AND message_id = :m",
            d=doctor["tg_id"], m=reply_to["message_id"],
        )
        target = next((p for p in patients if row and p["tg_id"] == row["patient_id"]), None)
        if not target:
            return send(doctor["tg_id"], "Не нашёл пациента для этого сообщения. "
                        "Нажмите «Ответить» на сообщении пациента.")
    elif len(patients) == 1:
        target = patients[0]
    elif not patients:
        return send(doctor["tg_id"], "У вас пока нет подключённых пациентов.")
    else:
        return send(doctor["tg_id"], "У вас несколько пациентов. Нажмите «Ответить» "
                    "на сообщении того, кому пишете.")
    forward(message, target["tg_id"], f"{doctor['name']}, врач:")
    send(doctor["tg_id"], f"Отправлено: {target['full_name']}")


# --- Пациенты -------------------------------------------------------------


def connect_patient(user_id, code):
    doctor = db.one("SELECT * FROM doctors WHERE link_hash = :h", h=hash_code(code))
    if not doctor:
        return send(user_id, "Ссылка недействительна. Попросите у врача новую.")
    if doctor["tg_id"] == user_id or get_doctor(user_id):
        return send(user_id, "Врач не может подключиться как пациент.")
    patient = get_patient(user_id)
    if patient and patient["doctor_id"] == doctor["tg_id"] and patient["state"] == "active":
        return send(user_id, f"Вы уже подключены к врачу {doctor['name']}.")
    if patient and patient["state"] == "active":
        return send(user_id, "Вы уже подключены к другому врачу. Чтобы сменить врача, "
                    "отправьте /stop и откройте ссылку заново.")
    pending = db.one(
        "SELECT count(*) AS n FROM patients WHERE doctor_id = :d AND state = 'pending'",
        d=doctor["tg_id"],
    )["n"]
    if pending >= MAX_PENDING:
        return send(user_id, "У врача слишком много необработанных заявок. Попробуйте позже.")
    db.run(
        "INSERT INTO patients (tg_id, doctor_id, state) VALUES (:t, :d, 'consent') "
        "ON CONFLICT (tg_id) DO UPDATE SET doctor_id = :d, state = 'consent', "
        "full_name = NULL, phone = NULL",
        t=user_id, d=doctor["tg_id"],
    )
    send(user_id, f"Здравствуйте! Это резервный канал связи с врачом «{doctor['name']}».\n\n" + CONSENT_TEXT,
         reply_markup=buttons([("Согласен", "consent:yes"), ("Не сейчас", "consent:no")]))


def patient_message(patient, message):
    user_id = patient["tg_id"]
    state = patient["state"]
    if state == "consent":
        return send(user_id, "Сначала ответьте на вопрос о согласии кнопкой выше.")
    if state == "name":
        name = (message.get("text") or "").strip()
        if not 3 <= len(name) <= 100:
            return send(user_id, "Напишите, пожалуйста, ФИО текстом, например: Иванов Иван Иванович.")
        db.run("UPDATE patients SET full_name = :n, state = 'contact' WHERE tg_id = :t", n=name, t=user_id)
        return send(user_id, "Спасибо! Теперь поделитесь номером телефона кнопкой ниже.",
                    reply_markup={"keyboard": [[{"text": "Поделиться контактом", "request_contact": True}]],
                                  "resize_keyboard": True, "one_time_keyboard": True})
    if state == "contact":
        return save_contact(patient, message)
    if state == "pending":
        return send(user_id, "Заявка у врача. Как только он подтвердит, можно будет писать.")
    # state == "active"
    doctor = get_doctor(patient["doctor_id"])
    sent = forward(message, doctor["tg_id"], f"Пациент {patient['full_name']}:")
    for msg_id in sent:
        db.run("INSERT INTO msg_map (doctor_id, message_id, patient_id) VALUES (:d, :m, :p) "
               "ON CONFLICT DO NOTHING", d=doctor["tg_id"], m=msg_id, p=user_id)
    # Разговор по ТЗ живёт до 72 часов тишины; на первое сообщение нового разговора отвечаем.
    last = patient["last_msg_at"]
    db.run("UPDATE patients SET last_msg_at = now() WHERE tg_id = :t", t=user_id)
    if last is None or datetime.now(timezone.utc) - last > timedelta(hours=72):
        send(user_id, f"Передал врачу «{doctor['name']}», он ответит здесь же.")


def save_contact(patient, message):
    user_id = patient["tg_id"]
    contact = message.get("contact")
    # Аналог проверки подписи номера в MAX: принимаем только свой контакт с кнопки.
    if not contact or contact.get("user_id") != user_id:
        return send(user_id, "Нажмите кнопку «Поделиться контактом» внизу. Чужой контакт или номер текстом не подойдёт.")
    db.run("UPDATE patients SET phone = :p, state = 'pending' WHERE tg_id = :t",
           p=contact["phone_number"], t=user_id)
    send(user_id, "Заявка отправлена врачу. Как только он подтвердит, я напишу.",
         reply_markup={"remove_keyboard": True})
    doctor = get_doctor(patient["doctor_id"])
    send(doctor["tg_id"],
         f"Новая заявка от пациента:\nФИО: {patient['full_name']}\nТелефон: {contact['phone_number']}",
         reply_markup=buttons([("Подтвердить", f"approve:{user_id}"), ("Отклонить", f"reject:{user_id}")]))


def stop_patient(user_id):
    patient = get_patient(user_id)
    if not patient:
        return send(user_id, "Вы не подключены как пациент.")
    db.run("DELETE FROM patients WHERE tg_id = :t", t=user_id)
    db.run("DELETE FROM msg_map WHERE patient_id = :t", t=user_id)
    log_consent(user_id, "revoke")
    send(user_id, "Вы отключены, ваши данные удалены. Чтобы подключиться снова, откройте ссылку врача.",
         reply_markup={"remove_keyboard": True})
    if patient["state"] in ("pending", "active"):
        send(patient["doctor_id"], f"Пациент {patient['full_name']} отключился.")


def forward(message, chat_id, header):
    """Копирует сообщение получателю с подписью автора. Возвращает ID отправленных сообщений."""
    if "text" in message:
        return [send(chat_id, f"{header}\n{message['text']}")["message_id"]]
    head = send(chat_id, header)["message_id"]
    copy = call("copyMessage", {"chat_id": chat_id, "from_chat_id": message["chat"]["id"],
                                "message_id": message["message_id"]})
    return [head, copy["message_id"]]


# --- Кнопки ---------------------------------------------------------------


def handle_callback(query):
    user_id = query["from"]["id"]
    action, _, arg = (query.get("data") or "").partition(":")
    call("answerCallbackQuery", {"callback_query_id": query["id"]})
    msg = query.get("message") or {}

    def close(text):
        if msg:
            call("editMessageText", {"chat_id": user_id, "message_id": msg["message_id"], "text": text})

    if action == "consent":
        patient = get_patient(user_id)
        if not patient or patient["state"] != "consent":
            return close("Этот вопрос уже не актуален.")
        if arg == "yes":
            log_consent(user_id, "accept")
            db.run("UPDATE patients SET state = 'name' WHERE tg_id = :t", t=user_id)
            close(msg.get("text", "") + "\n\n✅ Согласие получено.")
            return send(user_id, "Напишите, пожалуйста, ваши ФИО полностью.")
        log_consent(user_id, "decline")
        db.run("DELETE FROM patients WHERE tg_id = :t", t=user_id)
        return close("Хорошо. Если передумаете, откройте ссылку врача ещё раз.")

    if action in ("approve", "reject"):
        patient = get_patient(int(arg))
        if not patient or patient["doctor_id"] != user_id or patient["state"] != "pending":
            return close("Заявка уже обработана или отозвана.")
        doctor = get_doctor(user_id)
        if action == "approve":
            db.run("UPDATE patients SET state = 'active' WHERE tg_id = :t", t=patient["tg_id"])
            close(f"✅ Пациент {patient['full_name']} подключён. Его сообщения будут приходить сюда.")
            return send(patient["tg_id"], f"Готово! Врач {doctor['name']} подтвердил подключение. "
                        "Можете писать ему прямо здесь.")
        db.run("DELETE FROM patients WHERE tg_id = :t", t=patient["tg_id"])
        close(f"❌ Заявка {patient['full_name']} отклонена.")
        return send(patient["tg_id"], "Врач отклонил заявку. Если это ошибка, попросите у него ссылку ещё раз.")


# --- Администратор --------------------------------------------------------


def cmd_doctor_add(admin_id, name):
    if not name:
        return send(admin_id, "Укажите ФИО: /doctor_add Смирнова Е. В.")
    code, code_hash = new_code()
    db.run("INSERT INTO doctor_invites (code_hash, name, expires_at) VALUES (:h, :n, now() + interval '14 days')",
           h=code_hash, n=name)
    send(admin_id, f"Ссылка для врача {name} (одноразовая, действует 14 дней):\n{deep_link('d_' + code)}")


def cmd_doctors(admin_id, _):
    rows = db.run(
        "SELECT d.tg_id, d.name, count(p.tg_id) FILTER (WHERE p.state = 'active') AS active, "
        "count(p.tg_id) FILTER (WHERE p.state = 'pending') AS pending "
        "FROM doctors d LEFT JOIN patients p ON p.doctor_id = d.tg_id GROUP BY d.tg_id ORDER BY d.name"
    )
    lines = [f"• {r['name']} (ID {r['tg_id']}): пациентов {r['active']}, заявок {r['pending']}" for r in rows]
    send(admin_id, "Врачи:\n" + "\n".join(lines) if lines else "Врачей пока нет. Добавьте: /doctor_add ФИО")


def cmd_doctor_remove(admin_id, arg):
    doctor = get_doctor(int(arg)) if arg.isdigit() else None
    if not doctor:
        return send(admin_id, "Укажите ID врача из /doctors: /doctor_remove 123456")
    patients = db.run("SELECT tg_id FROM patients WHERE doctor_id = :d", d=doctor["tg_id"])
    db.run("DELETE FROM msg_map WHERE doctor_id = :d", d=doctor["tg_id"])
    db.run("DELETE FROM doctors WHERE tg_id = :d", d=doctor["tg_id"])
    for p in patients:
        send(p["tg_id"], "Врач больше не подключён к резервному каналу, ваша связка удалена.")
    send(doctor["tg_id"], "Администратор отключил вас от бота.")
    send(admin_id, f"Врач {doctor['name']} отключён, пациентов отвязано: {len(patients)}.")


def cmd_status(admin_id, _):
    s = db.one(
        "SELECT (SELECT count(*) FROM doctors) AS doctors, "
        "(SELECT count(*) FROM patients WHERE state = 'active') AS active, "
        "(SELECT count(*) FROM patients WHERE state = 'pending') AS pending, "
        "(SELECT count(*) FROM patients WHERE state IN ('consent', 'name', 'contact')) AS filling"
    )
    send(admin_id, f"Врачей: {s['doctors']}\nПациентов подключено: {s['active']}\n"
         f"Заявок ждут врача: {s['pending']}\nЗаполняют анкету: {s['filling']}")


ADMIN_COMMANDS = {"/doctor_add": cmd_doctor_add, "/doctors": cmd_doctors,
                  "/doctor_remove": cmd_doctor_remove, "/status": cmd_status}
DOCTOR_COMMANDS = {"/link": cmd_link, "/newlink": cmd_newlink, "/patients": cmd_patients}


# --- HTTP-сервер ----------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._respond(200, b"ok")

    def do_HEAD(self):
        self._respond(200, b"")

    def do_POST(self):
        if self.path != WEBHOOK_PATH:
            return self._respond(404, b"not found")
        if self.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
            return self._respond(403, b"forbidden")
        length = int(self.headers.get("Content-Length", 0))
        try:
            handle_update(json.loads(self.rfile.read(length)))
        except Exception as exc:  # не роняем сервер из-за одного сообщения
            print("error:", type(exc).__name__, exc, flush=True)
        self._respond(200, b"ok")

    def _respond(self, code, body):
        self.send_response(code)
        self.end_headers()
        if body:
            self.wfile.write(body)


def set_webhook():
    base = os.environ.get("RENDER_EXTERNAL_URL")
    if not base:
        print("RENDER_EXTERNAL_URL не задан, вебхук не установлен", flush=True)
        return
    result = call("setWebhook", {"url": base + WEBHOOK_PATH, "secret_token": SECRET,
                                 "allowed_updates": ["message", "callback_query"]})
    print("setWebhook:", result, flush=True)


if __name__ == "__main__":
    db.init()
    BOT_USERNAME = call("getMe", {})["username"]
    set_webhook()
    port = int(os.environ.get("PORT", 10000))
    print(f"Бот @{BOT_USERNAME}, слушаю порт {port}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
