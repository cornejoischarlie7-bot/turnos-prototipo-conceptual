"""Casos de uso del backend (spec.md §11). Une el dominio puro con la base de datos.

Cada función recibe la conexión, el actor (fila de usuario o None si es Anónimo)
y opcionalmente `ahora`, para poder probar las reglas temporales sin esperar.
"""
import sqlite3
import uuid
from datetime import date, datetime, timedelta

import config
import db
from domain import (ErrorDominio, a_iso, ahora_utc, calcular_fin, de_iso, dentro_de_horario, slots_del_dia,
                    validar_cancelacion, validar_contacto, validar_inicio)


# ---------------------------------------------------------------- serialización (§4.1)
def turno_json(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in ("id", "cliente_id", "profesional_id", "servicio_id", "inicio", "fin", "estado",
                              "motivo_cancelacion", "cancelado_por_id", "creado_en", "actualizado_en")}


def usuario_json(r: sqlite3.Row) -> dict:
    # Nunca se expone password_hash.
    return {"id": r["id"], "email": r["email"], "email_verificado": bool(r["email_verificado"]),
            "nombre_completo": r["nombre_completo"], "telefono": r["telefono"], "rol": r["rol"],
            "activo": bool(r["activo"]), "creado_en": r["creado_en"]}


def servicio_json(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "profesional_id": r["profesional_id"], "nombre": r["nombre"], "descripcion": r["descripcion"],
            "duracion_min": r["duracion_min"], "precio_referencia": r["precio_referencia"], "activo": bool(r["activo"])}


# ---------------------------------------------------------------- identidad y auditoría
def actor_por_id(conn: sqlite3.Connection, usuario_id: str | None) -> sqlite3.Row | None:
    """Identidad de desarrollo (spec §11): id inexistente o inactivo = Anónimo."""
    if not usuario_id:
        return None
    return conn.execute("SELECT * FROM usuario WHERE id = ? AND activo = 1", (usuario_id,)).fetchone()


def _auditar(conn, actor, accion: str, entidad: str, entidad_id: str, detalle: str, ahora: datetime) -> None:
    conn.execute(
        "INSERT INTO auditoria_evento (id, entidad, entidad_id, accion, actor_id, rol, ocurrido_en, detalle)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), entidad, entidad_id, accion, actor["id"], actor["rol"], a_iso(ahora), detalle))


def _visible(actor, t) -> bool:
    """RBAC-02: un turno ajeno se trata como inexistente."""
    if actor is None:
        return False
    if actor["rol"] == "CLIENTE":
        return t["cliente_id"] == actor["id"]
    if actor["rol"] == "PROFESIONAL":
        return t["profesional_id"] == actor["id"]
    return True


# ---------------------------------------------------------------- GET /api/turnos (§11.1)
def listar_turnos(conn, actor, estado: str | None = None, desde: str | None = None, hasta: str | None = None) -> list[dict]:
    if actor is None:
        raise ErrorDominio("FORBIDDEN", "Iniciá sesión para ver turnos.")
    sql, args = "SELECT * FROM turno WHERE 1 = 1", []
    if actor["rol"] == "CLIENTE":
        sql += " AND cliente_id = ?"; args.append(actor["id"])
    elif actor["rol"] == "PROFESIONAL":
        sql += " AND profesional_id = ?"; args.append(actor["id"])
    if estado:
        sql += " AND estado = ?"; args.append(estado)
    if desde:
        sql += " AND inicio >= ?"; args.append(a_iso(de_iso(desde)))
    if hasta:
        sql += " AND inicio < ?"; args.append(a_iso(de_iso(hasta)))
    return [turno_json(r) for r in conn.execute(sql + " ORDER BY inicio", args)]


# ---------------------------------------------------------------- POST /api/turnos (§11.2)
def crear_turno(conn, actor, servicio_id: str | None, inicio_txt: str | None, ahora: datetime | None = None) -> dict:
    if actor is None or actor["rol"] != "CLIENTE":
        raise ErrorDominio("FORBIDDEN", "Solo un cliente puede reservar para sí mismo.")
    try:
        with db.transaccion(conn):
            ahora = ahora or ahora_utc()
            svc = conn.execute("SELECT * FROM servicio WHERE id = ?", (servicio_id,)).fetchone()
            if svc is None or not svc["activo"]:
                raise ErrorDominio("SERVICE_UNAVAILABLE", "El servicio ya no está disponible para reservas.")

            inicio = de_iso(inicio_txt)
            validar_inicio(inicio, ahora)                                    # RN-04

            cliente = conn.execute("SELECT * FROM usuario WHERE id = ?", (actor["id"],)).fetchone()
            errores = validar_contacto(cliente["nombre_completo"], cliente["email"], cliente["telefono"])
            if errores:                                                      # RN-03
                raise ErrorDominio("INVALID_CONTACT", "Datos de contacto inválidos: " + " ".join(errores.values()), errores)
            if not cliente["email_verificado"]:
                raise ErrorDominio("EMAIL_NOT_VERIFIED", "Tenés que verificar tu email antes de reservar.")

            prof_id, fin = svc["profesional_id"], calcular_fin(inicio, svc["duracion_min"])
            ini_s, fin_s = a_iso(inicio), a_iso(fin)
            horarios = [(h["dia_semana"], h["hora_inicio"], h["hora_fin"]) for h in
                        conn.execute("SELECT * FROM horario_laboral WHERE profesional_id = ?", (prof_id,))]
            if not dentro_de_horario(inicio, fin, horarios):                 # RN-02
                raise ErrorDominio("SLOT_UNAVAILABLE", "Ese horario está fuera de la jornada laboral del profesional.")
            if conn.execute("SELECT 1 FROM bloqueo WHERE profesional_id = ? AND inicio < ? AND ? < fin",
                            (prof_id, fin_s, ini_s)).fetchone():             # RN-05
                raise ErrorDominio("SLOT_UNAVAILABLE", "El profesional bloqueó ese horario.")
            if conn.execute("SELECT 1 FROM turno WHERE profesional_id = ? AND estado = 'CONFIRMADO' AND inicio < ? AND ? < fin",
                            (prof_id, fin_s, ini_s)).fetchone():             # RN-02
                raise ErrorDominio("SLOT_UNAVAILABLE", "Ese horario ya fue reservado.")
            if conn.execute("SELECT 1 FROM turno WHERE cliente_id = ? AND estado = 'CONFIRMADO' AND inicio < ? AND ? < fin",
                            (actor["id"], fin_s, ini_s)).fetchone():         # RN-02
                raise ErrorDominio("SLOT_UNAVAILABLE", "Ya tenés un turno confirmado que se solapa con ese horario.")

            turno_id, ahora_s = str(uuid.uuid4()), a_iso(ahora)
            conn.execute(
                "INSERT INTO turno (id, cliente_id, profesional_id, servicio_id, inicio, fin, estado, creado_en, actualizado_en)"
                " VALUES (?, ?, ?, ?, ?, ?, 'CONFIRMADO', ?, ?)",
                (turno_id, actor["id"], prof_id, svc["id"], ini_s, fin_s, ahora_s, ahora_s))
            _auditar(conn, actor, "TURNO_CREADO", "Turno", turno_id, f"{svc['nombre']} · {ini_s}", ahora)   # RN-08
            return turno_json(conn.execute("SELECT * FROM turno WHERE id = ?", (turno_id,)).fetchone())
    except sqlite3.IntegrityError as e:
        # Segunda barrera: los triggers de RN-02 en la base.
        if "SLOT_UNAVAILABLE" in str(e):
            raise ErrorDominio("SLOT_UNAVAILABLE", "Ese horario ya fue reservado.")
        raise


# ---------------------------------------------------------------- POST /api/turnos/{id}/cancelar (§11.3)
def cancelar_turno(conn, actor, turno_id: str, motivo: str | None, ahora: datetime | None = None) -> dict:
    with db.transaccion(conn):
        ahora = ahora or ahora_utc()
        t = conn.execute("SELECT * FROM turno WHERE id = ?", (turno_id,)).fetchone()
        if t is None or not _visible(actor, t):
            raise ErrorDominio("NOT_FOUND", "El recurso no existe o no tenés acceso a él.")
        motivo = validar_cancelacion(t["estado"], de_iso(t["inicio"]), ahora, actor["rol"], motivo)  # RN-01, RN-06
        conn.execute(
            "UPDATE turno SET estado = 'CANCELADO', motivo_cancelacion = ?, cancelado_por_id = ?, actualizado_en = ?"
            " WHERE id = ?", (motivo, actor["id"], a_iso(ahora), turno_id))
        _auditar(conn, actor, "TURNO_CANCELADO", "Turno", turno_id,
                 f"Motivo: {motivo}" if motivo else "Cancelado por el cliente", ahora)               # RN-08
        return turno_json(conn.execute("SELECT * FROM turno WHERE id = ?", (turno_id,)).fetchone())


# ---------------------------------------------------------------- lecturas de §11.5
def catalogo(conn, actor) -> dict:
    out = {
        "servicios": [servicio_json(r) for r in conn.execute("SELECT * FROM servicio ORDER BY nombre")],
        "profesionales": [{"id": r["id"], "nombre_completo": r["nombre_completo"]} for r in conn.execute(
            "SELECT id, nombre_completo FROM usuario WHERE rol = 'PROFESIONAL' AND activo = 1 ORDER BY nombre_completo")],
        "horarios": [dict(r) for r in conn.execute("SELECT * FROM horario_laboral ORDER BY profesional_id, dia_semana, hora_inicio")],
    }
    if actor is not None and actor["rol"] in ("PROFESIONAL", "ADMIN"):
        sql, args = "SELECT * FROM bloqueo", []
        if actor["rol"] == "PROFESIONAL":
            sql += " WHERE profesional_id = ?"; args.append(actor["id"])
        out["bloqueos"] = [dict(r) for r in conn.execute(sql + " ORDER BY inicio", args)]
    return out


def disponibilidad(conn, servicio_id: str | None, fecha_txt: str | None, ahora: datetime | None = None) -> list[dict]:
    ahora = ahora or ahora_utc()
    svc = conn.execute("SELECT * FROM servicio WHERE id = ?", (servicio_id,)).fetchone()
    if svc is None or not svc["activo"]:
        raise ErrorDominio("SERVICE_UNAVAILABLE", "El servicio no está disponible.")
    try:
        fecha = date.fromisoformat(fecha_txt or "")
    except ValueError:
        raise ErrorDominio("INVALID_TIME_RANGE", "La fecha debe tener formato AAAA-MM-DD.")
    hoy = ahora.astimezone(config.ZONA_OPERACION).date()
    if fecha < hoy:
        raise ErrorDominio("INVALID_TIME_RANGE", "Esa fecha ya pasó.")
    if fecha > hoy + timedelta(days=config.HORIZON_DAYS):
        raise ErrorDominio("INVALID_TIME_RANGE", f"Solo se puede reservar hasta {config.HORIZON_DAYS} días hacia adelante.")

    prof = svc["profesional_id"]
    horarios = [(h["dia_semana"], h["hora_inicio"], h["hora_fin"]) for h in
                conn.execute("SELECT * FROM horario_laboral WHERE profesional_id = ?", (prof,))]
    ocupados = [(de_iso(r["inicio"]), de_iso(r["fin"])) for r in conn.execute(
        "SELECT inicio, fin FROM bloqueo WHERE profesional_id = ?"
        " UNION ALL SELECT inicio, fin FROM turno WHERE profesional_id = ? AND estado = 'CONFIRMADO'", (prof, prof))]
    return [{"inicio": a_iso(i), "fin": a_iso(f)} for i, f in slots_del_dia(fecha, svc["duracion_min"], horarios, ocupados, ahora)]


def usuarios_visibles(conn, actor) -> list[dict]:
    """RBAC-04."""
    if actor is None:
        raise ErrorDominio("FORBIDDEN", "Iniciá sesión para ver usuarios.")
    if actor["rol"] == "ADMIN":
        filas = conn.execute("SELECT * FROM usuario ORDER BY rol, nombre_completo")
    elif actor["rol"] == "PROFESIONAL":
        filas = conn.execute(
            "SELECT * FROM usuario WHERE id = ? OR id IN (SELECT cliente_id FROM turno WHERE profesional_id = ?)"
            " ORDER BY rol, nombre_completo", (actor["id"], actor["id"]))
    else:
        filas = conn.execute("SELECT * FROM usuario WHERE id = ?", (actor["id"],))
    return [usuario_json(r) for r in filas]


def auditoria(conn, actor) -> list[dict]:
    if actor is None or actor["rol"] == "CLIENTE":
        raise ErrorDominio("FORBIDDEN", "No tenés permiso para ver la auditoría.")
    sql, args = "SELECT * FROM auditoria_evento", []
    if actor["rol"] == "PROFESIONAL":
        sql += (" WHERE actor_id = ? OR (entidad = 'Turno' AND entidad_id IN"
                " (SELECT id FROM turno WHERE profesional_id = ?))")
        args = [actor["id"], actor["id"]]
    return [dict(r) for r in conn.execute(sql + " ORDER BY ocurrido_en DESC, rowid DESC", args)]


def usuarios_dev(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, nombre_completo, rol FROM usuario WHERE activo = 1 ORDER BY rol, nombre_completo")]
