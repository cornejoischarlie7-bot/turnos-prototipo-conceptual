"""Dominio puro: las reglas de spec.md §3 sin acceso a base de datos ni a HTTP.

Todas las fechas son datetime con zona (aware). En la base se guardan en UTC
como texto ISO 8601 con sufijo Z, que se ordena bien como string.
"""
import re
from datetime import date, datetime, time, timedelta, timezone

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


def validar_cierre(estado: str, inicio: datetime, fin: datetime, ahora: datetime, nuevo: str) -> None:
    """RN-06 (spec §11.11): COMPLETADO desde el inicio, AUSENTE desde el fin, solo desde CONFIRMADO."""
    if estado != "CONFIRMADO":
        raise ErrorDominio("INVALID_STATE_TRANSITION", f"El turno está {estado}; los estados finales no admiten cambios.")
    if nuevo == "COMPLETADO" and ahora < inicio:
        raise ErrorDominio("INVALID_STATE_TRANSITION", "No se puede completar un turno que todavía no comenzó.")
    if nuevo == "AUSENTE" and ahora < fin:
        raise ErrorDominio("INVALID_STATE_TRANSITION", "Solo se puede marcar ausente una vez finalizado el turno.")


# ---------------------------------------------------------------- F-08 (spec §11.8)
HORA_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$|^24:00$")


def validar_franjas(franjas: list[tuple[int, str, str]]) -> None:
    """Horas HH:MM alineadas a la grilla, inicio < fin y sin solapamiento dentro del mismo día."""
    for d, a, b in franjas:
        if not (HORA_RE.match(a) and HORA_RE.match(b)):
            raise ErrorDominio("VALIDATION_ERROR", "Las horas deben tener formato HH:MM.")
        if int(a[3:]) % config.GRID_MIN or int(b[3:]) % config.GRID_MIN:
            raise ErrorDominio("INVALID_TIME_RANGE", f"Las horas deben estar alineadas a la grilla de {config.GRID_MIN} minutos.")
        if not a < b:
            raise ErrorDominio("INVALID_TIME_RANGE", "Cada franja debe terminar después de empezar.")
    for d in {f[0] for f in franjas}:
        del_dia = sorted((a, b) for x, a, b in franjas if x == d)
        if any(del_dia[i][1] > del_dia[i + 1][0] for i in range(len(del_dia) - 1)):
            raise ErrorDominio("INVALID_TIME_RANGE", "Las franjas de un mismo día no pueden solaparse.")


def _ventana(fecha: date, a: str, b: str) -> tuple[datetime, datetime]:
    """Franja "HH:MM"–"HH:MM" de una fecha local, como instantes con zona."""
    tz = config.ZONA_OPERACION
    ini = datetime.combine(fecha, time.fromisoformat(a), tz)
    fin = datetime.combine(fecha, time(0, 0), tz) + timedelta(days=1) if b == "24:00" \
        else datetime.combine(fecha, time.fromisoformat(b), tz)
    return ini, fin


# ---------------------------------------------------------------- F-03 (spec §11.9)
def slots_del_dia(fecha: date, duracion_min: int, horarios: list[tuple[int, str, str]],
                  ocupados: list[tuple[datetime, datetime]], ahora: datetime) -> list[tuple[datetime, datetime]]:
    """Slots libres de un día (fecha en zona de operación), con las reglas de RN-02, RN-04 y RN-05."""
    dia = fecha.isoweekday() % 7
    libres = []
    for d, a, b in sorted(horarios, key=lambda h: h[1]):
        if d != dia:
            continue
        cursor, limite = _ventana(fecha, a, b)
        while cursor + timedelta(minutes=duracion_min) <= limite:
            ini, fin = cursor.astimezone(timezone.utc), calcular_fin(cursor, duracion_min).astimezone(timezone.utc)
            try:
                validar_inicio(ini, ahora)
                if not any(solapa(ini, fin, oi, of) for oi, of in ocupados):
                    libres.append((ini, fin))
            except ErrorDominio:
                pass
            cursor += timedelta(minutes=config.GRID_MIN)
    return libres


# ---------------------------------------------------------------- F-12 (spec §5, §11.13)
def _minutos(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 60)


def _unir(intervalos: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for a, b in sorted(intervalos):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def minutos_disponibles(desde: datetime, hasta: datetime, horarios: list[tuple[int, str, str]],
                        bloqueos: list[tuple[datetime, datetime]]) -> float:
    """Minutos de horario laboral de un profesional en [desde, hasta), menos sus bloqueos."""
    tz = config.ZONA_OPERACION
    bloqueos = _unir(bloqueos)
    total, fecha = 0.0, desde.astimezone(tz).date()
    while fecha <= hasta.astimezone(tz).date():
        for d, a, b in horarios:
            if d != fecha.isoweekday() % 7:
                continue
            ini, fin = _ventana(fecha, a, b)
            ini, fin = max(ini, desde), min(fin, hasta)
            if ini >= fin:
                continue
            total += _minutos(ini, fin) - sum(_minutos(max(ini, bi), min(fin, bf)) for bi, bf in bloqueos)
        fecha += timedelta(days=1)
    return total


def calcular_metricas(turnos: list[dict], disponibles: float, rol_de: dict[str, str]) -> dict:
    """Métricas de spec §5 sobre turnos ya filtrados por período y alcance.

    turnos: dicts con estado, inicio y fin (datetime), servicio_id y cancelado_por_id.
    """
    tz = config.ZONA_OPERACION
    por_estado = {e: sum(1 for t in turnos if t["estado"] == e) for e in ("CONFIRMADO", "COMPLETADO", "CANCELADO", "AUSENTE")}
    total = len(turnos)
    activos = [t for t in turnos if t["estado"] != "CANCELADO"]
    por_dia: dict[str, int] = {}
    for t in turnos:
        if t["estado"] in ("CONFIRMADO", "COMPLETADO"):
            k = t["inicio"].astimezone(tz).date().isoformat()
            por_dia[k] = por_dia.get(k, 0) + 1
    cancel_rol = {"CLIENTE": 0, "PROFESIONAL": 0, "ADMIN": 0}
    for t in turnos:
        rol = rol_de.get(t["cancelado_por_id"] or "")
        if t["estado"] == "CANCELADO" and rol in cancel_rol:
            cancel_rol[rol] += 1
    top: dict[str, int] = {}
    for t in activos:
        top[t["servicio_id"]] = top.get(t["servicio_id"], 0) + 1
    reservados = sum(_minutos(t["inicio"], t["fin"]) for t in activos)
    cerrados = por_estado["COMPLETADO"] + por_estado["AUSENTE"]
    return {
        "total": total,
        "por_estado": por_estado,
        "turnos_por_dia": [{"fecha": f, "cantidad": n} for f, n in sorted(por_dia.items())],
        "tasa_cancelacion": round(por_estado["CANCELADO"] / total, 4) if total else 0,
        "cancelaciones_por_rol": cancel_rol,
        "tasa_ausentismo": round(por_estado["AUSENTE"] / cerrados, 4) if cerrados else 0,
        "ocupacion": round(min(1.0, reservados / disponibles), 4) if disponibles else 0,
        "minutos_reservados": round(reservados),
        "minutos_disponibles": round(disponibles),
        "top_servicios": [{"servicio_id": s, "cantidad": n} for s, n in sorted(top.items(), key=lambda x: (-x[1], x[0]))],
    }
