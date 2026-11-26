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


COLUMNAS_APOYO = """id, solicitud_id, codigo_solicitud, tipos_apoyo, personal_adicional, prioridad, mensaje, estado,
    solicitante_id, solicitante_nombre, creada_en, cerrada_por_id, cerrada_por_nombre, cerrada_en"""


def apoyo_json(fila) -> dict:
    (id_, sol, codigo, tipos, personal, prioridad, mensaje, estado, s_id, s_nombre, creada, c_id, c_nombre, cerrada) = fila
    return {
        "id": str(id_), "solicitud_id": str(sol), "codigo_solicitud": codigo, "tipos_apoyo": list(tipos),
        "personal_adicional": personal, "prioridad": prioridad, "mensaje": mensaje, "estado": estado,
        "solicitante": {"id": str(s_id), "nombre_completo": s_nombre}, "creada_en": fecha_iso(creada),
        "cerrada_por": {"id": str(c_id), "nombre_completo": c_nombre} if c_id else None, "cerrada_en": fecha_iso(cerrada),
    }


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
    personal_adicional: int = Field(ge=0, le=100, examples=[2])
    prioridad: Prioridad = Field(examples=["alto"])
    mensaje: Optional[str] = Field(None, max_length=500, examples=["Se necesita equipo con herramientas de corte, hay dos personas atrapadas."])


class CambioApoyo(BaseModel):
    estado: Literal["atendida", "cancelada"]
    unidad_ids: Optional[list[UUID]] = Field(None, min_length=1, max_length=MAX_UNIDADES,
                                             description="Obligatorio para atendida: las unidades que se envían.")


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
# Apoyo entre compañeros
# ==================================================

@app.post("/v1/solicitudes-apoyo", status_code=201, tags=["Apoyo"], summary="Pedir refuerzos (pantalla 5)")
def pedir_apoyo(datos: NuevoApoyo, operador: Operador):
    solicitud = consultar_solicitud(str(datos.solicitud_id))
    exigir_en_atencion(solicitud, operador)
    with transaccion() as cur:
        cur.execute(
            f"""INSERT INTO recursos.solicitudes_apoyo (solicitud_id, codigo_solicitud, tipos_apoyo, personal_adicional,
                                                        prioridad, mensaje, solicitante_id, solicitante_nombre)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {COLUMNAS_APOYO}""",
            (solicitud["id"], solicitud["codigo"], list(dict.fromkeys(datos.tipos_apoyo)), datos.personal_adicional,
             datos.prioridad, (datos.mensaje or "").strip() or None, operador.id, operador.nombre),
        )
        apoyo = apoyo_json(cur.fetchone())
    publicar_evento("apoyo.solicitado", {
        "id": apoyo["id"], "solicitud_id": apoyo["solicitud_id"], "codigo_solicitud": apoyo["codigo_solicitud"],
        "tipos_apoyo": apoyo["tipos_apoyo"], "personal_adicional": apoyo["personal_adicional"],
        "prioridad": apoyo["prioridad"], "solicitante": apoyo["solicitante"],
    })
    return apoyo


@app.get("/v1/solicitudes-apoyo", tags=["Apoyo"], summary="Ver pedidos de refuerzo")
def listar_apoyos(
    operador: Operador,
    estado: Literal["abierta", "atendida", "cancelada"] = "abierta",
    pagina: Annotated[int, Query(ge=1)] = 1,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
):
    with transaccion() as cur:
        cur.execute("SELECT count(*) FROM recursos.solicitudes_apoyo WHERE estado = %s", (estado,))
        total = cur.fetchone()[0]
        cur.execute(
            f"""SELECT {COLUMNAS_APOYO} FROM recursos.solicitudes_apoyo WHERE estado = %s
                ORDER BY CASE prioridad WHEN 'critico' THEN 1 WHEN 'alto' THEN 2 WHEN 'medio' THEN 3 ELSE 4 END, creada_en
                LIMIT %s OFFSET %s""",
            (estado, limite, (pagina - 1) * limite),
        )
        return {"total": total, "pagina": pagina, "limite": limite, "elementos": [apoyo_json(f) for f in cur.fetchall()]}


@app.patch("/v1/solicitudes-apoyo/{id}", tags=["Apoyo"], summary="Atender o cancelar un pedido de refuerzo")
def cerrar_apoyo(id: Annotated[UUID, Path()], datos: CambioApoyo, operador: Operador):
    if datos.estado == "atendida" and not datos.unidad_ids:
        raise ErrorApi(422, "DATOS_INVALIDOS", "Los datos enviados no cumplen el contrato.",
                       [{"campo": "unidad_ids", "problema": "Para atender el pedido hay que indicar las unidades que se envían."}])

    with transaccion() as cur:
        cur.execute(f"SELECT {COLUMNAS_APOYO} FROM recursos.solicitudes_apoyo WHERE id = %s", (str(id),))
        fila = cur.fetchone()
    if fila is None:
        raise ErrorApi(404, "NO_ENCONTRADO", "No existe un pedido de apoyo con ese id.")
    apoyo = apoyo_json(fila)
    es_solicitante = apoyo["solicitante"]["id"] == operador.id
    if datos.estado == "cancelada" and not es_solicitante:
        raise ErrorApi(403, "NO_ES_SOLICITANTE", "Solo quien pidió el apoyo puede cancelarlo.")
    if datos.estado == "atendida" and es_solicitante:
        raise ErrorApi(403, "ES_EL_SOLICITANTE", "El pedido de apoyo lo atiende otro operador; para enviar sus propias unidades use POST /v1/asignaciones.")

    solicitud = consultar_solicitud(apoyo["solicitud_id"]) if datos.estado == "atendida" else None
    if solicitud:
        exigir_en_atencion(solicitud)

    with transaccion() as cur:
        # FOR UPDATE: si dos operadores atienden el mismo pedido, el segundo ve que ya no está abierto.
        cur.execute("SELECT estado FROM recursos.solicitudes_apoyo WHERE id = %s FOR UPDATE", (str(id),))
        if cur.fetchone()[0] != "abierta":
            raise ErrorApi(409, "APOYO_NO_ABIERTO", "El pedido de apoyo ya fue atendido o cancelado.")
        asignacion = None
        if datos.estado == "atendida":
            asignacion_id = crear_asignacion(cur, solicitud, datos.unidad_ids, operador, str(id))
            asignacion = cargar_asignacion(cur, asignacion_id)
        cur.execute(
            """UPDATE recursos.solicitudes_apoyo SET estado = %s, cerrada_por_id = %s, cerrada_por_nombre = %s, cerrada_en = now()
               WHERE id = %s""",
            (datos.estado, operador.id, operador.nombre, str(id)),
        )
        cur.execute(f"SELECT {COLUMNAS_APOYO} FROM recursos.solicitudes_apoyo WHERE id = %s", (str(id),))
        respuesta = apoyo_json(cur.fetchone())
    respuesta["asignacion"] = asignacion
    return respuesta
