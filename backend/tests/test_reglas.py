"""Criterios de aceptación de spec.md §8 contra SQLite real (capa de servicio)."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import service
from domain import ErrorDominio, a_iso
from seed import uid
from tests.conftest import AHORA

UTC = timezone.utc


def vie(h, m=0):
    """Viernes 2026-11-06 a las h:m (UTC-3) en ISO UTC. La Lic. Ibáñez (p3) tiene ese día libre de 9 a 13."""
    return a_iso(datetime(2026, 11, 6, h + 3, m, tzinfo=UTC))


def actor(conn, clave):
    return service.actor_por_id(conn, uid(clave))


def reservar(conn, cliente, servicio, inicio, ahora=AHORA):
    return service.crear_turno(conn, actor(conn, cliente), uid(servicio), inicio, ahora)


def codigo(fn, *args, **kw):
    with pytest.raises(ErrorDominio) as e:
        fn(*args, **kw)
    return e.value.codigo


# ---------------------------------------------------------------- RN-02
def test_criterio_1_doble_reserva(conn):
    reservar(conn, "c1", "s5", vie(10))
    antes = conn.execute("SELECT COUNT(*) FROM turno").fetchone()[0]
    assert codigo(reservar, conn, "c3", "s5", vie(10, 15)) == "SLOT_UNAVAILABLE"
    assert conn.execute("SELECT COUNT(*) FROM turno").fetchone()[0] == antes   # no persiste nada


def test_criterio_3_contiguidad(conn):
    reservar(conn, "c1", "s5", vie(10))
    assert reservar(conn, "c3", "s5", vie(10, 30))["estado"] == "CONFIRMADO"


def test_cliente_no_solapa_consigo_mismo(conn):
    # c1 tiene s4 con p2 el viernes 11:00–12:00 (semilla t13).
    assert codigo(reservar, conn, "c1", "s5", vie(11, 30)) == "SLOT_UNAVAILABLE"


def test_fuera_de_horario_y_bloqueo(conn):
    assert codigo(reservar, conn, "c1", "s5", vie(13)) == "SLOT_UNAVAILABLE"        # p3 trabaja hasta 13
    assert codigo(reservar, conn, "c3", "s1", vie(12)) == "SLOT_UNAVAILABLE"        # p1 bloqueó 12–13 (b1)


def test_fin_lo_calcula_el_servidor(conn):
    t = reservar(conn, "c1", "s6", vie(9))
    assert t["fin"] == vie(9, 45) and t["profesional_id"] == uid("p3")


# ---------------------------------------------------------------- RN-01
def test_criterio_4_cancelacion_en_ventana(conn):
    t = reservar(conn, "c1", "s5", vie(12))
    inicio = datetime.fromisoformat(t["inicio"])
    r = service.cancelar_turno(conn, actor(conn, "c1"), t["id"], None, inicio - timedelta(hours=3))
    assert r["estado"] == "CANCELADO" and r["cancelado_por_id"] == uid("c1")
    reservar(conn, "c3", "s5", vie(12))   # el slot quedó libre


def test_criterio_5_cancelacion_fuera_de_ventana(conn):
    t = reservar(conn, "c1", "s5", vie(12))
    inicio = datetime.fromisoformat(t["inicio"])
    assert codigo(service.cancelar_turno, conn, actor(conn, "c1"), t["id"], None,
                  inicio - timedelta(hours=1, minutes=59)) == "CANCELLATION_WINDOW_EXPIRED"


def test_profesional_cancela_fuera_de_ventana_con_motivo(conn):
    t = reservar(conn, "c1", "s5", vie(12))
    casi = datetime.fromisoformat(t["inicio"]) - timedelta(minutes=30)
    assert codigo(service.cancelar_turno, conn, actor(conn, "p3"), t["id"], "", casi) == "REASON_REQUIRED"
    r = service.cancelar_turno(conn, actor(conn, "p3"), t["id"], "Viaje", casi)
    assert r["motivo_cancelacion"] == "Viaje"


def test_no_se_cancela_dos_veces(conn):
    t = reservar(conn, "c1", "s5", vie(12))
    service.cancelar_turno(conn, actor(conn, "admin"), t["id"], "x", AHORA)
    assert codigo(service.cancelar_turno, conn, actor(conn, "admin"), t["id"], "x", AHORA) == "INVALID_STATE_TRANSITION"


# ---------------------------------------------------------------- RN-03
def test_criterio_6_contacto_invalido(conn):
    conn.execute("UPDATE usuario SET telefono = '1155550101' WHERE id = ?", (uid("c1"),))
    with pytest.raises(ErrorDominio) as e:
        reservar(conn, "c1", "s5", vie(10))
    assert e.value.codigo == "INVALID_CONTACT" and "telefono" in e.value.detalles


def test_email_sin_verificar(conn):
    assert codigo(reservar, conn, "c2", "s5", vie(10)) == "EMAIL_NOT_VERIFIED"


# ---------------------------------------------------------------- RBAC
def test_criterio_7_aislamiento(conn):
    t = reservar(conn, "c1", "s5", vie(10))
    assert codigo(service.cancelar_turno, conn, actor(conn, "c3"), t["id"], None, AHORA) == "NOT_FOUND"
    assert codigo(service.cancelar_turno, conn, actor(conn, "p1"), t["id"], "x", AHORA) == "NOT_FOUND"
    assert all(x["cliente_id"] == uid("c3") for x in service.listar_turnos(conn, actor(conn, "c3")))


def test_solo_el_cliente_reserva(conn):
    assert codigo(reservar, conn, "admin", "s5", vie(10)) == "FORBIDDEN"
    assert codigo(service.crear_turno, conn, None, uid("s5"), vie(10), AHORA) == "FORBIDDEN"


def test_usuarios_visibles_rbac04(conn):
    assert [u["id"] for u in service.usuarios_visibles(conn, actor(conn, "c1"))] == [uid("c1")]
    vistos = {u["id"] for u in service.usuarios_visibles(conn, actor(conn, "p2"))}
    assert uid("c1") in vistos and uid("c3") not in vistos   # c3 no tiene turnos con p2 en la semilla
    assert all("password_hash" not in u for u in service.usuarios_visibles(conn, actor(conn, "admin")))


# ---------------------------------------------------------------- INV-03 + RN-08
def test_auditoria_en_la_misma_transaccion(conn):
    t = reservar(conn, "c1", "s5", vie(10))
    acciones = [a["accion"] for a in service.auditoria(conn, actor(conn, "admin")) if a["entidad_id"] == t["id"]]
    assert acciones == ["TURNO_CREADO"]


def test_reserva_rechazada_no_deja_auditoria(conn):
    antes = conn.execute("SELECT COUNT(*) FROM auditoria_evento").fetchone()[0]
    codigo(reservar, conn, "c2", "s5", vie(10))
    assert conn.execute("SELECT COUNT(*) FROM auditoria_evento").fetchone()[0] == antes


def test_persiste_tras_reabrir(db_path):
    import db
    c = db.connect(db_path)
    t = reservar(c, "c1", "s5", vie(10))
    c.close()
    c2 = db.connect(db_path)   # simula el reinicio del proceso
    assert c2.execute("SELECT estado FROM turno WHERE id = ?", (t["id"],)).fetchone()[0] == "CONFIRMADO"
    c2.close()


# ---------------------------------------------------------------- restricciones en la base (segunda barrera)
def test_trigger_solapamiento(conn):
    reservar(conn, "c1", "s5", vie(10))
    with pytest.raises(sqlite3.IntegrityError, match="SLOT_UNAVAILABLE"):
        conn.execute(
            "INSERT INTO turno (id, cliente_id, profesional_id, servicio_id, inicio, fin, estado, creado_en, actualizado_en)"
            " VALUES ('x', ?, ?, ?, ?, ?, 'CONFIRMADO', '', '')",
            (uid("c3"), uid("p3"), uid("s5"), vie(10, 15), vie(10, 45)))


def test_trigger_historial_inmutable(conn):
    t = reservar(conn, "c1", "s5", vie(10))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM turno WHERE id = ?", (t["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE turno SET inicio = ? WHERE id = ?", (vie(11), t["id"]))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM auditoria_evento")
    conn.execute("UPDATE turno SET estado = 'COMPLETADO' WHERE id = ?", (t["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE turno SET estado = 'CONFIRMADO' WHERE id = ?", (t["id"],))


# ---------------------------------------------------------------- F-03
def test_disponibilidad_excluye_ocupados(conn):
    reservar(conn, "c1", "s5", vie(10))
    libres = {s["inicio"] for s in service.disponibilidad(conn, uid("s5"), "2026-11-06", AHORA)}
    assert vie(9, 30) in libres and vie(10, 30) in libres
    assert vie(9, 45) not in libres and vie(10) not in libres and vie(10, 15) not in libres
