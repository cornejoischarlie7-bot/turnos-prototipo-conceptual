"""Contrato v2.1.0 (spec.md §11.6 – §11.13) y criterios 8 y 13 a 20 de §8.

Semilla con AHORA = miércoles 2026-11-04 10:00 (UTC-3). Turnos relevantes:
lun 02: t1 (c1, s1, COMPLETADO), t2 (c3, s2, COMPLETADO) · mar 03: t3 (c1, s3, COMPLETADO), t4 (c3, s5, AUSENTE)
mié 04: t5 (c2, s1, COMPLETADO), t6 (c1, s4, CANCELADO por c1), t7 (c3, s6, CANCELADO por p3), r1 (c3, s6, 11:15)
jue 05: t8, t9 (s5), t10, t11 · vie 06: t12 (c3, s1, 10:00), t13 (c1, s4, 11:00) · lun 09: t14 (c2, s5, 09:00)
Bloqueos: b1 (p1, vie 06 12–13), b2 (p2, lun 09 10–19).
"""
import sqlite3
from datetime import datetime, timezone

import pytest

import service
from seed import uid
from tests.conftest import como
from tests.test_api import err, reservar
from tests.test_reglas import vie

UTC = timezone.utc


def art(dia, h, m=0):
    """2026-11-<dia> a las h:m en la zona de operación (UTC-3), en ISO UTC."""
    return datetime(2026, 11, dia, h + 3, m, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def contar(db_path, sql, args=()):
    with sqlite3.connect(db_path) as c:
        return c.execute(sql, args).fetchone()[0]


# ---------------------------------------------------------------- §11.6 servicios
def test_catalogo_publico(api):
    r = api.get("/api/servicios")                               # Anónimo
    assert r.status_code == 200
    servicios = r.json()
    assert len(servicios) == 6 and [s["nombre"] for s in servicios] == sorted(s["nombre"] for s in servicios)
    assert all(list(s) == list(service.CAMPOS_SERVICIO) for s in servicios)
    assert api.get("/api/servicios", headers=como("c1")).status_code == 200
    r = api.get("/api/servicios", headers={"X-Usuario-Id": "no-existe"})
    assert r.status_code == 401 and err(r) == "UNAUTHENTICATED"


# ---------------------------------------------------------------- criterio 13 (§11.9)
def test_criterio_13_disponibilidad_real(api):
    assert reservar(api, "c1", inicio=vie(10)).status_code == 201
    r = api.get("/api/disponibilidad", params={"servicio_id": uid("s5"), "fecha": "2026-11-06"}, headers=como("c3"))
    assert r.status_code == 200
    inicios = [s["inicio"] for s in r.json()]
    assert vie(10) not in inicios and vie(10, 15) not in inicios and vie(10, 30) in inicios and vie(9, 30) in inicios
    assert all(set(s) == {"inicio", "fin"} for s in r.json())   # no expone quién reservó (RBAC-04)
    assert inicios == sorted(inicios)


def test_disponibilidad_respeta_bloqueo_y_es_publica(api):
    r = api.get("/api/disponibilidad", params={"servicio_id": uid("s1"), "fecha": "2026-11-06"})
    inicios = [s["inicio"] for s in r.json()]
    assert r.status_code == 200 and vie(11, 30) in inicios
    assert vie(12) not in inicios and vie(12, 30) not in inicios and vie(11, 45) not in inicios   # b1 12–13
    assert vie(10) not in inicios                                                                   # t12


@pytest.mark.parametrize("params,http,codigo", [
    ({"servicio_id": uid("s99"), "fecha": "2026-11-06"}, 404, "NOT_FOUND"),
    ({"servicio_id": uid("s5"), "fecha": "06/11/2026"}, 422, "VALIDATION_ERROR"),
    ({"servicio_id": uid("s5")}, 422, "VALIDATION_ERROR"),
    ({"servicio_id": uid("s5"), "fecha": "2026-11-03"}, 422, "INVALID_TIME_RANGE"),
    ({"servicio_id": uid("s5"), "fecha": "2027-02-01"}, 422, "INVALID_TIME_RANGE"),
])
def test_disponibilidad_errores(api, params, http, codigo):
    r = api.get("/api/disponibilidad", params=params)
    assert r.status_code == http and err(r) == codigo


# ---------------------------------------------------------------- criterio 14 (§11.7)
def test_criterio_14_servicio_desactivado(api, db_path):
    r = api.post(f"/api/servicios/{uid('s5')}/desactivar", headers=como("p3"))
    assert r.status_code == 200 and r.json()["activo"] is False
    assert contar(db_path, "SELECT estado FROM turno WHERE id = ?", (uid("t9"),)) == "CONFIRMADO"   # RN-07
    r = reservar(api, "c1", inicio=vie(10))
    assert r.status_code == 409 and err(r) == "SERVICE_UNAVAILABLE"
    r = api.get("/api/disponibilidad", params={"servicio_id": uid("s5"), "fecha": "2026-11-06"})
    assert r.status_code == 409 and err(r) == "SERVICE_UNAVAILABLE"
    api.post(f"/api/servicios/{uid('s5')}/desactivar", headers=como("p3"))               # repetir: sin cambios
    assert contar(db_path, "SELECT COUNT(*) FROM auditoria_evento WHERE accion = 'SERVICIO_DESACTIVADO'") == 1
    r = api.post(f"/api/servicios/{uid('s5')}/activar", headers=como("admin"))
    assert r.status_code == 200 and r.json()["activo"] is True
    assert reservar(api, "c1", inicio=vie(10)).status_code == 201


@pytest.mark.parametrize("quien,servicio,http,codigo", [
    ("c1", "s5", 403, "FORBIDDEN"),
    ("p1", "s5", 404, "NOT_FOUND"),          # servicio de otro profesional
    ("admin", "s99", 404, "NOT_FOUND"),
])
def test_servicio_permisos(api, quien, servicio, http, codigo):
    r = api.post(f"/api/servicios/{uid(servicio)}/desactivar", headers=como(quien))
    assert r.status_code == http and err(r) == codigo


# ---------------------------------------------------------------- criterios 8 y 15 (§11.10)
def bloquear(api, quien="p3", prof="p3", inicio=None, fin=None, **extra):
    return api.post("/api/bloqueos", json={"profesional_id": uid(prof), "inicio": inicio or vie(10), "fin": fin or vie(11),
                                           **extra}, headers=como(quien))


def test_criterio_8_y_15_bloqueo_con_conflicto(api, db_path):
    t = reservar(api, "c1", inicio=vie(10)).json()
    antes = contar(db_path, "SELECT COUNT(*) FROM bloqueo")

    r = bloquear(api, motivo="Congreso")
    assert r.status_code == 409 and r.json()["error"]["codigo"] == "SLOT_UNAVAILABLE"
    assert [c["id"] for c in r.json()["error"]["conflictos"]] == [t["id"]]
    assert contar(db_path, "SELECT COUNT(*) FROM bloqueo") == antes                       # no se crea nada

    r = bloquear(api, confirmar_cancelacion=True)
    assert r.status_code == 422 and err(r) == "REASON_REQUIRED"

    r = bloquear(api, motivo="Congreso", confirmar_cancelacion=True)
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert r.headers["location"] == f"/api/bloqueos/{cuerpo['bloqueo']['id']}"
    assert list(cuerpo["bloqueo"]) == list(service.CAMPOS_BLOQUEO)
    [c] = cuerpo["turnos_cancelados"]
    assert (c["id"], c["estado"], c["cancelado_por_id"], c["motivo_cancelacion"]) == (t["id"], "CANCELADO", uid("p3"), "Congreso")
    with sqlite3.connect(db_path) as conn:
        acciones = [a for (a,) in conn.execute(
            "SELECT accion FROM auditoria_evento WHERE entidad_id IN (?, ?) ORDER BY rowid", (t["id"], cuerpo["bloqueo"]["id"]))]
    assert acciones == ["TURNO_CREADO", "TURNO_CANCELADO", "BLOQUEO_CREADO"]


def test_bloqueo_atomico_si_falla_la_auditoria(api, db_path, monkeypatch):
    t = reservar(api, "c1", inicio=vie(10)).json()
    original = service._auditar

    def falla_en_bloqueo(conn, actor, accion, *a):
        if accion == "BLOQUEO_CREADO":
            raise sqlite3.OperationalError("disk I/O error")
        return original(conn, actor, accion, *a)
    monkeypatch.setattr(service, "_auditar", falla_en_bloqueo)
    r = bloquear(api, motivo="x", confirmar_cancelacion=True)
    assert r.status_code == 503 and err(r) == "PERSISTENCE_UNAVAILABLE"
    assert contar(db_path, "SELECT estado FROM turno WHERE id = ?", (t["id"],)) == "CONFIRMADO"   # ROLLBACK
    assert contar(db_path, "SELECT COUNT(*) FROM bloqueo WHERE profesional_id = ?", (uid("p3"),)) == 0


def test_bloqueo_con_turno_en_curso(api, reloj_fijo):
    reservar(api, "c1", inicio=vie(10))
    reloj_fijo.valor = datetime.fromisoformat(vie(10, 10))
    r = bloquear(api, motivo="x", confirmar_cancelacion=True)
    assert r.status_code == 409 and err(r) == "INVALID_STATE_TRANSITION"


@pytest.mark.parametrize("quien,prof,inicio,fin,http,codigo", [
    ("c1", "p3", vie(10), vie(11), 403, "FORBIDDEN"),
    ("p3", "p1", vie(10), vie(11), 404, "NOT_FOUND"),
    ("admin", "c1", vie(10), vie(11), 404, "NOT_FOUND"),          # no es profesional
    ("p3", "p3", vie(11), vie(10), 422, "INVALID_TIME_RANGE"),
    ("p3", "p3", art(3, 9), art(3, 10), 422, "INVALID_TIME_RANGE"),  # ya terminó
    ("p3", "p3", "mañana", vie(10), 422, "VALIDATION_ERROR"),
])
def test_bloqueo_errores(api, quien, prof, inicio, fin, http, codigo):
    r = bloquear(api, quien, prof, inicio, fin)
    assert r.status_code == http and err(r) == codigo


def test_listar_bloqueos_por_rol(api):
    assert {b["profesional_id"] for b in api.get("/api/bloqueos", headers=como("admin")).json()} == {uid("p1"), uid("p2")}
    assert [b["id"] for b in api.get("/api/bloqueos", headers=como("p1")).json()] == [uid("b1")]
    assert api.get("/api/bloqueos", headers=como("p3")).json() == []
    assert [b["id"] for b in api.get("/api/bloqueos", params={"profesional_id": uid("p2")}, headers=como("admin")).json()] == [uid("b2")]
    r = api.get("/api/bloqueos", headers=como("c1"))
    assert r.status_code == 403 and err(r) == "FORBIDDEN"
    assert api.get("/api/bloqueos").status_code == 401


# ---------------------------------------------------------------- criterio 16
def test_criterio_16_bloqueo_y_reserva(api, db_path):
    b = bloquear(api, inicio=vie(9), fin=vie(10), motivo="Trámite").json()["bloqueo"]
    r = reservar(api, "c1", inicio=vie(9))
    assert r.status_code == 409 and err(r) == "SLOT_UNAVAILABLE"
    assert api.post(f"/api/bloqueos/{b['id']}/eliminar", headers=como("p1")).status_code == 404      # ajeno
    r = api.post(f"/api/bloqueos/{b['id']}/eliminar", headers=como("p3"))
    assert r.status_code == 200 and r.json() == b
    assert reservar(api, "c1", inicio=vie(9)).status_code == 201
    assert contar(db_path, "SELECT COUNT(*) FROM auditoria_evento WHERE accion = 'BLOQUEO_ELIMINADO'") == 1
    assert api.post(f"/api/bloqueos/{b['id']}/eliminar", headers=como("p3")).status_code == 404       # ya no existe


# ---------------------------------------------------------------- criterio 17 (§11.11)
def test_criterio_17_cierre_del_turno(api, reloj_fijo):
    t = reservar(api, "c1", inicio=vie(10)).json()
    r = api.post(f"/api/turnos/{t['id']}/completar", headers=como("p3"))
    assert r.status_code == 409 and err(r) == "INVALID_STATE_TRANSITION"        # todavía no empezó
    r = api.post(f"/api/turnos/{t['id']}/completar", headers=como("c1"))
    assert r.status_code == 403 and err(r) == "FORBIDDEN"
    r = api.post(f"/api/turnos/{t['id']}/completar", headers=como("p1"))
    assert r.status_code == 404 and err(r) == "NOT_FOUND"

    reloj_fijo.valor = datetime.fromisoformat(vie(10))
    r = api.post(f"/api/turnos/{t['id']}/ausente", headers=como("p3"))
    assert r.status_code == 409 and err(r) == "INVALID_STATE_TRANSITION"        # ausente exige ahora ≥ fin
    r = api.post(f"/api/turnos/{t['id']}/completar", headers=como("p3"))
    assert r.status_code == 200 and r.json()["estado"] == "COMPLETADO"
    r = api.post(f"/api/turnos/{t['id']}/completar", headers=como("admin"))
    assert r.status_code == 409 and err(r) == "INVALID_STATE_TRANSITION"        # estado terminal


def test_marcar_ausente(api, reloj_fijo):
    t = reservar(api, "c1", inicio=vie(10)).json()
    reloj_fijo.valor = datetime.fromisoformat(vie(10, 30))
    r = api.post(f"/api/turnos/{t['id']}/ausente", headers=como("admin"))
    assert r.status_code == 200 and r.json()["estado"] == "AUSENTE"
    r = api.post(f"/api/turnos/{t['id']}/cancelar", json={"motivo": "x"}, headers=como("admin"))
    assert r.status_code == 409 and err(r) == "INVALID_STATE_TRANSITION"


# ---------------------------------------------------------------- criterio 18 (§11.8, D-09)
def test_criterio_18_horario_laboral(api, db_path):
    franjas = [{"dia_semana": d, "hora_inicio": "09:00", "hora_fin": "13:00"} for d in (2, 3, 4, 5)]   # sin lunes
    r = api.post("/api/horarios", json={"profesional_id": uid("p3"), "franjas": franjas}, headers=como("p3"))
    assert r.status_code == 200 and [h["dia_semana"] for h in r.json()] == [2, 3, 4, 5]
    assert all(list(h) == list(service.CAMPOS_HORARIO) for h in r.json())
    assert contar(db_path, "SELECT estado FROM turno WHERE id = ?", (uid("t14"),)) == "CONFIRMADO"   # D-09
    r = api.get("/api/disponibilidad", params={"servicio_id": uid("s5"), "fecha": "2026-11-09"})
    assert r.status_code == 200 and r.json() == []
    r = reservar(api, "c1", inicio=art(9, 10))
    assert r.status_code == 422 and err(r) == "INVALID_TIME_RANGE"
    assert [h["dia_semana"] for h in api.get("/api/horarios", params={"profesional_id": uid("p3")}).json()] == [2, 3, 4, 5]
    assert contar(db_path, "SELECT COUNT(*) FROM auditoria_evento WHERE accion = 'HORARIO_ACTUALIZADO'") == 1


def test_horarios_publicos(api):
    r = api.get("/api/horarios")
    assert r.status_code == 200 and len(r.json()) == 25          # p1 y p2: 2 franjas × 5 días; p3: 1 × 5


@pytest.mark.parametrize("quien,prof,franjas,http,codigo", [
    ("c1", "p3", [], 403, "FORBIDDEN"),
    ("p3", "p1", [], 404, "NOT_FOUND"),
    ("p3", "p3", [(1, "09:00", "11:00"), (1, "10:00", "12:00")], 422, "INVALID_TIME_RANGE"),   # se solapan
    ("p3", "p3", [(1, "09:10", "11:00")], 422, "INVALID_TIME_RANGE"),                          # fuera de grilla
    ("p3", "p3", [(1, "12:00", "09:00")], 422, "INVALID_TIME_RANGE"),
    ("p3", "p3", [(1, "9:00", "11:00")], 422, "VALIDATION_ERROR"),
    ("p3", "p3", [(7, "09:00", "11:00")], 422, "VALIDATION_ERROR"),
])
def test_horario_errores(api, quien, prof, franjas, http, codigo):
    body = {"profesional_id": uid(prof), "franjas": [{"dia_semana": d, "hora_inicio": a, "hora_fin": b} for d, a, b in franjas]}
    r = api.post("/api/horarios", json=body, headers=como(quien))
    assert r.status_code == http and err(r) == codigo


def test_horario_vacio_y_franjas_contiguas(api):
    body = {"profesional_id": uid("p3"), "franjas": [{"dia_semana": 1, "hora_inicio": "09:00", "hora_fin": "12:00"},
                                                     {"dia_semana": 1, "hora_inicio": "12:00", "hora_fin": "24:00"}]}
    assert api.post("/api/horarios", json=body, headers=como("admin")).status_code == 200
    r = api.post("/api/horarios", json={"profesional_id": uid("p3"), "franjas": []}, headers=como("p3"))
    assert r.status_code == 200 and r.json() == []


# ---------------------------------------------------------------- criterio 19 (§11.12)
def test_criterio_19_auditoria(api):
    t = reservar(api, "c1", inicio=vie(10)).json()
    api.post(f"/api/turnos/{t['id']}/cancelar", headers=como("c1"))
    eventos = api.get("/api/auditoria", headers=como("admin")).json()
    assert all(list(e) == list(service.CAMPOS_AUDITORIA) for e in eventos)
    propios = [e for e in eventos if e["entidad_id"] == t["id"]]
    assert [(e["accion"], e["actor_id"], e["rol"]) for e in propios] == [
        ("TURNO_CANCELADO", uid("c1"), "CLIENTE"), ("TURNO_CREADO", uid("c1"), "CLIENTE")]   # más reciente primero
    assert t["id"] in {e["entidad_id"] for e in api.get("/api/auditoria", headers=como("p3")).json()}
    assert t["id"] not in {e["entidad_id"] for e in api.get("/api/auditoria", headers=como("p1")).json()}
    r = api.get("/api/auditoria", headers=como("c1"))
    assert r.status_code == 403 and err(r) == "FORBIDDEN"


# ---------------------------------------------------------------- criterio 20 (§11.13)
SEMANA = {"desde": art(2, 0), "hasta": art(7, 0)}   # lunes 02 a sábado 07


def test_criterio_20_metricas_globales(api):
    r = api.get("/api/metricas", params=SEMANA, headers=como("admin"))
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["total"] == 14
    assert m["por_estado"] == {"CONFIRMADO": 7, "COMPLETADO": 4, "CANCELADO": 2, "AUSENTE": 1}
    assert m["tasa_cancelacion"] == round(2 / 14, 4) and m["tasa_ausentismo"] == 0.2
    assert m["cancelaciones_por_rol"] == {"CLIENTE": 1, "PROFESIONAL": 1, "ADMIN": 0}
    assert m["turnos_por_dia"] == [{"fecha": "2026-11-02", "cantidad": 2}, {"fecha": "2026-11-03", "cantidad": 1},
                                   {"fecha": "2026-11-04", "cantidad": 2}, {"fecha": "2026-11-05", "cantidad": 4},
                                   {"fecha": "2026-11-06", "cantidad": 2}]
    assert sum(x["cantidad"] for x in m["top_servicios"]) == 12


def test_criterio_20_metricas_del_profesional(api):
    m = api.get("/api/metricas", params=SEMANA, headers=como("p3")).json()
    # p3: t4 AUSENTE, t7 CANCELADO, t9 y r1 CONFIRMADO; 9–13 de lunes a viernes, sin bloqueos.
    assert m["total"] == 4 and m["por_estado"]["AUSENTE"] == 1
    assert (m["minutos_reservados"], m["minutos_disponibles"]) == (105, 1200)
    assert m["ocupacion"] == round(105 / 1200, 4)
    assert api.get("/api/metricas", params={**SEMANA, "profesional_id": uid("p3")}, headers=como("admin")).json() == m
    r = api.get("/api/metricas", params={**SEMANA, "profesional_id": uid("p1")}, headers=como("p3"))
    assert r.status_code == 404 and err(r) == "NOT_FOUND"


def test_metricas_descuenta_bloqueos(api):
    m = api.get("/api/metricas", params={"desde": art(6, 0), "hasta": art(7, 0), "profesional_id": uid("p1")},
                headers=como("admin")).json()
    assert m["minutos_disponibles"] == 8 * 60 - 60      # p1 trabaja 8 h el viernes y bloqueó 12–13 (b1)


@pytest.mark.parametrize("quien,params,http,codigo", [
    ("c1", SEMANA, 403, "FORBIDDEN"),
    ("admin", {"desde": art(7, 0), "hasta": art(2, 0)}, 422, "INVALID_TIME_RANGE"),
    ("admin", {"desde": art(2, 0), "hasta": "2028-01-01T00:00:00Z"}, 422, "INVALID_TIME_RANGE"),
    ("admin", {"desde": "ayer", "hasta": art(2, 0)}, 422, "VALIDATION_ERROR"),
    ("admin", {"desde": art(2, 0)}, 422, "VALIDATION_ERROR"),
])
def test_metricas_errores(api, quien, params, http, codigo):
    r = api.get("/api/metricas", params=params, headers=como(quien))
    assert r.status_code == http and err(r) == codigo
