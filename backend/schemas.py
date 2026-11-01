"""Modelos Pydantic del contrato HTTP (spec.md §4.1 y §11).

Solo atributos de §4.1. Los instantes viajan como texto ISO 8601 en UTC con sufijo Z,
tal como se guardan en la base.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Estado = Literal["CONFIRMADO", "CANCELADO", "COMPLETADO", "AUSENTE"]   # RN-06
Rol = Literal["CLIENTE", "PROFESIONAL", "ADMIN"]
Instante = str

EJ_INICIO = "2026-11-06T13:00:00Z"


class Estricto(BaseModel):
    """Salidas: el servidor nunca devuelve campos fuera de §4.1."""
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- entrada
# Tolerante a propósito: los campos faltantes o inválidos se rechazan en el servicio con
# códigos de §7 (INVALID_TIME_RANGE, SERVICE_UNAVAILABLE), en el formato de §11.4.
class CrearTurno(BaseModel):
    """§11.2. fin, profesional_id y cliente_id los define el servidor."""
    servicio_id: str | None = Field(default=None, examples=["<uuid de Servicio>"])
    inicio: Instante | None = Field(default=None, examples=[EJ_INICIO])


class CancelarTurno(BaseModel):
    """§11.3. Obligatorio para PROFESIONAL/ADMIN; se ignora si cancela el CLIENTE."""
    motivo_cancelacion: str | None = Field(default=None, examples=["Imprevisto del profesional"])


# ---------------------------------------------------------------- salida
class Turno(Estricto):
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


class Error(Estricto):
    """§11.4. `codigo` es uno de los de §7."""
    codigo: str = Field(examples=["SLOT_UNAVAILABLE"])
    mensaje: str = Field(examples=["Ese horario ya fue reservado."])
    detalles: dict[str, str] | None = None


class Servicio(Estricto):
    id: str
    profesional_id: str
    nombre: str
    descripcion: str | None
    duracion_min: int
    precio_referencia: float
    activo: bool


class Profesional(Estricto):
    id: str
    nombre_completo: str


class HorarioLaboral(Estricto):
    id: str
    profesional_id: str
    dia_semana: int = Field(ge=0, le=6, description="0 = domingo … 6 = sábado")
    hora_inicio: str
    hora_fin: str


class Bloqueo(Estricto):
    id: str
    profesional_id: str
    inicio: Instante
    fin: Instante
    motivo: str | None


class Catalogo(Estricto):
    servicios: list[Servicio]
    profesionales: list[Profesional]
    horarios: list[HorarioLaboral]
    bloqueos: list[Bloqueo] | None = Field(default=None, description="Solo para PROFESIONAL (propios) y ADMIN")


class Slot(Estricto):
    inicio: Instante
    fin: Instante


class Usuario(Estricto):
    """Usuario de §4.1 sin password_hash."""
    id: str
    email: str
    email_verificado: bool
    nombre_completo: str
    telefono: str
    rol: Rol
    activo: bool
    creado_en: Instante


class AuditoriaEvento(Estricto):
    id: str
    entidad: str
    entidad_id: str
    accion: str
    actor_id: str
    rol: Rol
    ocurrido_en: Instante
    detalle: str | None


class UsuarioDev(Estricto):
    id: str
    nombre_completo: str
    rol: Rol


def errores(*codigos: int) -> dict[int | str, dict]:
    """Respuestas de error para documentar en Swagger."""
    return {c: {"model": Error} for c in codigos}
