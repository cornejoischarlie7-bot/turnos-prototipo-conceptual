"""Capa 3: persistencia en SQLite (INV-03). Esquema y triggers de plan.md §3.3.1 y §3.3.2.

Solo las seis tablas de spec.md §4.1, con exactamente sus campos. Las reglas que la
base puede reforzar (RN-02, RN-05, RN-06, RN-08, §4.3) se declaran como CHECK y triggers.
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
  nombre_completo  TEXT NOT NULL CHECK (length(nombre_completo) BETWEEN 2 AND 100),
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
  CHECK (inicio < fin),
  CHECK (cliente_id <> profesional_id)            -- spec §4.3
);
CREATE INDEX IF NOT EXISTS ix_turno_profesional_inicio ON turno (profesional_id, inicio);
CREATE INDEX IF NOT EXISTS ix_turno_cliente_inicio     ON turno (cliente_id, inicio);

CREATE TABLE IF NOT EXISTS horario_laboral (
  id             TEXT PRIMARY KEY,
  profesional_id TEXT NOT NULL REFERENCES usuario(id),
  dia_semana     INTEGER NOT NULL CHECK (dia_semana BETWEEN 0 AND 6),
  hora_inicio    TEXT NOT NULL,
  hora_fin       TEXT NOT NULL,
  CHECK (hora_inicio < hora_fin)
);

CREATE TABLE IF NOT EXISTS bloqueo (
  id             TEXT PRIMARY KEY,
  profesional_id TEXT NOT NULL REFERENCES usuario(id),
  inicio         TEXT NOT NULL,
  fin            TEXT NOT NULL,
  motivo         TEXT,
  CHECK (inicio < fin)
);

CREATE TABLE IF NOT EXISTS auditoria_evento (
  id          TEXT PRIMARY KEY,
  entidad     TEXT NOT NULL,
  entidad_id  TEXT NOT NULL,
  accion      TEXT NOT NULL,
  actor_id    TEXT NOT NULL REFERENCES usuario(id),
  rol         TEXT NOT NULL CHECK (rol IN ('CLIENTE', 'PROFESIONAL', 'ADMIN')),
  ocurrido_en TEXT NOT NULL,
  detalle     TEXT
);

-- INV-02 / RN-02: sin solapamiento por profesional ni por cliente, ni con bloqueos.
CREATE TRIGGER IF NOT EXISTS trg_turno_solapamiento
BEFORE INSERT ON turno
WHEN NEW.estado = 'CONFIRMADO'
BEGIN
  SELECT RAISE(ABORT, 'SLOT_UNAVAILABLE')
  WHERE EXISTS (
    SELECT 1 FROM turno t
    WHERE t.estado = 'CONFIRMADO'
      AND (t.profesional_id = NEW.profesional_id OR t.cliente_id = NEW.cliente_id)
      AND t.inicio < NEW.fin AND NEW.inicio < t.fin
  ) OR EXISTS (
    SELECT 1 FROM bloqueo b
    WHERE b.profesional_id = NEW.profesional_id
      AND b.inicio < NEW.fin AND NEW.inicio < b.fin
  );
END;

-- spec §4.3: el profesional del turno es el del servicio.
CREATE TRIGGER IF NOT EXISTS trg_turno_profesional_servicio
BEFORE INSERT ON turno
BEGIN
  SELECT RAISE(ABORT, 'Turno.profesional_id debe coincidir con Servicio.profesional_id')
  WHERE NEW.profesional_id <> (SELECT profesional_id FROM servicio WHERE id = NEW.servicio_id);
END;

-- RN-06: estados terminales y sin reprogramación.
CREATE TRIGGER IF NOT EXISTS trg_turno_transicion
BEFORE UPDATE ON turno
BEGIN
  SELECT RAISE(ABORT, 'INVALID_STATE_TRANSITION')
  WHERE OLD.estado <> 'CONFIRMADO'
     OR NEW.inicio <> OLD.inicio OR NEW.fin <> OLD.fin
     OR NEW.cliente_id <> OLD.cliente_id OR NEW.profesional_id <> OLD.profesional_id
     OR NEW.servicio_id <> OLD.servicio_id;
END;

-- RN-06: sin borrado físico de turnos.
CREATE TRIGGER IF NOT EXISTS trg_turno_sin_borrado BEFORE DELETE ON turno
BEGIN SELECT RAISE(ABORT, 'INVALID_STATE_TRANSITION: el historial es inmutable'); END;

-- RN-08: auditoría inmutable.
CREATE TRIGGER IF NOT EXISTS trg_auditoria_sin_update BEFORE UPDATE ON auditoria_evento
BEGIN SELECT RAISE(ABORT, 'La auditoría es inmutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_auditoria_sin_borrado BEFORE DELETE ON auditoria_evento
BEGIN SELECT RAISE(ABORT, 'La auditoría es inmutable'); END;
"""


def connect(path: Path) -> sqlite3.Connection:
    # isolation_level=None: las transacciones se abren a mano con BEGIN IMMEDIATE.
    conn = sqlite3.connect(path, isolation_level=None, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")      # SQLite las trae apagadas por defecto
    conn.execute("PRAGMA journal_mode = WAL")     # lecturas sin bloquear la escritura en curso
    conn.execute("PRAGMA synchronous = FULL")     # el COMMIT llega al disco antes de responder (INV-03)
    conn.execute("PRAGMA busy_timeout = 5000")    # una reserva concurrente espera en lugar de fallar
    return conn


def init_db(path: Path) -> None:
    conn = connect(path)
    try:
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
