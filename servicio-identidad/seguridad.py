"""
Piezas comunes a todos los servicios de SENTIR: formato de errores,
verificación del token JWT y de la clave entre servicios.

Cada servicio tiene su propia copia de este archivo porque en Vercel
cada carpeta se despliega como un proyecto independiente.
Si cambia aquí, cambie también la copia de los demás servicios.
"""
import os
import secrets
from datetime import datetime, timezone
from typing import Annotated, Optional

import jwt
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException as StarletteHTTPException


# ==================================================
# Errores con la forma del contrato:
# {"error": {"codigo": ..., "mensaje": ..., "detalles": ...}}
# ==================================================

class ErrorApi(Exception):
    def __init__(self, estado: int, codigo: str, mensaje: str, detalles=None, encabezados=None):
        self.estado = estado
        self.codigo = codigo
        self.mensaje = mensaje
        self.detalles = detalles
        self.encabezados = encabezados or {}


def _respuesta_error(estado, codigo, mensaje, detalles=None, encabezados=None):
    return JSONResponse(
        status_code=estado,
        content={"error": {"codigo": codigo, "mensaje": mensaje, "detalles": detalles}},
        headers=encabezados,
    )


_CODIGOS_HTTP = {404: "NO_ENCONTRADO", 405: "METODO_NO_PERMITIDO"}


def registrar_manejadores(app: FastAPI):
    @app.exception_handler(ErrorApi)
    async def _error_api(request: Request, error: ErrorApi):
        return _respuesta_error(error.estado, error.codigo, error.mensaje, error.detalles, error.encabezados)

    @app.exception_handler(RequestValidationError)
    async def _error_validacion(request: Request, error: RequestValidationError):
        detalles = [
            {
                "campo": ".".join(str(parte) for parte in e["loc"] if parte != "body"),
                "problema": e["msg"],
            }
            for e in error.errors()
        ]
        return _respuesta_error(422, "DATOS_INVALIDOS", "Los datos enviados no cumplen el contrato.", detalles)

    @app.exception_handler(StarletteHTTPException)
    async def _error_http(request: Request, error: StarletteHTTPException):
        codigo = _CODIGOS_HTTP.get(error.status_code, "ERROR_HTTP")
        return _respuesta_error(error.status_code, codigo, str(error.detail))


# ==================================================
# Token JWT (HS256 con la llave compartida JWT_SECRETO)
# ==================================================

ALGORITMO = "HS256"


def llave_jwt() -> str:
    llave = os.getenv("JWT_SECRETO", "").strip()
    if len(llave) < 32:
        # Sin llave no hay forma segura de firmar ni verificar.
        raise ErrorApi(500, "CONFIGURACION_INCOMPLETA", "El servicio no tiene configurada la variable JWT_SECRETO.")
    return llave


_esquema_bearer = HTTPBearer(auto_error=False, description="Token que entrega POST /v1/sesiones")


class Usuario:
    def __init__(self, datos: dict):
        self.id = datos["sub"]
        self.rol = datos["rol"]
        self.nombre = datos.get("nombre", "")


def usuario_actual(
    credenciales: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_esquema_bearer)],
) -> Usuario:
    if credenciales is None or not credenciales.credentials:
        raise ErrorApi(401, "TOKEN_AUSENTE", "Falta el encabezado Authorization: Bearer <token>.")
    try:
        datos = jwt.decode(
            credenciales.credentials,
            llave_jwt(),
            algorithms=[ALGORITMO],
            options={"require": ["sub", "rol", "exp"]},
        )
    except jwt.ExpiredSignatureError:
        raise ErrorApi(401, "TOKEN_INVALIDO", "El token venció. Inicie sesión de nuevo.")
    except jwt.InvalidTokenError:
        raise ErrorApi(401, "TOKEN_INVALIDO", "El token no es válido.")
    return Usuario(datos)


def requiere_rol(*roles: str):
    def _verificar(usuario: Annotated[Usuario, Depends(usuario_actual)]) -> Usuario:
        if usuario.rol not in roles:
            raise ErrorApi(403, "ROL_NO_PERMITIDO", f"Esta operación es solo para el rol {' o '.join(roles)}.")
        return usuario
    return _verificar


# ==================================================
# Clave entre servicios (rutas /v1/interno)
# ==================================================

def clave_servicio(x_clave_servicio: Annotated[Optional[str], Header()] = None):
    esperada = os.getenv("CLAVE_SERVICIO", "").strip()
    if not esperada:
        raise ErrorApi(500, "CONFIGURACION_INCOMPLETA", "El servicio no tiene configurada la variable CLAVE_SERVICIO.")
    if not x_clave_servicio or not secrets.compare_digest(x_clave_servicio, esperada):
        raise ErrorApi(401, "CLAVE_SERVICIO_INVALIDA", "Falta el encabezado X-Clave-Servicio o no es correcto.")


# ==================================================
# Fechas en ISO 8601 UTC con Z
# ==================================================

def fecha_iso(valor: Optional[datetime]) -> Optional[str]:
    if valor is None:
        return None
    return valor.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
