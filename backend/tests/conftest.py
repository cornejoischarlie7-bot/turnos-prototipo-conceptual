from datetime import datetime, timezone

import pytest

import db
import seed

# Miércoles 2026-11-04 10:00 en la zona de operación (UTC-3).
# D0 de la semilla = jueves 05; D0+1 = viernes 06.
AHORA = datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc)


def sembrada(path):
    db.init_db(path)
    conn = db.connect(path)
    seed.sembrar(conn, AHORA)
    return conn


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "turnos.db"
    sembrada(path).close()
    return path


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()
