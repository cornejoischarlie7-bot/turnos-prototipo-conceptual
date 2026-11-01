"""Casos de uso del backend (spec.md §11.3 – §11.13). Une el dominio puro con la base de datos.

Cada función recibe la conexión y el actor (fila de `usuario`, ya autenticado). La hora
sale de `reloj.ahora()`, que los tests reemplazan para probar las reglas temporales.
"""
import sqlite3
import uuid
from datetime import date, datetime, timedelta

import config
import db
import reloj
from domain import (ErrorDominio, a_iso, calcular_fin, calcular_metricas, de_iso, dentro_de_horario,
                    minutos_disponibles, slots_del_dia, validar_cancelacion, validar_cierre, validar_contacto,
                    validar_franjas, validar_inicio)

# Representaciones de §11.2 y §11.14: exactamente los atributos de §4.1.
CAMPOS_TURNO = ("id", "cliente_id", "profesional_id", "servicio_id", "inicio", "fin", "estado",
                "motivo_cancelacion", "cancelado_por_id", "creado_en", "actualizado_en")
CAMPOS_SERVICIO = ("id", "profesional_id", "nombre", "descripcion", "duracion_min", "precio_referencia", "activo")
CAMPOS_HORARIO = ("id", "profesional_id", "dia_semana", "hora_inicio", "hora_fin")
CAMPOS_BLOQUEO = ("id", "profesional_id", "inicio", "fin", "motivo")
CAMPOS_AUDITORIA = ("id", "entidad", "entidad_id", "accion", "actor_id", "rol", "ocurrido_en", "detalle")


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


# ================================================================ v2.1.0 (spec §11.6 – §11.13)
def _fila(r: sqlite3.Row, campos: tuple[str, ...]) -> dict:
    return {k: r[k] for k in campos}


def servicio_json(r: sqlite3.Row) -> dict:
    d = _fila(r, CAMPOS_SERVICIO)
    d["activo"] = bool(d["activo"])
    return d


def _profesional_en_alcance(conn, actor, profesional_id: str) -> None:
    """RBAC (§2.2): CLIENTE → 403; un PROFESIONAL solo sobre sí mismo (otro → 404, RBAC-02)."""
    if actor["rol"] == "CLIENTE":
        raise ErrorDominio("FORBIDDEN", "No tenés permiso para esta acción.")
    if actor["rol"] == "PROFESIONAL" and profesional_id != actor["id"]:
        raise ErrorDominio("NOT_FOUND", "El profesional no existe o no tenés acceso a él.")
    if conn.execute("SELECT 1 FROM usuario WHERE id = ? AND rol = 'PROFESIONAL'", (profesional_id,)).fetchone() is None:
        raise ErrorDominio("NOT_FOUND", "El profesional no existe.")


def _horarios_de(conn, profesional_id: str) -> list[tuple[int, str, str]]:
    return [(h["dia_semana"], h["hora_inicio"], h["hora_fin"]) for h in
            conn.execute("SELECT * FROM horario_laboral WHERE profesional_id = ?", (profesional_id,))]


# ---------------------------------------------------------------- §11.6 – §11.7 servicios
def listar_servicios(conn) -> list[dict]:
    return [servicio_json(r) for r in conn.execute("SELECT * FROM servicio ORDER BY nombre")]


def cambiar_activo_servicio(conn, actor, servicio_id: str, activo: bool) -> dict:
    """RN-07: baja lógica; los turnos ya confirmados no cambian."""
    if actor["rol"] == "CLIENTE":
        raise ErrorDominio("FORBIDDEN", "No tenés permiso para esta acción.")
    with db.transaccion(conn):
        svc = conn.execute("SELECT * FROM servicio WHERE id = ?", (servicio_id,)).fetchone()
        if svc is None or (actor["rol"] == "PROFESIONAL" and svc["profesional_id"] != actor["id"]):
            raise ErrorDominio("NOT_FOUND", "El servicio no existe o no tenés acceso a él.")
        if bool(svc["activo"]) != activo:
            conn.execute("UPDATE servicio SET activo = ? WHERE id = ?", (int(activo), servicio_id))
            _auditar(conn, actor, "SERVICIO_ACTIVADO" if activo else "SERVICIO_DESACTIVADO", "Servicio", servicio_id,
                     svc["nombre"], reloj.ahora())
        return servicio_json(conn.execute("SELECT * FROM servicio WHERE id = ?", (servicio_id,)).fetchone())


# ---------------------------------------------------------------- §11.8 horario laboral
def listar_horarios(conn, profesional_id: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM horario_laboral", []
    if profesional_id:
        sql += " WHERE profesional_id = ?"; args.append(profesional_id)
    return [_fila(r, CAMPOS_HORARIO) for r in conn.execute(sql + " ORDER BY profesional_id, dia_semana, hora_inicio", args)]


def reemplazar_horario(conn, actor, profesional_id: str, franjas: list[tuple[int, str, str]]) -> list[dict]:
    """F-08. D-09: los turnos ya confirmados no se modifican."""
    validar_franjas(franjas)
    with db.transaccion(conn):
        _profesional_en_alcance(conn, actor, profesional_id)
        conn.execute("DELETE FROM horario_laboral WHERE profesional_id = ?", (profesional_id,))
        for d, a, b in sorted(franjas):
            conn.execute("INSERT INTO horario_laboral (id, profesional_id, dia_semana, hora_inicio, hora_fin)"
                         " VALUES (?, ?, ?, ?, ?)", (str(uuid.uuid4()), profesional_id, d, a, b))
        detalle = ", ".join(f"{d}:{a}-{b}" for d, a, b in sorted(franjas)) or "sin franjas"
        _auditar(conn, actor, "HORARIO_ACTUALIZADO", "HorarioLaboral", profesional_id, detalle, reloj.ahora())
        return listar_horarios(conn, profesional_id)


# ---------------------------------------------------------------- §11.9 disponibilidad
def disponibilidad(conn, servicio_id: str, fecha_txt: str) -> list[dict]:
    """F-03 con los turnos CONFIRMADO de todos los clientes, sin exponer quién reservó (RBAC-04)."""
    try:
        fecha = date.fromisoformat(fecha_txt)
    except ValueError:
        raise ErrorDominio("VALIDATION_ERROR", "La fecha debe tener formato AAAA-MM-DD.")
    ahora = reloj.ahora()
    svc = conn.execute("SELECT * FROM servicio WHERE id = ?", (servicio_id,)).fetchone()
    if svc is None:
        raise ErrorDominio("NOT_FOUND", "El servicio no existe.")
    if not svc["activo"]:
        raise ErrorDominio("SERVICE_UNAVAILABLE", "El servicio no está disponible.")
    hoy = ahora.astimezone(config.ZONA_OPERACION).date()
    if fecha < hoy:
        raise ErrorDominio("INVALID_TIME_RANGE", "Esa fecha ya pasó.")
    if fecha > hoy + timedelta(days=config.HORIZON_DAYS):
        raise ErrorDominio("INVALID_TIME_RANGE", f"Solo se puede reservar hasta {config.HORIZON_DAYS} días hacia adelante.")
    prof = svc["profesional_id"]
    ocupados = [(de_iso(r["inicio"]), de_iso(r["fin"])) for r in conn.execute(
        "SELECT inicio, fin FROM bloqueo WHERE profesional_id = ?"
        " UNION ALL SELECT inicio, fin FROM turno WHERE profesional_id = ? AND estado = 'CONFIRMADO'", (prof, prof))]
    return [{"inicio": a_iso(i), "fin": a_iso(f)}
            for i, f in slots_del_dia(fecha, svc["duracion_min"], _horarios_de(conn, prof), ocupados, ahora)]


# ---------------------------------------------------------------- §11.10 bloqueos
def listar_bloqueos(conn, actor, profesional_id: str | None = None) -> list[dict]:
    if actor["rol"] == "CLIENTE":
        raise ErrorDominio("FORBIDDEN", "No tenés permiso para ver bloqueos.")
    sql, args = "SELECT * FROM bloqueo WHERE 1 = 1", []
    if actor["rol"] == "PROFESIONAL":
        sql += " AND profesional_id = ?"; args.append(actor["id"])
    if profesional_id:
        sql += " AND profesional_id = ?"; args.append(profesional_id)
    return [_fila(r, CAMPOS_BLOQUEO) for r in conn.execute(sql + " ORDER BY inicio", args)]


def crear_bloqueo(conn, actor, profesional_id: str, inicio_txt: str, fin_txt: str, motivo: str | None,
                  confirmar: bool) -> dict:
    """RN-05: con turnos en el rango, lista los conflictos o (confirmado) los cancela en la misma transacción."""
    inicio, fin = de_iso(inicio_txt), de_iso(fin_txt)
    motivo = (motivo or "").strip() or None
    with db.transaccion(conn):
        ahora = reloj.ahora()
        _profesional_en_alcance(conn, actor, profesional_id)
        if not inicio < fin:
            raise ErrorDominio("INVALID_TIME_RANGE", "El fin del bloqueo debe ser posterior al inicio.")
        if fin <= ahora:
            raise ErrorDominio("INVALID_TIME_RANGE", "No se puede bloquear un rango que ya terminó.")
        ini_s, fin_s = a_iso(inicio), a_iso(fin)
        conflictos = conn.execute(
            "SELECT * FROM turno WHERE profesional_id = ? AND estado = 'CONFIRMADO' AND inicio < ? AND ? < fin"
            " ORDER BY inicio", (profesional_id, fin_s, ini_s)).fetchall()
        if any(de_iso(t["inicio"]) <= ahora for t in conflictos):
            raise ErrorDominio("INVALID_STATE_TRANSITION", "Hay un turno en curso dentro del rango; cerralo antes de bloquear.")
        if conflictos and not confirmar:
            raise ErrorDominio("SLOT_UNAVAILABLE",
                               f"El rango tiene {len(conflictos)} turno(s) confirmado(s). Confirmá su cancelación para crear el bloqueo.",
                               {"conflictos": [turno_json(t) for t in conflictos]})
        if conflictos and not motivo:
            raise ErrorDominio("REASON_REQUIRED", "El motivo es obligatorio para cancelar los turnos afectados.")
        ahora_s = a_iso(ahora)
        for t in conflictos:                                               # excepción de RN-01
            conn.execute("UPDATE turno SET estado = 'CANCELADO', motivo_cancelacion = ?, cancelado_por_id = ?,"
                         " actualizado_en = ? WHERE id = ?", (motivo, actor["id"], ahora_s, t["id"]))
            _auditar(conn, actor, "TURNO_CANCELADO", "Turno", t["id"], f"Motivo: {motivo} (bloqueo)", ahora)
        bloqueo_id = str(uuid.uuid4())
        conn.execute("INSERT INTO bloqueo (id, profesional_id, inicio, fin, motivo) VALUES (?, ?, ?, ?, ?)",
                     (bloqueo_id, profesional_id, ini_s, fin_s, motivo))
        _auditar(conn, actor, "BLOQUEO_CREADO", "Bloqueo", bloqueo_id,
                 f"{ini_s} → {fin_s}" + (f" · {motivo}" if motivo else "") +
                 (f" · {len(conflictos)} turno(s) cancelado(s)" if conflictos else ""), ahora)
        return {
            "bloqueo": _fila(conn.execute("SELECT * FROM bloqueo WHERE id = ?", (bloqueo_id,)).fetchone(), CAMPOS_BLOQUEO),
            "turnos_cancelados": [turno_json(conn.execute("SELECT * FROM turno WHERE id = ?", (t["id"],)).fetchone())
                                  for t in conflictos],
        }


def eliminar_bloqueo(conn, actor, bloqueo_id: str) -> dict:
    if actor["rol"] == "CLIENTE":
        raise ErrorDominio("FORBIDDEN", "No tenés permiso para esta acción.")
    with db.transaccion(conn):
        b = conn.execute("SELECT * FROM bloqueo WHERE id = ?", (bloqueo_id,)).fetchone()
        if b is None or (actor["rol"] == "PROFESIONAL" and b["profesional_id"] != actor["id"]):
            raise ErrorDominio("NOT_FOUND", "El bloqueo no existe o no tenés acceso a él.")
        conn.execute("DELETE FROM bloqueo WHERE id = ?", (bloqueo_id,))
        _auditar(conn, actor, "BLOQUEO_ELIMINADO", "Bloqueo", bloqueo_id, f"{b['inicio']} → {b['fin']}", reloj.ahora())
        return _fila(b, CAMPOS_BLOQUEO)


# ---------------------------------------------------------------- §11.11 completar / ausente
def cerrar_turno(conn, actor, turno_id: str, nuevo: str) -> dict:
    if actor["rol"] == "CLIENTE":
        raise ErrorDominio("FORBIDDEN", "No tenés permiso para esta acción.")
    with db.transaccion(conn):
        ahora = reloj.ahora()
        t = conn.execute("SELECT * FROM turno WHERE id = ?", (turno_id,)).fetchone()
        if t is None or not _visible(actor, t):
            raise ErrorDominio("NOT_FOUND", "El turno no existe o no tenés acceso a él.")
        validar_cierre(t["estado"], de_iso(t["inicio"]), de_iso(t["fin"]), ahora, nuevo)     # RN-06
        conn.execute("UPDATE turno SET estado = ?, actualizado_en = ? WHERE id = ?", (nuevo, a_iso(ahora), turno_id))
        _auditar(conn, actor, f"TURNO_{nuevo}", "Turno", turno_id, "", ahora)                  # RN-08
        return turno_json(conn.execute("SELECT * FROM turno WHERE id = ?", (turno_id,)).fetchone())


# ---------------------------------------------------------------- §11.12 auditoría
def auditoria(conn, actor) -> list[dict]:
    if actor["rol"] == "CLIENTE":
        raise ErrorDominio("FORBIDDEN", "No tenés permiso para ver la auditoría.")
    sql, args = "SELECT * FROM auditoria_evento", []
    if actor["rol"] == "PROFESIONAL":
        sql += (" WHERE actor_id = ? OR (entidad = 'Turno' AND entidad_id IN"
                " (SELECT id FROM turno WHERE profesional_id = ?))")
        args = [actor["id"], actor["id"]]
    return [_fila(r, CAMPOS_AUDITORIA) for r in conn.execute(sql + " ORDER BY ocurrido_en DESC, rowid DESC", args)]


# ---------------------------------------------------------------- §11.13 métricas
def metricas(conn, actor, desde_txt: str, hasta_txt: str, profesional_id: str | None = None) -> dict:
    if actor["rol"] == "CLIENTE":
        raise ErrorDominio("FORBIDDEN", "No tenés permiso para ver métricas.")
    desde, hasta = de_iso(desde_txt), de_iso(hasta_txt)
    if not desde < hasta or hasta - desde > timedelta(days=366):
        raise ErrorDominio("INVALID_TIME_RANGE", "El período debe tener desde < hasta y durar a lo sumo 366 días.")
    if actor["rol"] == "PROFESIONAL":
        if profesional_id and profesional_id != actor["id"]:
            raise ErrorDominio("NOT_FOUND", "El profesional no existe o no tenés acceso a él.")
        profesional_id = actor["id"]
    if profesional_id:
        if conn.execute("SELECT 1 FROM usuario WHERE id = ? AND rol = 'PROFESIONAL'", (profesional_id,)).fetchone() is None:
            raise ErrorDominio("NOT_FOUND", "El profesional no existe.")
        profs = [profesional_id]
    else:
        profs = [r["id"] for r in conn.execute("SELECT id FROM usuario WHERE rol = 'PROFESIONAL'")]
    marcas = ",".join("?" * len(profs)) or "NULL"
    filas = conn.execute(f"SELECT * FROM turno WHERE profesional_id IN ({marcas}) AND inicio >= ? AND inicio < ?",
                         (*profs, a_iso(desde), a_iso(hasta))).fetchall()
    turnos = [{"estado": r["estado"], "inicio": de_iso(r["inicio"]), "fin": de_iso(r["fin"]),
               "servicio_id": r["servicio_id"], "cancelado_por_id": r["cancelado_por_id"]} for r in filas]
    disponibles = sum(minutos_disponibles(desde, hasta, _horarios_de(conn, p), [
        (de_iso(b["inicio"]), de_iso(b["fin"])) for b in conn.execute(
            "SELECT inicio, fin FROM bloqueo WHERE profesional_id = ?", (p,))]) for p in profs)
    rol_de = {r["id"]: r["rol"] for r in conn.execute("SELECT id, rol FROM usuario")}
    return {"desde": a_iso(desde), "hasta": a_iso(hasta), **calcular_metricas(turnos, disponibles, rol_de)}
