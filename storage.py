"""SQLite connection per request and the small, additive database schema."""

import sqlite3
from pathlib import Path

from flask import current_app, g


SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 100)
);
CREATE TABLE IF NOT EXISTS time_entries (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    description TEXT NOT NULL CHECK (length(trim(description)) BETWEEN 1 AND 1000),
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_timer_per_employee
    ON time_entries(employee_id) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS entries_employee_start
    ON time_entries(employee_id, started_at);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"], timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db(app):
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    app.teardown_appcontext(close_db)
    with app.app_context():
        connection = get_db()
        connection.executescript(SCHEMA)
        connection.commit()
