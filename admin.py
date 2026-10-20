"""Веб-админка бота: врачи, пациенты и журнал сессий без текста сообщений.

Живёт в том же HTTP-сервере, что и вебхук, по адресам /admin/...
Вход без пароля: администратор пишет боту /admin и получает одноразовую ссылку
на 10 минут. Пускаем только тех, кто сейчас в ADMIN_IDS.
"""
import html
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote, urlparse

import segno

import db

bot = None  # модуль бота, подставляет bot.py при запуске
COOKIE = "admin_session"
SESSION_DAYS = 7
STATES = {"active": "🟢 подключён", "pending": "🟡 ждёт врача", "contact": "⚪️ анкета: телефон",
          "name": "⚪️ анкета: ФИО", "consent": "⚪️ анкета: согласие"}


def owns(path):
    return path == "/admin" or path.startswith("/admin/") or path.startswith("/admin?")


def base_url():
    return os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")


def login_link(admin_id):
    if not base_url():
        return None
    code, code_hash = bot.new_code()
    db.run("DELETE FROM admin_logins WHERE expires_at < now()")
    db.run("INSERT INTO admin_logins (code_hash, admin_id, expires_at) "
           "VALUES (:h, :a, now() + interval '10 minutes')", h=code_hash, a=admin_id)
    return f"{base_url()}/admin/login?code={code}"


def e(value):
    return html.escape(str(value if value is not None else ""))


# --- HTTP ------------------------------------------------------------------


class Request:
    def __init__(self, handler, method):
        self.handler = handler
        self.method = method
        url = urlparse(handler.path)
        self.path = url.path.rstrip("/") or "/admin"
        self.query = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        self.form = {}
        if method == "POST":
            length = min(int(handler.headers.get("Content-Length", 0)), 100_000)
            body = handler.rfile.read(length).decode("utf-8", "replace")
            self.form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        cookie = SimpleCookie(handler.headers.get("Cookie", ""))
        self.token = cookie[COOKIE].value if COOKIE in cookie else None
        self.admin_id = None


def respond(handler, code, body="", headers=None):
    data = body.encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Frame-Options", "DENY")
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(data)


def redirect(handler, location, cookie=None):
    headers = {"Location": location}
    if cookie is not None:
        headers["Set-Cookie"] = cookie
    respond(handler, 303, "", headers)


def back(handler, path, msg):
    redirect(handler, f"{path}?msg={quote(msg)}")


def current_admin(req):
    if not req.token:
        return None
    row = db.one("SELECT admin_id FROM admin_sessions WHERE token_hash = :h AND expires_at > now()",
                 h=bot.hash_code(req.token))
    if row and row["admin_id"] in bot.ADMIN_IDS:
        return row["admin_id"]
    return None


def handle(handler, method):
    try:
        req = Request(handler, method)
        if req.path == "/admin/login":
            return login(req)
        req.admin_id = current_admin(req)
        if not req.admin_id:
            return respond(handler, 401, page("Вход", LOGIN_HELP, nav=False))
        route = ROUTES.get((method, req.path))
        if not route:
            return respond(handler, 404, page("Не найдено", "<p>Такой страницы нет.</p>"))
        route(req)
    except Exception as exc:  # одна ошибка не должна ронять бота
        print("admin error:", type(exc).__name__, exc, flush=True)
        respond(handler, 500, page("Ошибка", f"<p class=err>Что-то пошло не так: {e(type(exc).__name__)}. "
                                   "Подробности в логах Render.</p>"))


LOGIN_HELP = ("<p>Чтобы войти, напишите боту команду <code>/admin</code> со своего аккаунта "
              "администратора и откройте ссылку из ответа.</p>")


def login(req):
    # По GET только показываем кнопку: так ссылку не «съест» предпросмотр в мессенджере.
    code = req.form.get("code") if req.method == "POST" else req.query.get("code")
    if not code:
        return respond(req.handler, 400, page("Вход", LOGIN_HELP, nav=False))
    if req.method == "GET":
        return respond(req.handler, 200, page("Вход", f"""
<form method=post action=/admin/login class=card>
  <input type=hidden name=code value="{e(code)}">
  <p>Вход в админку бота.</p><button>Войти</button>
</form>""", nav=False))
    row = db.one("DELETE FROM admin_logins WHERE code_hash = :h AND expires_at > now() RETURNING admin_id",
                 h=bot.hash_code(code))
    if not row or row["admin_id"] not in bot.ADMIN_IDS:
        return respond(req.handler, 403, page("Вход", "<p class=err>Ссылка недействительна: её уже "
                                              "использовали или прошло 10 минут.</p>" + LOGIN_HELP, nav=False))
    token = secrets.token_urlsafe(32)
    db.run("DELETE FROM admin_sessions WHERE expires_at < now()")
    db.run(f"INSERT INTO admin_sessions (token_hash, admin_id, expires_at) "
           f"VALUES (:h, :a, now() + interval '{SESSION_DAYS} days')", h=bot.hash_code(token), a=row["admin_id"])
    cookie = (f"{COOKIE}={token}; Path=/admin; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}"
              + ("; Secure" if base_url().startswith("https") else ""))
    redirect(req.handler, "/admin", cookie)


def logout(req):
    db.run("DELETE FROM admin_sessions WHERE token_hash = :h", h=bot.hash_code(req.token))
    redirect(req.handler, "/admin/login", f"{COOKIE}=; Path=/admin; Max-Age=0")


# --- Страницы --------------------------------------------------------------


def overview(req):
    s = db.one(
        "SELECT (SELECT count(*) FROM doctors) AS doctors, "
        "(SELECT count(*) FROM patients WHERE state = 'active') AS active, "
        "(SELECT count(*) FROM patients WHERE state = 'pending') AS pending, "
        "(SELECT count(*) FROM patients WHERE state IN ('consent', 'name', 'contact')) AS filling, "
        "(SELECT count(*) FROM chat_sessions WHERE day = (now() AT TIME ZONE 'Europe/Moscow')::date) AS today"
    )
    tiles = "".join(f"<a class=tile href={href}><b>{n}</b><span>{label}</span></a>" for n, label, href in [
        (s["doctors"], "врачей", "/admin/doctors"), (s["active"], "пациентов подключено", "/admin/patients"),
        (s["pending"], "заявок ждут врача", "/admin/patients"), (s["filling"], "заполняют анкету", "/admin/patients"),
        (s["today"], "сессий сегодня", "/admin/sessions")])
    rows = session_rows(None, None)
    body = f"<div class=tiles>{tiles}</div><h2>Кто с кем общался сегодня</h2>{sessions_table(rows)}"
    respond(req.handler, 200, page("Сводка", body, req))


def doctors_page(req, created=None):
    doctors = db.run(
        "SELECT d.tg_id, d.name, d.created_at, "
        "count(p.tg_id) FILTER (WHERE p.state = 'active') AS active, "
        "count(p.tg_id) FILTER (WHERE p.state = 'pending') AS pending, "
        "(SELECT max(last_at) FROM chat_sessions c WHERE c.doctor_id = d.tg_id) AS last_at "
        "FROM doctors d LEFT JOIN patients p ON p.doctor_id = d.tg_id GROUP BY d.tg_id ORDER BY d.name"
    )
    invites = db.run("SELECT code_hash, name, expires_at, used_at FROM doctor_invites "
                     "WHERE used_at IS NULL ORDER BY expires_at DESC")
    rows = "".join(f"""<tr>
<td><form method=post action=/admin/doctors/rename class=inline>
  <input type=hidden name=id value={d['tg_id']}><input name=name value="{e(d['name'])}" required maxlength=100>
  <button class=ghost title="Сохранить ФИО">💾</button></form>
  <div class=muted>ID {d['tg_id']} · с {fmt_date(d['created_at'])}</div></td>
<td><a href="/admin/patients?doctor={d['tg_id']}">{d['active']}</a>{f" <span class=muted>+{d['pending']} ждут</span>" if d['pending'] else ""}</td>
<td>{fmt_dt(d['last_at']) or '<span class=muted>—</span>'}</td>
<td class=actions>
  <a class=btn href="/admin/sessions?doctor={d['tg_id']}&day=">Журнал</a>
  <form method=post action=/admin/doctors/newlink class=inline><input type=hidden name=id value={d['tg_id']}>
    <button class=ghost onclick="return confirm('Старая ссылка врача для пациентов перестанет работать. Продолжить?')">Новая ссылка</button></form>
  <form method=post action=/admin/doctors/remove class=inline><input type=hidden name=id value={d['tg_id']}>
    <button class=danger onclick="return confirm('Отключить врача {e(d['name'])}? Все его пациенты будут отвязаны.')">Отключить</button></form>
</td></tr>""" for d in doctors)
    table = (f"<table><tr><th>Врач</th><th>Пациентов</th><th>Последнее сообщение</th><th></th></tr>{rows}</table>"
             if doctors else "<p class=muted>Врачей пока нет.</p>")
    inv_rows = "".join(f"""<tr><td>{e(i['name'])}</td><td>{'до ' + fmt_date(i['expires_at']) if not expired(i) else '<span class=err>истекла</span>'}</td>
<td class=actions><form method=post action=/admin/invites/delete class=inline><input type=hidden name=h value="{e(i['code_hash'])}">
<button class=ghost>Удалить</button></form></td></tr>""" for i in invites)
    inv = (f"<h2>Ждут активации</h2><p class=muted>Ссылки хранятся только хешем, поэтому показать их снова нельзя. "
           f"Если ссылка потерялась, удалите приглашение и создайте новое.</p>"
           f"<table><tr><th>ФИО</th><th>Ссылка</th><th></th></tr>{inv_rows}</table>") if invites else ""
    body = f"""{links_block(created)}
<form method=post action=/admin/doctors/add class=card>
  <h2>Добавить врачей</h2>
  <p class=muted>По одному ФИО в строке. Для каждого появится одноразовая ссылка на 14 дней: врач открывает её в Telegram и становится врачом в боте.</p>
  <textarea name=names rows=4 required placeholder="Смирнова Елена Викторовна&#10;Петров Олег Иванович"></textarea>
  <button>Создать ссылки</button>
</form>
<h2>Врачи</h2>{table}{inv}"""
    respond(req.handler, 200, page("Врачи", body, req))


def doctors_add(req):
    names = [n.strip() for n in req.form.get("names", "").splitlines() if n.strip()][:100]
    created = [(n[:100], bot.create_doctor_invite(n[:100]), "Врач") for n in names]
    doctors_page(req, created)


def doctor_from(req):
    value = req.form.get("id", "")
    return bot.get_doctor(int(value)) if value.lstrip("-").isdigit() else None


def doctors_rename(req):
    doctor, name = doctor_from(req), req.form.get("name", "").strip()[:100]
    if doctor and name:
        db.run("UPDATE doctors SET name = :n WHERE tg_id = :t", n=name, t=doctor["tg_id"])
    back(req.handler, "/admin/doctors", "ФИО сохранено")


def doctors_newlink(req):
    doctor = doctor_from(req)
    if not doctor:
        return back(req.handler, "/admin/doctors", "Врач не найден")
    link = bot.new_doctor_link(doctor)
    bot.send(doctor["tg_id"], f"🔗 <b>Новая ссылка для пациентов</b> (выдал администратор)\n{link}\n\n"
             "<i>Старая больше не работает.</i>")
    doctors_page(req, [(doctor["name"], link, "Ссылка врача для пациентов")])


def doctors_remove(req):
    doctor = doctor_from(req)
    if not doctor:
        return back(req.handler, "/admin/doctors", "Врач не найден")
    count = bot.remove_doctor(doctor)
    back(req.handler, "/admin/doctors", f"Врач {doctor['name']} отключён, пациентов отвязано: {count}")


def invites_delete(req):
    db.run("DELETE FROM doctor_invites WHERE code_hash = :h AND used_at IS NULL", h=req.form.get("h", ""))
    back(req.handler, "/admin/doctors", "Приглашение удалено")


def patients_page(req, created=None):
    doctors = db.run("SELECT tg_id, name FROM doctors ORDER BY name")
    selected = req.query.get("doctor") or req.form.get("doctor_id") or ""
    flt = "WHERE p.doctor_id = :d" if selected.lstrip("-").isdigit() else ""
    params = {"d": int(selected)} if flt else {}
    patients = db.run(
        "SELECT p.*, d.name AS doctor_name, "
        "(SELECT max(last_at) FROM chat_sessions c WHERE c.patient_id = p.tg_id) AS last_at "
        f"FROM patients p LEFT JOIN doctors d ON d.tg_id = p.doctor_id {flt} "
        "ORDER BY d.name, p.full_name NULLS LAST", **params)
    invites = db.run(
        "SELECT i.code_hash, i.full_name, i.expires_at, d.name AS doctor_name FROM patient_invites i "
        "JOIN doctors d ON d.tg_id = i.doctor_id WHERE i.used_at IS NULL "
        + ("AND i.doctor_id = :d " if flt else "") + "ORDER BY i.created_at DESC", **params)

    def doctor_options(current):
        return "".join(f"<option value={d['tg_id']}{' selected' if str(d['tg_id']) == str(current) else ''}>"
                       f"{e(d['name'])}</option>" for d in doctors)

    rows = "".join(f"""<tr>
<td>{e(p['full_name']) or '<span class=muted>без имени</span>'}<div class=muted>ID {p['tg_id']}</div></td>
<td>{e(p['phone']) or '<span class=muted>—</span>'}</td>
<td>{STATES.get(p['state'], e(p['state']))}</td>
<td>{fmt_dt(p['last_at']) or '<span class=muted>—</span>'}</td>
<td class=actions>
  {f'''<form method=post action=/admin/patients/move class=inline><input type=hidden name=id value={p['tg_id']}>
    <select name=doctor_id>{doctor_options(p['doctor_id'])}</select><button class=ghost>Перевести</button></form>''' if p['state'] == 'active' else f"<span class=muted>{e(p['doctor_name'])}</span>"}
  {f'''<form method=post action=/admin/patients/approve class=inline><input type=hidden name=id value={p['tg_id']}>
    <button class=ghost>Подтвердить</button></form>''' if p['state'] == 'pending' else ''}
  <form method=post action=/admin/patients/remove class=inline><input type=hidden name=id value={p['tg_id']}>
    <button class=danger onclick="return confirm('Отключить пациента? Его данные будут удалены.')">Отключить</button></form>
</td></tr>""" for p in patients)
    table = (f"<table><tr><th>Пациент</th><th>Телефон</th><th>Статус</th><th>Последнее сообщение</th>"
             f"<th>Врач</th></tr>{rows}</table>" if patients else "<p class=muted>Пациентов нет.</p>")
    inv_rows = "".join(f"""<tr><td>{e(i['full_name'])}</td><td>{e(i['doctor_name'])}</td>
<td>{'до ' + fmt_date(i['expires_at']) if not expired(i) else '<span class=err>истекла</span>'}</td>
<td class=actions><form method=post action=/admin/patient_invites/delete class=inline>
<input type=hidden name=h value="{e(i['code_hash'])}"><button class=ghost>Удалить</button></form></td></tr>""" for i in invites)
    inv = (f"<h2>Приглашены, ещё не подключились</h2>"
           f"<table><tr><th>ФИО</th><th>Врач</th><th>Ссылка</th><th></th></tr>{inv_rows}</table>") if invites else ""
    add = (f"""<form method=post action=/admin/patients/invite class=card>
  <h2>Добавить пациентов к врачу</h2>
  <p class=muted>По одному ФИО в строке. Каждому — личная ссылка на 14 дней. Пациент открывает её, даёт согласие и
  делится телефоном, после этого сразу подключён к врачу без подтверждения. Без ссылки подключить нельзя: Telegram
  не даёт боту написать человеку первым.</p>
  <label>Врач <select name=doctor_id required>{doctor_options(selected)}</select></label>
  <textarea name=names rows=4 required placeholder="Иванов Иван Иванович&#10;Сидорова Анна Петровна"></textarea>
  <button>Создать ссылки</button>
</form>""" if doctors else "<p class=muted>Сначала добавьте врача.</p>")
    filt = f"""<form method=get class=filters><label>Врач <select name=doctor onchange="this.form.submit()">
<option value="">все</option>{doctor_options(selected)}</select></label></form>"""
    body = f"{links_block(created)}{add}<h2>Пациенты</h2>{filt}{table}{inv}"
    respond(req.handler, 200, page("Пациенты", body, req))


def patient_from(req):
    value = req.form.get("id", "")
    return bot.get_patient(int(value)) if value.lstrip("-").isdigit() else None


def patients_invite(req):
    value = req.form.get("doctor_id", "")
    doctor = bot.get_doctor(int(value)) if value.lstrip("-").isdigit() else None
    if not doctor:
        return back(req.handler, "/admin/patients", "Выберите врача")
    names = [n.strip()[:100] for n in req.form.get("names", "").splitlines() if n.strip()][:100]
    created = [(n, bot.create_patient_invite(doctor["tg_id"], n), f"Пациент · {doctor['name']}") for n in names]
    patients_page(req, created)


def patients_move(req):
    patient = patient_from(req)
    value = req.form.get("doctor_id", "")
    doctor = bot.get_doctor(int(value)) if value.lstrip("-").isdigit() else None
    if not patient or not doctor or patient["state"] != "active":
        return back(req.handler, "/admin/patients", "Перевести можно только подключённого пациента")
    if patient["doctor_id"] == doctor["tg_id"]:
        return back(req.handler, "/admin/patients", "Пациент уже у этого врача")
    bot.move_patient(patient, doctor)
    back(req.handler, "/admin/patients", f"Пациент передан врачу {doctor['name']}")


def patients_approve(req):
    patient = patient_from(req)
    if not patient or patient["state"] != "pending":
        return back(req.handler, "/admin/patients", "Заявка уже обработана")
    doctor = bot.get_doctor(patient["doctor_id"])
    bot.approve_patient(patient, doctor)
    bot.send(doctor["tg_id"], f"✅ Администратор подтвердил пациента <b>{bot.e(patient['full_name'])}</b>.")
    back(req.handler, "/admin/patients", "Пациент подключён")


def patients_remove(req):
    patient = patient_from(req)
    if patient:
        bot.remove_patient(patient)
    back(req.handler, "/admin/patients", "Пациент отключён, данные удалены")


def patient_invites_delete(req):
    db.run("DELETE FROM patient_invites WHERE code_hash = :h AND used_at IS NULL", h=req.form.get("h", ""))
    back(req.handler, "/admin/patients", "Приглашение удалено")


def sessions_page(req):
    doctors = db.run("SELECT tg_id, name FROM doctors ORDER BY name")
    # Без параметра — сегодня по Москве; пустой day — все 14 дней.
    day = req.query.get("day", "") if "day" in req.query else datetime.now(MSK).date().isoformat()
    doctor = req.query.get("doctor", "")
    rows = session_rows(day or "all", doctor if doctor.lstrip("-").isdigit() else None)
    options = "".join(f"<option value={d['tg_id']}{' selected' if str(d['tg_id']) == doctor else ''}>{e(d['name'])}</option>"
                      for d in doctors)
    body = f"""<p class=muted>Одна строка — пара «врач — пациент» за день (по Москве): когда было первое и последнее
сообщение и сколько сообщений в каждую сторону. Текст переписки не хранится. Журнал хранится 14 дней.</p>
<form method=get class=filters>
  <label>День <input type=date name=day value="{e(day)}"></label>
  <label>Врач <select name=doctor><option value="">все</option>{options}</select></label>
  <button class=ghost>Показать</button> <a href="/admin/sessions?day=&doctor={e(doctor)}">за 14 дней</a>
</form>{sessions_table(rows, show_day=not day)}"""
    respond(req.handler, 200, page("Журнал сессий", body, req))


def session_rows(day, doctor_id):
    where, params = [], {}
    if day is None:
        where.append("s.day = (now() AT TIME ZONE 'Europe/Moscow')::date")
    elif day != "all":
        try:
            params["day"] = date.fromisoformat(day)
            where.append("s.day = :day")
        except ValueError:
            pass
    if doctor_id:
        where.append("s.doctor_id = :d")
        params["d"] = int(doctor_id)
    return db.run(
        "SELECT s.*, d.name AS doctor_name, p.full_name AS patient_name FROM chat_sessions s "
        "LEFT JOIN doctors d ON d.tg_id = s.doctor_id LEFT JOIN patients p ON p.tg_id = s.patient_id "
        + ("WHERE " + " AND ".join(where) if where else "") + " ORDER BY s.last_at DESC LIMIT 500", **params)


def sessions_table(rows, show_day=False):
    if not rows:
        return "<p class=muted>Сессий нет.</p>"
    body = "".join(f"""<tr>{f"<td>{fmt_date(r['day'])}</td>" if show_day else ""}
<td>{e(r['doctor_name']) if r['doctor_name'] else f"<span class=muted>отключён · {r['doctor_id']}</span>"}</td>
<td>{e(r['patient_name']) if r['patient_name'] else f"<span class=muted>отключён · {r['patient_id']}</span>"}</td>
<td>{fmt_time(r['first_at'])} – {fmt_time(r['last_at'])}</td>
<td>{r['from_patient']}</td><td>{r['from_doctor']}</td></tr>""" for r in rows)
    return (f"<table><tr>{'<th>День</th>' if show_day else ''}<th>Врач</th><th>Пациент</th><th>Время (МСК)</th>"
            f"<th>От пациента</th><th>От врача</th></tr>{body}</table>")


# --- Оформление ------------------------------------------------------------


def links_block(created):
    """Только что созданные ссылки: показываем один раз, в базе остаётся только хеш."""
    if not created:
        return ""
    items = "".join(f"""<div class=link><div class=qr>{qr_svg(link)}</div><div>
<b>{e(name)}</b><div class=muted>{e(kind)}</div><input readonly value="{e(link)}" onclick="this.select()">
<button class=ghost type=button onclick="navigator.clipboard.writeText('{e(link)}');this.textContent='Скопировано'">Копировать</button>
</div></div>""" for name, link, kind in created)
    every = "\\n".join(f"{n}: {l}" for n, l, _ in created)
    return f"""<div class="card ok"><h2>Ссылки готовы</h2>
<p>Сохраните или отправьте их сейчас: <b>второй раз показать их нельзя</b>, в базе хранится только хеш.</p>
<button class=ghost type=button onclick="navigator.clipboard.writeText(`{e(every)}`);this.textContent='Скопировано'">Скопировать все</button>
{items}</div>"""


MSK = timezone(timedelta(hours=3))


def qr_svg(link):
    """QR-код рисуем на сервере: ссылка никуда не уходит."""
    return segno.make(link, error="m").svg_inline(scale=3, border=2)


def expired(invite):
    return invite["expires_at"] < datetime.now(timezone.utc)


def msk(dt):
    return dt.astimezone(MSK) if dt else None


def fmt_dt(dt):
    return msk(dt).strftime("%d.%m %H:%M") if dt else ""


def fmt_time(dt):
    return msk(dt).strftime("%H:%M") if dt else ""


def fmt_date(value):
    if not value:
        return ""
    return (msk(value) if hasattr(value, "hour") else value).strftime("%d.%m.%Y")


NAV = [("/admin", "Сводка"), ("/admin/doctors", "Врачи"), ("/admin/patients", "Пациенты"),
       ("/admin/sessions", "Журнал сессий")]


def page(title, body, req=None, nav=True):
    menu = ""
    if nav and req:
        links = "".join(f"<a href={href}{' class=on' if req.path == href else ''}>{label}</a>" for href, label in NAV)
        menu = (f"<nav>{links}<form method=post action=/admin/logout class=inline>"
                f"<button class=ghost>Выйти</button></form></nav>")
    msg = f"<div class='card ok'>{e(req.query['msg'])}</div>" if req and req.query.get("msg") else ""
    return f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>{e(title)} · Админка бота</title>
<style>{CSS}</style></head><body><header><b>🏥 Админка бота</b>{menu}</header>
<main><h1>{e(title)}</h1>{msg}{body}</main>
</body></html>"""


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#6b7280;--line:#e3e6eb;--accent:#2563eb;--danger:#dc2626;--ok:#ecfdf3}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--text:#e6e8eb;--muted:#9aa1ab;--line:#2d333c;--accent:#60a5fa;--danger:#f87171;--ok:#16301f}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text)}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;padding:12px 16px;background:var(--card);border-bottom:1px solid var(--line)}
nav{display:flex;flex-wrap:wrap;gap:4px;align-items:center}nav a{padding:6px 10px;border-radius:8px;color:var(--text);text-decoration:none}
nav a.on,nav a:hover{background:var(--bg)}main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:8px 0 16px}h2{font-size:17px;margin:24px 0 8px}.card h2{margin-top:0}
a{color:var(--accent)}.muted{color:var(--muted);font-size:13px}.err{color:var(--danger)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 16px}.card.ok{background:var(--ok)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;display:block;overflow-x:auto}table>tbody{display:table;width:100%}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:13px;color:var(--muted);font-weight:600;white-space:nowrap}
tr:last-child td{border-bottom:0}.actions{white-space:nowrap}form.inline{display:inline-flex;gap:4px;align-items:center;margin:2px 0}
input,select,textarea{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:6px 8px}
textarea{width:100%;display:block;margin:8px 0}label{margin-right:12px}
button,.btn{font:inherit;border:0;border-radius:8px;padding:7px 12px;background:var(--accent);color:#fff;cursor:pointer;text-decoration:none;display:inline-block}
button.ghost,.btn{background:transparent;color:var(--accent);border:1px solid var(--line)}button.danger{background:transparent;color:var(--danger);border:1px solid var(--line)}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;text-decoration:none;color:var(--text)}
.tile b{display:block;font-size:26px}.tile span{color:var(--muted);font-size:13px}
.link{display:flex;gap:14px;align-items:center;padding:12px 0;border-top:1px solid var(--line)}.link input{width:100%;max-width:420px;margin:6px 0}
.qr{background:#fff;padding:4px;border-radius:8px;flex:none;line-height:0}
@media (max-width:600px){.link{flex-direction:column;align-items:flex-start}}
"""

ROUTES = {
    ("GET", "/admin"): overview,
    ("POST", "/admin/logout"): logout,
    ("GET", "/admin/doctors"): doctors_page,
    ("POST", "/admin/doctors/add"): doctors_add,
    ("POST", "/admin/doctors/rename"): doctors_rename,
    ("POST", "/admin/doctors/newlink"): doctors_newlink,
    ("POST", "/admin/doctors/remove"): doctors_remove,
    ("POST", "/admin/invites/delete"): invites_delete,
    ("GET", "/admin/patients"): patients_page,
    ("POST", "/admin/patients/invite"): patients_invite,
    ("POST", "/admin/patients/move"): patients_move,
    ("POST", "/admin/patients/approve"): patients_approve,
    ("POST", "/admin/patients/remove"): patients_remove,
    ("POST", "/admin/patient_invites/delete"): patient_invites_delete,
    ("GET", "/admin/sessions"): sessions_page,
}
