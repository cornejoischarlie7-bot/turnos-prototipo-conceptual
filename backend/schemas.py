"""Modelos Pydantic del contrato HTTP (spec.md §11; plan.md §3.2).

Todos con extra="forbid": un campo que el contrato no acepta (por ejemplo `fin`,
`profesional_id` o `estado` en una reserva) es un cuerpo mal formado → 422 VALIDATION_ERROR.
Las salidas exponen exactamente los atributos de §4.1 (§11.2, §11.14).
"""
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

Estado = Literal["CONFIRMADO", "CANCELADO", "COMPLETADO", "AUSENTE"]   # RN-06
Rol = Literal["CLIENTE", "PROFESIONAL", "ADMIN"]
Instante = str   # ISO 8601 en UTC con sufijo Z (§11.1)

EJ_INICIO = "2026-11-06T13:00:00Z"


def _con_zona(v: str) -> str:
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        raise ValueError("El instante no tiene formato ISO 8601.")
    if d.tzinfo is None:
        raise ValueError("El instante debe incluir zona horaria (sufijo Z).")
    return v


InstanteIn = Annotated[str, AfterValidator(_con_zona)]


class Estricto(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- entrada
class ReservaIn(Estricto):
    """§11.4. `fin`, `profesional_id` y `estado` los define el servidor."""
    servicio_id: uuid.UUID = Field(examples=["<uuid de Servicio>"])
    inicio: InstanteIn = Field(examples=[EJ_INICIO])
    cliente_id: uuid.UUID | None = Field(default=None, description="Solo ADMIN: reserva en nombre de un cliente.")


class CancelacionIn(Estricto):
    """§11.5. Obligatorio para PROFESIONAL/ADMIN; opcional para el CLIENTE."""
    motivo: str | None = Field(default=None, examples=["El profesional no puede asistir."])


class FranjaIn(Estricto):
    dia_semana: int = Field(ge=0, le=6, description="0 = domingo … 6 = sábado")
    hora_inicio: str = Field(examples=["09:00"])
    hora_fin: str = Field(examples=["13:00"])


class HorarioIn(Estricto):
    """§11.8: reemplaza el horario semanal completo del profesional."""
    profesional_id: uuid.UUID
    franjas: list[FranjaIn]


class BloqueoIn(Estricto):
    """§11.10."""
    profesional_id: uuid.UUID
    inicio: InstanteIn
    fin: InstanteIn
    motivo: str | None = None
    confirmar_cancelacion: bool = False


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


class Servicio(Estricto):
    id: str
    profesional_id: str
    nombre: str
    descripcion: str | None
    duracion_min: int
    precio_referencia: float
    activo: bool


class HorarioLaboral(Estricto):
    id: str
    profesional_id: str
    dia_semana: int
    hora_inicio: str
    hora_fin: str


class Bloqueo(Estricto):
    id: str
    profesional_id: str
    inicio: Instante
    fin: Instante
    motivo: str | None


class BloqueoCreado(Estricto):
    bloqueo: Bloqueo
    turnos_cancelados: list[Turno]


class AuditoriaEvento(Estricto):
    id: str
    entidad: str
    entidad_id: str
    accion: str
    actor_id: str
    rol: Rol
    ocurrido_en: Instante
    detalle: str | None


class Slot(Estricto):
    inicio: Instante
    fin: Instante


class PorDia(Estricto):
    fecha: str
    cantidad: int


class PorServicio(Estricto):
    servicio_id: str
    cantidad: int


class Metricas(Estricto):
    """§11.13 / §5."""
    desde: Instante
    hasta: Instante
    total: int
    por_estado: dict[Estado, int]
    turnos_por_dia: list[PorDia]
    tasa_cancelacion: float
    cancelaciones_por_rol: dict[Rol, int]
    tasa_ausentismo: float
    ocupacion: float
    minutos_reservados: int
    minutos_disponibles: int
    top_servicios: list[PorServicio]


class ErrorCuerpo(Estricto):
    codigo: str = Field(examples=["SLOT_UNAVAILABLE"])
    mensaje: str = Field(examples=["Ese horario ya fue reservado."])
    conflictos: list[Turno] | None = Field(default=None, description="Solo en el error de RN-05 (§11.10).")


class Error(Estricto):
    """§11.1: { "error": { "codigo", "mensaje" } }, con `codigo` de la §7."""
    error: ErrorCuerpo


def errores(*codigos: int) -> dict[int | str, dict]:
    """Respuestas de error para documentar en Swagger."""
    return {c: {"model": Error} for c in codigos}
