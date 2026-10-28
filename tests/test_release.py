"""Release checks for local access boundaries and concurrent requests."""

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest

import app as app_module


def saved_rows(app, sql):
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        return connection.execute(sql).fetchall()


@pytest.mark.parametrize("host", ["localhost", "localhost:5000", "127.0.0.1", "127.0.0.1:5000"])
def test_loopback_hosts_are_accepted(client, host):
    assert client.get("/", base_url=f"http://{host}").status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example", "127.0.0.1.evil.example"])
def test_untrusted_host_cannot_read_page_or_static_files(client, host):
    for path in ("/", "/static/style.css"):
        response = client.get(path, base_url=f"http://{host}")
        assert response.status_code == 400
        assert "Traceback" not in response.get_data(as_text=True)


def test_untrusted_host_cannot_mutate_even_with_valid_csrf(app, client, post):
    with client.session_transaction() as session:
        token = session["_csrf_token"]
    signed_cookie = client.get_cookie("session").value
    # A manually supplied valid cookie makes this test independent of the
    # client's normal cookie-domain filtering: the Host boundary must reject it.
    attacker = app.test_client(use_cookies=False)
    response = attacker.post(
        "/employees",
        data={"name": "Алексей Тестов", "csrf_token": token},
        headers={"Host": "evil.example", "X-Forwarded-Host": "localhost", "Cookie": f"session={signed_cookie}"},
    )
    assert response.status_code == 400
    assert saved_rows(app, "SELECT * FROM employees") == []


@pytest.mark.parametrize("data", [{"name": "Пример"}, {"name": "Я" * 70_000, "csrf_token": "wrong"}])
def test_untrusted_host_is_rejected_before_form_or_csrf_errors(app, client, data):
    response = client.post("/employees", base_url="http://evil.example", data=data)
    assert response.status_code == 400
    assert response.mimetype == "text/plain"
    assert saved_rows(app, "SELECT * FROM employees") == []


@pytest.mark.parametrize("bad_key", ["", " \n", "not-a-generated-secret"])
def test_corrupt_local_secret_fails_closed_without_overwriting(tmp_path, monkeypatch, bad_key):
    monkeypatch.setattr(app_module, "ROOT", tmp_path)
    directory = tmp_path / "instance"
    directory.mkdir()
    secret_file = directory / "secret.key"
    secret_file.write_text(bad_key, encoding="ascii")
    with pytest.raises(RuntimeError):
        app_module.local_secret()
    assert secret_file.read_text(encoding="ascii") == bad_key


def test_secret_first_creation_is_atomic_for_a_second_start(tmp_path, monkeypatch):
    """Pause the first generator at the point that used to expose an empty file."""
    monkeypatch.setattr(app_module, "ROOT", tmp_path)
    first_generation = Event()
    release_first = Event()
    lock = Lock()
    calls = 0

    def controlled_token(size):
        nonlocal calls
        assert size == 32
        with lock:
            calls += 1
            number = calls
        if number == 1:
            first_generation.set()
            assert release_first.wait(timeout=10)
        return str(number) * 64

    monkeypatch.setattr(app_module.secrets, "token_hex", controlled_token)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(app_module.local_secret)
        assert first_generation.wait(timeout=10)
        try:
            second_value = app_module.local_secret()
            assert re.fullmatch(r"[0-9a-f]{64}", second_value)
        finally:
            release_first.set()
        assert first.result(timeout=10) == second_value
    assert (tmp_path / "instance" / "secret.key").read_text(encoding="ascii").strip() == second_value


def test_oversized_form_is_rejected_without_saving(app, post):
    assert post("/employees", name="Я" * 70_000).status_code == 413
    assert saved_rows(app, "SELECT * FROM employees") == []


def test_nonnumeric_and_extremely_long_timer_route_is_not_server_error(post):
    for identifier in ("not-an-id", "9" * 5000):
        response = post(f"/timers/{identifier}/stop")
        assert response.status_code == 404


def test_malformed_csrf_is_rejected_without_saving(app, client, post):
    response = client.post("/employees", data={"name": "Алексей Тестов", "csrf_token": "\x00Пример"})
    assert response.status_code == 400
    assert saved_rows(app, "SELECT * FROM employees") == []


def test_concurrent_http_starts_and_stops_keep_one_unchanged_task(app, clock):
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        employee_id = connection.execute("INSERT INTO employees(name) VALUES (?)", ("Алексей Тестов",)).lastrowid
    start_barrier = Barrier(2)

    def submit_start(number):
        with app.test_client() as client:
            client.get("/")
            with client.session_transaction() as session:
                token = session["_csrf_token"]
            start_barrier.wait(timeout=10)
            response = client.post("/timers/start", data={"employee_id": employee_id, "description": f"Задача {number}", "csrf_token": token})
            assert response.status_code == 303
            with client.session_transaction() as session:
                return [category for category, message in session.get("_flashes", [])]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit_start, (1, 2)))
    assert sorted(category for outcome in outcomes for category in outcome) == ["error", "success"]
    tasks = saved_rows(app, "SELECT id, started_at FROM time_entries")
    assert len(tasks) == 1
    task_id, initial_start = tasks[0]
    stop_barrier = Barrier(2)

    def submit_stop(number):
        with app.test_client() as client:
            client.get("/")
            with client.session_transaction() as session:
                token = session["_csrf_token"]
            stop_barrier.wait(timeout=10)
            return client.post(f"/timers/{task_id}/stop", data={"csrf_token": token}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(submit_stop, (1, 2))) == [303, 303]
    assert saved_rows(app, "SELECT id, started_at, ended_at FROM time_entries") == [(task_id, initial_start, initial_start)]
