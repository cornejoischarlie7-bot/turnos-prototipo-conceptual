from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import config
import db
import main
import reloj
import seed

# Miércoles 2026-11-04 10:00 en la zona de operación (UTC-3).
# D0 de la semilla = jueves 05; D0+1 = viernes 06.
AHORA = datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc)


class Reloj:
    """Reloj controlable que reemplaza a reloj.ahora() (plan §3.2)."""

    def __init__(self, ahora: datetime):
        self.valor = ahora

    def __call__(self) -> datetime:
        return self.valor


@pytest.fixture(autouse=True)
def reloj_fijo(monkeypatch):
    r = Reloj(AHORA)
    monkeypatch.setattr(reloj, "ahora", r)
    return r


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "turnos.db"
    db.init_db(path)
    conn = db.connect(path)
    seed.sembrar(conn, AHORA)
    conn.close()
    return path


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()


@pytest.fixture
def api(db_path, monkeypatch):
    """Cliente HTTP sobre una turnos.db temporal ya sembrada con la hora AHORA."""
    monkeypatch.setattr(config, "DB_PATH", db_path)
    with TestClient(main.app) as c:
        yield c


def como(clave: str) -> dict:
    return {"X-Usuario-Id": seed.uid(clave)}
