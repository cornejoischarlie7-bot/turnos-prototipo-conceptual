"""Criterio de aceptación 2 (spec.md §8) e INV-03: dos procesos reservan el mismo slot
al mismo tiempo y exactamente uno lo obtiene. Prueba el bloqueo de BEGIN IMMEDIATE entre
procesos distintos (los hilos de un mismo proceso se prueban en test_api.py, criterio 12)."""
import multiprocessing as mp
from datetime import datetime, timezone

import pytest

from domain import a_iso
from tests.conftest import AHORA


def _reservar(db_path: str, cliente: str, inicio: str, barrera) -> str:
    # Corre en otro proceso: importa todo desde cero y abre su propia conexión.
    import db
    import reloj
    import service
    from domain import ErrorDominio
    from seed import uid
    reloj.ahora = lambda: AHORA
    conn = db.connect(db_path)
    try:
        yo = service.actor_por_id(conn, uid(cliente))
        barrera.wait()
        service.crear_turno(conn, yo, uid("s5"), inicio)
        return "OK"
    except ErrorDominio as e:
        return e.codigo
    finally:
        conn.close()


@pytest.mark.parametrize("hora", [9, 10, 12])
def test_criterio_2_dos_procesos_un_slot(db_path, hora):
    inicio = a_iso(datetime(2026, 11, 6, hora + 3, 0, tzinfo=timezone.utc))
    ctx = mp.get_context("spawn")
    with ctx.Manager() as m, ctx.Pool(2) as pool:
        barrera = m.Barrier(2)
        resultados = pool.starmap(_reservar, [(str(db_path), "c1", inicio, barrera), (str(db_path), "c3", inicio, barrera)])
    assert sorted(resultados) == ["OK", "SLOT_UNAVAILABLE"]
