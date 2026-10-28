from datetime import datetime, timezone

import pytest

from app import create_app


class Clock:
    """A controllable server clock: tests never wait for real time to pass."""

    def __init__(self):
        self.value = datetime(2026, 10, 28, 7, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(tmp_path, clock):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "SECRET_KEY": "test-only-not-a-real-secret",
            "NOW_PROVIDER": clock,
            "TIMEZONE": "Asia/Dushanbe",
        }
    )


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def post(client):
    """Submit the same CSRF-protected forms as a browser would."""
    client.get("/")

    def submit(path, **data):
        with client.session_transaction() as session:
            token = session["_csrf_token"]
        return client.post(path, data={"csrf_token": token, **data})

    return submit
