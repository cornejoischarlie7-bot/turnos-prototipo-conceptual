"""Capa 2: backend HTTP (FastAPI, puerto 8000). Contrato: spec.md §11; plan.md §3.2.

Arranque (desde backend/):
    python -m uvicorn main:app --port 8000
Swagger UI: http://localhost:8000/docs
"""
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import config
import db
import schemas
import seed
import service
from domain import HTTP, ErrorDominio


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Crea el esquema de plan §3.3.1 si no existe y siembra si la base está vacía.
    seed.sembrar_si_vacia(config.DB_PATH)
    yield


app = FastAPI(
    title="Sistema de Turnos",
    version="2.0.0",
    description="Contrato de spec.md §11. El actor se identifica con el encabezado `X-Usuario-Id` (D-06).",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=config.CORS_METHODS,
                   allow_headers=config.CORS_HEADERS)


# ---------------------------------------------------------------- errores (§11.1)
def error(codigo: str, mensaje: str) -> JSONResponse:
    return JSONResponse({"error": {"codigo": codigo, "mensaje": mensaje}}, status_code=HTTP[codigo])


@app.exception_handler(ErrorDominio)
async def error_dominio(_: Request, e: ErrorDominio) -> JSONResponse:
    return error(e.codigo, e.mensaje)


@app.exception_handler(RequestValidationError)
async def error_validacion(_: Request, e: RequestValidationError) -> JSONResponse:
    detalle = "; ".join(f"{'.'.join(str(x) for x in err['loc'][1:]) or 'cuerpo'}: {err['msg']}" for err in e.errors())
    return error("VALIDATION_ERROR", f"Solicitud mal formada. {detalle}")


@app.exception_handler(sqlite3.Error)
async def error_persistencia(_: Request, e: sqlite3.Error) -> JSONResponse:
    # INV-03: la transacción ya hizo ROLLBACK; no quedan escrituras parciales.
    codigo = str(e).split(":")[0]
    if codigo in HTTP and codigo != "PERSISTENCE_UNAVAILABLE":   # RAISE(ABORT, '<código>') de un trigger
        return error(codigo, "La base de datos rechazó la operación.")
    return error("PERSISTENCE_UNAVAILABLE", "No se pudo acceder a la base de datos. Intentá de nuevo en unos segundos.")


# ---------------------------------------------------------------- dependencias
def conexion() -> Iterator[sqlite3.Connection]:
    # Una conexión por solicitud: SQLite serializa a los escritores con BEGIN IMMEDIATE.
    conn = db.connect(config.DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


Conexion = Annotated[sqlite3.Connection, Depends(conexion)]


def actor(conn: Conexion, x_usuario_id: Annotated[str | None, Header()] = None) -> sqlite3.Row:
    return service.actor_por_id(conn, x_usuario_id)


Actor = Annotated[sqlite3.Row, Depends(actor)]


# ---------------------------------------------------------------- §11.3 – §11.5
@app.get("/api/turnos", response_model=list[schemas.Turno], responses=schemas.errores(401, 422, 503), tags=["turnos"])
def listar_turnos(conn: Conexion, yo: Actor, estado: schemas.Estado | None = None, profesional_id: str | None = None,
                  desde: str | None = None, hasta: str | None = None) -> list[dict[str, Any]]:
    """§11.3: turnos visibles para el actor según su rol (RBAC-01), por `inicio` ascendente."""
    return service.listar_turnos(conn, yo, estado, profesional_id, desde, hasta)


@app.post("/api/turnos", status_code=201, response_model=schemas.Turno,
          responses=schemas.errores(401, 403, 404, 409, 422, 503), tags=["turnos"])
def crear_turno(body: schemas.ReservaIn, conn: Conexion, yo: Actor, response: Response) -> dict[str, Any]:
    """§11.4: 409 `SLOT_UNAVAILABLE` si choca (INV-02). El 201 sale después del COMMIT (INV-03)."""
    turno = service.crear_turno(conn, yo, str(body.servicio_id), body.inicio,
                                str(body.cliente_id) if body.cliente_id else None)
    response.headers["Location"] = f"/api/turnos/{turno['id']}"
    return turno


@app.post("/api/turnos/{turno_id}/cancelar", response_model=schemas.Turno,
          responses=schemas.errores(401, 404, 409, 422, 503), tags=["turnos"])
def cancelar_turno(turno_id: str, conn: Conexion, yo: Actor, body: schemas.CancelacionIn | None = None) -> dict[str, Any]:
    """§11.5: 409 `CANCELLATION_WINDOW_EXPIRED` si el cliente cancela a menos de 2 h (INV-01)."""
    return service.cancelar_turno(conn, yo, turno_id, body.motivo if body else None)
