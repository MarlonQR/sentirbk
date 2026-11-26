import math
import os
from typing import Annotated, Literal, Optional
from uuid import UUID

import psycopg2
import requests
from fastapi import Depends, FastAPI, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator, model_validator

from con_db import transaccion
from seguridad import ErrorApi, Usuario, fecha_iso, registrar_manejadores, requiere_rol


app = FastAPI(
    title="SENTIR - Servicio de Recursos",
    description=(
        "Unidades de respuesta (brigadas, vehículos, equipos), el inventario, la asignación "
        "de unidades a una solicitud y los pedidos de apoyo entre operadores.\n\n"
        "Asignar varias unidades es todo o nada: si una no está disponible, no se asigna ninguna.\n\n"
        "Este servicio consulta a Asistencia el estado, el responsable y la ubicación de cada "
        "solicitud, y publica `apoyo.solicitado` en Alertas."
    ),
    version="0.2.0",
    openapi_tags=[
        {"name": "Servicio", "description": "Verificación de que el servicio responde."},
        {"name": "Inventario", "description": "Pantalla 4. Operador o administrador."},
        {"name": "Asignaciones", "description": "Unidades enviadas a una solicitud."},
        {"name": "Apoyo", "description": "Pantalla 5: pedidos de refuerzo entre operadores."},
        {"name": "Administración", "description": "Registro y estado de las unidades. Solo el rol administrador."},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
registrar_manejadores(app)


# ==================================================
# Reglas
# ==================================================

Categoria = Literal["brigada", "vehiculo_socorro", "equipo_tactico", "equipamiento_rescate"]
Especialidad = Literal["rescate_busqueda", "atencion_medica", "control_incendio", "logistica_traslado"]
EstadoUnidad = Literal["disponible", "ocupada", "mantenimiento", "fuera_servicio"]
EstadoManual = Literal["disponible", "mantenimiento", "fuera_servicio"]
Prioridad = Literal["critico", "alto", "medio", "bajo"]
CATEGORIAS = ("brigada", "vehiculo_socorro", "equipo_tactico", "equipamiento_rescate")
ESTADOS = ("disponible", "ocupada", "mantenimiento", "fuera_servicio")
VELOCIDAD_KMH = 30          # velocidad promedio supuesta en ciudad (H15)
MAX_UNIDADES = 10           # por asignación

Operador = Annotated[Usuario, Depends(requiere_rol("operador"))]
Administrador = Annotated[Usuario, Depends(requiere_rol("administrador"))]
OperadorOAdmin = Annotated[Usuario, Depends(requiere_rol("operador", "administrador"))]


def distancia_km(lat1, lon1, lat2, lon2) -> float:
    """Distancia en línea recta (fórmula del haversine)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ==================================================
# Otros servicios
# ==================================================

def _clave() -> str:
    return os.getenv("CLAVE_SERVICIO", "").strip()


def consultar_solicitud(solicitud_id: str) -> dict:
    """Pide a Asistencia estado, responsable y ubicación de la solicitud."""
    url = os.getenv("ASISTENCIA_URL", "").strip().rstrip("/")
    if not url:
        raise ErrorApi(500, "CONFIGURACION_INCOMPLETA", "El servicio no tiene configurada la variable ASISTENCIA_URL.")
    try:
        r = requests.get(f"{url}/v1/interno/solicitudes/{solicitud_id}",
                         headers={"X-Clave-Servicio": _clave()}, timeout=(3, 8))
    except requests.RequestException as error:
        print(f"Asistencia no respondió: {error}")
        raise ErrorApi(503, "SERVICIO_NO_DISPONIBLE", "No fue posible consultar la solicitud en Asistencia.")
    if r.status_code == 404:
        raise ErrorApi(404, "NO_ENCONTRADO", "No existe una solicitud con ese id.")
    if r.status_code != 200:
        print(f"Asistencia respondió {r.status_code}: {r.text[:200]}")
        raise ErrorApi(503, "SERVICIO_NO_DISPONIBLE", "No fue posible consultar la solicitud en Asistencia.")
    return r.json()


def ubicacion_operador(operador_id: str) -> Optional[dict]:
    """La ubicación de un operador es la de la emergencia que atiende. Sin GPS.
    Si no atiende ninguna o Asistencia no responde, devuelve None y la lista sale sin distancias."""
    url = os.getenv("ASISTENCIA_URL", "").strip().rstrip("/")
    if not url:
        return None
    try:
        r = requests.get(f"{url}/v1/interno/operadores/{operador_id}/solicitud-activa",
                         headers={"X-Clave-Servicio": _clave()}, timeout=(3, 8))
        datos = r.json() if r.status_code == 200 else {}
    except (requests.RequestException, ValueError) as error:
        print(f"No se pudo consultar la emergencia del operador: {error}")
        return None
    if not datos.get("activa") or datos["ubicacion"]["latitud"] is None:
        return None
    return {"solicitud_id": datos["solicitud_id"], "codigo": datos["codigo"], **datos["ubicacion"]}


def exigir_en_atencion(solicitud: dict, operador: Optional[Usuario] = None):
    if solicitud["estado"] != "en_atencion":
        raise ErrorApi(409, "SOLICITUD_NO_EN_ATENCION",
                       f"La solicitud {solicitud['codigo']} está {solicitud['estado']}; solo se le asignan unidades en atención.")
    if operador is not None and solicitud["responsable_id"] != operador.id:
        raise ErrorApi(403, "NO_ES_RESPONSABLE", "Solo el operador responsable de la solicitud puede hacer esta operación.")


def publicar_evento(nombre: str, datos: dict):
    url = os.getenv("ALERTAS_URL", "").strip().rstrip("/")
    if not url:
        return
    try:
        r = requests.post(f"{url}/v1/interno/eventos", json={"nombre": nombre, "datos": datos},
                          headers={"X-Clave-Servicio": _clave()}, timeout=(2, 3))
        if r.status_code >= 300:
            print(f"Alertas respondió {r.status_code} al publicar {nombre}")
    except requests.RequestException as error:
        print(f"No se pudo publicar {nombre} en Alertas: {error}")


# ==================================================
# Lectura
# ==================================================

COLUMNAS_UNIDAD = "id, nombre, categoria, especialidades, personal, estado, base_latitud, base_longitud, base_direccion"


def _num(valor):
    return float(valor) if valor is not None else None


def unidad_json(fila) -> dict:
    id_, nombre, categoria, especialidades, personal, estado, lat, lon, direccion = fila
    return {
        "id": str(id_), "nombre": nombre, "categoria": categoria, "especialidades": list(especialidades),
        "personal": personal, "estado": estado,
        "base": {"latitud": _num(lat), "longitud": _num(lon), "direccion": direccion},
    }


def cargar_asignacion(cur, asignacion_id: str) -> dict:
    cur.execute(
        """SELECT id, solicitud_id, codigo_solicitud, solicitud_apoyo_id, estado, creada_por_id, creada_por_nombre,
                  creada_en, finalizada_por_id, finalizada_por_nombre, finalizada_en
           FROM recursos.asignaciones WHERE id = %s""",
        (asignacion_id,),
    )
    (id_, sol, codigo, apoyo, estado, cp_id, cp_nombre, creada, fp_id, fp_nombre, finalizada) = cur.fetchone()
    cur.execute(
        f"""SELECT {', '.join('u.' + c.strip() for c in COLUMNAS_UNIDAD.split(','))}
            FROM recursos.asignacion_unidades au JOIN recursos.unidades u ON u.id = au.unidad_id
            WHERE au.asignacion_id = %s ORDER BY u.nombre""",
        (asignacion_id,),
    )
    return {
        "id": str(id_), "solicitud_id": str(sol), "codigo_solicitud": codigo,
        "solicitud_apoyo_id": str(apoyo) if apoyo else None, "estado": estado,
        "unidades": [unidad_json(f) for f in cur.fetchall()],
        "creada_por": {"id": str(cp_id), "nombre_completo": cp_nombre}, "creada_en": fecha_iso(creada),
        "finalizada_por": {"id": str(fp_id), "nombre_completo": fp_nombre} if fp_id else None,
        "finalizada_en": fecha_iso(finalizada),
    }


COLUMNAS_APOYO = """id, solicitud_id, codigo_solicitud, tipos_apoyo, personal_adicional, personal_asignado, prioridad,
    mensaje, estado, solicitante_id, solicitante_nombre, creada_en, cerrada_por_id, cerrada_por_nombre, cerrada_en,
    solicitud_latitud, solicitud_longitud, solicitud_direccion"""


def apoyo_json(fila) -> dict:
    (id_, sol, codigo, tipos, requerido, asignado, prioridad, mensaje, estado, s_id, s_nombre, creada,
     c_id, c_nombre, cerrada, lat, lon, direccion) = fila
    return {
        "id": str(id_), "solicitud_id": str(sol), "codigo_solicitud": codigo, "tipos_apoyo": list(tipos),
        "personal_adicional": requerido, "personal_asignado": asignado, "faltan": max(0, requerido - asignado),
        "prioridad": prioridad, "mensaje": mensaje, "estado": estado,
        "ubicacion": {"latitud": _num(lat), "longitud": _num(lon), "direccion": direccion},
        "solicitante": {"id": str(s_id), "nombre_completo": s_nombre}, "creada_en": fecha_iso(creada),
        "cerrada_por": {"id": str(c_id), "nombre_completo": c_nombre} if c_id else None, "cerrada_en": fecha_iso(cerrada),
    }


def cargar_apoyos(cur, filas, operador_id: str, referencia: Optional[dict] = None) -> list:
    """Agrega a cada pedido quiénes aceptaron, si el operador actual ya lo hizo y, si el operador
    atiende una emergencia, a qué distancia está de ella."""
    apoyos = [apoyo_json(f) for f in filas]
    for a in apoyos:
        u = a["ubicacion"]
        a["distancia_km"] = (round(distancia_km(referencia["latitud"], referencia["longitud"], u["latitud"], u["longitud"]), 1)
                             if referencia and u["latitud"] is not None else None)
    if not apoyos:
        return apoyos
    cur.execute(
        """SELECT apoyo_id, operador_id, operador_nombre, personas, asignacion_id, fecha
           FROM recursos.aceptaciones_apoyo WHERE apoyo_id = ANY(%s::uuid[]) ORDER BY fecha, id""",
        ([a["id"] for a in apoyos],),
    )
    por_apoyo = {}
    for apoyo_id, op_id, op_nombre, personas, asignacion_id, fecha in cur.fetchall():
        por_apoyo.setdefault(str(apoyo_id), []).append({
            "operador": {"id": str(op_id), "nombre_completo": op_nombre}, "personas": personas,
            "asignacion_id": str(asignacion_id) if asignacion_id else None, "fecha": fecha_iso(fecha),
        })
    for a in apoyos:
        a["aceptaciones"] = por_apoyo.get(a["id"], [])
        a["aceptado_por_mi"] = any(x["operador"]["id"] == operador_id for x in a["aceptaciones"])
    return apoyos


def evento_apoyo(apoyo: dict) -> dict:
    return {k: apoyo[k] for k in ("id", "solicitud_id", "codigo_solicitud", "personal_adicional",
                                  "personal_asignado", "faltan", "estado")}


def crear_asignacion(cur, solicitud: dict, unidad_ids: list, operador: Usuario, apoyo_id: Optional[str] = None) -> str:
    """Pasa todas las unidades a ocupada o ninguna. Debe correr dentro de una transacción."""
    ids = list(dict.fromkeys(str(u) for u in unidad_ids))
    cur.execute(
        "UPDATE recursos.unidades SET estado = 'ocupada', actualizado_en = now() WHERE id = ANY(%s::uuid[]) AND estado = 'disponible' RETURNING id",
        (ids,),
    )
    ocupadas = {str(f[0]) for f in cur.fetchall()}
    if len(ocupadas) != len(ids):
        faltantes = [i for i in ids if i not in ocupadas]
        cur.execute("SELECT id, nombre, estado FROM recursos.unidades WHERE id = ANY(%s::uuid[])", (faltantes,))
        conocidas = {str(i): (n, e) for i, n, e in cur.fetchall()}
        detalles = [{"id": i, "nombre": conocidas[i][0] if i in conocidas else None,
                     "estado": conocidas[i][1] if i in conocidas else "no_existe"} for i in faltantes]
        # El ErrorApi deshace la transacción: las unidades que sí se marcaron vuelven a disponible.
        raise ErrorApi(409, "UNIDAD_NO_DISPONIBLE", "Alguna unidad no está disponible; no se asignó ninguna.",
                       {"no_disponibles": detalles})
    cur.execute(
        """INSERT INTO recursos.asignaciones (solicitud_id, codigo_solicitud, solicitud_apoyo_id, creada_por_id, creada_por_nombre)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (solicitud["id"], solicitud["codigo"], apoyo_id, operador.id, operador.nombre),
    )
    asignacion_id = str(cur.fetchone()[0])
    cur.executemany("INSERT INTO recursos.asignacion_unidades (asignacion_id, unidad_id) VALUES (%s, %s)",
                    [(asignacion_id, i) for i in ids])
    return asignacion_id


# ==================================================
# Modelos
# ==================================================

class Base(BaseModel):
    latitud: Optional[float] = Field(None, ge=-90, le=90, examples=[7.888611])
    longitud: Optional[float] = Field(None, ge=-180, le=180, examples=[-72.496944])
    direccion: Optional[str] = Field(None, max_length=300, examples=["Estación de Bomberos, Av. 0, Cúcuta"])

    @model_validator(mode="after")
    def _pareja(self):
        if (self.latitud is None) != (self.longitud is None):
            raise ValueError("latitud y longitud van juntas")
        return self


class NuevaUnidad(BaseModel):
    nombre: str = Field(min_length=3, max_length=80, examples=["Brigada Central"])
    categoria: Categoria
    especialidades: list[Especialidad] = Field(default_factory=list, max_length=4)
    personal: int = Field(0, ge=0, le=500)
    base: Base = Field(default_factory=Base)

    @field_validator("nombre")
    @classmethod
    def _limpiar(cls, valor: str) -> str:
        return " ".join(valor.split())


class CambioUnidad(BaseModel):
    nombre: Optional[str] = Field(None, min_length=3, max_length=80)
    especialidades: Optional[list[Especialidad]] = Field(None, max_length=4)
    personal: Optional[int] = Field(None, ge=0, le=500)
    base: Optional[Base] = None
    estado: Optional[EstadoManual] = Field(None, description="ocupada no se pone a mano: cambia al asignar y liberar.")


class NuevaAsignacion(BaseModel):
    solicitud_id: UUID
    unidad_ids: list[UUID] = Field(min_length=1, max_length=MAX_UNIDADES)


class CambioAsignacion(BaseModel):
    estado: Literal["finalizada"]


class NuevoApoyo(BaseModel):
    solicitud_id: UUID
    tipos_apoyo: list[Especialidad] = Field(min_length=1, max_length=4, examples=[["rescate_busqueda", "logistica_traslado"]])
    personal_adicional: int = Field(ge=1, le=100, examples=[3], description="Personas que faltan. Son los cupos del pedido.")
    prioridad: Prioridad = Field(examples=["alto"])
    mensaje: Optional[str] = Field(None, max_length=500, examples=["Se necesita equipo con herramientas de corte, hay dos personas atrapadas."])


class CambioApoyo(BaseModel):
    estado: Literal["cancelada"]


class Aceptacion(BaseModel):
    personas: int = Field(1, ge=1, le=100, description="Personas con las que se suma. Es lo único que cuenta para los cupos.")
    unidad_ids: Optional[list[UUID]] = Field(None, min_length=1, max_length=MAX_UNIDADES,
                                             description="Opcional: vehículos o equipos que también envía. No cambian el conteo de personas.")


# ==================================================
# Servicio e inventario
# ==================================================

@app.get("/", tags=["Servicio"], summary="Estado del servicio")
def estado():
    return {"servicio": "recursos", "estado": "activo", "version": app.version}


@app.get("/v1/inventario", tags=["Inventario"], summary="Totales por categoría y estado (pantalla 4)")
def inventario(usuario: OperadorOAdmin):
    with transaccion() as cur:
        cur.execute("SELECT categoria, estado, count(*), coalesce(sum(personal), 0) FROM recursos.unidades GROUP BY categoria, estado")
        filas = cur.fetchall()
    por_estado = {e: 0 for e in ESTADOS}
    por_categoria = {c: {"categoria": c, "total": 0, **{e: 0 for e in ESTADOS}} for c in CATEGORIAS}
    personal_total = personal_disponible = 0
    for categoria, est, cantidad, personal in filas:
        por_estado[est] += cantidad
        por_categoria[categoria]["total"] += cantidad
        por_categoria[categoria][est] += cantidad
        personal_total += personal
        if est == "disponible":
            personal_disponible += personal
    total = sum(por_estado.values())
    operativas = por_estado["disponible"] + por_estado["ocupada"]
    return {
        "total_unidades": total,
        "operativas": operativas,
        "porcentaje_operativo": round(100 * operativas / total) if total else 0,
        "por_estado": por_estado,
        "por_categoria": list(por_categoria.values()),
        "personal": {"total": personal_total, "disponible": personal_disponible},
    }


@app.get("/v1/unidades", tags=["Inventario"], summary="Listar unidades, con distancia a una solicitud")
def listar_unidades(
    usuario: OperadorOAdmin,
    categoria: Optional[Categoria] = None,
    estado: Optional[EstadoUnidad] = None,
    especialidad: Optional[Especialidad] = None,
    cerca_de: Annotated[Optional[UUID], Query(description="Id de una solicitud: ordena por distancia a ella.")] = None,
    pagina: Annotated[int, Query(ge=1)] = 1,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
):
    condiciones, parametros = [], []
    if categoria:
        condiciones.append("categoria = %s"); parametros.append(categoria)
    if estado:
        condiciones.append("estado = %s"); parametros.append(estado)
    if especialidad:
        condiciones.append("%s = ANY(especialidades)"); parametros.append(especialidad)
    donde = " AND ".join(condiciones) or "TRUE"

    with transaccion() as cur:
        cur.execute(f"SELECT {COLUMNAS_UNIDAD} FROM recursos.unidades WHERE {donde} ORDER BY nombre", parametros)
        unidades = [unidad_json(f) for f in cur.fetchall()]

    if cerca_de:
        ubicacion = consultar_solicitud(str(cerca_de))["ubicacion"]
        for u in unidades:
            b = u["base"]
            if ubicacion["latitud"] is not None and b["latitud"] is not None:
                km = distancia_km(ubicacion["latitud"], ubicacion["longitud"], b["latitud"], b["longitud"])
                u["distancia_km"] = round(km, 1)
                u["tiempo_estimado_min"] = max(1, math.ceil(km / VELOCIDAD_KMH * 60))
            else:
                u["distancia_km"] = u["tiempo_estimado_min"] = None
        unidades.sort(key=lambda u: (u["distancia_km"] is None, u["distancia_km"] or 0, u["nombre"]))

    inicio = (pagina - 1) * limite
    return {"total": len(unidades), "pagina": pagina, "limite": limite, "elementos": unidades[inicio:inicio + limite]}


# ==================================================
# Administración de unidades
# ==================================================

@app.post("/v1/unidades", status_code=201, tags=["Administración"], summary="Registrar una unidad")
def registrar_unidad(datos: NuevaUnidad, admin: Administrador):
    try:
        with transaccion() as cur:
            cur.execute(
                f"""INSERT INTO recursos.unidades (nombre, categoria, especialidades, personal, base_latitud, base_longitud, base_direccion)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {COLUMNAS_UNIDAD}""",
                (datos.nombre, datos.categoria, list(dict.fromkeys(datos.especialidades)), datos.personal,
                 datos.base.latitud, datos.base.longitud, datos.base.direccion),
            )
            return unidad_json(cur.fetchone())
    except psycopg2.errors.UniqueViolation:
        raise ErrorApi(409, "NOMBRE_YA_REGISTRADO", "Ya existe una unidad con ese nombre.")


@app.patch("/v1/unidades/{id}", tags=["Administración"], summary="Cambiar datos o estado de una unidad")
def cambiar_unidad(id: Annotated[UUID, Path()], datos: CambioUnidad, admin: Administrador):
    try:
        with transaccion() as cur:
            cur.execute("SELECT estado FROM recursos.unidades WHERE id = %s FOR UPDATE", (str(id),))
            fila = cur.fetchone()
            if fila is None:
                raise ErrorApi(404, "NO_ENCONTRADO", "No existe una unidad con ese id.")
            if datos.estado and fila[0] == "ocupada":
                raise ErrorApi(409, "UNIDAD_OCUPADA", "La unidad está asignada a una emergencia; finalice la asignación antes de cambiar su estado.")
            cambios, valores = [], []
            if datos.nombre is not None:
                cambios.append("nombre = %s"); valores.append(" ".join(datos.nombre.split()))
            if datos.especialidades is not None:
                cambios.append("especialidades = %s"); valores.append(list(dict.fromkeys(datos.especialidades)))
            if datos.personal is not None:
                cambios.append("personal = %s"); valores.append(datos.personal)
            if datos.base is not None:
                cambios += ["base_latitud = %s", "base_longitud = %s", "base_direccion = %s"]
                valores += [datos.base.latitud, datos.base.longitud, datos.base.direccion]
            if datos.estado is not None:
                cambios.append("estado = %s"); valores.append(datos.estado)
            if cambios:
                cur.execute(f"UPDATE recursos.unidades SET {', '.join(cambios)}, actualizado_en = now() WHERE id = %s",
                            valores + [str(id)])
            cur.execute(f"SELECT {COLUMNAS_UNIDAD} FROM recursos.unidades WHERE id = %s", (str(id),))
            return unidad_json(cur.fetchone())
    except psycopg2.errors.UniqueViolation:
        raise ErrorApi(409, "NOMBRE_YA_REGISTRADO", "Ya existe una unidad con ese nombre.")


# ==================================================
# Asignaciones
# ==================================================

@app.post("/v1/asignaciones", status_code=201, tags=["Asignaciones"], summary="Asignar unidades a una solicitud")
def asignar(datos: NuevaAsignacion, operador: Operador):
    solicitud = consultar_solicitud(str(datos.solicitud_id))
    exigir_en_atencion(solicitud, operador)
    with transaccion() as cur:
        asignacion_id = crear_asignacion(cur, solicitud, datos.unidad_ids, operador)
        return cargar_asignacion(cur, asignacion_id)


@app.get("/v1/asignaciones", tags=["Asignaciones"], summary="Asignaciones de una solicitud")
def listar_asignaciones(
    usuario: OperadorOAdmin,
    solicitud_id: UUID,
    estado: Optional[Literal["activa", "finalizada"]] = None,
):
    with transaccion() as cur:
        condicion, parametros = "solicitud_id = %s", [str(solicitud_id)]
        if estado:
            condicion += " AND estado = %s"; parametros.append(estado)
        cur.execute(f"SELECT id FROM recursos.asignaciones WHERE {condicion} ORDER BY creada_en", parametros)
        ids = [str(f[0]) for f in cur.fetchall()]
        elementos = [cargar_asignacion(cur, i) for i in ids]
    return {"total": len(elementos), "pagina": 1, "limite": len(elementos), "elementos": elementos}


@app.patch("/v1/asignaciones/{id}", tags=["Asignaciones"], summary="Finalizar una asignación y liberar sus unidades")
def finalizar_asignacion(id: Annotated[UUID, Path()], datos: CambioAsignacion, usuario: OperadorOAdmin):
    with transaccion() as cur:
        cur.execute("SELECT estado FROM recursos.asignaciones WHERE id = %s FOR UPDATE", (str(id),))
        fila = cur.fetchone()
        if fila is None:
            raise ErrorApi(404, "NO_ENCONTRADO", "No existe una asignación con ese id.")
        if fila[0] == "finalizada":
            raise ErrorApi(409, "ASIGNACION_FINALIZADA", "La asignación ya estaba finalizada.")
        cur.execute(
            """UPDATE recursos.asignaciones SET estado = 'finalizada', finalizada_por_id = %s,
                      finalizada_por_nombre = %s, finalizada_en = now() WHERE id = %s""",
            (usuario.id, usuario.nombre, str(id)),
        )
        # Solo vuelven a disponible las que siguen ocupadas.
        cur.execute(
            """UPDATE recursos.unidades SET estado = 'disponible', actualizado_en = now()
               WHERE estado = 'ocupada' AND id IN (SELECT unidad_id FROM recursos.asignacion_unidades WHERE asignacion_id = %s)""",
            (str(id),),
        )
        return cargar_asignacion(cur, str(id))

# ==================================================
# Apoyo entre compañeros (por cupos, H21)
# ==================================================

MENSAJE_CUBIERTA = "Esta solicitud de apoyo ya está cubierta en su totalidad."


def leer_apoyo(cur, apoyo_id: str, operador_id: str, bloquear: bool = False, referencia: Optional[dict] = None) -> Optional[dict]:
    cur.execute(f"SELECT {COLUMNAS_APOYO} FROM recursos.solicitudes_apoyo WHERE id = %s" + (" FOR UPDATE" if bloquear else ""),
                (apoyo_id,))
    fila = cur.fetchone()
    return cargar_apoyos(cur, [fila], operador_id, referencia)[0] if fila else None


@app.post("/v1/solicitudes-apoyo", status_code=201, tags=["Apoyo"], summary="Pedir refuerzos (pantalla 5)")
def pedir_apoyo(datos: NuevoApoyo, operador: Operador):
    solicitud = consultar_solicitud(str(datos.solicitud_id))
    exigir_en_atencion(solicitud, operador)
    with transaccion() as cur:
        cur.execute(
            """INSERT INTO recursos.solicitudes_apoyo (solicitud_id, codigo_solicitud, tipos_apoyo, personal_adicional,
                                                       prioridad, mensaje, solicitante_id, solicitante_nombre,
                                                       solicitud_latitud, solicitud_longitud, solicitud_direccion)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (solicitud["id"], solicitud["codigo"], list(dict.fromkeys(datos.tipos_apoyo)), datos.personal_adicional,
             datos.prioridad, (datos.mensaje or "").strip() or None, operador.id, operador.nombre,
             solicitud["ubicacion"]["latitud"], solicitud["ubicacion"]["longitud"], solicitud["ubicacion"]["direccion"]),
        )
        apoyo = leer_apoyo(cur, str(cur.fetchone()[0]), operador.id)
    publicar_evento("apoyo.solicitado", {
        **evento_apoyo(apoyo), "tipos_apoyo": apoyo["tipos_apoyo"], "prioridad": apoyo["prioridad"],
        "solicitante": apoyo["solicitante"],
    })
    return apoyo


@app.get("/v1/solicitudes-apoyo", tags=["Apoyo"], summary="Ver pedidos de refuerzo")
def listar_apoyos(
    operador: Operador,
    estado: Literal["abierta", "cubierta", "cancelada"] = "abierta",
    orden: Annotated[Literal["prioridad", "distancia"], Query(description="distancia: los más cercanos a la emergencia que atiende el operador primero.")] = "prioridad",
    pagina: Annotated[int, Query(ge=1)] = 1,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
):
    referencia = ubicacion_operador(operador.id)
    with transaccion() as cur:
        cur.execute(
            f"""SELECT {COLUMNAS_APOYO} FROM recursos.solicitudes_apoyo WHERE estado = %s
                ORDER BY CASE prioridad WHEN 'critico' THEN 1 WHEN 'alto' THEN 2 WHEN 'medio' THEN 3 ELSE 4 END, creada_en""",
            (estado,),
        )
        apoyos = cargar_apoyos(cur, cur.fetchall(), operador.id, referencia)
    if orden == "distancia":
        apoyos.sort(key=lambda a: (a["distancia_km"] is None, a["distancia_km"] or 0))
    inicio = (pagina - 1) * limite
    return {"total": len(apoyos), "pagina": pagina, "limite": limite,
            "referencia": {"solicitud_id": referencia["solicitud_id"], "codigo": referencia["codigo"]} if referencia else None,
            "elementos": apoyos[inicio:inicio + limite]}


@app.get("/v1/solicitudes-apoyo/{id}", tags=["Apoyo"], summary="Detalle de un pedido de refuerzo")
def detalle_apoyo(id: Annotated[UUID, Path()], operador: Operador):
    referencia = ubicacion_operador(operador.id)
    with transaccion() as cur:
        apoyo = leer_apoyo(cur, str(id), operador.id, referencia=referencia)
    if apoyo is None:
        raise ErrorApi(404, "NO_ENCONTRADO", "No existe un pedido de apoyo con ese id.")
    return apoyo


@app.post("/v1/solicitudes-apoyo/{id}/aceptaciones", status_code=201, tags=["Apoyo"],
          summary="Sumarse a un pedido de refuerzo (ocupar cupos)")
def aceptar_apoyo(id: Annotated[UUID, Path()], datos: Aceptacion, operador: Operador):
    with transaccion() as cur:
        apoyo = leer_apoyo(cur, str(id), operador.id)
    if apoyo is None:
        raise ErrorApi(404, "NO_ENCONTRADO", "No existe un pedido de apoyo con ese id.")
    if apoyo["solicitante"]["id"] == operador.id:
        raise ErrorApi(403, "ES_EL_SOLICITANTE",
                       "Usted pidió este apoyo; para enviar sus propias unidades use POST /v1/asignaciones.")
    # La emergencia tiene que seguir en atención para que tenga sentido sumarse.
    solicitud = consultar_solicitud(apoyo["solicitud_id"])
    exigir_en_atencion(solicitud)

    with transaccion() as cur:
        # FOR UPDATE: si dos operadores toman el último cupo a la vez, el segundo espera y ve el pedido cubierto.
        apoyo = leer_apoyo(cur, str(id), operador.id, bloquear=True)
        if apoyo["estado"] == "cubierta":
            raise ErrorApi(409, "APOYO_CUBIERTO", MENSAJE_CUBIERTA,
                           {"personal_adicional": apoyo["personal_adicional"], "personal_asignado": apoyo["personal_asignado"]})
        if apoyo["estado"] == "cancelada":
            raise ErrorApi(409, "APOYO_CANCELADO", "Quien pidió este apoyo lo canceló.")
        if apoyo["aceptado_por_mi"]:
            raise ErrorApi(409, "YA_ACEPTADO", "Usted ya se sumó a este pedido de apoyo.")

        # Los cupos se cuentan por las personas que declara el operador (respuesta de Tópicos).
        # Las unidades que envíe quedan asignadas a la emergencia, pero no cambian el conteo.
        asignacion, asignacion_id, personas = None, None, datos.personas
        if datos.unidad_ids:
            asignacion_id = crear_asignacion(cur, solicitud, datos.unidad_ids, operador, str(id))
            asignacion = cargar_asignacion(cur, asignacion_id)

        cur.execute(
            """INSERT INTO recursos.aceptaciones_apoyo (apoyo_id, operador_id, operador_nombre, personas, asignacion_id)
               VALUES (%s, %s, %s, %s, %s)""",
            (str(id), operador.id, operador.nombre, personas, asignacion_id),
        )
        cur.execute(
            """UPDATE recursos.solicitudes_apoyo
               SET personal_asignado = personal_asignado + %s,
                   estado = CASE WHEN personal_asignado + %s >= personal_adicional THEN 'cubierta' ELSE estado END,
                   cerrada_por_id = CASE WHEN personal_asignado + %s >= personal_adicional THEN %s::uuid ELSE cerrada_por_id END,
                   cerrada_por_nombre = CASE WHEN personal_asignado + %s >= personal_adicional THEN %s ELSE cerrada_por_nombre END,
                   cerrada_en = CASE WHEN personal_asignado + %s >= personal_adicional THEN now() ELSE cerrada_en END
               WHERE id = %s""",
            (personas, personas, personas, operador.id, personas, operador.nombre, personas, str(id)),
        )
        apoyo = leer_apoyo(cur, str(id), operador.id)

    publicar_evento("apoyo.actualizado", evento_apoyo(apoyo))
    apoyo["asignacion"] = asignacion
    return apoyo


@app.patch("/v1/solicitudes-apoyo/{id}", tags=["Apoyo"], summary="Cancelar un pedido de refuerzo")
def cancelar_apoyo(id: Annotated[UUID, Path()], datos: CambioApoyo, operador: Operador):
    with transaccion() as cur:
        apoyo = leer_apoyo(cur, str(id), operador.id, bloquear=True)
        if apoyo is None:
            raise ErrorApi(404, "NO_ENCONTRADO", "No existe un pedido de apoyo con ese id.")
        if apoyo["solicitante"]["id"] != operador.id:
            raise ErrorApi(403, "NO_ES_SOLICITANTE", "Solo quien pidió el apoyo puede cancelarlo.")
        if apoyo["estado"] == "cubierta":
            raise ErrorApi(409, "APOYO_CUBIERTO", MENSAJE_CUBIERTA)
        if apoyo["estado"] == "cancelada":
            raise ErrorApi(409, "APOYO_CANCELADO", "El pedido ya estaba cancelado.")
        # Quienes ya aceptaron siguen yendo: cancelar solo cierra los cupos que faltan.
        cur.execute(
            """UPDATE recursos.solicitudes_apoyo SET estado = 'cancelada', cerrada_por_id = %s,
                      cerrada_por_nombre = %s, cerrada_en = now() WHERE id = %s""",
            (operador.id, operador.nombre, str(id)),
        )
        apoyo = leer_apoyo(cur, str(id), operador.id)
    publicar_evento("apoyo.actualizado", evento_apoyo(apoyo))
    return apoyo
