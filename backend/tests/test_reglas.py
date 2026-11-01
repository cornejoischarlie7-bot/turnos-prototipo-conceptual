"""Criterios de aceptación de spec.md §8 contra SQLite real (capa de servicio) y
restricciones de la base (plan §3.3.2) probadas por SQL directo."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import service
from domain import ErrorDominio, a_iso
from seed import uid

UTC = timezone.utc


def vie(h, m=0):
    """Viernes 2026-11-06 a las h:m (UTC-3) en ISO UTC. La Lic. Ibáñez (p3) tiene ese día libre de 9 a 13."""
    return a_iso(datetime(2026, 11, 6, h + 3, m, tzinfo=UTC))


def actor(conn, clave):
    return service.actor_por_id(conn, uid(clave))


def reservar(conn, quien, servicio, inicio, cliente=None):
    return service.crear_turno(conn, actor(conn, quien), uid(servicio), inicio, uid(cliente) if cliente else None)


def codigo(fn, *args, **kw):
    with pytest.raises(ErrorDominio) as e:
        fn(*args, **kw)
    return e.value.codigo


def contar(conn, tabla):
    return conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]


# ---------------------------------------------------------------- INV-02 / RN-02
def test_criterio_1_doble_reserva(conn):
    reservar(conn, "c1", "s5", vie(10))
    antes = contar(conn, "turno")
    assert codigo(reservar, conn, "c3", "s5", vie(10, 15)) == "SLOT_UNAVAILABLE"
    assert contar(conn, "turno") == antes   # no persiste nada


def test_criterio_3_contiguidad(conn):
    reservar(conn, "c1", "s5", vie(10))
    assert reservar(conn, "c3", "s5", vie(10, 30))["estado"] == "CONFIRMADO"


def test_cliente_no_solapa_consigo_mismo(conn):
    # c1 tiene s4 con p2 el viernes 11:00–12:00 (semilla t13).
    assert codigo(reservar, conn, "c1", "s5", vie(11, 30)) == "SLOT_UNAVAILABLE"


def test_fuera_de_horario_y_bloqueo(conn):
    assert codigo(reservar, conn, "c1", "s5", vie(13)) == "INVALID_TIME_RANGE"      # p3 trabaja hasta 13 (§11.4)
    assert codigo(reservar, conn, "c3", "s1", vie(12)) == "SLOT_UNAVAILABLE"        # p1 bloqueó 12–13 (b1, RN-05)


def test_fin_lo_calcula_el_servidor(conn):
    t = reservar(conn, "c1", "s6", vie(9))
    assert t["fin"] == vie(9, 45) and t["profesional_id"] == uid("p3")


# ---------------------------------------------------------------- INV-01 / RN-01
def test_criterio_4_cancelacion_en_ventana(conn, reloj_fijo):
    t = reservar(conn, "c1", "s5", vie(12))
    reloj_fijo.valor = datetime.fromisoformat(t["inicio"]) - timedelta(hours=3)
    r = service.cancelar_turno(conn, actor(conn, "c1"), t["id"], None)
    assert r["estado"] == "CANCELADO" and r["cancelado_por_id"] == uid("c1")
    reservar(conn, "c3", "s5", vie(12))   # el slot quedó libre


def test_criterio_5_cancelacion_fuera_de_ventana(conn, reloj_fijo):
    t = reservar(conn, "c1", "s5", vie(12))
    reloj_fijo.valor = datetime.fromisoformat(t["inicio"]) - timedelta(hours=1, minutes=59)
    assert codigo(service.cancelar_turno, conn, actor(conn, "c1"), t["id"], None) == "CANCELLATION_WINDOW_EXPIRED"


def test_profesional_cancela_fuera_de_ventana_con_motivo(conn, reloj_fijo):
    t = reservar(conn, "c1", "s5", vie(12))
    reloj_fijo.valor = datetime.fromisoformat(t["inicio"]) - timedelta(minutes=30)
    assert codigo(service.cancelar_turno, conn, actor(conn, "p3"), t["id"], "") == "REASON_REQUIRED"
    r = service.cancelar_turno(conn, actor(conn, "p3"), t["id"], "Viaje")
    assert r["motivo_cancelacion"] == "Viaje" and r["cancelado_por_id"] == uid("p3")


def test_no_se_cancela_dos_veces(conn):
    t = reservar(conn, "c1", "s5", vie(12))
    service.cancelar_turno(conn, actor(conn, "admin"), t["id"], "x")
    assert codigo(service.cancelar_turno, conn, actor(conn, "admin"), t["id"], "x") == "INVALID_STATE_TRANSITION"


def test_turno_iniciado_no_se_cancela(conn, reloj_fijo):
    t = reservar(conn, "c1", "s5", vie(12))
    reloj_fijo.valor = datetime.fromisoformat(t["inicio"])
    assert codigo(service.cancelar_turno, conn, actor(conn, "admin"), t["id"], "x") == "INVALID_STATE_TRANSITION"


# ---------------------------------------------------------------- RN-03
def test_criterio_6_contacto_invalido(conn):
    conn.execute("UPDATE usuario SET telefono = '1155550101' WHERE id = ?", (uid("c1"),))
    with pytest.raises(ErrorDominio) as e:
        reservar(conn, "c1", "s5", vie(10))
    assert e.value.codigo == "INVALID_CONTACT" and "telefono" in e.value.detalles


def test_email_sin_verificar(conn):
    assert codigo(reservar, conn, "c2", "s5", vie(10)) == "EMAIL_NOT_VERIFIED"


# ---------------------------------------------------------------- RBAC (§2.2, §11.4)
def test_criterio_7_aislamiento(conn):
    t = reservar(conn, "c1", "s5", vie(10))
    assert codigo(service.cancelar_turno, conn, actor(conn, "c3"), t["id"], None) == "NOT_FOUND"
    assert codigo(service.cancelar_turno, conn, actor(conn, "p1"), t["id"], "x") == "NOT_FOUND"
    assert all(x["cliente_id"] == uid("c3") for x in service.listar_turnos(conn, actor(conn, "c3")))


def test_quien_puede_reservar(conn):
    assert codigo(reservar, conn, "p3", "s5", vie(10)) == "FORBIDDEN"                       # profesional
    assert codigo(reservar, conn, "c1", "s5", vie(10), cliente="c3") == "FORBIDDEN"         # cliente ajeno
    assert codigo(reservar, conn, "admin", "s5", vie(10)) == "VALIDATION_ERROR"             # admin sin cliente_id
    assert codigo(reservar, conn, "admin", "s5", vie(10), cliente="p1") == "NOT_FOUND"      # no es cliente
    t = reservar(conn, "admin", "s5", vie(9), cliente="c3")                                 # en nombre de c3
    assert t["cliente_id"] == uid("c3")
    fila = conn.execute("SELECT actor_id, rol FROM auditoria_evento WHERE entidad_id = ?", (t["id"],)).fetchone()
    assert (fila["actor_id"], fila["rol"]) == (uid("admin"), "ADMIN")


def test_actor_desconocido(conn):
    assert codigo(service.actor_por_id, conn, None) == "UNAUTHENTICATED"
    assert codigo(service.actor_por_id, conn, "no-existe") == "UNAUTHENTICATED"
    conn.execute("UPDATE usuario SET activo = 0 WHERE id = ?", (uid("c1"),))
    assert codigo(service.actor_por_id, conn, uid("c1")) == "UNAUTHENTICATED"


def test_servicio_inexistente_y_desactivado(conn):
    assert codigo(service.crear_turno, conn, actor(conn, "c1"), uid("s99"), vie(10)) == "NOT_FOUND"
    conn.execute("UPDATE servicio SET activo = 0 WHERE id = ?", (uid("s5"),))
    assert codigo(reservar, conn, "c1", "s5", vie(10)) == "SERVICE_UNAVAILABLE"


# ---------------------------------------------------------------- INV-03 + RN-08
def test_auditoria_en_la_misma_transaccion(conn):
    t = reservar(conn, "c1", "s5", vie(10))
    acciones = [r["accion"] for r in conn.execute("SELECT accion FROM auditoria_evento WHERE entidad_id = ?", (t["id"],))]
    assert acciones == ["TURNO_CREADO"]


def test_reserva_rechazada_no_deja_auditoria(conn):
    antes = contar(conn, "auditoria_evento")
    codigo(reservar, conn, "c2", "s5", vie(10))
    assert contar(conn, "auditoria_evento") == antes


def test_criterio_10_falla_la_auditoria(conn, monkeypatch):
    def falla(*_):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(service, "_auditar", falla)
    antes = contar(conn, "turno")
    with pytest.raises(sqlite3.OperationalError):
        reservar(conn, "c1", "s5", vie(10))
    assert contar(conn, "turno") == antes   # ROLLBACK: el turno tampoco quedó


def test_persiste_tras_reabrir(db_path):
    import db
    c = db.connect(db_path)
    t = reservar(c, "c1", "s5", vie(10))
    c.close()
    c2 = db.connect(db_path)   # simula el reinicio del proceso
    assert c2.execute("SELECT estado FROM turno WHERE id = ?", (t["id"],)).fetchone()[0] == "CONFIRMADO"
    c2.close()


# ---------------------------------------------------------------- triggers (plan §3.3.2), saltando el backend
def insertar(conn, id_, cliente, servicio, prof, inicio, fin, estado="CONFIRMADO"):
    conn.execute(
        "INSERT INTO turno (id, cliente_id, profesional_id, servicio_id, inicio, fin, estado, creado_en, actualizado_en)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, '2026-11-04T13:00:00Z', '2026-11-04T13:00:00Z')",
        (id_, uid(cliente), uid(prof), uid(servicio), inicio, fin, estado))


def test_trigger_solapamiento_profesional(conn):
    reservar(conn, "c1", "s5", vie(10))
    with pytest.raises(sqlite3.IntegrityError, match="SLOT_UNAVAILABLE"):
        insertar(conn, "x", "c3", "s5", "p3", vie(10, 15), vie(10, 45))


def test_trigger_solapamiento_cliente(conn):
    reservar(conn, "c1", "s5", vie(10))
    with pytest.raises(sqlite3.IntegrityError, match="SLOT_UNAVAILABLE"):
        insertar(conn, "x", "c1", "s3", "p2", vie(10), vie(10, 45))


def test_trigger_bloqueo(conn):
    with pytest.raises(sqlite3.IntegrityError, match="SLOT_UNAVAILABLE"):
        insertar(conn, "x", "c3", "s1", "p1", vie(12, 30), vie(13))    # b1: p1 12–13


def test_trigger_contiguo_y_cancelado_no_bloquean(conn):
    t = reservar(conn, "c1", "s5", vie(10))
    insertar(conn, "x", "c3", "s5", "p3", vie(10, 30), vie(11))       # contiguo
    service.cancelar_turno(conn, actor(conn, "admin"), t["id"], "x")
    insertar(conn, "y", "c2", "s5", "p3", vie(10), vie(10, 30))       # el slot quedó libre


def test_trigger_profesional_del_servicio(conn):
    with pytest.raises(sqlite3.IntegrityError, match="profesional_id"):
        insertar(conn, "x", "c1", "s5", "p1", vie(10), vie(10, 30))    # s5 es de p3


def test_trigger_historial_inmutable(conn):
    t = reservar(conn, "c1", "s5", vie(10))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM turno WHERE id = ?", (t["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="INVALID_STATE_TRANSITION"):
        conn.execute("UPDATE turno SET inicio = ? WHERE id = ?", (vie(11), t["id"]))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM auditoria_evento")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE auditoria_evento SET detalle = 'x'")
    conn.execute("UPDATE turno SET estado = 'CANCELADO' WHERE id = ?", (t["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="INVALID_STATE_TRANSITION"):
        conn.execute("UPDATE turno SET estado = 'CONFIRMADO' WHERE id = ?", (t["id"],))


def test_esquema_solo_tablas_de_la_spec(conn):
    tablas = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tablas == {"usuario", "servicio", "turno", "horario_laboral", "bloqueo", "auditoria_evento"}
    campos = [r[1] for r in conn.execute("PRAGMA table_info(turno)")]
    assert campos == list(service.CAMPOS_TURNO)


def test_pragmas(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2      # FULL
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
