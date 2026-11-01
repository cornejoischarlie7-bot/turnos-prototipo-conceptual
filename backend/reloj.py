"""Reloj del backend (plan.md §3.2): ahora() en UTC, inyectable en los tests."""
from datetime import datetime, timezone


def ahora() -> datetime:
    return datetime.now(timezone.utc)
