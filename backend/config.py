"""Configuración del backend (spec.md §1.4, RN-01, RN-04)."""
import os
from datetime import timedelta, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("TURNOS_DB", Path(__file__).parent / "turnos.db"))

CANCELLATION_WINDOW_HOURS = 2   # RN-01
MIN_LEAD_HOURS = 1              # RN-04: antelación mínima
HORIZON_DAYS = 60               # RN-04: horizonte máximo
GRID_MIN = 15                   # RN-04: grilla de slots

# §1.4: una única zona horaria de operación. Argentina no usa horario de verano,
# así que alcanza con un offset fijo y no hace falta el paquete tzdata en Windows.
ZONA_OPERACION = timezone(timedelta(hours=-3), "ART")

# Capa 2 con CORS habilitado (plan.md §8.2). Solo para desarrollo local.
CORS_ORIGINS = ["*"]
