"""Contrato HTTP de spec.md §11 y criterios 9 a 12 de §8, sobre una turnos.db temporal."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import config
import main
import service
from seed import uid
from tests.conftest import como
from tests.test_reglas import vie


def reservar(api, quien="c1", servicio="s5", inicio=None, **extra):
    return api.post("/api/turnos", json={"servicio_id": uid(servicio), "inicio": inicio or vie(10), **extra},
                    headers=como(quien))


def err(r):
    """§11.1: { "error": { "codigo", "mensaje" } } y nada más."""
    cuerpo = r.json()
    assert set(cuerpo) == {"error"} and set(cuerpo["error"]) == {"codigo", "mensaje"}
    return cuerpo["error"]["codigo"]


# ---------------------------------------------------------------- §11.4 POST /api/turnos
def test_reserva_201_location_y_representacion(api):
    r = reservar(api)
    assert r.status_code == 201, r.text
    t = r.json()
    assert r.headers["location"] == f"/api/turnos/{t['id']}"
    assert list(t) == list(service.CAMPOS_TURNO)                          # §11.2: exactamente 11 campos
    assert t["estado"] == "CONFIRMADO" and t["cliente_id"] == uid("c1") and t["profesional_id"] == uid("p3")
    assert t["inicio"] == vie(10) and t["fin"] == vie(10, 30) and t["inicio"].endswith("Z")


def test_criterio_1_y_3_via_api(api):
    assert reservar(api).status_code == 201
    r = reservar(api, "c3", inicio=vie(10, 15))
    assert r.status_code == 409 and err(r) == "SLOT_UNAVAILABLE"
    assert reservar(api, "c3", inicio=vie(10, 30)).status_code == 201     # contiguo


@pytest.mark.parametrize("campo", [{"fin": vie(11)}, {"profesional_id": uid("p3")}, {"estado": "CANCELADO"}])
def test_campos_del_servidor_rechazados(api, campo):
    r = reservar(api, **campo)
    assert r.status_code == 422 and err(r) == "VALIDATION_ERROR"


@pytest.mark.parametrize("cuerpo", [
    {"inicio": vie(10)},                                          # falta servicio_id
    {"servicio_id": uid("s5")},                                   # falta inicio
    {"servicio_id": "no-es-uuid", "inicio": vie(10)},
    {"servicio_id": uid("s5"), "inicio": "mañana"},
    {"servicio_id": uid("s5"), "inicio": "2026-11-06T10:00:00"},  # sin zona horaria
])
def test_cuerpo_mal_formado(api, cuerpo):
    r = api.post("/api/turnos", json=cuerpo, headers=como("c1"))
    assert r.status_code == 422 and err(r) == "VALIDATION_ERROR"


@pytest.mark.parametrize("quien,servicio,inicio,extra,http,codigo", [
    (None, "s5", vie(10), {}, 401, "UNAUTHENTICATED"),
    ("p3", "s5", vie(10), {}, 403, "FORBIDDEN"),
    ("c1", "s5", vie(10), {"cliente_id": uid("c3")}, 403, "FORBIDDEN"),
    ("c1", "s99", vie(10), {}, 404, "NOT_FOUND"),
    ("c2", "s5", vie(10), {}, 403, "EMAIL_NOT_VERIFIED"),
    ("c1", "s5", vie(13), {}, 422, "INVALID_TIME_RANGE"),          # fuera del horario laboral
    ("c1", "s5", vie(10, 10), {}, 422, "INVALID_TIME_RANGE"),      # fuera de grilla
    ("c3", "s1", vie(12), {}, 409, "SLOT_UNAVAILABLE"),            # bloqueo b1
    ("c1", "s5", vie(11, 30), {}, 409, "SLOT_UNAVAILABLE"),        # c1 ya tiene t13 11:00–12:00
])
def test_errores_de_reserva(api, quien, servicio, inicio, extra, http, codigo):
    r = api.post("/api/turnos", json={"servicio_id": uid(servicio), "inicio": inicio, **extra},
                 headers=como(quien) if quien else {})
    assert r.status_code == http and err(r) == codigo


def test_sin_actor_gana_sobre_cuerpo_invalido(api):
    r = api.post("/api/turnos", json={"fin": "x"})
    assert r.status_code == 401 and err(r) == "UNAUTHENTICATED"


def test_servicio_desactivado(api, db_path):
    with sqlite3.connect(db_path) as c:
        c.execute("UPDATE servicio SET activo = 0 WHERE id = ?", (uid("s5"),))
    r = reservar(api)
    assert r.status_code == 409 and err(r) == "SERVICE_UNAVAILABLE"


def test_criterio_6_contacto_invalido(api, db_path):
    with sqlite3.connect(db_path) as c:
        c.execute("UPDATE usuario SET telefono = '1155550101' WHERE id = ?", (uid("c1"),))
    r = reservar(api)
    assert r.status_code == 422 and err(r) == "INVALID_CONTACT"


def test_admin_reserva_en_nombre_de_un_cliente(api):
    r = reservar(api, "admin", inicio=vie(9), cliente_id=uid("c3"))
    assert r.status_code == 201 and r.json()["cliente_id"] == uid("c3")
    r = reservar(api, "admin", inicio=vie(11))
    assert r.status_code == 422 and err(r) == "VALIDATION_ERROR"


# ---------------------------------------------------------------- §11.3 GET /api/turnos
def test_listado_por_rol_y_orden(api):
    t = reservar(api).json()
    c1 = api.get("/api/turnos", headers=como("c1")).json()
    assert t["id"] in [x["id"] for x in c1] and all(x["cliente_id"] == uid("c1") for x in c1)
    assert [x["inicio"] for x in c1] == sorted(x["inicio"] for x in c1)
    assert t["id"] not in [x["id"] for x in api.get("/api/turnos", headers=como("c3")).json()]   # criterio 7
    p3 = api.get("/api/turnos", headers=como("p3")).json()
    assert t["id"] in [x["id"] for x in p3] and all(x["profesional_id"] == uid("p3") for x in p3)
    todos = api.get("/api/turnos", headers=como("admin")).json()
    assert len(todos) > len(p3)


def test_filtros(api):
    reservar(api)
    h = como("admin")
    assert {x["estado"] for x in api.get("/api/turnos", params={"estado": "CANCELADO"}, headers=h).json()} == {"CANCELADO"}
    assert {x["profesional_id"] for x in api.get("/api/turnos", params={"profesional_id": uid("p3")}, headers=h).json()} == {uid("p3")}
    rango = api.get("/api/turnos", params={"desde": vie(0), "hasta": vie(11)}, headers=h).json()
    assert rango and all(vie(0) <= x["inicio"] < vie(11) for x in rango)
    # Un profesional que filtra por otro profesional recibe una lista vacía.
    assert api.get("/api/turnos", params={"profesional_id": uid("p1")}, headers=como("p3")).json() == []


@pytest.mark.parametrize("params", [{"estado": "PENDIENTE"}, {"desde": "ayer"}, {"hasta": "2026-11-06T10:00:00"}])
def test_filtros_mal_formados(api, params):
    r = api.get("/api/turnos", params=params, headers=como("admin"))
    assert r.status_code == 422 and err(r) == "VALIDATION_ERROR"


def test_listar_sin_actor(api):
    r = api.get("/api/turnos")
    assert r.status_code == 401 and err(r) == "UNAUTHENTICATED"
    r = api.get("/api/turnos", headers={"X-Usuario-Id": "no-existe"})
    assert r.status_code == 401 and err(r) == "UNAUTHENTICATED"


# ---------------------------------------------------------------- §11.5 cancelar
def cancelar(api, turno_id, quien, motivo=None):
    return api.post(f"/api/turnos/{turno_id}/cancelar", json={"motivo": motivo} if motivo is not None else None,
                    headers=como(quien))


def test_criterio_4_y_11_cancelacion_via_api(api, reloj_fijo, db_path):
    t = reservar(api, inicio=vie(12)).json()
    inicio = datetime.fromisoformat(t["inicio"])

    reloj_fijo.valor = inicio - timedelta(hours=1, minutes=59)        # criterio 11 (INV-01)
    r = cancelar(api, t["id"], "c1")
    assert r.status_code == 409 and err(r) == "CANCELLATION_WINDOW_EXPIRED"
    with sqlite3.connect(db_path) as c:
        assert c.execute("SELECT estado FROM turno WHERE id = ?", (t["id"],)).fetchone()[0] == "CONFIRMADO"

    reloj_fijo.valor = inicio - timedelta(hours=3)                    # criterio 4
    r = cancelar(api, t["id"], "c1", "Me surgió algo")
    assert r.status_code == 200
    assert (r.json()["estado"], r.json()["cancelado_por_id"], r.json()["motivo_cancelacion"]) == \
        ("CANCELADO", uid("c1"), "Me surgió algo")
    r = cancelar(api, t["id"], "c1")
    assert r.status_code == 409 and err(r) == "INVALID_STATE_TRANSITION"   # dos veces
    assert reservar(api, "c3", inicio=vie(12)).status_code == 201          # el slot quedó libre


def test_cancelacion_profesional_y_admin(api, reloj_fijo):
    t = reservar(api, inicio=vie(12)).json()
    reloj_fijo.valor = datetime.fromisoformat(t["inicio"]) - timedelta(minutes=30)   # sin ventana mínima
    for quien in ("p3", "admin"):
        r = cancelar(api, t["id"], quien)
        assert r.status_code == 422 and err(r) == "REASON_REQUIRED"
        r = cancelar(api, t["id"], quien, "   ")
        assert r.status_code == 422 and err(r) == "REASON_REQUIRED"
    r = cancelar(api, t["id"], "p3", "El profesional no puede asistir.")
    assert r.status_code == 200 and r.json()["cancelado_por_id"] == uid("p3")


def test_cancelacion_errores(api, reloj_fijo):
    t = reservar(api).json()
    for quien in ("c3", "p1"):                                          # RBAC-02: ajeno → 404
        r = cancelar(api, t["id"], quien, "x")
        assert r.status_code == 404 and err(r) == "NOT_FOUND"
    r = cancelar(api, "no-existe", "admin", "x")
    assert r.status_code == 404 and err(r) == "NOT_FOUND"
    r = api.post(f"/api/turnos/{t['id']}/cancelar", json={"motivo": "x"})
    assert r.status_code == 401 and err(r) == "UNAUTHENTICATED"
    r = api.post(f"/api/turnos/{t['id']}/cancelar", json={"motivo_cancelacion": "x"}, headers=como("admin"))
    assert r.status_code == 422 and err(r) == "VALIDATION_ERROR"
    reloj_fijo.valor = datetime.fromisoformat(t["inicio"])               # ya iniciado
    r = cancelar(api, t["id"], "admin", "x")
    assert r.status_code == 409 and err(r) == "INVALID_STATE_TRANSITION"


def test_cancelacion_audita_en_la_misma_transaccion(api, db_path):
    t = reservar(api).json()
    cancelar(api, t["id"], "admin", "Imprevisto")
    with sqlite3.connect(db_path) as c:
        filas = c.execute("SELECT accion, rol, detalle FROM auditoria_evento WHERE entidad_id = ? ORDER BY rowid",
                          (t["id"],)).fetchall()
    assert filas == [("TURNO_CREADO", "CLIENTE", f"Asesoría nutricional · {vie(10)}"),
                     ("TURNO_CANCELADO", "ADMIN", "Motivo: Imprevisto")]


# ---------------------------------------------------------------- INV-03
def test_criterio_9_sobrevive_al_reinicio(db_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    with TestClient(main.app) as c:
        t = reservar(c).json()
        antes = c.get("/api/turnos", headers=como("admin")).json()
    with TestClient(main.app) as c:   # nuevo arranque: la semilla no pisa datos existentes
        despues = c.get("/api/turnos", headers=como("admin")).json()
    assert despues == antes and {"id": t["id"], "estado": "CONFIRMADO"}.items() <= \
        next(x for x in despues if x["id"] == t["id"]).items()


def test_criterio_10_atomicidad_via_api(api, db_path, monkeypatch):
    def falla(*_):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(service, "_auditar", falla)
    r = reservar(api)
    assert r.status_code == 503 and err(r) == "PERSISTENCE_UNAVAILABLE"
    with sqlite3.connect(db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM turno WHERE servicio_id = ? AND inicio = ?",
                         (uid("s5"), vie(10))).fetchone()[0] == 0


def test_base_inaccesible_503(api, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path)          # un directorio no es una base SQLite
    r = api.get("/api/turnos", headers=como("admin"))
    assert r.status_code == 503 and err(r) == "PERSISTENCE_UNAVAILABLE"


# ---------------------------------------------------------------- criterio 12 (INV-02 con concurrencia real)
def en_paralelo(api, pedidos):
    with ThreadPoolExecutor(len(pedidos)) as ex:
        return list(ex.map(lambda p: reservar(api, p[0], inicio=p[1], **p[2]), pedidos))


def test_criterio_12_slot_libre_un_solo_201(api, db_path):
    pedidos = [("c1", vie(9), {}), ("c3", vie(9), {}), ("admin", vie(9), {"cliente_id": uid("c3")})] * 3
    respuestas = en_paralelo(api, pedidos)
    codigos = sorted(r.status_code for r in respuestas)
    assert codigos == [201] + [409] * (len(pedidos) - 1)
    assert all(err(r) == "SLOT_UNAVAILABLE" for r in respuestas if r.status_code == 409)
    with sqlite3.connect(db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM turno WHERE inicio = ? AND estado = 'CONFIRMADO'",
                         (vie(9),)).fetchone()[0] == 1


def test_criterio_12_slot_ocupado_ambos_409(api):
    assert reservar(api, inicio=vie(10)).status_code == 201
    respuestas = en_paralelo(api, [("c3", vie(10, 15), {}), ("admin", vie(10, 15), {"cliente_id": uid("c3")})])
    assert [r.status_code for r in respuestas] == [409, 409]
    assert all(err(r) == "SLOT_UNAVAILABLE" for r in respuestas)


# ---------------------------------------------------------------- CORS (plan §3.2)
def preflight(api, origen):
    return api.options("/api/turnos", headers={
        "Origin": origen, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-usuario-id"})


@pytest.mark.parametrize("origen", ["http://localhost:5500", "http://127.0.0.1:5500"])
def test_cors_origen_permitido(api, origen):
    r = preflight(api, origen)
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == origen
    assert "x-usuario-id" in r.headers["access-control-allow-headers"].lower()
    r = api.get("/api/turnos", headers={"Origin": origen, **como("c1")})
    assert r.headers["access-control-allow-origin"] == origen


def test_cors_origen_no_listado(api):
    r = preflight(api, "http://evil.test")
    assert "access-control-allow-origin" not in r.headers
    r = api.get("/api/turnos", headers={"Origin": "http://evil.test", **como("c1")})
    assert "access-control-allow-origin" not in r.headers


def test_cors_metodo_no_permitido(api):
    r = api.options("/api/turnos", headers={"Origin": "http://localhost:5500", "Access-Control-Request-Method": "DELETE"})
    assert r.status_code == 400
