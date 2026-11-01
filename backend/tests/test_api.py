"""Contrato HTTP de spec.md §11, con la hora real y una base temporal."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import config
import main
from seed import uid


@pytest.fixture
def cliente_http(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "turnos.db")
    with TestClient(main.app) as c:
        yield c


def como(clave):
    return {"X-Usuario-Id": uid(clave)}


def primer_slot(c, servicio="s5"):
    """Primer slot libre a partir de pasado mañana (lejos de la ventana de 2 h)."""
    for n in range(2, 15):
        fecha = (date.today() + timedelta(days=n)).isoformat()
        r = c.get("/api/disponibilidad", params={"servicio_id": uid(servicio), "fecha": fecha})
        assert r.status_code == 200
        if r.json():
            return r.json()[0]["inicio"]
    pytest.fail("No hay slots libres en dos semanas")


def test_dev_usuarios_y_catalogo(cliente_http):
    roles = {u["rol"] for u in cliente_http.get("/api/dev/usuarios").json()}
    assert roles == {"ADMIN", "PROFESIONAL", "CLIENTE"}
    anon = cliente_http.get("/api/catalogo").json()
    assert len(anon["servicios"]) == 6 and "bloqueos" not in anon
    assert len(cliente_http.get("/api/catalogo", headers=como("admin")).json()["bloqueos"]) == 2


def test_anonimo_no_lista_turnos(cliente_http):
    r = cliente_http.get("/api/turnos")
    assert r.status_code == 403 and r.json()["codigo"] == "FORBIDDEN" and "mensaje" in r.json()


def test_flujo_reservar_y_cancelar(cliente_http):
    inicio = primer_slot(cliente_http)
    body = {"servicio_id": uid("s5"), "inicio": inicio}

    r = cliente_http.post("/api/turnos", json=body, headers=como("c1"))
    assert r.status_code == 201, r.text
    turno = r.json()
    assert turno["estado"] == "CONFIRMADO" and turno["cliente_id"] == uid("c1")

    r = cliente_http.post("/api/turnos", json=body, headers=como("c3"))
    assert r.status_code == 409 and r.json()["codigo"] == "SLOT_UNAVAILABLE"

    assert turno["id"] in [t["id"] for t in cliente_http.get("/api/turnos", headers=como("c1")).json()]
    assert turno["id"] not in [t["id"] for t in cliente_http.get("/api/turnos", headers=como("c3")).json()]

    r = cliente_http.post(f"/api/turnos/{turno['id']}/cancelar", headers=como("c3"))
    assert r.status_code == 404                                  # RBAC-02
    r = cliente_http.post(f"/api/turnos/{turno['id']}/cancelar", json={}, headers=como("admin"))
    assert r.status_code == 422 and r.json()["codigo"] == "REASON_REQUIRED"
    r = cliente_http.post(f"/api/turnos/{turno['id']}/cancelar", headers=como("c1"))
    assert r.status_code == 200 and r.json()["estado"] == "CANCELADO"


def test_errores_de_validacion(cliente_http):
    r = cliente_http.post("/api/turnos", json={"servicio_id": uid("s5"), "inicio": "mañana"}, headers=como("c1"))
    assert r.status_code == 422 and r.json()["codigo"] == "INVALID_TIME_RANGE"
    r = cliente_http.post("/api/turnos", json={"servicio_id": "no-existe", "inicio": primer_slot(cliente_http)}, headers=como("c1"))
    assert r.status_code == 409 and r.json()["codigo"] == "SERVICE_UNAVAILABLE"
    r = cliente_http.post("/api/turnos", json={"servicio_id": uid("s5"), "inicio": primer_slot(cliente_http)}, headers=como("c2"))
    assert r.status_code == 403 and r.json()["codigo"] == "EMAIL_NOT_VERIFIED"


def test_inv03_sobrevive_al_reinicio(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "turnos.db")
    with TestClient(main.app) as c:
        inicio = primer_slot(c)
        turno_id = c.post("/api/turnos", json={"servicio_id": uid("s5"), "inicio": inicio}, headers=como("c1")).json()["id"]
    with TestClient(main.app) as c:   # nuevo arranque: la semilla no pisa datos existentes
        ids = [t["id"] for t in c.get("/api/turnos", headers=como("c1")).json()]
        assert turno_id in ids


def test_cors(cliente_http):
    r = cliente_http.options("/api/turnos", headers={
        "Origin": "http://localhost:5500", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-usuario-id,content-type"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] in ("*", "http://localhost:5500")
