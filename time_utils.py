"""Exact UTC arithmetic; calendar boundaries belong to the configured zone."""

import re
from datetime import date, datetime, time, timedelta, timezone


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def to_us(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Server clock must provide an aware datetime")
    delta = value.astimezone(timezone.utc) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def from_us(value):
    return EPOCH + timedelta(microseconds=value)


def format_duration(value):
    seconds = max(0, int(value)) // 1_000_000
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def period_bounds(start_text, end_text, zone):
    for value in (start_text, end_text):
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("Введите обе даты в формате ГГГГ-ММ-ДД.")
    try:
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
    except ValueError:
        raise ValueError("Укажите существующие календарные даты.") from None
    if start > end:
        raise ValueError("Дата начала не может быть позже даты окончания.")
    try:
        lower = datetime.combine(start, time.min, tzinfo=zone)
        upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone)
        return to_us(lower), to_us(upper)
    except (OverflowError, ValueError):
        raise ValueError("Выберите период с датами от 0002-01-01 до 9998-12-31.") from None
