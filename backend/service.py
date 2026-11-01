"""Casos de uso del backend (spec.md §11.3 – §11.5). Une el dominio puro con la base de datos.

Cada función recibe la conexión y el actor (fila de `usuario`, ya autenticado). La hora
sale de `reloj.ahora()`, que los tests reemplazan para probar las reglas temporales.
"""
import sqlite3
import uuid
from datetime import datetime

import db
import reloj
from domain import (ErrorDominio, a_iso, calcular_fin, de_iso, dentro_de_horario, validar_cancelacion,
                    validar_contacto, validar_inicio)

CAMPOS_TURNO = ("id", "cliente_id", "profesional_id", "servicio_id", "inicio", "fin", "estado",
                "motivo_cancelacion", "cancelado_por_id", "creado_en", "actualizado_en")


def turno_json(r: sqlite3.Row) -> dict:
    """§11.2: exactamente los campos de Turno en §4.1."""
    return {k: r[k] for k in CAMPOS_TURNO}


# ---------------------------------------------------------------- identidad y auditoría
def actor_por_id(conn: sqlite3.Connection, usuario_id: str | None) -> sqlite3.Row:
    """§11.1 / D-06: el rol se lee de la base. Sin encabezado, inexistente o inactivo → 401."""
    fila = None
    if usuario_id:
        fila = conn.execute("SELECT * FROM usuario WHERE id = ? AND activo = 1", (usuario_id,)).fetchone()
    if fila is None:
        raise ErrorDominio("UNAUTHENTICATED", "Falta identificar al usuario (encabezado X-Usuario-Id).")
    return fila


def _auditar(conn, actor, accion: str, entidad: str, entidad_id: str, detalle: str, ahora: datetime) -> None:
    conn.execute(
        "INSERT INTO auditoria_evento (id, entidad, entidad_id, accion, actor_id, rol, ocurrido_en, detalle)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), entidad, entidad_id, accion, actor["id"], actor["rol"], a_iso(ahora), detalle))


def _visible(actor, t) -> bool:
    """RBAC-02: un turno ajeno se trata como inexistente."""
    if actor["rol"] == "CLIENTE":
        return t["cliente_id"] == actor["id"]
    if actor["rol"] == "PROFESIONAL":
        return t["profesional_id"] == actor["id"]
    return True


# ---------------------------------------------------------------- GET /api/turnos (§11.3)
def listar_turnos(conn, actor, estado: str | None = None, profesional_id: str | None = None,
                  desde: str | None = None, hasta: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM turno WHERE 1 = 1", []
    if actor["rol"] == "CLIENTE":
        sql += " AND cliente_id = ?"; args.append(actor["id"])
    elif actor["rol"] == "PROFESIONAL":
        sql += " AND profesional_id = ?"; args.append(actor["id"])   # otro profesional_id → lista vacía
    if estado:
        sql += " AND estado = ?"; args.append(estado)
    if profesional_id:
        sql += " AND profesional_id = ?"; args.append(profesional_id)
    if desde:
        sql += " AND inicio >= ?"; args.append(a_iso(de_iso(desde)))
    if hasta:
        sql += " AND inicio < ?"; args.append(a_iso(de_iso(hasta)))
    return [turno_json(r) for r in conn.execute(sql + " ORDER BY inicio", args)]


# ---------------------------------------------------------------- POST /api/turnos (§11.4)
def crear_turno(conn, actor, servicio_id: str, inicio_txt: str, cliente_id: str | None = None) -> dict:
    if actor["rol"] == "PROFESIONAL":
        raise ErrorDominio("FORBIDDEN", "Un profesional no puede crear turnos.")
    if actor["rol"] == "CLIENTE":
        if cliente_id is not None and cliente_id != actor["id"]:
            raise ErrorDominio("FORBIDDEN", "Un cliente solo puede reservar para sí mismo.")
        cliente_id = actor["id"]
    elif cliente_id is None:
        raise ErrorDominio("VALIDATION_ERROR", "Un administrador debe indicar cliente_id.")

    inicio = de_iso(inicio_txt)
    try:
        with db.transaccion(conn):
            ahora = reloj.ahora()
            cliente = conn.execute("SELECT * FROM usuario WHERE id = ? AND rol = 'CLIENTE' AND activo = 1",
                                   (cliente_id,)).fetchone()
            if cliente is None:
                raise ErrorDominio("NOT_FOUND", "El cliente no existe.")
            svc = conn.execute("SELECT * FROM servicio WHERE id = ?", (servicio_id,)).fetchone()
            if svc is None:
                raise ErrorDominio("NOT_FOUND", "El servicio no existe.")
            if not svc["activo"]:                                            # RN-07
                raise ErrorDominio("SERVICE_UNAVAILABLE", "El servicio ya no está disponible para reservas.")

            errores = validar_contacto(cliente["nombre_completo"], cliente["email"], cliente["telefono"])
            if errores:                                                      # RN-03
                raise ErrorDominio("INVALID_CONTACT", "Datos de contacto inválidos: " + " ".join(errores.values()), errores)
            if not cliente["email_verificado"]:
                raise ErrorDominio("EMAIL_NOT_VERIFIED", "Hay que verificar el email antes de reservar.")

            validar_inicio(inicio, ahora)                                    # RN-04
            prof_id, fin = svc["profesional_id"], calcular_fin(inicio, svc["duracion_min"])
            ini_s, fin_s = a_iso(inicio), a_iso(fin)
            horarios = [(h["dia_semana"], h["hora_inicio"], h["hora_fin"]) for h in
                        conn.execute("SELECT * FROM horario_laboral WHERE profesional_id = ?", (prof_id,))]
            if not dentro_de_horario(inicio, fin, horarios):                 # RN-04 (§11.4)
                raise ErrorDominio("INVALID_TIME_RANGE", "Ese horario está fuera de la jornada laboral del profesional.")
            if conn.execute("SELECT 1 FROM bloqueo WHERE profesional_id = ? AND inicio < ? AND ? < fin",
                            (prof_id, fin_s, ini_s)).fetchone():             # RN-05
                raise ErrorDominio("SLOT_UNAVAILABLE", "El profesional bloqueó ese horario.")
            if conn.execute("SELECT 1 FROM turno WHERE profesional_id = ? AND estado = 'CONFIRMADO' AND inicio < ? AND ? < fin",
                            (prof_id, fin_s, ini_s)).fetchone():             # INV-02
                raise ErrorDominio("SLOT_UNAVAILABLE", "Ese horario ya fue reservado.")
            if conn.execute("SELECT 1 FROM turno WHERE cliente_id = ? AND estado = 'CONFIRMADO' AND inicio < ? AND ? < fin",
                            (cliente_id, fin_s, ini_s)).fetchone():          # INV-02
                raise ErrorDominio("SLOT_UNAVAILABLE", "El cliente ya tiene un turno confirmado que se solapa con ese horario.")

            turno_id, ahora_s = str(uuid.uuid4()), a_iso(ahora)
            conn.execute(
                "INSERT INTO turno (id, cliente_id, profesional_id, servicio_id, inicio, fin, estado, creado_en, actualizado_en)"
                " VALUES (?, ?, ?, ?, ?, ?, 'CONFIRMADO', ?, ?)",
                (turno_id, cliente_id, prof_id, svc["id"], ini_s, fin_s, ahora_s, ahora_s))
            _auditar(conn, actor, "TURNO_CREADO", "Turno", turno_id, f"{svc['nombre']} · {ini_s}", ahora)   # RN-08
            return turno_json(conn.execute("SELECT * FROM turno WHERE id = ?", (turno_id,)).fetchone())
    except sqlite3.IntegrityError as e:
        # Segunda barrera: trg_turno_solapamiento en la base.
        if "SLOT_UNAVAILABLE" in str(e):
            raise ErrorDominio("SLOT_UNAVAILABLE", "Ese horario ya fue reservado.")
        raise


# ---------------------------------------------------------------- POST /api/turnos/{id}/cancelar (§11.5)
def cancelar_turno(conn, actor, turno_id: str, motivo: str | None) -> dict:
    with db.transaccion(conn):
        ahora = reloj.ahora()
        t = conn.execute("SELECT * FROM turno WHERE id = ?", (turno_id,)).fetchone()
        if t is None or not _visible(actor, t):
            raise ErrorDominio("NOT_FOUND", "El turno no existe o no tenés acceso a él.")
        motivo = validar_cancelacion(t["estado"], de_iso(t["inicio"]), ahora, actor["rol"], motivo)  # INV-01, RN-06
        conn.execute(
            "UPDATE turno SET estado = 'CANCELADO', motivo_cancelacion = ?, cancelado_por_id = ?, actualizado_en = ?"
            " WHERE id = ?", (motivo, actor["id"], a_iso(ahora), turno_id))
        _auditar(conn, actor, "TURNO_CANCELADO", "Turno", turno_id,
                 f"Motivo: {motivo}" if motivo else "Cancelado sin motivo", ahora)                 # RN-08
        return turno_json(conn.execute("SELECT * FROM turno WHERE id = ?", (turno_id,)).fetchone())
