"""Modelos Pydantic del contrato HTTP (spec.md §11; plan.md §3.2).

Todos con extra="forbid": un campo que el contrato no acepta (por ejemplo `fin`,
`profesional_id` o `estado` en una reserva) es un cuerpo mal formado → 422 VALIDATION_ERROR.
"""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Estado = Literal["CONFIRMADO", "CANCELADO", "COMPLETADO", "AUSENTE"]   # RN-06
Instante = str   # ISO 8601 en UTC con sufijo Z (§11.1)

EJ_INICIO = "2026-11-06T13:00:00Z"


class Estricto(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- entrada
class ReservaIn(Estricto):
    """§11.4. `fin`, `profesional_id` y `estado` los define el servidor."""
    servicio_id: uuid.UUID = Field(examples=["<uuid de Servicio>"])
    inicio: Instante = Field(examples=[EJ_INICIO])
    cliente_id: uuid.UUID | None = Field(default=None, description="Solo ADMIN: reserva en nombre de un cliente.")

    @field_validator("inicio")
    @classmethod
    def instante_con_zona(cls, v: str) -> str:
        try:
            d = datetime.fromisoformat(v)
        except ValueError:
            raise ValueError("El instante no tiene formato ISO 8601.")
        if d.tzinfo is None:
            raise ValueError("El instante debe incluir zona horaria (sufijo Z).")
        return v


class CancelacionIn(Estricto):
    """§11.5. Obligatorio para PROFESIONAL/ADMIN; opcional para el CLIENTE."""
    motivo: str | None = Field(default=None, examples=["El profesional no puede asistir."])


# ---------------------------------------------------------------- salida
class Turno(Estricto):
    """§11.2: exactamente los atributos de Turno en §4.1."""
    id: str
    cliente_id: str
    profesional_id: str
    servicio_id: str
    inicio: Instante
    fin: Instante
    estado: Estado
    motivo_cancelacion: str | None
    cancelado_por_id: str | None
    creado_en: Instante
    actualizado_en: Instante


class ErrorCuerpo(Estricto):
    codigo: str = Field(examples=["SLOT_UNAVAILABLE"])
    mensaje: str = Field(examples=["Ese horario ya fue reservado."])


class Error(Estricto):
    """§11.1: { "error": { "codigo", "mensaje" } }, con `codigo` de la §7."""
    error: ErrorCuerpo


def errores(*codigos: int) -> dict[int | str, dict]:
    """Respuestas de error para documentar en Swagger."""
    return {c: {"model": Error} for c in codigos}
