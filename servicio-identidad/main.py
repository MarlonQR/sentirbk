from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID

import bcrypt
import jwt
import psycopg2
from fastapi import Depends, FastAPI, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field, field_validator

from con_db import transaccion
from seguridad import (
    ALGORITMO, ErrorApi, Usuario, fecha_iso, llave_jwt, registrar_manejadores, requiere_rol, usuario_actual,
)


app = FastAPI(
    title="SENTIR - Servicio de Identidad",
    description=(
        "Cuentas de usuario, roles y emisión de tokens JWT.\n\n"
        "Los demás servicios verifican el token con la llave compartida `JWT_SECRETO`, "
        "sin llamar a este servicio en cada petición.\n\n"
        "Para probar en Swagger: inicie sesión en **POST /v1/sesiones**, copie el `token` "
        "y péguelo en el botón **Authorize**."
    ),
    version="0.2.0",
    openapi_tags=[
        {"name": "Servicio", "description": "Verificación de que el servicio responde."},
        {"name": "Sesiones", "description": "Registro de ciudadanos e inicio de sesión."},
        {"name": "Administración", "description": "Gestión de operadores. Solo el rol administrador."},
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

VIGENCIA_TOKEN = {
    "ciudadano": timedelta(days=30),     # no pedir contraseña en plena emergencia
    "operador": timedelta(hours=12),     # dura un turno
    "administrador": timedelta(hours=2),  # permisos amplios, uso ocasional
}
MAX_INTENTOS = 5
VENTANA_INTENTOS = timedelta(minutes=15)

TipoDocumento = Literal["CC", "TI", "CE", "PA"]


def hash_contrasena(contrasena: str) -> str:
    return bcrypt.hashpw(contrasena.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def contrasena_correcta(contrasena: str, guardada: str) -> bool:
    try:
        return bcrypt.checkpw(contrasena.encode("utf-8"), guardada.encode("utf-8"))
    except ValueError:
        return False


# Hash fijo para comparar cuando la cuenta no existe: así la respuesta tarda
# lo mismo y no revela qué cuentas están registradas.
_HASH_SEÑUELO = hash_contrasena("cuenta-inexistente-sentir")


def emitir_token(usuario_id, rol: str, nombre: str) -> dict:
    ahora = datetime.now(timezone.utc)
    expira = ahora + VIGENCIA_TOKEN[rol]
    token = jwt.encode(
        {"sub": str(usuario_id), "rol": rol, "nombre": nombre, "iat": ahora, "exp": expira},
        llave_jwt(),
        algorithm=ALGORITMO,
    )
    return {"token": token, "expira": fecha_iso(expira)}


def usuario_json(fila) -> dict:
    id_, rol, nombre, correo, tipo_doc, num_doc, activo, creado = fila
    datos = {"id": str(id_), "nombre_completo": nombre, "rol": rol, "activo": activo, "creado_en": fecha_iso(creado)}
    if correo:
        datos["correo"] = correo
    if num_doc:
        datos["tipo_documento"] = tipo_doc
        datos["numero_documento"] = num_doc
    return datos


COLUMNAS = "id, rol, nombre_completo, correo, tipo_documento, numero_documento, activo, creado_en"


def ip_cliente(request: Request):
    # Detrás de Vercel la IP real llega en X-Forwarded-For; la primera es la del cliente.
    reenviada = request.headers.get("x-forwarded-for", "")
    ip = reenviada.split(",")[0].strip() if reenviada else (request.client.host if request.client else "")
    return ip[:45] or None


# ==================================================
# Modelos
# ==================================================

class NuevoCiudadano(BaseModel):
    nombre_completo: str = Field(min_length=3, max_length=120, examples=["Yeimy Carrascal"])
    tipo_documento: TipoDocumento = Field(examples=["CC"])
    numero_documento: str = Field(pattern=r"^[0-9A-Za-z]{4,20}$", examples=["1090456789"])
    contrasena: str = Field(min_length=8, max_length=72)

    @field_validator("nombre_completo")
    @classmethod
    def _limpiar(cls, valor: str) -> str:
        return " ".join(valor.split())


class Credenciales(BaseModel):
    identificador: str = Field(min_length=3, max_length=120, description="Correo (operador, administrador) o número de documento (ciudadano).", examples=["diego.pacheco@sentir.co"])
    contrasena: str = Field(min_length=1, max_length=72)


class NuevoOperador(BaseModel):
    nombre_completo: str = Field(min_length=3, max_length=120, examples=["Diego Pacheco"])
    correo: EmailStr = Field(examples=["diego.pacheco@sentir.co"])
    contrasena_temporal: str = Field(min_length=8, max_length=72)

    @field_validator("nombre_completo")
    @classmethod
    def _limpiar(cls, valor: str) -> str:
        return " ".join(valor.split())


class CambioUsuario(BaseModel):
    activo: bool


# ==================================================
# Servicio
# ==================================================

@app.get("/", tags=["Servicio"], summary="Estado del servicio")
def estado():
    return {"servicio": "identidad", "estado": "activo", "version": app.version}


# ==================================================
# Sesiones
# ==================================================

@app.post("/v1/ciudadanos", status_code=201, tags=["Sesiones"], summary="Registrar un ciudadano")
def registrar_ciudadano(datos: NuevoCiudadano):
    try:
        with transaccion() as cur:
            cur.execute(
                f"""INSERT INTO identidad.usuarios (rol, nombre_completo, tipo_documento, numero_documento, contrasena_hash)
                    VALUES ('ciudadano', %s, %s, %s, %s) RETURNING {COLUMNAS}""",
                (datos.nombre_completo, datos.tipo_documento, datos.numero_documento, hash_contrasena(datos.contrasena)),
            )
            fila = cur.fetchone()
    except psycopg2.errors.UniqueViolation:
        raise ErrorApi(409, "DOCUMENTO_YA_REGISTRADO", "Ya existe una cuenta con ese número de documento.")
    usuario = usuario_json(fila)
    return {"usuario": usuario, **emitir_token(usuario["id"], "ciudadano", usuario["nombre_completo"])}


@app.post("/v1/sesiones", tags=["Sesiones"], summary="Iniciar sesión y recibir el token")
def iniciar_sesion(datos: Credenciales, request: Request):
    identificador = datos.identificador.strip().lower() if "@" in datos.identificador else datos.identificador.strip()
    with transaccion() as cur:
        cur.execute(
            "SELECT count(*), min(fecha) FROM identidad.intentos_fallidos WHERE identificador = %s AND fecha > now() - %s",
            (identificador, VENTANA_INTENTOS),
        )
        intentos, primero = cur.fetchone()
        if intentos >= MAX_INTENTOS:
            espera = int((primero + VENTANA_INTENTOS - datetime.now(timezone.utc)).total_seconds()) + 1
            raise ErrorApi(
                429, "DEMASIADAS_PETICIONES",
                "Demasiados intentos fallidos. Espere antes de intentar de nuevo.",
                encabezados={"Retry-After": str(max(espera, 1))},
            )

        campo = "lower(correo)" if "@" in identificador else "numero_documento"
        cur.execute(f"SELECT {COLUMNAS}, contrasena_hash FROM identidad.usuarios WHERE {campo} = %s", (identificador,))
        fila = cur.fetchone()

        valida = contrasena_correcta(datos.contrasena, fila[-1] if fila else _HASH_SEÑUELO) and fila is not None
        if not valida:
            cur.execute("INSERT INTO identidad.intentos_fallidos (identificador) VALUES (%s)", (identificador,))
        else:
            cur.execute("DELETE FROM identidad.intentos_fallidos WHERE identificador = %s", (identificador,))
            # Registro de accesos: quién entró, cuándo y desde dónde.
            cur.execute(
                """INSERT INTO identidad.inicios_sesion (usuario_id, resultado, ip, agente)
                   VALUES (%s, %s, %s, %s)""",
                (fila[0], "exitoso" if fila[6] else "cuenta_inactiva", ip_cliente(request),
                 (request.headers.get("user-agent") or "")[:200] or None),
            )

    # Se lanza fuera del bloque para que el intento fallido quede guardado.
    if not valida:
        raise ErrorApi(401, "CREDENCIALES_INVALIDAS", "El identificador o la contraseña no son correctos.")
    usuario = usuario_json(fila[:-1])
    if not usuario["activo"]:
        raise ErrorApi(403, "USUARIO_INACTIVO", "La cuenta está desactivada. Contacte al administrador.")
    return {"usuario": usuario, **emitir_token(usuario["id"], usuario["rol"], usuario["nombre_completo"])}


@app.get("/v1/usuarios/yo", tags=["Sesiones"], summary="Datos del dueño del token")
def usuario_yo(usuario: Annotated[Usuario, Depends(usuario_actual)]):
    with transaccion() as cur:
        cur.execute(f"SELECT {COLUMNAS} FROM identidad.usuarios WHERE id = %s", (usuario.id,))
        fila = cur.fetchone()
    if fila is None:
        raise ErrorApi(404, "NO_ENCONTRADO", "La cuenta del token ya no existe.")
    return usuario_json(fila)


# ==================================================
# Administración
# ==================================================

SoloAdministrador = Annotated[Usuario, Depends(requiere_rol("administrador"))]


@app.post("/v1/operadores", status_code=201, tags=["Administración"], summary="Registrar un operador")
def registrar_operador(datos: NuevoOperador, admin: SoloAdministrador):
    try:
        with transaccion() as cur:
            cur.execute(
                f"""INSERT INTO identidad.usuarios (rol, nombre_completo, correo, contrasena_hash)
                    VALUES ('operador', %s, %s, %s) RETURNING {COLUMNAS}""",
                (datos.nombre_completo, datos.correo.lower(), hash_contrasena(datos.contrasena_temporal)),
            )
            fila = cur.fetchone()
    except psycopg2.errors.UniqueViolation:
        raise ErrorApi(409, "CORREO_YA_REGISTRADO", "Ya existe una cuenta con ese correo.")
    return usuario_json(fila)


@app.get("/v1/operadores", tags=["Administración"], summary="Listar operadores")
def listar_operadores(
    admin: SoloAdministrador,
    pagina: Annotated[int, Query(ge=1)] = 1,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
):
    with transaccion() as cur:
        cur.execute("SELECT count(*) FROM identidad.usuarios WHERE rol = 'operador'")
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT {COLUMNAS} FROM identidad.usuarios WHERE rol = 'operador' ORDER BY nombre_completo LIMIT %s OFFSET %s",
            (limite, (pagina - 1) * limite),
        )
        filas = cur.fetchall()
    return {"total": total, "pagina": pagina, "limite": limite, "elementos": [usuario_json(f) for f in filas]}


@app.get("/v1/usuarios/{id}/inicios-sesion", tags=["Administración"], summary="Registro de inicios de sesión de una cuenta")
def inicios_sesion(
    id: Annotated[UUID, Path()],
    admin: SoloAdministrador,
    pagina: Annotated[int, Query(ge=1)] = 1,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
):
    with transaccion() as cur:
        cur.execute("SELECT 1 FROM identidad.usuarios WHERE id = %s", (str(id),))
        if cur.fetchone() is None:
            raise ErrorApi(404, "NO_ENCONTRADO", "No existe una cuenta con ese id.")
        cur.execute("SELECT count(*) FROM identidad.inicios_sesion WHERE usuario_id = %s", (str(id),))
        total = cur.fetchone()[0]
        cur.execute(
            """SELECT fecha, resultado, ip, agente FROM identidad.inicios_sesion
               WHERE usuario_id = %s ORDER BY fecha DESC LIMIT %s OFFSET %s""",
            (str(id), limite, (pagina - 1) * limite),
        )
        elementos = [{"fecha": fecha_iso(f), "resultado": r, "ip": ip, "agente": a} for f, r, ip, a in cur.fetchall()]
    return {"total": total, "pagina": pagina, "limite": limite, "elementos": elementos}


@app.patch("/v1/usuarios/{id}", tags=["Administración"], summary="Activar o desactivar una cuenta")
def cambiar_usuario(id: Annotated[UUID, Path()], datos: CambioUsuario, admin: SoloAdministrador):
    if str(id) == admin.id and not datos.activo:
        raise ErrorApi(409, "NO_PUEDE_DESACTIVARSE", "Un administrador no puede desactivar su propia cuenta.")
    with transaccion() as cur:
        cur.execute(
            f"UPDATE identidad.usuarios SET activo = %s, actualizado_en = now() WHERE id = %s RETURNING {COLUMNAS}",
            (datos.activo, str(id)),
        )
        fila = cur.fetchone()
    if fila is None:
        raise ErrorApi(404, "NO_ENCONTRADO", "No existe una cuenta con ese id.")
    return usuario_json(fila)
