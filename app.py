"""Local time tracker: HTML forms, server-owned timestamps and SQLite."""

import hmac
import os
import re
import secrets
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException, SecurityError

from storage import get_db, init_db
from time_utils import format_duration, from_us, period_bounds, to_us


ROOT = Path(__file__).resolve().parent


def local_secret():
    """Keep sessions valid across restarts, without a secret in source control."""
    path = ROOT / "instance" / "secret.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        descriptor, temporary_path = tempfile.mkstemp(prefix=".secret-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as secret_file:
                secret_file.write(secrets.token_hex(32))
                secret_file.flush()
                os.fsync(secret_file.fileno())
            # Publish only a complete key. A hard link is atomic and, unlike
            # replace(), cannot overwrite another process's winning key.
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                pass
        finally:
            Path(temporary_path).unlink(missing_ok=True)
    try:
        key = path.read_text(encoding="ascii").strip()
    except UnicodeError:
        raise RuntimeError("Локальный secret.key повреждён. Восстановите его из резервной копии или переименуйте для создания нового ключа.") from None
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise RuntimeError("Локальный secret.key пуст или повреждён. Восстановите его из резервной копии или переименуйте для создания нового ключа.")
    return key


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=str(ROOT / "instance" / "timekeeper.sqlite3"),
        SECRET_KEY=os.environ.get("TIMEKEEPER_SECRET_KEY"),
        TIMEZONE=os.environ.get("TIMEKEEPER_TIMEZONE", "Asia/Dushanbe"),
        NOW_PROVIDER=lambda: datetime.now(timezone.utc),
        MAX_CONTENT_LENGTH=64 * 1024,
        MAX_FORM_PARTS=16,
        TRUSTED_HOSTS=["127.0.0.1", "localhost"],
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if config:
        app.config.update(config)
    if not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = local_secret()
    database_path = Path(app.config["DATABASE"])
    if not database_path.is_absolute():
        database_path = ROOT / database_path
    app.config["DATABASE"] = str(database_path.resolve())
    zone = ZoneInfo(app.config["TIMEZONE"])
    init_db(app)

    def now_us():
        return to_us(app.config["NOW_PROVIDER"]())

    def csrf_token():
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_urlsafe(32)
        return session["_csrf_token"]

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.filters["duration"] = format_duration
    app.jinja_env.filters["local_datetime"] = lambda value: from_us(value).astimezone(zone).strftime("%d.%m.%Y %H:%M:%S")
    app.jinja_env.filters["iso_utc"] = lambda value: from_us(value).isoformat()

    @app.before_request
    def protect_forms():
        if isinstance(request.routing_exception, SecurityError):
            # Reject an untrusted host before body parsing or CSRF errors can
            # replace the routing exception with a different error.
            raise request.routing_exception
        if request.method == "POST":
            expected = session.get("_csrf_token", "")
            received = request.form.get("csrf_token", "")
            if not expected or not hmac.compare_digest(expected.encode(), received.encode()):
                abort(400, description="Не удалось проверить форму. Обновите страницу и повторите действие.")

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def friendly_error(error):
        if isinstance(error, SecurityError):
            # Host validation can fail before Flask creates its URL adapter;
            # a template using url_for() cannot be rendered in that case.
            return app.response_class("Недопустимый заголовок Host. Откройте приложение через localhost или 127.0.0.1.", status=400, mimetype="text/plain")
        titles = {400: "Не удалось выполнить запрос", 404: "Запись не найдена", 405: "Действие недоступно", 413: "Слишком большой запрос"}
        messages = {404: "Такого сотрудника, таймера или страницы нет. Вернитесь на главную.", 405: "Используйте кнопки и формы на главной странице.", 413: "Уменьшите объём введённых данных и повторите действие."}
        return render_template("error.html", error_code=error.code, error_title=titles.get(error.code, "Ошибка запроса"), error_message=messages.get(error.code, error.description)), error.code

    def home_redirect():
        query = {}
        for field in ("start_date", "end_date"):
            if field in request.form:
                query[field] = request.form[field]
        if request.form.get("report_employee_id"):
            query["employee_id"] = request.form["report_employee_id"]
        return redirect(url_for("index", **query), code=303)

    def employee_by_id(value):
        if not re.fullmatch(r"[0-9]{1,18}", str(value)):
            abort(404)
        person = get_db().execute("SELECT id, name FROM employees WHERE id = ?", (int(value),)).fetchone()
        if person is None:
            abort(404)
        return person

    @app.get("/")
    def index():
        connection = get_db()
        # One read snapshot: concurrent additions cannot appear in entries
        # without also appearing in the employee totals built below.
        connection.execute("BEGIN")
        current = now_us()
        today = from_us(current).astimezone(zone).date().isoformat()
        start_date = request.args.get("start_date", today)
        end_date = request.args.get("end_date", today)
        selected = request.args.get("employee_id", "")
        selected_id = employee_by_id(selected)["id"] if selected else None
        employees = [dict(row) for row in connection.execute("SELECT id, name FROM employees ORDER BY id")]
        active = []
        for row in connection.execute("SELECT t.*, e.name AS employee_name FROM time_entries t JOIN employees e ON e.id = t.employee_id WHERE t.ended_at IS NULL ORDER BY t.started_at, t.id"):
            item = dict(row)
            item["elapsed_us"] = max(0, current - item["started_at"])
            active.append(item)

        totals = {person["id"]: {**person, "duration_us": 0, "entry_count": 0} for person in employees if selected_id is None or person["id"] == selected_id}
        entries = []
        report_error = None
        try:
            lower, upper = period_bounds(start_date, end_date, zone)
        except ValueError as error:
            report_error = str(error)
        else:
            sql = "SELECT t.*, e.name AS employee_name FROM time_entries t JOIN employees e ON e.id = t.employee_id WHERE t.ended_at IS NOT NULL AND t.ended_at >= ? AND t.started_at < ?"
            parameters = [lower, upper]
            if selected_id is not None:
                sql += " AND t.employee_id = ?"
                parameters.append(selected_id)
            sql += " ORDER BY t.started_at DESC, t.id DESC"
            for row in connection.execute(sql, parameters):
                item = dict(row)
                overlap = max(0, min(item["ended_at"], upper) - max(item["started_at"], lower))
                # Keep a genuine zero-second task inside the period, but not a
                # task that merely ends at the period's opening boundary.
                zero_inside = item["started_at"] == item["ended_at"] and lower <= item["started_at"] < upper
                if overlap == 0 and not zero_inside:
                    continue
                item["overlap_us"] = overlap
                entries.append(item)
                totals[item["employee_id"]]["duration_us"] += overlap
                totals[item["employee_id"]]["entry_count"] += 1
        connection.commit()
        return render_template(
            "index.html", employees=employees, active_entries=active,
            report_entries=entries, totals=list(totals.values()),
            total_us=sum(person["duration_us"] for person in totals.values()),
            report_count=len(entries), start_date=start_date, end_date=end_date,
            selected_employee_id=selected_id, report_error=report_error,
            timezone_name=app.config["TIMEZONE"], server_now_us=current, today=today,
        )

    @app.post("/employees")
    def add_employee():
        name = request.form.get("name", "").strip()
        if not name or len(name) > 100 or "\x00" in name:
            flash("Введите имя сотрудника: от 1 до 100 символов, без нулевых символов.", "error")
        else:
            connection = get_db()
            with connection:
                connection.execute("INSERT INTO employees (name) VALUES (?)", (name,))
            flash("Сотрудник добавлен.", "success")
        return home_redirect()

    @app.post("/timers/start")
    def start_timer():
        if not request.form.get("employee_id"):
            flash("Выберите сотрудника для запуска таймера.", "error")
            return home_redirect()
        person = employee_by_id(request.form["employee_id"])
        description = request.form.get("description", "").strip()
        if not description or len(description) > 1000 or "\x00" in description:
            flash("Введите описание задачи: от 1 до 1000 символов, без нулевых символов.", "error")
            return home_redirect()
        connection = get_db()
        try:
            # The unique partial index also handles simultaneous requests.
            with connection:
                connection.execute("INSERT INTO time_entries (employee_id, description, started_at) VALUES (?, ?, ?)", (person["id"], description, now_us()))
        except sqlite3.IntegrityError:
            flash("У сотрудника уже запущен таймер. Сначала остановите текущую задачу.", "error")
        else:
            flash("Таймер запущен.", "success")
        return home_redirect()

    @app.post("/timers/<int:entry_id>/stop")
    def stop_timer(entry_id):
        if entry_id > 9_223_372_036_854_775_807:
            abort(404)
        connection = get_db()
        with connection:
            # Conditional UPDATE makes repeated and concurrent stops harmless.
            result = connection.execute("UPDATE time_entries SET ended_at = MAX(started_at, ?) WHERE id = ? AND ended_at IS NULL", (now_us(), entry_id))
        if result.rowcount:
            flash("Таймер остановлен. Время добавлено в отчёт.", "success")
        elif connection.execute("SELECT id FROM time_entries WHERE id = ?", (entry_id,)).fetchone():
            flash("Эта задача уже остановлена. Сохранённое время не изменилось.", "success")
        else:
            abort(404)
        return home_redirect()

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
