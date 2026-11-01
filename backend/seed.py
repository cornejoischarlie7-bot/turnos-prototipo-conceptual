"""Semilla de turnos.db con los mismos datos de ejemplo que el prototipo (plan.md §4).

Uso:
    python seed.py           # crea y siembra la base si está vacía
    python seed.py --reset   # borra turnos.db y la vuelve a sembrar

Las fechas son relativas a "hoy" real, así los datos no envejecen al resembrar.
"""
import hashlib
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone

import config
import db
from domain import a_iso, ahora_utc, dentro_de_horario

NAMESPACE = uuid.UUID("6f1d2c1e-7a3b-4c5d-9e8f-0a1b2c3d4e5f")


def uid(clave: str) -> str:
    """UUID determinístico, para que los ids no cambien entre resiembras."""
    return str(uuid.uuid5(NAMESPACE, clave))


def password_hash_inutilizable() -> str:
    # F-01 no tiene contrato todavía: nadie inicia sesión con contraseña.
    # Se guarda un hash scrypt de un secreto aleatorio que no se conserva.
    sal = os.urandom(16)
    h = hashlib.scrypt(os.urandom(32), salt=sal, n=2**14, r=8, p=1)
    return f"scrypt${sal.hex()}${h.hex()}"


def es_habil(d: datetime) -> bool:
    return d.weekday() < 5


def sumar_habiles(d: datetime, n: int) -> datetime:
    paso = -1 if n < 0 else 1
    while n:
        d += timedelta(days=paso)
        if es_habil(d):
            n -= paso
    return d


USUARIOS = [
    ("admin", "ADMIN", "Admin General", "admin@turnospro.test", "+5491100000000", True),
    ("p1", "PROFESIONAL", "Dra. Ana Martínez", "ana@turnospro.test", "+5491100000001", True),
    ("p2", "PROFESIONAL", "Dr. Bruno Salas", "bruno@turnospro.test", "+5491100000002", True),
    ("p3", "PROFESIONAL", "Lic. Carla Ibáñez", "carla@turnospro.test", "+5491100000003", True),
    ("c1", "CLIENTE", "Lucía Fernández", "lucia@mail.test", "+5491155550101", True),
    ("c2", "CLIENTE", "Martín Gómez", "martin@mail.test", "+5491155550102", False),
    ("c3", "CLIENTE", "Sofía Ruiz", "sofia@mail.test", "+5491155550103", True),
]

SERVICIOS = [
    ("s1", "p1", "Consulta general", "Evaluación clínica y orientación.", 30, 18000),
    ("s2", "p1", "Control anual", "Chequeo completo con estudios básicos.", 60, 32000),
    ("s3", "p2", "Sesión de terapia", "Sesión individual.", 45, 25000),
    ("s4", "p2", "Evaluación inicial", "Primera entrevista y plan de trabajo.", 60, 30000),
    ("s5", "p3", "Asesoría nutricional", "Seguimiento y ajustes.", 30, 15000),
    ("s6", "p3", "Plan alimentario", "Diseño de plan personalizado.", 45, 22000),
]

# dia_semana: 0 = domingo … 6 = sábado (spec §11). Lunes a viernes.
HORARIOS = {
    "p1": [("09:00", "13:00"), ("14:00", "18:00")],
    "p2": [("10:00", "14:00"), ("15:00", "19:00")],
    "p3": [("09:00", "13:00")],
}

# (clave, cliente, servicio, día hábil relativo a D0, hora, minuto, estado, motivo, cancelado_por)
# D0 es el próximo día hábil después de hoy, para que ningún turno CONFIRMADO quede en el pasado.
TURNOS = [
    ("t1", "c1", "s1", -3, 9, 0, "COMPLETADO", None, None),
    ("t2", "c3", "s2", -3, 10, 0, "COMPLETADO", None, None),
    ("t3", "c1", "s3", -2, 10, 0, "COMPLETADO", None, None),
    ("t4", "c3", "s5", -2, 11, 0, "AUSENTE", None, None),
    ("t5", "c2", "s1", -1, 9, 30, "COMPLETADO", None, None),
    ("t6", "c1", "s4", -1, 15, 0, "CANCELADO", None, "c1"),
    ("t7", "c3", "s6", -1, 10, 0, "CANCELADO", "Imprevisto médico", "p3"),
    ("t8", "c1", "s1", 0, 9, 30, "CONFIRMADO", None, None),
    ("t9", "c3", "s5", 0, 10, 30, "CONFIRMADO", None, None),
    ("t10", "c2", "s3", 0, 11, 0, "CONFIRMADO", None, None),
    ("t11", "c1", "s2", 0, 15, 0, "CONFIRMADO", None, None),
    ("t12", "c3", "s1", 1, 10, 0, "CONFIRMADO", None, None),
    ("t13", "c1", "s4", 1, 11, 0, "CONFIRMADO", None, None),
    ("t14", "c2", "s5", 2, 9, 0, "CONFIRMADO", None, None),
    ("t15", "c1", "s2", 3, 15, 30, "CONFIRMADO", None, None),
]

BLOQUEOS = [
    ("b1", "p1", 1, (12, 0), (13, 0), "Trámite personal"),
    ("b2", "p2", 2, (10, 0), (19, 0), "Capacitación"),
]


def _redondear_a_grilla(d: datetime) -> datetime:
    resto = (d.minute % config.GRID_MIN) or config.GRID_MIN
    return (d + timedelta(minutes=config.GRID_MIN - resto)).replace(second=0, microsecond=0)


def sembrar(conn: sqlite3.Connection, ahora: datetime | None = None) -> None:
    ahora = ahora or ahora_utc()
    tz = config.ZONA_OPERACION
    hoy = ahora.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    d0 = sumar_habiles(hoy, 1)

    def en(n: int, h: int, m: int) -> datetime:
        dia = d0 if n == 0 else sumar_habiles(d0, n)
        return dia.replace(hour=h, minute=m).astimezone(timezone.utc)

    creado = a_iso(ahora - timedelta(days=5))
    svc = {k: (p, dur) for k, p, _, _, dur, _ in SERVICIOS}

    with db.transaccion(conn):
        for k, rol, nombre, email, tel, verif in USUARIOS:
            conn.execute(
                "INSERT INTO usuario (id, email, email_verificado, password_hash, nombre_completo, telefono, rol, activo, creado_en)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (uid(k), email, int(verif), password_hash_inutilizable(), nombre, tel, rol, creado))
        for k, p, nombre, desc, dur, precio in SERVICIOS:
            conn.execute(
                "INSERT INTO servicio (id, profesional_id, nombre, descripcion, duracion_min, precio_referencia, activo)"
                " VALUES (?, ?, ?, ?, ?, ?, 1)", (uid(k), uid(p), nombre, desc, dur, precio))
        for p, rangos in HORARIOS.items():
            for dia in range(1, 6):
                for hi, hf in rangos:
                    conn.execute(
                        "INSERT INTO horario_laboral (id, profesional_id, dia_semana, hora_inicio, hora_fin) VALUES (?, ?, ?, ?, ?)",
                        (uid(f"h-{p}-{dia}-{hi}"), uid(p), dia, hi, hf))
        for k, p, n, (hi, mi), (hf, mf), motivo in BLOQUEOS:
            conn.execute("INSERT INTO bloqueo (id, profesional_id, inicio, fin, motivo) VALUES (?, ?, ?, ?, ?)",
                         (uid(k), uid(p), a_iso(en(n, hi, mi)), a_iso(en(n, hf, mf)), motivo))

        filas = [(k, c, s, en(n, h, m), estado, motivo, por) for k, c, s, n, h, m, estado, motivo, por in TURNOS]
        # Dos turnos relativos a "ahora" para probar RN-01 sin esperar: uno dentro de la ventana
        # de 2 h (no cancelable por el cliente) y otro a 3 h (cancelable). Solo si caen en horario
        # laboral: la semilla no debe crear datos que violen RN-02.
        horarios_p3 = [(d, a, b) for d in range(1, 6) for a, b in HORARIOS["p3"]]
        for k, c, s, delta in (("r1", "c3", "s6", timedelta(hours=1, minutes=15)), ("r2", "c1", "s5", timedelta(hours=3))):
            inicio = _redondear_a_grilla(ahora + delta)
            if dentro_de_horario(inicio, inicio + timedelta(minutes=svc[s][1]), horarios_p3):
                filas.append((k, c, s, inicio, "CONFIRMADO", None, None))
            else:
                print(f"[seed] turno {k} omitido: {inicio.astimezone(tz):%a %H:%M} queda fuera del horario de p3", file=sys.stderr)

        for k, c, s, inicio, estado, motivo, por in filas:
            p, dur = svc[s]
            try:
                conn.execute(
                    "INSERT INTO turno (id, cliente_id, profesional_id, servicio_id, inicio, fin, estado,"
                    " motivo_cancelacion, cancelado_por_id, creado_en, actualizado_en)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (uid(k), uid(c), uid(p), uid(s), a_iso(inicio), a_iso(inicio + timedelta(minutes=dur)),
                     estado, motivo, uid(por) if por else None, creado, creado))
            except sqlite3.IntegrityError as e:
                # Un turno relativo a "ahora" puede chocar con uno fijo según la hora de siembra.
                print(f"[seed] turno {k} omitido: {e}", file=sys.stderr)

        conn.execute(
            "INSERT INTO auditoria_evento (id, entidad, entidad_id, accion, actor_id, rol, ocurrido_en, detalle)"
            " VALUES (?, 'Sistema', '-', 'DATOS_INICIALES', ?, 'ADMIN', ?, 'Se cargaron los datos de ejemplo.')",
            (str(uuid.uuid4()), uid("admin"), a_iso(ahora)))


def sembrar_si_vacia(path=config.DB_PATH) -> None:
    db.init_db(path)
    conn = db.connect(path)
    try:
        if conn.execute("SELECT COUNT(*) FROM usuario").fetchone()[0] == 0:
            sembrar(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    if "--reset" in sys.argv:
        # RN-06 y RN-08 impiden borrar filas, así que el reinicio borra el archivo.
        for sufijo in ("", "-wal", "-shm"):
            p = config.DB_PATH.with_name(config.DB_PATH.name + sufijo)
            if p.exists():
                p.unlink()
    sembrar_si_vacia()
    print(f"Base lista en {config.DB_PATH}")
