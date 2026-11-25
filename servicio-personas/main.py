import os
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal, Optional
from uuid import UUID

import requests
from fastapi import Depends, FastAPI, Path, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from cifrado import cifrar, descifrar
from con_db import transaccion
from seguridad import ErrorApi, Usuario, fecha_iso, registrar_manejadores, requiere_rol


app = FastAPI(
    title="SENTIR - Servicio de Personas",
    description=(
        "Datos de cada ciudadano, contactos de emergencia y perfil médico.\n\n"
        "El perfil médico y los contactos se guardan cifrados con AES-256-GCM. "
        "Un operador solo puede ver el perfil de un ciudadano que tenga una solicitud "
        "activa en Asistencia, y cada consulta queda registrada.\n\n"
        "Para guardar un perfil, el ciudadano debe aceptar antes la autorización de "
        "tratamiento de datos de salud (Ley 1581 de 2012)."
    ),
    version="0.2.0",
    openapi_tags=[
        {"name": "Servicio", "description": "Verificación de que el servicio responde."},
        {"name": "Ciudadano", "description": "Autorización y perfil propio. Hoy lo usa el simulador."},
        {"name": "Operador", "description": "Pantalla de la persona afectada."},
        {"name": "Administración", "description": "Auditoría de accesos. Solo el rol administrador."},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
registrar_manejadores(app)


@app.middleware("http")
async def sin_cache(request: Request, call_next):
    # Datos de salud: ningún intermediario debe guardar copia (contrato, sección 4).
    respuesta = await call_next(request)
    respuesta.headers["Cache-Control"] = "no-store"
    return respuesta


# ==================================================
# Reglas
# ==================================================

TipoSangre = Literal["O+", "O-", "A+", "A-", "B+", "B-", "AB+", "AB-", "desconocido"]
TipoDocumento = Literal["CC", "TI", "CE", "PA"]
ZONA_COLOMBIA = timezone(timedelta(hours=-5))
MAX_CONTACTOS = 3
PATRON_TELEFONO = r"^\+?[0-9]{7,15}$"

Texto100 = Annotated[str, Field(min_length=1, max_length=100)]


def edad(nacimiento: date) -> int:
    hoy = datetime.now(ZONA_COLOMBIA).date()
    return hoy.year - nacimiento.year - ((hoy.month, hoy.day) < (nacimiento.month, nacimiento.day))


# ==================================================
# Consulta a Asistencia (única dependencia de este servicio)
# ==================================================

def solicitud_activa(ciudadano_id: str) -> dict:
    url = os.getenv("ASISTENCIA_URL", "").strip().rstrip("/")
    if not url:
        raise ErrorApi(500, "CONFIGURACION_INCOMPLETA", "El servicio no tiene configurada la variable ASISTENCIA_URL.")
    try:
        respuesta = requests.get(
            f"{url}/v1/interno/ciudadanos/{ciudadano_id}/solicitud-activa",
            headers={"X-Clave-Servicio": os.getenv("CLAVE_SERVICIO", "").strip()},
            timeout=(3, 8),
        )
    except requests.RequestException as error:
        print(f"Asistencia no respondió: {error}")
        respuesta = None
    if respuesta is None or respuesta.status_code != 200:
        if respuesta is not None:
            print(f"Asistencia respondió {respuesta.status_code}: {respuesta.text[:200]}")
        # Sin confirmar la solicitud activa no se entrega el perfil (riesgo aceptado en el contrato).
        raise ErrorApi(503, "SERVICIO_NO_DISPONIBLE",
                       "No fue posible confirmar con Asistencia que el ciudadano tenga una solicitud activa.")
    return respuesta.json()


# ==================================================
# Modelos
# ==================================================

class ContactoEmergencia(BaseModel):
    nombre_completo: str = Field(min_length=3, max_length=120, examples=["Andrés Ropero"])
    telefono: str = Field(pattern=PATRON_TELEFONO, examples=["+573209876543"])
    parentesco: str = Field(min_length=2, max_length=40, examples=["Hermano"])


class Perfil(BaseModel):
    nombre_completo: str = Field(min_length=3, max_length=120, examples=["Yeimy Carrascal"])
    tipo_documento: TipoDocumento = Field(examples=["CC"])
    numero_documento: str = Field(pattern=r"^[0-9A-Za-z]{4,20}$", examples=["1090456789"])
    fecha_nacimiento: date = Field(examples=["1992-03-14"])
    telefono: str = Field(pattern=PATRON_TELEFONO, examples=["+573154567890"])
    tipo_sangre: TipoSangre = Field(examples=["O+"])
    alergias: list[Texto100] = Field(default_factory=list, max_length=20, examples=[["Penicilina", "Polvo"]])
    condiciones_cronicas: list[Texto100] = Field(default_factory=list, max_length=20, examples=[["Asma bronquial"]])
    medicamentos: list[Texto100] = Field(default_factory=list, max_length=20, examples=[["Salbutamol"]])
    contactos_emergencia: list[ContactoEmergencia] = Field(default_factory=list, max_length=MAX_CONTACTOS)

    @field_validator("nombre_completo")
    @classmethod
    def _limpiar(cls, valor: str) -> str:
        return " ".join(valor.split())

    @field_validator("fecha_nacimiento")
    @classmethod
    def _fecha_razonable(cls, valor: date) -> date:
        if valor > datetime.now(ZONA_COLOMBIA).date():
            raise ValueError("la fecha de nacimiento no puede estar en el futuro")
        if valor < date(1900, 1, 1):
            raise ValueError("la fecha de nacimiento no puede ser anterior a 1900")
        return valor


class NuevaAutorizacion(BaseModel):
    version_texto: str = Field(min_length=1, max_length=40, examples=["2026-11-v1"])
    acepta: bool = Field(examples=[True])


Ciudadano = Annotated[Usuario, Depends(requiere_rol("ciudadano"))]
Operador = Annotated[Usuario, Depends(requiere_rol("operador"))]
Administrador = Annotated[Usuario, Depends(requiere_rol("administrador"))]

PROTEGIDOS = ("tipo_sangre", "alergias", "condiciones_cronicas", "medicamentos", "contactos_emergencia")


def perfil_json(fila) -> dict:
    ciudadano_id, nombre, tipo_doc, num_doc, nacimiento, telefono, protegidos, actualizado = fila
    datos = {
        "ciudadano_id": str(ciudadano_id),
        "nombre_completo": nombre,
        "tipo_documento": tipo_doc,
        "numero_documento": num_doc,
        "fecha_nacimiento": nacimiento.isoformat(),
        "edad": edad(nacimiento),
        "telefono": telefono,
    }
    datos.update(descifrar(protegidos, str(ciudadano_id)))
    datos["fecha_actualizacion"] = fecha_iso(actualizado)
    return datos


COLUMNAS = """ciudadano_id, nombre_completo, tipo_documento, numero_documento, fecha_nacimiento,
    telefono, datos_protegidos, fecha_actualizacion"""


def cargar_perfil(cur, ciudadano_id: str) -> Optional[dict]:
    cur.execute(f"SELECT {COLUMNAS} FROM personas.perfiles WHERE ciudadano_id = %s", (ciudadano_id,))
    fila = cur.fetchone()
    return perfil_json(fila) if fila else None


# ==================================================
# Servicio
# ==================================================

@app.get("/", tags=["Servicio"], summary="Estado del servicio")
def estado():
    return {"servicio": "personas", "estado": "activo", "version": app.version}


# ==================================================
# Ciudadano
# ==================================================

@app.post("/v1/autorizaciones", status_code=201, tags=["Ciudadano"], summary="Aceptar el tratamiento de datos de salud")
def autorizar(datos: NuevaAutorizacion, ciudadano: Ciudadano, response: Response):
    if not datos.acepta:
        raise ErrorApi(422, "DATOS_INVALIDOS", "Los datos enviados no cumplen el contrato.",
                       [{"campo": "acepta", "problema": "Para registrar la autorización el valor debe ser true."}])
    with transaccion() as cur:
        cur.execute(
            """SELECT version_texto, aceptada_en FROM personas.autorizaciones
               WHERE ciudadano_id = %s AND retirada_en IS NULL FOR UPDATE""",
            (ciudadano.id,),
        )
        vigente = cur.fetchone()
        if vigente and vigente[0] == datos.version_texto:
            response.status_code = 200
            return {"version_texto": vigente[0], "fecha": fecha_iso(vigente[1])}
        if vigente:
            # Acepta una versión nueva del texto: la anterior queda cerrada, no borrada.
            cur.execute("UPDATE personas.autorizaciones SET retirada_en = now() WHERE ciudadano_id = %s AND retirada_en IS NULL",
                        (ciudadano.id,))
        cur.execute(
            "INSERT INTO personas.autorizaciones (ciudadano_id, version_texto) VALUES (%s, %s) RETURNING aceptada_en",
            (ciudadano.id, datos.version_texto),
        )
        return {"version_texto": datos.version_texto, "fecha": fecha_iso(cur.fetchone()[0])}


@app.delete("/v1/autorizaciones/actual", status_code=204, tags=["Ciudadano"],
            summary="Retirar la autorización; borra el perfil")
def retirar_autorizacion(ciudadano: Ciudadano):
    with transaccion() as cur:
        cur.execute(
            "UPDATE personas.autorizaciones SET retirada_en = now() WHERE ciudadano_id = %s AND retirada_en IS NULL RETURNING id",
            (ciudadano.id,),
        )
        if cur.fetchone() is None:
            raise ErrorApi(404, "NO_ENCONTRADO", "No hay una autorización vigente para retirar.")
        # Sin autorización no se pueden conservar los datos de salud.
        cur.execute("DELETE FROM personas.perfiles WHERE ciudadano_id = %s", (ciudadano.id,))
    return Response(status_code=204)


@app.put("/v1/perfiles/yo", tags=["Ciudadano"], summary="Crear o reemplazar el perfil propio")
def guardar_perfil(datos: Perfil, ciudadano: Ciudadano):
    protegidos = cifrar({k: v for k, v in datos.model_dump(mode="json").items() if k in PROTEGIDOS}, ciudadano.id)
    with transaccion() as cur:
        cur.execute("SELECT 1 FROM personas.autorizaciones WHERE ciudadano_id = %s AND retirada_en IS NULL", (ciudadano.id,))
        if cur.fetchone() is None:
            raise ErrorApi(403, "AUTORIZACION_PENDIENTE",
                           "Antes de guardar el perfil hay que aceptar la autorización de datos de salud (POST /v1/autorizaciones).")
        cur.execute(
            """INSERT INTO personas.perfiles (ciudadano_id, nombre_completo, tipo_documento, numero_documento,
                                              fecha_nacimiento, telefono, datos_protegidos)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (ciudadano_id) DO UPDATE SET
                   nombre_completo = EXCLUDED.nombre_completo, tipo_documento = EXCLUDED.tipo_documento,
                   numero_documento = EXCLUDED.numero_documento, fecha_nacimiento = EXCLUDED.fecha_nacimiento,
                   telefono = EXCLUDED.telefono, datos_protegidos = EXCLUDED.datos_protegidos,
                   fecha_actualizacion = now()""",
            (ciudadano.id, datos.nombre_completo, datos.tipo_documento, datos.numero_documento,
             datos.fecha_nacimiento, datos.telefono, protegidos),
        )
        return cargar_perfil(cur, ciudadano.id)


@app.get("/v1/perfiles/yo", tags=["Ciudadano"], summary="Ver el perfil propio")
def ver_perfil_propio(ciudadano: Ciudadano):
    with transaccion() as cur:
        perfil = cargar_perfil(cur, ciudadano.id)
    if perfil is None:
        raise ErrorApi(404, "PERFIL_NO_REGISTRADO", "Todavía no ha registrado su perfil.")
    return perfil


# ==================================================
# Operador
# ==================================================

@app.get("/v1/perfiles/{id_ciudadano}", tags=["Operador"], summary="Ver la persona afectada (pantalla 3)")
def ver_perfil(id_ciudadano: Annotated[UUID, Path()], operador: Operador):
    ciudadano_id = str(id_ciudadano)
    # 1. Rol verificado por la dependencia. 2. Solicitud activa en Asistencia.
    activa = solicitud_activa(ciudadano_id)
    if not activa.get("activa"):
        raise ErrorApi(403, "SIN_SOLICITUD_ACTIVA",
                       "El ciudadano no tiene una solicitud pendiente o en atención; su perfil no se puede consultar.")
    with transaccion() as cur:
        # 3. Perfil registrado. 4. Descifrar y registrar el acceso.
        perfil = cargar_perfil(cur, ciudadano_id)
        if perfil is None:
            raise ErrorApi(404, "PERFIL_NO_REGISTRADO", "El ciudadano no ha registrado su perfil.")
        cur.execute(
            """INSERT INTO personas.accesos (ciudadano_id, operador_id, operador_nombre, solicitud_id)
               VALUES (%s, %s, %s, %s)""",
            (ciudadano_id, operador.id, operador.nombre, activa["solicitud_id"]),
        )
    return {"ciudadano_id": perfil.pop("ciudadano_id"), "solicitud_id": activa["solicitud_id"], **perfil}


# ==================================================
# Administración
# ==================================================

@app.get("/v1/perfiles/{id_ciudadano}/accesos", tags=["Administración"], summary="Ver quién consultó un perfil")
def accesos(
    id_ciudadano: Annotated[UUID, Path()],
    admin: Administrador,
    pagina: Annotated[int, Query(ge=1)] = 1,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
):
    with transaccion() as cur:
        cur.execute("SELECT count(*) FROM personas.accesos WHERE ciudadano_id = %s", (str(id_ciudadano),))
        total = cur.fetchone()[0]
        cur.execute(
            """SELECT fecha, operador_id, operador_nombre, solicitud_id FROM personas.accesos
               WHERE ciudadano_id = %s ORDER BY fecha DESC, id DESC LIMIT %s OFFSET %s""",
            (str(id_ciudadano), limite, (pagina - 1) * limite),
        )
        elementos = [
            {"fecha": fecha_iso(f), "operador": {"id": str(oi), "nombre_completo": on}, "solicitud_id": str(s)}
            for f, oi, on, s in cur.fetchall()
        ]
    return {"total": total, "pagina": pagina, "limite": limite, "elementos": elementos}
