"""Dominio puro: las reglas de spec.md §3 sin acceso a base de datos ni a HTTP.

Todas las fechas son datetime con zona (aware). En la base se guardan en UTC
como texto ISO 8601 con sufijo Z, que se ordena bien como string.
"""
import re
from datetime import datetime, time, timedelta, timezone

import config

# Catálogo de errores de dominio (spec.md §7).
HTTP = {
    "SLOT_UNAVAILABLE": 409,
    "CANCELLATION_WINDOW_EXPIRED": 409,
    "INVALID_CONTACT": 422,
    "EMAIL_NOT_VERIFIED": 403,
    "INVALID_TIME_RANGE": 422,
    "INVALID_STATE_TRANSITION": 409,
    "SERVICE_HAS_ACTIVE_BOOKINGS": 409,
    "SERVICE_UNAVAILABLE": 409,
    "REASON_REQUIRED": 422,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "UNAUTHENTICATED": 401,
    "VALIDATION_ERROR": 422,
    "PERSISTENCE_UNAVAILABLE": 503,
}


class ErrorDominio(Exception):
    def __init__(self, codigo: str, mensaje: str, detalles: dict | None = None):
        super().__init__(mensaje)
        self.codigo, self.mensaje, self.detalles = codigo, mensaje, detalles
        self.http = HTTP[codigo]


# ---------------------------------------------------------------- tiempo
def a_iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def de_iso(s: str) -> datetime:
    """Parsea un instante ISO 8601 (spec §11.1). Sin zona horaria es ambiguo y se rechaza."""
    try:
        d = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        raise ErrorDominio("VALIDATION_ERROR", "El instante no tiene formato ISO 8601.")
    if d.tzinfo is None:
        raise ErrorDominio("VALIDATION_ERROR", "El instante debe incluir zona horaria (por ejemplo, sufijo Z).")
    return d.astimezone(timezone.utc)


def dia_semana(d: datetime) -> int:
    """0 = domingo … 6 = sábado (spec.md §11), en la zona de operación."""
    return (d.astimezone(config.ZONA_OPERACION).weekday() + 1) % 7


# ---------------------------------------------------------------- RN-02
def solapa(a_ini: datetime, a_fin: datetime, b_ini: datetime, b_fin: datetime) -> bool:
    """Intervalos semiabiertos [inicio, fin): turnos contiguos no se solapan."""
    return a_ini < b_fin and b_ini < a_fin


# ---------------------------------------------------------------- RN-03
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
TELEFONO_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def validar_contacto(nombre: str, email: str, telefono: str) -> dict:
    """Devuelve {campo: mensaje} con los campos inválidos; vacío si todo está bien."""
    errores = {}
    if not 2 <= len((nombre or "").strip()) <= 100:
        errores["nombre"] = "Nombre completo de 2 a 100 caracteres."
    if not EMAIL_RE.match(email or ""):
        errores["email"] = "Email inválido."
    if not TELEFONO_RE.match(telefono or ""):
        errores["telefono"] = "Teléfono en formato internacional, ej. +5491155550101."
    return errores


# ---------------------------------------------------------------- RN-04
def calcular_fin(inicio: datetime, duracion_min: int) -> datetime:
    return inicio + timedelta(minutes=duracion_min)


def validar_inicio(inicio: datetime, ahora: datetime) -> None:
    local = inicio.astimezone(config.ZONA_OPERACION)
    if local.second or local.microsecond or local.minute % config.GRID_MIN:
        raise ErrorDominio("INVALID_TIME_RANGE", f"El inicio debe estar alineado a la grilla de {config.GRID_MIN} minutos.")
    if inicio < ahora + timedelta(hours=config.MIN_LEAD_HOURS):
        raise ErrorDominio("INVALID_TIME_RANGE", f"Se requiere al menos {config.MIN_LEAD_HOURS} hora de antelación.")
    if inicio > ahora + timedelta(days=config.HORIZON_DAYS):
        raise ErrorDominio("INVALID_TIME_RANGE", f"No se puede reservar a más de {config.HORIZON_DAYS} días.")


def dentro_de_horario(inicio: datetime, fin: datetime, horarios: list[tuple[int, str, str]]) -> bool:
    """horarios: (dia_semana, "HH:MM", "HH:MM") en la zona de operación."""
    li, lf = inicio.astimezone(config.ZONA_OPERACION), fin.astimezone(config.ZONA_OPERACION)
    if li.date() != lf.date() and lf.time() != time(0, 0):
        return False
    hi, hf = li.strftime("%H:%M"), ("24:00" if lf.time() == time(0, 0) else lf.strftime("%H:%M"))
    dia = dia_semana(inicio)
    return any(d == dia and a <= hi and hf <= b for d, a, b in horarios)


# ---------------------------------------------------------------- RN-01 / RN-06
def validar_cancelacion(estado: str, inicio: datetime, ahora: datetime, rol: str, motivo: str | None) -> str | None:
    """Valida la cancelación y devuelve el motivo a guardar (spec §11.5: opcional para el cliente)."""
    if estado != "CONFIRMADO":
        raise ErrorDominio("INVALID_STATE_TRANSITION", f"El turno está {estado} y no puede cancelarse.")
    if ahora >= inicio:
        raise ErrorDominio("INVALID_STATE_TRANSITION", "El turno ya comenzó: corresponde marcarlo como completado o ausente.")
    if rol == "CLIENTE":
        if inicio - ahora < timedelta(hours=config.CANCELLATION_WINDOW_HOURS):
            raise ErrorDominio("CANCELLATION_WINDOW_EXPIRED",
                               f"No podés cancelar: faltan menos de {config.CANCELLATION_WINDOW_HOURS} horas para el turno.")
        return (motivo or "").strip() or None
    motivo = (motivo or "").strip()
    if not motivo:
        raise ErrorDominio("REASON_REQUIRED", "El motivo de cancelación es obligatorio.")
    return motivo
