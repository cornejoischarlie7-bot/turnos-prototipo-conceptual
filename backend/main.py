"""Capa 2: backend HTTP (FastAPI, puerto 8000). Contrato: spec.md §11.

Arranque (desde backend/):
    .venv/Scripts/python -m uvicorn main:app --port 8000 --reload
Swagger UI: http://localhost:8000/docs
"""
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import config
import db
import schemas
import seed
import service
from domain import ErrorDominio


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Crea el esquema de spec §4.1 si no existe y siembra si la base está vacía.
    seed.sembrar_si_vacia(config.DB_PATH)
    yield


app = FastAPI(
    title="Sistema de Turnos",
    version="0.2.1",
    description="Contrato de spec.md §11. La identidad de desarrollo se envía en el header `X-Usuario-Id` "
                "(ids en `GET /api/dev/usuarios`).",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["GET", "POST"], allow_headers=["*"])


@app.exception_handler(ErrorDominio)
async def error_dominio(_: Request, e: ErrorDominio) -> JSONResponse:
    cuerpo = schemas.Error(codigo=e.codigo, mensaje=e.mensaje, detalles=e.detalles)   # §11.4
    return JSONResponse(cuerpo.model_dump(exclude_none=True), status_code=e.http)


# ---------------------------------------------------------------- dependencias
def conexion() -> Iterator[sqlite3.Connection]:
    # Una conexión por request: SQLite serializa a los escritores con BEGIN IMMEDIATE.
    conn = db.connect(config.DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


Conexion = Annotated[sqlite3.Connection, Depends(conexion)]


def actor(conn: Conexion, x_usuario_id: Annotated[str | None, Header()] = None) -> sqlite3.Row | None:
    return service.actor_por_id(conn, x_usuario_id)


Actor = Annotated[sqlite3.Row | None, Depends(actor)]


# ---------------------------------------------------------------- §11.1 – §11.3
@app.get("/api/turnos", response_model=list[schemas.Turno], responses=schemas.errores(403), tags=["turnos"])
def listar_turnos(conn: Conexion, yo: Actor, estado: str | None = None,
                  desde: str | None = None, hasta: str | None = None) -> list[dict[str, Any]]:
    """§11.1: turnos visibles para el solicitante según su rol (RBAC-01)."""
    return service.listar_turnos(conn, yo, estado, desde, hasta)


@app.post("/api/turnos", status_code=201, response_model=schemas.Turno,
          responses=schemas.errores(403, 409, 422), tags=["turnos"])
def crear_turno(body: schemas.CrearTurno, conn: Conexion, yo: Actor) -> dict[str, Any]:
    """§11.2: reserva para el cliente de la sesión. 409 `SLOT_UNAVAILABLE` si choca (INV-02 / RN-02)."""
    return service.crear_turno(conn, yo, body.servicio_id, body.inicio)


@app.post("/api/turnos/{turno_id}/cancelar", response_model=schemas.Turno,
          responses=schemas.errores(404, 409, 422), tags=["turnos"])
def cancelar_turno(turno_id: str, conn: Conexion, yo: Actor, body: schemas.CancelarTurno | None = None) -> dict[str, Any]:
    """§11.3: 409 `CANCELLATION_WINDOW_EXPIRED` si el cliente cancela a menos de 2 h (INV-01 / RN-01)."""
    return service.cancelar_turno(conn, yo, turno_id, body.motivo_cancelacion if body else None)


# ---------------------------------------------------------------- §11.5 (solo lectura)
@app.get("/api/catalogo", response_model=schemas.Catalogo, response_model_exclude_none=True, tags=["lectura"])
def catalogo(conn: Conexion, yo: Actor) -> dict[str, Any]:
    return service.catalogo(conn, yo)


@app.get("/api/disponibilidad", response_model=list[schemas.Slot], responses=schemas.errores(409, 422), tags=["lectura"])
def disponibilidad(conn: Conexion, servicio_id: str | None = None, fecha: str | None = None) -> list[dict[str, str]]:
    return service.disponibilidad(conn, servicio_id, fecha)


@app.get("/api/usuarios", response_model=list[schemas.Usuario], responses=schemas.errores(403), tags=["lectura"])
def usuarios(conn: Conexion, yo: Actor) -> list[dict[str, Any]]:
    return service.usuarios_visibles(conn, yo)


@app.get("/api/auditoria", response_model=list[schemas.AuditoriaEvento], responses=schemas.errores(403), tags=["lectura"])
def auditoria(conn: Conexion, yo: Actor) -> list[dict[str, Any]]:
    return service.auditoria(conn, yo)


@app.get("/api/dev/usuarios", response_model=list[schemas.UsuarioDev], tags=["desarrollo"])
def usuarios_dev(conn: Conexion) -> list[dict[str, Any]]:
    return service.usuarios_dev(conn)
