import os
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional
from uuid import UUID, uuid4

import requests
from fastapi import Depends, FastAPI, Path, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator, model_validator

from con_db import transaccion
from seguridad import (
    ErrorApi, Usuario, clave_servicio, fecha_iso, registrar_manejadores, requiere_rol, usuario_actual,
)


app = FastAPI(
    title="SENTIR - Servicio de Asistencia",
    description=(
        "Solicitudes de auxilio (los \"reportes\" de la app), su ubicación, su ciclo de estados "
        "y las observaciones del operador.\n\n"
        "Cada solicitud nueva y cada cambio de estado o de severidad se publican en el servicio "
        "de Alertas. Si Alertas no responde, la operación igual queda guardada.\n\n"
        "Para probar en Swagger: pida un token en el servicio de Identidad (POST /v1/sesiones) "
        "y péguelo en el botón **Authorize**."
    ),
    version="0.2.0",
    openapi_tags=[
        {"name": "Servicio", "description": "Verificación de que el servicio responde."},
        {"name": "Catálogo", "description": "Tipos de emergencia."},
        {"name": "Ciudadano", "description": "Lo que haría la app del ciudadano. Hoy lo usa el simulador."},
        {"name": "Operador", "description": "Bandeja, detalle y gestión de las solicitudes."},
        {"name": "Interno", "description": "Rutas para Personas y Recursos. Requieren X-Clave-Servicio."},
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

Severidad = Literal["critico", "alto", "medio", "bajo"]
Estado = Literal["pendiente", "en_atencion", "atendido", "descartado"]
SEVERIDADES = ("critico", "alto", "medio", "bajo")
ESTADOS = ("pendiente", "en_atencion", "atendido", "descartado")
ACTIVOS = ("pendiente", "en_atencion")

# (desde, hacia) -> quién puede hacer el cambio (hallazgo H6)
TRANSICIONES = {
    ("pendiente", "en_atencion"): "cualquiera",
    ("pendiente", "descartado"): "cualquiera",
    ("en_atencion", "atendido"): "responsable",
    ("en_atencion", "descartado"): "responsable",
}

MAX_SOLICITUDES = 3                      # por ciudadano...
VENTANA_SOLICITUDES = timedelta(minutes=10)  # ...en esta ventana
LIMITE_DIFERIDA = timedelta(minutes=5)
MIN_DESCRIPCION_OTRO = 20


# ==================================================
# Publicación de eventos en Alertas
# ==================================================

def publicar_evento(nombre: str, datos: dict):
    """Envía el evento a Alertas. Si falla, solo lo registra: la operación ya quedó guardada."""
    url = os.getenv("ALERTAS_URL", "").strip().rstrip("/")
    if not url:
        return
    try:
        respuesta = requests.post(
            f"{url}/v1/interno/eventos",
            json={"nombre": nombre, "datos": datos},
            headers={"X-Clave-Servicio": os.getenv("CLAVE_SERVICIO", "").strip()},
            timeout=(2, 3),
        )
        if respuesta.status_code >= 300:
            print(f"Alertas respondió {respuesta.status_code} al publicar {nombre}")
    except requests.RequestException as error:
        print(f"No se pudo publicar {nombre} en Alertas: {error}")


# ==================================================
# Lectura de solicitudes
# ==================================================

COLUMNAS = """s.id, s.codigo, s.estado, s.severidad, s.codigo_tipo, t.nombre, s.descripcion,
    s.latitud, s.longitud, s.precision_metros, s.direccion, s.ciudadano_id, s.ciudadano_nombre,
    s.fecha_hora_dispositivo, s.fecha_hora_recepcion, s.responsable_id, s.responsable_nombre"""
DESDE = "asistencia.solicitudes s JOIN asistencia.tipos_emergencia t ON t.codigo = s.codigo_tipo"
ORDEN = """CASE s.severidad WHEN 'critico' THEN 1 WHEN 'alto' THEN 2 WHEN 'medio' THEN 3 ELSE 4 END,
    s.fecha_hora_dispositivo"""


def _numero(valor):
    return float(valor) if valor is not None else None


def solicitud_json(fila, completa: bool) -> dict:
    (id_, codigo, estado, severidad, cod_tipo, nombre_tipo, descripcion, lat, lon, precision, direccion,
     ciu_id, ciu_nombre, f_disp, f_recep, resp_id, resp_nombre) = fila
    datos = {
        "id": str(id_),
        "codigo": codigo,
        "estado": estado,
        "severidad": severidad,
        "tipo": {"codigo": cod_tipo, "nombre": nombre_tipo},
        "ubicacion": {"latitud": _numero(lat), "longitud": _numero(lon), "direccion": direccion},
        "fecha_hora_dispositivo": fecha_iso(f_disp),
        "diferida": (f_recep - f_disp) > LIMITE_DIFERIDA,
    }
    if completa:
        datos["ubicacion"]["precision_metros"] = _numero(precision)
        datos["descripcion"] = descripcion
        datos["ciudadano"] = {"id": str(ciu_id), "nombre_completo": ciu_nombre}
        datos["fecha_hora_recepcion"] = fecha_iso(f_recep)
        datos["responsable"] = {"id": str(resp_id), "nombre_completo": resp_nombre} if resp_id else None
    return datos


def cargar_detalle(cur, solicitud_id: str) -> Optional[dict]:
    cur.execute(f"SELECT {COLUMNAS} FROM {DESDE} WHERE s.id = %s", (solicitud_id,))
    fila = cur.fetchone()
    if fila is None:
        return None
    datos = solicitud_json(fila, completa=True)
    cur.execute(
        """SELECT estado, fecha, operador_id, operador_nombre, motivo FROM asistencia.historial_estados
           WHERE solicitud_id = %s ORDER BY fecha, id""",
        (solicitud_id,),
    )
    datos["historial"] = [
        {"estado": e, "fecha": fecha_iso(f), "operador": {"id": str(oi), "nombre_completo": on} if oi else None, "motivo": m}
        for e, f, oi, on, m in cur.fetchall()
    ]
    cur.execute(
        """SELECT id, texto, operador_id, operador_nombre, fecha FROM asistencia.observaciones
           WHERE solicitud_id = %s ORDER BY fecha, id""",
        (solicitud_id,),
    )
    datos["observaciones"] = [observacion_json(f) for f in cur.fetchall()]
    return datos


def observacion_json(fila) -> dict:
    id_, texto, op_id, op_nombre, fecha = fila
    return {"id": str(id_), "texto": texto, "autor": {"id": str(op_id), "nombre_completo": op_nombre}, "fecha": fecha_iso(fecha)}


def lista_csv(valor: Optional[str], permitidos: tuple, campo: str) -> Optional[list]:
    if not valor:
        return None
    partes = [p.strip() for p in valor.split(",") if p.strip()]
    invalidas = [p for p in partes if p not in permitidos]
    if invalidas:
        raise ErrorApi(422, "DATOS_INVALIDOS", "Los datos enviados no cumplen el contrato.",
                       [{"campo": campo, "problema": f"Valores no permitidos: {', '.join(invalidas)}. Use: {', '.join(permitidos)}."}])
    return partes


def paginado(cur, condiciones: list, parametros: list, pagina: int, limite: int) -> dict:
    donde = " AND ".join(condiciones) if condiciones else "TRUE"
    cur.execute(f"SELECT count(*) FROM {DESDE} WHERE {donde}", parametros)
    total = cur.fetchone()[0]
    cur.execute(
        f"SELECT {COLUMNAS} FROM {DESDE} WHERE {donde} ORDER BY {ORDEN} LIMIT %s OFFSET %s",
        parametros + [limite, (pagina - 1) * limite],
    )
    return {"total": total, "pagina": pagina, "limite": limite,
            "elementos": [solicitud_json(f, completa=False) for f in cur.fetchall()]}


# ==================================================
# Modelos
# ==================================================

class Ubicacion(BaseModel):
    latitud: Optional[float] = Field(None, ge=-90, le=90, examples=[7.893912])
    longitud: Optional[float] = Field(None, ge=-180, le=180, examples=[-72.507834])
    precision_metros: Optional[float] = Field(None, ge=0, le=100000, examples=[15])
    direccion: Optional[str] = Field(None, max_length=300, examples=["Calle 10 # 4-32, barrio La Cabrera, Cúcuta"])

    @model_validator(mode="after")
    def _completa(self):
        if (self.latitud is None) != (self.longitud is None):
            raise ValueError("latitud y longitud van juntas")
        if self.direccion is not None:
            self.direccion = " ".join(self.direccion.split()) or None
        if self.latitud is None and not self.direccion:
            raise ValueError("hace falta la pareja latitud y longitud, la dirección, o ambas")
        return self


class NuevaSolicitud(BaseModel):
    id: UUID = Field(description="Lo genera el cliente y lo reutiliza en cada reintento.", examples=[str(uuid4())])
    codigo_tipo: str = Field(examples=["derrumbe"])
    severidad: Optional[Severidad] = Field(None, description="Opcional. Si no llega, se usa la del tipo.")
    descripcion: str = Field(min_length=1, max_length=1000, examples=["Se cayó parte de la casa vecina. Hay dos personas atrapadas."])
    ubicacion: Ubicacion
    fecha_hora_dispositivo: datetime = Field(examples=["2026-11-20T13:42:00Z"])

    @field_validator("descripcion")
    @classmethod
    def _limpiar(cls, valor: str) -> str:
        valor = valor.strip()
        if not valor:
            raise ValueError("la descripción no puede estar vacía")
        return valor

    @field_validator("fecha_hora_dispositivo")
    @classmethod
    def _con_zona(cls, valor: datetime) -> datetime:
        if valor.tzinfo is None:
            raise ValueError("la fecha debe indicar la zona horaria, por ejemplo terminar en Z")
        return valor


class CambioEstado(BaseModel):
    estado: Estado = Field(examples=["en_atencion"])
    motivo: Optional[str] = Field(None, max_length=500, description="Obligatorio para descartado.")


class CambioSeveridad(BaseModel):
    severidad: Severidad


class NuevaObservacion(BaseModel):
    texto: str = Field(min_length=1, max_length=500, examples=["Paciente ubicada y colaborativa."])


Operador = Annotated[Usuario, Depends(requiere_rol("operador"))]
Ciudadano = Annotated[Usuario, Depends(requiere_rol("ciudadano"))]


# ==================================================
# Servicio y catálogo
# ==================================================

@app.get("/", tags=["Servicio"], summary="Estado del servicio")
def estado():
    return {"servicio": "asistencia", "estado": "activo", "version": app.version}


@app.get("/v1/tipos-emergencia", tags=["Catálogo"], summary="Catálogo de tipos")
def tipos_emergencia(usuario: Annotated[Usuario, Depends(usuario_actual)]):
    with transaccion() as cur:
        cur.execute("SELECT codigo, nombre, severidad_por_defecto FROM asistencia.tipos_emergencia WHERE activo ORDER BY orden")
        return [{"codigo": c, "nombre": n, "severidad_por_defecto": s} for c, n, s in cur.fetchall()]


# ==================================================
# Ciudadano
# ==================================================

@app.post("/v1/solicitudes", status_code=201, tags=["Ciudadano"], summary="Registrar una solicitud")
def registrar_solicitud(datos: NuevaSolicitud, ciudadano: Ciudadano, response: Response):
    with transaccion() as cur:
        cur.execute("SELECT severidad_por_defecto FROM asistencia.tipos_emergencia WHERE codigo = %s AND activo", (datos.codigo_tipo,))
        tipo = cur.fetchone()
        if tipo is None:
            raise ErrorApi(422, "DATOS_INVALIDOS", "Los datos enviados no cumplen el contrato.",
                           [{"campo": "codigo_tipo", "problema": "El tipo no existe. Consulte GET /v1/tipos-emergencia."}])
        if datos.codigo_tipo == "otro" and len(datos.descripcion) < MIN_DESCRIPCION_OTRO:
            raise ErrorApi(422, "DATOS_INVALIDOS", "Los datos enviados no cumplen el contrato.",
                           [{"campo": "descripcion", "problema": f"Con el tipo otro la descripción necesita al menos {MIN_DESCRIPCION_OTRO} caracteres."}])

        # Un ciudadano a la vez: evita que dos envíos simultáneos se salten el límite.
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (ciudadano.id,))

        # Reintento del mismo id: devolver lo guardado sin crear otra (H10).
        cur.execute("SELECT ciudadano_id FROM asistencia.solicitudes WHERE id = %s", (str(datos.id),))
        existente = cur.fetchone()
        if existente:
            if str(existente[0]) != ciudadano.id:
                raise ErrorApi(409, "ID_EN_USO", "Ese id ya pertenece a otra solicitud.")
            response.status_code = 200
            return cargar_detalle(cur, str(datos.id))

        cur.execute(
            """SELECT count(*), min(fecha_hora_recepcion) FROM asistencia.solicitudes
               WHERE ciudadano_id = %s AND fecha_hora_recepcion > now() - %s""",
            (ciudadano.id, VENTANA_SOLICITUDES),
        )
        recientes, primera = cur.fetchone()
        if recientes >= MAX_SOLICITUDES:
            espera = int((primera + VENTANA_SOLICITUDES - datetime.now(timezone.utc)).total_seconds()) + 1
            raise ErrorApi(429, "DEMASIADAS_PETICIONES",
                           f"Se permiten {MAX_SOLICITUDES} solicitudes cada {int(VENTANA_SOLICITUDES.total_seconds() // 60)} minutos.",
                           encabezados={"Retry-After": str(max(espera, 1))})

        u = datos.ubicacion
        cur.execute(
            """INSERT INTO asistencia.solicitudes
               (id, ciudadano_id, ciudadano_nombre, codigo_tipo, severidad, descripcion,
                latitud, longitud, precision_metros, direccion, fecha_hora_dispositivo)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (str(datos.id), ciudadano.id, ciudadano.nombre or "Sin nombre", datos.codigo_tipo,
             datos.severidad or tipo[0], datos.descripcion, u.latitud, u.longitud, u.precision_metros,
             u.direccion, datos.fecha_hora_dispositivo),
        )
        cur.execute("INSERT INTO asistencia.historial_estados (solicitud_id, estado) VALUES (%s, 'pendiente')", (str(datos.id),))
        detalle = cargar_detalle(cur, str(datos.id))

    # Fuera de la transacción: la solicitud ya quedó guardada.
    publicar_evento("solicitud.creada", {k: detalle[k] for k in
                    ("id", "codigo", "estado", "severidad", "tipo", "fecha_hora_dispositivo", "diferida")}
                    | {"ubicacion": {k: detalle["ubicacion"][k] for k in ("latitud", "longitud", "direccion")}})
    return detalle


@app.get("/v1/solicitudes/mias", tags=["Ciudadano"], summary="Ver las propias")
def mis_solicitudes(
    ciudadano: Ciudadano,
    pagina: Annotated[int, Query(ge=1)] = 1,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
):
    with transaccion() as cur:
        return paginado(cur, ["s.ciudadano_id = %s"], [ciudadano.id], pagina, limite)


# ==================================================
# Operador
# ==================================================

@app.get("/v1/solicitudes", tags=["Operador"], summary="Listar con filtros")
def listar_solicitudes(
    operador: Operador,
    estado: Annotated[Optional[str], Query(description="Lista separada por comas. Por defecto pendiente,en_atencion. Historial: atendido,descartado.")] = None,
    severidad: Annotated[Optional[str], Query(description="Lista separada por comas: critico, alto, medio, bajo.")] = None,
    codigo_tipo: Optional[str] = None,
    pagina: Annotated[int, Query(ge=1)] = 1,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
):
    estados = lista_csv(estado, ESTADOS, "estado") or list(ACTIVOS)
    condiciones, parametros = ["s.estado = ANY(%s)"], [estados]
    severidades = lista_csv(severidad, SEVERIDADES, "severidad")
    if severidades:
        condiciones.append("s.severidad = ANY(%s)")
        parametros.append(severidades)
    if codigo_tipo:
        condiciones.append("s.codigo_tipo = %s")
        parametros.append(codigo_tipo)
    with transaccion() as cur:
        return paginado(cur, condiciones, parametros, pagina, limite)


@app.get("/v1/solicitudes/{id}", tags=["Operador"], summary="Detalle con historial (operador o dueño)")
def detalle_solicitud(id: Annotated[UUID, Path()], usuario: Annotated[Usuario, Depends(usuario_actual)]):
    if usuario.rol not in ("operador", "ciudadano"):
        raise ErrorApi(403, "ROL_NO_PERMITIDO", "Esta operación es solo para el rol operador o ciudadano.")
    with transaccion() as cur:
        detalle = cargar_detalle(cur, str(id))
    # A un ciudadano que pide una solicitud ajena se le responde como si no existiera.
    if detalle is None or (usuario.rol == "ciudadano" and detalle["ciudadano"]["id"] != usuario.id):
        raise ErrorApi(404, "NO_ENCONTRADO", "No existe una solicitud con ese id.")
    return detalle


@app.patch("/v1/solicitudes/{id}/estado", tags=["Operador"], summary="Cambiar el estado")
def cambiar_estado(id: Annotated[UUID, Path()], datos: CambioEstado, operador: Operador):
    motivo = (datos.motivo or "").strip() or None
    if datos.estado == "descartado" and not motivo:
        raise ErrorApi(422, "DATOS_INVALIDOS", "Los datos enviados no cumplen el contrato.",
                       [{"campo": "motivo", "problema": "Para descartar una solicitud hay que indicar el motivo."}])

    with transaccion() as cur:
        # FOR UPDATE: si dos operadores llegan a la vez, el segundo espera y ve el estado nuevo (H9).
        cur.execute(
            "SELECT estado, responsable_id, responsable_nombre, codigo FROM asistencia.solicitudes WHERE id = %s FOR UPDATE",
            (str(id),),
        )
        fila = cur.fetchone()
        if fila is None:
            raise ErrorApi(404, "NO_ENCONTRADO", "No existe una solicitud con ese id.")
        actual, resp_id, resp_nombre, codigo = fila

        if datos.estado == "en_atencion" and actual == "en_atencion":
            raise ErrorApi(409, "SOLICITUD_YA_TOMADA", "Este reporte ya está siendo atendido por otro operador.",
                           {"responsable": {"id": str(resp_id), "nombre_completo": resp_nombre}})
        regla = TRANSICIONES.get((actual, datos.estado))
        if regla is None:
            raise ErrorApi(409, "TRANSICION_INVALIDA", f"La solicitud {codigo} está en {actual} y no puede pasar a {datos.estado}.")
        if regla == "responsable" and str(resp_id) != operador.id:
            raise ErrorApi(403, "NO_ES_RESPONSABLE", "Solo el operador responsable puede cerrar esta solicitud.",
                           {"responsable": {"id": str(resp_id), "nombre_completo": resp_nombre}})

        toma = datos.estado == "en_atencion"
        cur.execute(
            """UPDATE asistencia.solicitudes
               SET estado = %s, actualizado_en = now(),
                   responsable_id = CASE WHEN %s THEN %s::uuid ELSE responsable_id END,
                   responsable_nombre = CASE WHEN %s THEN %s ELSE responsable_nombre END
               WHERE id = %s""",
            (datos.estado, toma, operador.id, toma, operador.nombre, str(id)),
        )
        cur.execute(
            """INSERT INTO asistencia.historial_estados (solicitud_id, estado, operador_id, operador_nombre, motivo)
               VALUES (%s, %s, %s, %s, %s) RETURNING fecha""",
            (str(id), datos.estado, operador.id, operador.nombre, motivo),
        )
        fecha = cur.fetchone()[0]
        detalle = cargar_detalle(cur, str(id))

    publicar_evento("solicitud.estado_cambiado", {
        "id": detalle["id"], "codigo": detalle["codigo"], "estado_anterior": actual, "estado_nuevo": datos.estado,
        "responsable": detalle["responsable"], "fecha": fecha_iso(fecha),
    })
    return detalle


@app.patch("/v1/solicitudes/{id}/severidad", tags=["Operador"], summary="Reclasificar la severidad")
def cambiar_severidad(id: Annotated[UUID, Path()], datos: CambioSeveridad, operador: Operador):
    with transaccion() as cur:
        cur.execute("SELECT estado, severidad FROM asistencia.solicitudes WHERE id = %s FOR UPDATE", (str(id),))
        fila = cur.fetchone()
        if fila is None:
            raise ErrorApi(404, "NO_ENCONTRADO", "No existe una solicitud con ese id.")
        estado_actual, anterior = fila
        if estado_actual not in ACTIVOS:
            raise ErrorApi(409, "SOLICITUD_CERRADA", f"La solicitud está {estado_actual} y ya no se puede reclasificar.")
        cambio = anterior != datos.severidad
        if cambio:
            cur.execute("UPDATE asistencia.solicitudes SET severidad = %s, actualizado_en = now() WHERE id = %s",
                        (datos.severidad, str(id)))
        detalle = cargar_detalle(cur, str(id))

    if cambio:
        publicar_evento("solicitud.severidad_cambiada", {
            "id": detalle["id"], "codigo": detalle["codigo"], "severidad_anterior": anterior,
            "severidad_nueva": datos.severidad, "operador": {"id": operador.id, "nombre_completo": operador.nombre},
            "fecha": fecha_iso(datetime.now(timezone.utc)),
        })
    return detalle


@app.post("/v1/solicitudes/{id}/observaciones", status_code=201, tags=["Operador"], summary="Registrar una observación")
def registrar_observacion(id: Annotated[UUID, Path()], datos: NuevaObservacion, operador: Operador):
    texto = datos.texto.strip()
    if not texto:
        raise ErrorApi(422, "DATOS_INVALIDOS", "Los datos enviados no cumplen el contrato.",
                       [{"campo": "texto", "problema": "La observación no puede estar vacía."}])
    with transaccion() as cur:
        cur.execute("SELECT 1 FROM asistencia.solicitudes WHERE id = %s", (str(id),))
        if cur.fetchone() is None:
            raise ErrorApi(404, "NO_ENCONTRADO", "No existe una solicitud con ese id.")
        cur.execute(
            """INSERT INTO asistencia.observaciones (solicitud_id, texto, operador_id, operador_nombre)
               VALUES (%s, %s, %s, %s) RETURNING id, texto, operador_id, operador_nombre, fecha""",
            (str(id), texto, operador.id, operador.nombre),
        )
        return observacion_json(cur.fetchone())


# ==================================================
# Interno (Personas y Recursos)
# ==================================================

@app.get("/v1/interno/ciudadanos/{id}/solicitud-activa", tags=["Interno"], dependencies=[Depends(clave_servicio)],
         summary="¿El ciudadano tiene una solicitud abierta?")
def solicitud_activa(id: Annotated[UUID, Path()]):
    with transaccion() as cur:
        cur.execute(
            f"""SELECT s.id, s.estado FROM asistencia.solicitudes s
                WHERE s.ciudadano_id = %s AND s.estado = ANY(%s)
                ORDER BY s.fecha_hora_recepcion DESC LIMIT 1""",
            (str(id), list(ACTIVOS)),
        )
        fila = cur.fetchone()
    if fila is None:
        return {"activa": False}
    return {"activa": True, "solicitud_id": str(fila[0]), "estado": fila[1]}


@app.get("/v1/interno/operadores/{id}/solicitud-activa", tags=["Interno"], dependencies=[Depends(clave_servicio)],
         summary="¿Qué solicitud está atendiendo el operador?")
def solicitud_del_operador(id: Annotated[UUID, Path()]):
    # Recursos la usa como ubicación del operador: quien atiende una emergencia está en ella.
    with transaccion() as cur:
        cur.execute(
            """SELECT id, codigo, latitud, longitud, direccion FROM asistencia.solicitudes
               WHERE responsable_id = %s AND estado = 'en_atencion'
               ORDER BY actualizado_en DESC LIMIT 1""",
            (str(id),),
        )
        fila = cur.fetchone()
    if fila is None:
        return {"activa": False}
    id_, codigo, lat, lon, direccion = fila
    return {"activa": True, "solicitud_id": str(id_), "codigo": codigo,
            "ubicacion": {"latitud": _numero(lat), "longitud": _numero(lon), "direccion": direccion}}


@app.get("/v1/interno/solicitudes/{id}", tags=["Interno"], dependencies=[Depends(clave_servicio)],
         summary="Estado, responsable y ubicación de una solicitud")
def solicitud_interna(id: Annotated[UUID, Path()]):
    with transaccion() as cur:
        cur.execute(
            """SELECT id, codigo, estado, severidad, responsable_id, latitud, longitud, direccion
               FROM asistencia.solicitudes WHERE id = %s""",
            (str(id),),
        )
        fila = cur.fetchone()
    if fila is None:
        raise ErrorApi(404, "NO_ENCONTRADO", "No existe una solicitud con ese id.")
    id_, codigo, est, sev, resp, lat, lon, direccion = fila
    return {"id": str(id_), "codigo": codigo, "estado": est, "severidad": sev,
            "responsable_id": str(resp) if resp else None,
            "ubicacion": {"latitud": _numero(lat), "longitud": _numero(lon), "direccion": direccion}}
