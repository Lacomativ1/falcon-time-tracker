import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from urllib.parse import parse_qs, urlparse

import pytest
from flask import template_rendered

from app import create_app


def rows(app, sql, parameters=()):
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, parameters)]


def execute(app, sql, parameters=()):
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        return connection.execute(sql, parameters).lastrowid


def microseconds(value):
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def employee(app, name="Алексей Тестов"):
    return execute(app, "INSERT INTO employees (name) VALUES (?)", (name,))


def entry(app, employee_id, start, end=None, description="Тестовая задача"):
    return execute(
        app,
        "INSERT INTO time_entries (employee_id, description, started_at, ended_at) "
        "VALUES (?, ?, ?, ?)",
        (employee_id, description, microseconds(start), microseconds(end) if end else None),
    )


def report(app, client, **query):
    contexts = []

    def capture(sender, template, context, **extra):
        contexts.append(context)

    with template_rendered.connected_to(capture, app):
        response = client.get("/", query_string=query)
    assert response.status_code == 200
    return contexts[-1], response


def test_add_employee_trims_edges_and_allows_duplicate_names(app, post):
    for name in ("  Алексей Тестов  ", "Алексей Тестов"):
        assert post("/employees", name=name).status_code == 303
    saved = rows(app, "SELECT * FROM employees ORDER BY id")
    assert [person["name"] for person in saved] == ["Алексей Тестов"] * 2
    assert saved[0]["id"] != saved[1]["id"]


@pytest.mark.parametrize("name", ["", "   ", "а" * 101, "\x00", "Имя\x00скрытая часть"])
def test_invalid_employee_name_is_rejected(app, client, post, name):
    response = post("/employees", name=name)
    assert response.status_code == 303
    assert rows(app, "SELECT * FROM employees") == []
    assert client.get(response.location).status_code == 200


def test_maximum_name_length_is_accepted(app, post):
    post("/employees", name="Я" * 100)
    assert rows(app, "SELECT name FROM employees")[0]["name"] == "Я" * 100


def test_start_timer_uses_server_time_and_trims_description(app, post, clock):
    person = employee(app)
    response = post(
        "/timers/start",
        employee_id=person,
        description="  Проверка отчёта  ",
        started_at="0",
        duration="999999999",
    )
    assert response.status_code == 303
    saved = rows(app, "SELECT * FROM time_entries")[0]
    assert saved["employee_id"] == person
    assert saved["description"] == "Проверка отчёта"
    assert saved["started_at"] == microseconds(clock.value)
    assert saved["ended_at"] is None


@pytest.mark.parametrize("description", ["", " \n\t ", "я" * 1001, "\x00", "Задача\x00скрытая часть"])
def test_invalid_description_is_rejected(app, post, description):
    person = employee(app)
    assert post("/timers/start", employee_id=person, description=description).status_code == 303
    assert rows(app, "SELECT * FROM time_entries") == []


def test_maximum_description_length_is_accepted(app, post):
    person = employee(app)
    post("/timers/start", employee_id=person, description="я" * 1000)
    assert rows(app, "SELECT description FROM time_entries")[0]["description"] == "я" * 1000


def test_repeated_start_preserves_original_timer_and_reports_error(app, client, post, clock):
    person = employee(app)
    post("/timers/start", employee_id=person, description="Первая задача")
    original = rows(app, "SELECT * FROM time_entries")[0]
    clock.value += timedelta(minutes=20)
    response = post("/timers/start", employee_id=person, description="Вторая задача")
    assert response.status_code == 303
    assert rows(app, "SELECT * FROM time_entries") == [original]
    with client.session_transaction() as session:
        assert any(category == "error" for category, message in session.get("_flashes", []))


def test_different_employees_can_have_active_timers(app, post):
    people = [employee(app), employee(app, "Марина Примерова")]
    for person in people:
        post("/timers/start", employee_id=person, description="Разработка")
    active = rows(app, "SELECT employee_id FROM time_entries WHERE ended_at IS NULL")
    assert {row["employee_id"] for row in active} == set(people)


def test_stop_uses_server_end_time_and_report_has_duration(app, client, post, clock):
    person = employee(app)
    post("/timers/start", employee_id=person, description="Разработка")
    timer = rows(app, "SELECT * FROM time_entries")[0]
    clock.value += timedelta(hours=1, minutes=2, seconds=3)
    response = post(f"/timers/{timer['id']}/stop", ended_at="1", duration="1")
    assert response.status_code == 303
    saved = rows(app, "SELECT * FROM time_entries")[0]
    assert saved["ended_at"] == microseconds(clock.value)
    assert saved["ended_at"] - saved["started_at"] == 3_723_000_000
    context, _ = report(app, client, start_date="2026-10-28", end_date="2026-10-28")
    assert context["total_us"] == 3_723_000_000


def test_repeated_stop_does_not_change_saved_end(app, post, clock):
    person = employee(app)
    timer = entry(app, person, clock.value)
    clock.value += timedelta(seconds=10)
    post(f"/timers/{timer}/stop")
    original = rows(app, "SELECT * FROM time_entries")[0]
    clock.value += timedelta(hours=1)
    assert post(f"/timers/{timer}/stop").status_code == 303
    assert rows(app, "SELECT * FROM time_entries") == [original]


def test_new_timer_can_start_after_stop(app, post, clock):
    person = employee(app)
    timer = entry(app, person, clock.value)
    clock.value += timedelta(seconds=10)
    post(f"/timers/{timer}/stop")
    post("/timers/start", employee_id=person, description="Следующая задача")
    saved = rows(app, "SELECT * FROM time_entries ORDER BY id")
    assert len(saved) == 2
    assert saved[0]["ended_at"] is not None
    assert saved[1]["ended_at"] is None


def test_restart_preserves_employees_completed_entries_and_active_timer(app, clock):
    first_person = employee(app)
    second_person = employee(app, "Марина Примерова")
    entry(app, first_person, clock.value - timedelta(hours=2), clock.value - timedelta(hours=1))
    active_id = entry(app, second_person, clock.value - timedelta(minutes=10))
    clock.value += timedelta(minutes=5)
    restarted = create_app(
        {
            "TESTING": True,
            "DATABASE": app.config["DATABASE"],
            "SECRET_KEY": "test-only-not-a-real-secret",
            "NOW_PROVIDER": clock,
            "TIMEZONE": "Asia/Dushanbe",
        }
    )
    context, _ = report(restarted, restarted.test_client())
    assert len(context["employees"]) == 2
    assert context["report_count"] == 1
    assert context["total_us"] == 3_600_000_000
    assert len(context["active_entries"]) == 1
    assert context["active_entries"][0]["id"] == active_id
    assert context["active_entries"][0]["elapsed_us"] == 900_000_000


def test_report_filters_employee_and_date_and_ignores_active_entries(app, client, clock):
    first = employee(app)
    second = employee(app, "Марина Примерова")
    expected = entry(app, first, clock.value, clock.value + timedelta(minutes=30))
    entry(app, first, clock.value - timedelta(days=1), clock.value - timedelta(days=1, minutes=-10))
    entry(app, second, clock.value, clock.value + timedelta(hours=2))
    entry(app, first, clock.value + timedelta(hours=3))
    context, _ = report(
        app, client, employee_id=first, start_date="2026-10-28", end_date="2026-10-28"
    )
    assert [row["id"] for row in context["report_entries"]] == [expected]
    assert context["total_us"] == 1_800_000_000
    assert len(context["totals"]) == 1
    assert context["totals"][0]["id"] == first


def test_report_totals_are_per_employee_and_include_zero(app, client, clock):
    first = employee(app)
    second = employee(app, "Марина Примерова")
    third = employee(app, "Сергей Демо")
    entry(app, first, clock.value, clock.value + timedelta(minutes=10))
    entry(app, first, clock.value + timedelta(hours=1), clock.value + timedelta(hours=1, minutes=20))
    entry(app, second, clock.value, clock.value + timedelta(minutes=45))
    context, _ = report(app, client)
    totals = {row["id"]: row["duration_us"] for row in context["totals"]}
    assert totals == {first: 1_800_000_000, second: 2_700_000_000, third: 0}
    assert context["total_us"] == 4_500_000_000
    assert context["report_count"] == 3


@pytest.mark.parametrize("selected", [False, True])
def test_empty_report_has_zero_total_and_clear_message(app, client, selected):
    person = employee(app)
    query = {"start_date": "2000-01-01", "end_date": "2000-01-02"}
    if selected:
        query["employee_id"] = person
    context, response = report(app, client, **query)
    assert context["report_entries"] == []
    assert context["total_us"] == 0
    assert context["totals"][0]["duration_us"] == 0
    assert "За выбранный период записей нет" in response.get_data(as_text=True)


@pytest.mark.parametrize(
    "start_date,end_date",
    [("2026-10-29", "2026-10-28"), ("не дата", "2026-10-28"), ("2026-02-30", "2026-10-28")],
)
def test_invalid_date_range_has_friendly_error_and_no_report(app, client, start_date, end_date):
    context, response = report(app, client, start_date=start_date, end_date=end_date)
    assert context["report_error"]
    assert context["report_entries"] == []
    assert "Traceback" not in response.get_data(as_text=True)


@pytest.mark.parametrize("day,expected", [("2026-10-27", 600_000_000), ("2026-10-28", 1_200_000_000)])
def test_task_crossing_local_midnight_is_clipped_to_each_day(app, client, day, expected):
    person = employee(app)
    # Asia/Dushanbe is UTC+05: local 23:50 -> local 00:20 the following day.
    start = datetime(2026, 10, 27, 18, 50, tzinfo=timezone.utc)
    entry(app, person, start, start + timedelta(minutes=30))
    context, _ = report(app, client, start_date=day, end_date=day)
    assert context["total_us"] == expected
    assert context["report_entries"][0]["overlap_us"] == expected


def test_half_open_period_excludes_entries_touching_boundary(app, client):
    person = employee(app)
    lower = datetime(2026, 10, 27, 19, tzinfo=timezone.utc)
    upper = lower + timedelta(days=1)
    entry(app, person, lower - timedelta(minutes=10), lower)
    entry(app, person, upper, upper + timedelta(minutes=10))
    included = entry(app, person, lower, upper)
    context, _ = report(app, client, start_date="2026-10-28", end_date="2026-10-28")
    assert [row["id"] for row in context["report_entries"]] == [included]
    assert context["total_us"] == 86_400_000_000


def test_report_sums_microseconds_before_display_rounding(app, client, clock):
    person = employee(app)
    for offset in (0, 1):
        start = clock.value + timedelta(seconds=offset)
        entry(app, person, start, start + timedelta(microseconds=600_000))
    context, _ = report(app, client)
    assert context["total_us"] == 1_200_000
    assert app.jinja_env.filters["duration"](context["total_us"]) == "00:00:01"


def test_duration_can_display_more_than_24_hours(app):
    assert app.jinja_env.filters["duration"](90_061_000_000) == "25:01:01"


def test_unknown_employee_and_entry_return_friendly_404(client, post):
    responses = [
        post("/timers/start", employee_id=999999, description="Задача"),
        post("/timers/999999/stop"),
        client.get("/", query_string={"employee_id": 999999}),
    ]
    for response in responses:
        assert response.status_code == 404
        assert "Traceback" not in response.get_data(as_text=True)


def test_database_rejects_duplicate_active_timer_even_for_concurrent_writers(app, clock):
    person = employee(app)
    barrier = Barrier(2)

    def start_directly(number):
        with sqlite3.connect(app.config["DATABASE"], timeout=10) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            barrier.wait(timeout=10)
            try:
                connection.execute(
                    "INSERT INTO time_entries (employee_id, description, started_at) VALUES (?, ?, ?)",
                    (person, f"Задача {number}", microseconds(clock.value)),
                )
                connection.commit()
                return "saved"
            except sqlite3.IntegrityError:
                return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start_directly, [1, 2]))
    assert sorted(results) == ["duplicate", "saved"]
    assert len(rows(app, "SELECT * FROM time_entries WHERE ended_at IS NULL")) == 1


def test_database_rejects_negative_duration_and_unknown_employee(app, clock):
    person = employee(app)
    with pytest.raises(sqlite3.IntegrityError):
        entry(app, person, clock.value, clock.value - timedelta(seconds=1))
    with pytest.raises(sqlite3.IntegrityError):
        entry(app, 999999, clock.value)


def test_stop_clamps_backward_server_clock_to_avoid_negative_duration(app, post, clock):
    person = employee(app)
    timer = entry(app, person, clock.value)
    clock.value -= timedelta(seconds=1)
    assert post(f"/timers/{timer}/stop").status_code == 303
    saved = rows(app, "SELECT * FROM time_entries")[0]
    assert saved["ended_at"] == saved["started_at"]


def test_csrf_token_is_required_and_no_mutation_is_possible_via_get(app, client):
    client.get("/")
    assert client.post("/employees", data={"name": "Алексей Тестов"}).status_code == 400
    assert client.post("/employees", data={"name": "Алексей Тестов", "csrf_token": "wrong"}).status_code == 400
    assert client.get("/employees").status_code == 405
    assert client.get("/timers/start").status_code == 405
    assert client.get("/timers/1/stop").status_code == 405
    assert rows(app, "SELECT * FROM employees") == []


def test_user_text_is_escaped_and_sql_is_parameterized(app, client, post):
    dangerous_name = "<script>alert('x')</script>"
    post("/employees", name=dangerous_name)
    person = rows(app, "SELECT id FROM employees")[0]["id"]
    description = "<img src=x onerror=alert(1)> '; DROP TABLE employees; --"
    post("/timers/start", employee_id=person, description=description)
    response = client.get("/")
    html = response.get_data(as_text=True)
    assert dangerous_name not in html
    assert "<img src=x onerror=alert(1)>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img" in html
    assert rows(app, "SELECT name FROM employees")[0]["name"] == dangerous_name
    assert rows(app, "SELECT description FROM time_entries")[0]["description"] == description


def test_report_filters_survive_timer_action(app, post):
    person = employee(app)
    response = post(
        "/timers/start",
        employee_id=person,
        description="Задача",
        start_date="2026-09-01",
        end_date="2026-09-30",
        report_employee_id=person,
    )
    query = parse_qs(urlparse(response.location).query)
    assert query["start_date"] == ["2026-09-01"]
    assert query["end_date"] == ["2026-09-30"]
    assert query["employee_id"] == [str(person)]


def test_zero_duration_completed_task_is_visible_inside_period(app, client, clock):
    person = employee(app)
    saved = entry(app, person, clock.value, clock.value)
    context, _ = report(app, client)
    assert [item["id"] for item in context["report_entries"]] == [saved]
    assert context["total_us"] == 0
    assert context["report_count"] == 1


@pytest.mark.parametrize("value", ["abc", "-1", "99999999999999999999999999999"])
def test_malformed_employee_ids_do_not_cause_server_error(client, post, value):
    assert client.get("/", query_string={"employee_id": value}).status_code == 404
    assert post("/timers/start", employee_id=value, description="Задача").status_code == 404


def test_out_of_range_stop_id_is_friendly_404(post):
    assert post("/timers/99999999999999999999999999999/stop").status_code == 404


@pytest.mark.parametrize("start,end", [("0001-01-01", "2026-10-28"), ("2026-10-28", "9999-12-31"), ("", "")])
def test_extreme_or_empty_dates_do_not_crash(app, client, start, end):
    context, _ = report(app, client, start_date=start, end_date=end)
    assert context["report_error"]


def test_default_database_path_and_secret_survive_changed_working_directory(tmp_path, monkeypatch, clock):
    import app as app_module

    # Only the test's temporary project is touched; no real local secret is read.
    monkeypatch.setattr(app_module, "ROOT", tmp_path)
    settings = {"TESTING": True, "NOW_PROVIDER": clock}
    first = create_app(settings)
    person = employee(first)
    entry(first, person, clock.value)
    previous_secret = first.config["SECRET_KEY"]
    other_directory = tmp_path / "another-folder"
    other_directory.mkdir()
    monkeypatch.chdir(other_directory)
    restarted = create_app(settings)
    assert restarted.config["DATABASE"] == first.config["DATABASE"]
    assert restarted.config["SECRET_KEY"] == previous_secret
    assert len(rows(restarted, "SELECT * FROM employees")) == 1
    assert len(rows(restarted, "SELECT * FROM time_entries WHERE ended_at IS NULL")) == 1


def test_report_uses_consistent_snapshot_during_concurrent_addition(app, client, clock, monkeypatch):
    import app as app_module

    employee(app)
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        # WAL lets the simulated writer finish while a read snapshot is open.
        connection.execute("PRAGMA journal_mode=WAL")
    original_get_db = app_module.get_db
    added = False

    def get_db_with_concurrent_writer():
        connection = original_get_db()

        def during_read(action, table, column, database, trigger):
            nonlocal added
            if action == sqlite3.SQLITE_READ and table == "time_entries" and not added:
                added = True
                person = employee(app, "Новый Демо")
                entry(app, person, clock.value, clock.value + timedelta(seconds=10))
            return sqlite3.SQLITE_OK

        connection.set_authorizer(during_read)
        return connection

    monkeypatch.setattr(app_module, "get_db", get_db_with_concurrent_writer)
    context, _ = report(app, client)
    assert added
    assert context["report_entries"] == []
    assert len(context["employees"]) == 1
    # The next request sees the complete new state together.
    context, _ = report(app, client)
    assert len(context["employees"]) == 2
    assert context["total_us"] == 10_000_000
