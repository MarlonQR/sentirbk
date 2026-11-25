"""
Cifrado del perfil médico con AES-256-GCM (hallazgo H5).

- La llave vive en la variable de entorno PERFIL_LLAVE: 32 bytes en base64.
  Nunca en la base de datos ni en el repositorio.
- Cada cifrado usa un nonce aleatorio de 12 bytes, que se guarda junto al texto.
- El id del ciudadano entra como dato asociado: un perfil cifrado copiado a la
  fila de otro ciudadano no se puede descifrar.
"""
import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from seguridad import ErrorApi

VERSION = "v1"


def _llave() -> AESGCM:
    texto = os.getenv("PERFIL_LLAVE", "").strip()
    try:
        llave = base64.b64decode(texto, validate=True)
    except ValueError:
        llave = b""
    if len(llave) != 32:
        raise ErrorApi(500, "CONFIGURACION_INCOMPLETA",
                       "El servicio no tiene una PERFIL_LLAVE válida (32 bytes en base64).")
    return AESGCM(llave)


def cifrar(datos: dict, ciudadano_id: str) -> str:
    nonce = os.urandom(12)
    texto = json.dumps(datos, ensure_ascii=False).encode("utf-8")
    cifrado = _llave().encrypt(nonce, texto, ciudadano_id.encode("utf-8"))
    return f"{VERSION}:" + base64.b64encode(nonce + cifrado).decode("ascii")


def descifrar(guardado: str, ciudadano_id: str) -> dict:
    version, _, contenido = guardado.partition(":")
    if version != VERSION:
        raise ErrorApi(500, "DATOS_ILEGIBLES", "El perfil está guardado en un formato desconocido.")
    crudo = base64.b64decode(contenido)
    try:
        texto = _llave().decrypt(crudo[:12], crudo[12:], ciudadano_id.encode("utf-8"))
    except InvalidTag:
        # Llave cambiada o dato alterado. No se devuelve nada a medias.
        raise ErrorApi(500, "DATOS_ILEGIBLES", "No fue posible descifrar el perfil.")
    return json.loads(texto)
