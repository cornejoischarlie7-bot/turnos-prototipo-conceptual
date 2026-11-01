"""Capa 3: persistencia en SQLite (INV-03).

Solo tablas y campos de spec.md §4.1. Las reglas que la base puede reforzar
(RN-02, RN-06, RN-08) se declaran como CHECK y triggers.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS usuario (
    id               TEXT PRIMARY KEY,
    email            TEXT NOT NULL UNIQUE,
    email_verificado INTEGER NOT NULL DEFAULT 0 CHECK (email_verificado IN (0, 1)),
    password_hash    TEXT NOT NULL,
    nombre_completo  TEXT NOT NULL,
    telefono         TEXT NOT NULL,
    rol              TEXT NOT NULL CHECK (rol IN ('CLIENTE', 'PROFESIONAL', 'ADMIN')),
    activo           INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0, 1)),
    creado_en        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS servicio (
    id                TEXT PRIMARY KEY,
    profesional_id    TEXT NOT NULL REFERENCES usuario(id),
    nombre            TEXT NOT NULL,
    descripcion       TEXT,
    duracion_min      INTEGER NOT NULL CHECK (duracion_min BETWEEN 15 AND 480 AND duracion_min % 15 = 0),
    precio_referencia NUMERIC NOT NULL CHECK (precio_referencia >= 0),
    activo            INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0, 1))
);

CREATE TABLE IF NOT EXISTS turno (
    id                 TEXT PRIMARY KEY,
    cliente_id         TEXT NOT NULL REFERENCES usuario(id),
    profesional_id     TEXT NOT NULL REFERENCES usuario(id),
    servicio_id        TEXT NOT NULL REFERENCES servicio(id),
    inicio             TEXT NOT NULL,
    fin                TEXT NOT NULL,
    estado             TEXT NOT NULL CHECK (estado IN ('CONFIRMADO', 'CANCELADO', 'COMPLETADO', 'AUSENTE')),
    motivo_cancelacion TEXT,
    cancelado_por_id   TEXT REFERENCES usuario(id),
    creado_en          TEXT NOT NULL,
    actualizado_en     TEXT NOT NULL,
    CHECK (fin > inicio),
    CHECK (cliente_id <> profesional_id)
);
CREATE INDEX IF NOT EXISTS ix_turno_profesional ON turno (profesional_id, estado, inicio);
CREATE INDEX IF NOT EXISTS ix_turno_cliente ON turno (cliente_id, estado, inicio);

CREATE TABLE IF NOT EXISTS horario_laboral (
    id             TEXT PRIMARY KEY,
    profesional_id TEXT NOT NULL REFERENCES usuario(id),
    dia_semana     INTEGER NOT NULL CHECK (dia_semana BETWEEN 0 AND 6),
    hora_inicio    TEXT NOT NULL,
    hora_fin       TEXT NOT NULL,
    CHECK (hora_fin > hora_inicio)
);

CREATE TABLE IF NOT EXISTS bloqueo (
    id             TEXT PRIMARY KEY,
    profesional_id TEXT NOT NULL REFERENCES usuario(id),
    inicio         TEXT NOT NULL,
    fin            TEXT NOT NULL,
    motivo         TEXT,
    CHECK (fin > inicio)
);

CREATE TABLE IF NOT EXISTS auditoria_evento (
    id          TEXT PRIMARY KEY,
    entidad     TEXT NOT NULL,
    entidad_id  TEXT NOT NULL,
    accion      TEXT NOT NULL,
    actor_id    TEXT NOT NULL REFERENCES usuario(id),
    rol         TEXT NOT NULL,
    ocurrido_en TEXT NOT NULL,
    detalle     TEXT
);

-- RN-02: dos turnos CONFIRMADO del mismo profesional no se solapan en [inicio, fin).
CREATE TRIGGER IF NOT EXISTS turno_no_solapa_profesional
BEFORE INSERT ON turno WHEN NEW.estado = 'CONFIRMADO'
BEGIN
    SELECT RAISE(ABORT, 'SLOT_UNAVAILABLE')
    WHERE EXISTS (SELECT 1 FROM turno t
                  WHERE t.profesional_id = NEW.profesional_id AND t.estado = 'CONFIRMADO'
                    AND t.inicio < NEW.fin AND NEW.inicio < t.fin);
END;

-- RN-02: un cliente no tiene dos turnos CONFIRMADO solapados.
CREATE TRIGGER IF NOT EXISTS turno_no_solapa_cliente
BEFORE INSERT ON turno WHEN NEW.estado = 'CONFIRMADO'
BEGIN
    SELECT RAISE(ABORT, 'SLOT_UNAVAILABLE')
    WHERE EXISTS (SELECT 1 FROM turno t
                  WHERE t.cliente_id = NEW.cliente_id AND t.estado = 'CONFIRMADO'
                    AND t.inicio < NEW.fin AND NEW.inicio < t.fin);
END;

-- RN-06: los estados terminales no admiten transiciones.
CREATE TRIGGER IF NOT EXISTS turno_estado_terminal
BEFORE UPDATE OF estado ON turno WHEN OLD.estado <> 'CONFIRMADO'
BEGIN
    SELECT RAISE(ABORT, 'INVALID_STATE_TRANSITION');
END;

-- RN-02 + RN-06: el intervalo y las partes de un turno no cambian después de creado
-- (así el control de solapamiento al insertar sigue siendo válido).
CREATE TRIGGER IF NOT EXISTS turno_datos_inmutables
BEFORE UPDATE OF cliente_id, profesional_id, servicio_id, inicio, fin ON turno
BEGIN
    SELECT RAISE(ABORT, 'Los datos de un turno son inmutables (RN-06)');
END;

-- RN-06: el historial no se borra.
CREATE TRIGGER IF NOT EXISTS turno_sin_borrado
BEFORE DELETE ON turno
BEGIN
    SELECT RAISE(ABORT, 'Los turnos no se eliminan físicamente (RN-06)');
END;

-- RN-08: la auditoría es inmutable.
CREATE TRIGGER IF NOT EXISTS auditoria_sin_update
BEFORE UPDATE ON auditoria_evento
BEGIN
    SELECT RAISE(ABORT, 'La auditoría es inmutable (RN-08)');
END;
CREATE TRIGGER IF NOT EXISTS auditoria_sin_borrado
BEFORE DELETE ON auditoria_evento
BEGIN
    SELECT RAISE(ABORT, 'La auditoría es inmutable (RN-08)');
END;
"""


def connect(path: Path) -> sqlite3.Connection:
    # isolation_level=None: las transacciones se abren a mano con BEGIN IMMEDIATE.
    conn = sqlite3.connect(path, isolation_level=None, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(path: Path) -> None:
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaccion(conn: sqlite3.Connection):
    """INV-03: BEGIN IMMEDIATE toma el lock de escritura antes de leer, así la
    verificación de RN-02 y la escritura quedan serializadas entre procesos."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
