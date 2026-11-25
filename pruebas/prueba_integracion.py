"""
Prueba de integración de los servicios de Identidad y Asistencia de SENTIR.

Corre igual contra local o contra producción. Se configura con variables de entorno:

    IDENTIDAD_URL      http://127.0.0.1:8001  o  https://...vercel.app
    ASISTENCIA_URL     http://127.0.0.1:8002  o  https://...vercel.app
    ADMIN_CORREO       correo del administrador inicial
    ADMIN_CONTRASENA   su contraseña
    CLAVE_SERVICIO     la misma que tiene configurada Asistencia
    PUERTO_EVENTOS     (opcional) puerto local donde escuchar los eventos que
                       Asistencia publica. Solo en local, con ALERTAS_URL apuntando ahí.
    EVIDENCIA          (opcional) archivo donde guardar la salida

Uso en PowerShell:
    $env:IDENTIDAD_URL="http://127.0.0.1:8001"; ...; python pruebas/prueba_integracion.py
"""
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# strip(): al pegar en PowerShell a veces se cuela un salto de línea o un espacio.
IDENTIDAD = os.environ["IDENTIDAD_URL"].strip().rstrip("/")
ASISTENCIA = os.environ["ASISTENCIA_URL"].strip().rstrip("/")
ADMIN_CORREO = os.environ["ADMIN_CORREO"].strip()
ADMIN_CONTRASENA = os.environ["ADMIN_CONTRASENA"].strip()
CLAVE_SERVICIO = os.environ["CLAVE_SERVICIO"].strip()
PERSONAS = (os.getenv("PERSONAS_URL", "").strip().rstrip("/")) or None
PUERTO_EVENTOS = os.getenv("PUERTO_EVENTOS", "").strip() or None
EVIDENCIA = os.getenv("EVIDENCIA", "").strip() or None

lineas = []
fallas = 0
aprobadas = 0


def anotar(texto=""):
    print(texto)
    lineas.append(texto)


def verificar(descripcion, condicion, detalle=""):
    global fallas, aprobadas
    if condicion:
        aprobadas += 1
        anotar(f"  [OK]    {descripcion}")
    else:
        fallas += 1
        anotar(f"  [FALLA] {descripcion} {detalle}")


def ahora_iso(desplazamiento=timedelta()):
    return (datetime.now(timezone.utc) + desplazamiento).strftime("%Y-%m-%dT%H:%M:%SZ")


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------
# Receptor de eventos (hace de servicio de Alertas en local)
# --------------------------------------------------
eventos = []


class Receptor(BaseHTTPRequestHandler):
    def do_POST(self):
        cuerpo = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        eventos.append({"clave": self.headers.get("X-Clave-Servicio"), **json.loads(cuerpo)})
        self.send_response(202)
        self.end_headers()

    def log_message(self, *args):
        pass


if PUERTO_EVENTOS:
    servidor = HTTPServer(("127.0.0.1", int(PUERTO_EVENTOS)), Receptor)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()


# --------------------------------------------------
sufijo = uuid.uuid4().hex[:8]
anotar(f"Prueba de integración SENTIR - {ahora_iso()}")
anotar(f"Identidad:  {IDENTIDAD}")
anotar(f"Asistencia: {ASISTENCIA}")
anotar(f"Personas:   {PERSONAS or '(no se prueba)'}")
anotar(f"Ejecución:  {sufijo}")

anotar("\n1. Los servicios responden")
r = requests.get(f"{IDENTIDAD}/", timeout=30)
verificar("Identidad responde", r.status_code == 200 and r.json()["servicio"] == "identidad", r.text)
r = requests.get(f"{ASISTENCIA}/", timeout=30)
verificar("Asistencia responde", r.status_code == 200 and r.json()["servicio"] == "asistencia", r.text)

anotar("\n2. Administrador y operadores")
r = requests.post(f"{IDENTIDAD}/v1/sesiones", json={"identificador": ADMIN_CORREO, "contrasena": ADMIN_CONTRASENA})
verificar("El administrador inicia sesión", r.status_code == 200 and r.json()["usuario"]["rol"] == "administrador", r.text)
admin = r.json()["token"]

operadores = {}
for nombre in ("Diego Pacheco", "Laura Sanjuán"):
    correo = f"{nombre.split()[0].lower()}.{sufijo}@sentir.co"
    r = requests.post(f"{IDENTIDAD}/v1/operadores", headers=auth(admin),
                      json={"nombre_completo": nombre, "correo": correo, "contrasena_temporal": "Turno-Seguro-1"})
    verificar(f"Se registra al operador {nombre}", r.status_code == 201, r.text)
    r = requests.post(f"{IDENTIDAD}/v1/sesiones", json={"identificador": correo.upper(), "contrasena": "Turno-Seguro-1"})
    verificar(f"{nombre} inicia sesión (el correo no distingue mayúsculas)", r.status_code == 200, r.text)
    operadores[nombre] = {"token": r.json()["token"], "id": r.json()["usuario"]["id"], "correo": correo}
diego, laura = operadores["Diego Pacheco"], operadores["Laura Sanjuán"]

r = requests.post(f"{IDENTIDAD}/v1/operadores", headers=auth(admin),
                  json={"nombre_completo": "Repetido", "correo": diego["correo"], "contrasena_temporal": "Turno-Seguro-1"})
verificar("Un correo repetido da 409 CORREO_YA_REGISTRADO", r.status_code == 409 and r.json()["error"]["codigo"] == "CORREO_YA_REGISTRADO", r.text)
r = requests.post(f"{IDENTIDAD}/v1/operadores", headers=auth(diego["token"]),
                  json={"nombre_completo": "Intruso", "correo": f"x.{sufijo}@sentir.co", "contrasena_temporal": "Turno-Seguro-1"})
verificar("Un operador no puede crear operadores (403)", r.status_code == 403 and r.json()["error"]["codigo"] == "ROL_NO_PERMITIDO", r.text)
r = requests.get(f"{IDENTIDAD}/v1/operadores", headers=auth(admin))
verificar("El administrador lista operadores", r.status_code == 200 and r.json()["total"] >= 2, r.text)

anotar("\n3. Ciudadanos (lo que hará el simulador)")
ciudadanos = {}
for i, nombre in enumerate(("Yeimy Carrascal", "Andrés Ropero", "Natalia Ascanio", "Camilo Quintero")):
    documento = f"{int(sufijo, 16) % 10**8:08d}{i}"
    r = requests.post(f"{IDENTIDAD}/v1/ciudadanos",
                      json={"nombre_completo": nombre, "tipo_documento": "CC", "numero_documento": documento, "contrasena": "Ciudadano-2026"})
    verificar(f"Se registra {nombre} y recibe token", r.status_code == 201 and r.json()["token"], r.text)
    ciudadanos[nombre] = {"token": r.json()["token"], "id": r.json()["usuario"]["id"], "documento": documento}
yeimy, andres, natalia, camilo = (ciudadanos[n] for n in ("Yeimy Carrascal", "Andrés Ropero", "Natalia Ascanio", "Camilo Quintero"))

r = requests.post(f"{IDENTIDAD}/v1/ciudadanos",
                  json={"nombre_completo": "Otra", "tipo_documento": "CC", "numero_documento": yeimy["documento"], "contrasena": "Ciudadano-2026"})
verificar("Un documento repetido da 409 DOCUMENTO_YA_REGISTRADO", r.status_code == 409 and r.json()["error"]["codigo"] == "DOCUMENTO_YA_REGISTRADO", r.text)
r = requests.post(f"{IDENTIDAD}/v1/sesiones", json={"identificador": yeimy["documento"], "contrasena": "Ciudadano-2026"})
verificar("El ciudadano inicia sesión con su documento", r.status_code == 200 and r.json()["usuario"]["rol"] == "ciudadano", r.text)
r = requests.get(f"{IDENTIDAD}/v1/usuarios/yo", headers=auth(yeimy["token"]))
verificar("GET /v1/usuarios/yo devuelve al dueño del token", r.status_code == 200 and r.json()["id"] == yeimy["id"], r.text)
r = requests.get(f"{IDENTIDAD}/v1/usuarios/yo")
verificar("Sin token: 401 TOKEN_AUSENTE", r.status_code == 401 and r.json()["error"]["codigo"] == "TOKEN_AUSENTE", r.text)
r = requests.get(f"{IDENTIDAD}/v1/usuarios/yo", headers=auth(yeimy["token"][:-3] + "abc"))
verificar("Token alterado: 401 TOKEN_INVALIDO", r.status_code == 401 and r.json()["error"]["codigo"] == "TOKEN_INVALIDO", r.text)

anotar("\n4. Catálogo y validaciones de la solicitud")
r = requests.get(f"{ASISTENCIA}/v1/tipos-emergencia", headers=auth(yeimy["token"]))
codigos = [t["codigo"] for t in r.json()] if r.status_code == 200 else []
verificar("El catálogo tiene los 6 tipos acordados", codigos == ["terremoto", "tsunami", "incendio", "derrumbe", "inundacion", "otro"], r.text)

ubicacion = {"latitud": 7.893912, "longitud": -72.507834, "precision_metros": 15, "direccion": "Calle 10 # 4-32, barrio La Cabrera, Cúcuta"}


def solicitud(tipo="derrumbe", **extra):
    base = {"id": str(uuid.uuid4()), "codigo_tipo": tipo, "descripcion": "Se cayó parte de la casa vecina. Hay dos personas atrapadas.",
            "ubicacion": ubicacion, "fecha_hora_dispositivo": ahora_iso()}
    base.update(extra)
    return base


r = requests.post(f"{ASISTENCIA}/v1/solicitudes", headers=auth(andres["token"]), json=solicitud(ubicacion={"precision_metros": 5}))
verificar("Sin coordenadas ni dirección: 422", r.status_code == 422 and r.json()["error"]["codigo"] == "DATOS_INVALIDOS", r.text)
r = requests.post(f"{ASISTENCIA}/v1/solicitudes", headers=auth(andres["token"]), json=solicitud(ubicacion={"latitud": 7.89}))
verificar("Latitud sin longitud: 422", r.status_code == 422, r.text)
r = requests.post(f"{ASISTENCIA}/v1/solicitudes", headers=auth(andres["token"]), json=solicitud(tipo="otro", descripcion="Ayuda"))
verificar("Tipo otro con descripción corta: 422", r.status_code == 422 and "otro" in r.text, r.text)
r = requests.post(f"{ASISTENCIA}/v1/solicitudes", headers=auth(andres["token"]), json=solicitud(tipo="volcan"))
verificar("Tipo inexistente: 422", r.status_code == 422, r.text)
r = requests.post(f"{ASISTENCIA}/v1/solicitudes", headers=auth(diego["token"]), json=solicitud())
verificar("Un operador no crea solicitudes: 403", r.status_code == 403, r.text)

anotar("\n5. Registro sin duplicados (H10)")
cuerpo = solicitud()
r = requests.post(f"{ASISTENCIA}/v1/solicitudes", headers=auth(yeimy["token"]), json=cuerpo)
verificar("Yeimy registra una solicitud: 201", r.status_code == 201, r.text)
sol = r.json()
verificar("Queda pendiente, con código SEN y severidad del tipo (alto)",
          sol.get("estado") == "pendiente" and sol.get("codigo", "").startswith("SEN-") and sol.get("severidad") == "alto", r.text)
verificar("El ciudadano y su nombre salen del token", sol.get("ciudadano", {}).get("nombre_completo") == "Yeimy Carrascal", r.text)
r = requests.post(f"{ASISTENCIA}/v1/solicitudes", headers=auth(yeimy["token"]), json=cuerpo)
verificar("Reenviar el mismo id: 200 con la misma solicitud", r.status_code == 200 and r.json()["codigo"] == sol["codigo"], r.text)
r = requests.post(f"{ASISTENCIA}/v1/solicitudes", headers=auth(andres["token"]), json=cuerpo)
verificar("Otro ciudadano con ese id: 409 ID_EN_USO", r.status_code == 409 and r.json()["error"]["codigo"] == "ID_EN_USO", r.text)

r = requests.post(f"{ASISTENCIA}/v1/solicitudes", headers=auth(andres["token"]),
                  json=solicitud(tipo="incendio", severidad="critico", fecha_hora_dispositivo=ahora_iso(timedelta(minutes=-20))))
verificar("Solicitud enviada 20 minutos tarde queda diferida, con la severidad enviada",
          r.status_code == 201 and r.json()["diferida"] is True and r.json()["severidad"] == "critico", r.text)
diferida = r.json()

anotar("\n6. Bandeja del operador")
r = requests.get(f"{ASISTENCIA}/v1/solicitudes", headers=auth(diego["token"]), params={"limite": 100})
ids = [e["id"] for e in r.json().get("elementos", [])]
verificar("La bandeja incluye las solicitudes nuevas", r.status_code == 200 and sol["id"] in ids and diferida["id"] in ids, r.text)
verificar("La crítica va antes que la alta", sol["id"] in ids and diferida["id"] in ids and ids.index(diferida["id"]) < ids.index(sol["id"]))
verificar("El resumen no trae datos del ciudadano", "ciudadano" not in r.json()["elementos"][0])
r = requests.get(f"{ASISTENCIA}/v1/solicitudes", headers=auth(diego["token"]), params={"severidad": "critico"})
verificar("Filtro por severidad", r.status_code == 200 and all(e["severidad"] == "critico" for e in r.json()["elementos"]), r.text)
r = requests.get(f"{ASISTENCIA}/v1/solicitudes", headers=auth(diego["token"]), params={"estado": "archivado"})
verificar("Estado archivado ya no existe: 422", r.status_code == 422, r.text)
r = requests.get(f"{ASISTENCIA}/v1/solicitudes", headers=auth(yeimy["token"]))
verificar("Un ciudadano no ve la bandeja: 403", r.status_code == 403, r.text)
r = requests.get(f"{ASISTENCIA}/v1/solicitudes/mias", headers=auth(yeimy["token"]))
verificar("El ciudadano ve las suyas", r.status_code == 200 and [e["id"] for e in r.json()["elementos"]] == [sol["id"]], r.text)
r = requests.get(f"{ASISTENCIA}/v1/solicitudes/{sol['id']}", headers=auth(andres["token"]))
verificar("Un ciudadano no ve solicitudes ajenas: 404", r.status_code == 404, r.text)

anotar("\n7. Dos operadores toman la misma solicitud a la vez (H9)")


def tomar(op):
    return requests.patch(f"{ASISTENCIA}/v1/solicitudes/{sol['id']}/estado", headers=auth(op["token"]), json={"estado": "en_atencion"})


with ThreadPoolExecutor(max_workers=2) as pool:
    resultados = list(pool.map(tomar, (diego, laura)))
estados_http = sorted(r.status_code for r in resultados)
verificar("Una toma gana (200) y la otra recibe 409", estados_http == [200, 409], [r.text for r in resultados])
ganador = diego if resultados[0].status_code == 200 else laura
perdedor = laura if ganador is diego else diego
perdida = resultados[1] if ganador is diego else resultados[0]
verificar("El 409 es SOLICITUD_YA_TOMADA con el responsable",
          perdida.json()["error"]["codigo"] == "SOLICITUD_YA_TOMADA" and perdida.json()["error"]["detalles"]["responsable"]["id"] == ganador["id"], perdida.text)

anotar("\n8. Cierre, severidad y observaciones")
r = requests.patch(f"{ASISTENCIA}/v1/solicitudes/{sol['id']}/estado", headers=auth(perdedor["token"]), json={"estado": "atendido"})
verificar("Quien no es responsable no la cierra: 403 NO_ES_RESPONSABLE", r.status_code == 403 and r.json()["error"]["codigo"] == "NO_ES_RESPONSABLE", r.text)
r = requests.patch(f"{ASISTENCIA}/v1/solicitudes/{sol['id']}/severidad", headers=auth(perdedor["token"]), json={"severidad": "critico"})
verificar("Cualquier operador reclasifica la severidad", r.status_code == 200 and r.json()["severidad"] == "critico", r.text)
r = requests.post(f"{ASISTENCIA}/v1/solicitudes/{sol['id']}/observaciones", headers=auth(ganador["token"]), json={"texto": "Paciente ubicada y colaborativa."})
verificar("El operador registra una observación: 201", r.status_code == 201 and r.json()["autor"]["id"] == ganador["id"], r.text)
r = requests.get(f"{ASISTENCIA}/v1/interno/ciudadanos/{yeimy['id']}/solicitud-activa")
verificar("Ruta interna sin clave: 401", r.status_code == 401, r.text)
r = requests.get(f"{ASISTENCIA}/v1/interno/ciudadanos/{yeimy['id']}/solicitud-activa", headers={"X-Clave-Servicio": CLAVE_SERVICIO})
verificar("Con clave: Yeimy tiene una solicitud activa", r.status_code == 200 and r.json() == {"activa": True, "solicitud_id": sol["id"], "estado": "en_atencion"}, r.text)
r = requests.get(f"{ASISTENCIA}/v1/interno/solicitudes/{sol['id']}", headers={"X-Clave-Servicio": CLAVE_SERVICIO})
verificar("Ruta interna para Recursos: estado y responsable", r.status_code == 200 and r.json()["responsable_id"] == ganador["id"], r.text)
r = requests.patch(f"{ASISTENCIA}/v1/solicitudes/{sol['id']}/estado", headers=auth(ganador["token"]), json={"estado": "atendido"})
verificar("El responsable la marca atendida", r.status_code == 200 and r.json()["estado"] == "atendido", r.text)
detalle = r.json()
verificar("El historial guarda pendiente, en_atencion y atendido con el operador",
          [h["estado"] for h in detalle["historial"]] == ["pendiente", "en_atencion", "atendido"]
          and detalle["historial"][-1]["operador"]["id"] == ganador["id"], detalle.get("historial"))
r = requests.patch(f"{ASISTENCIA}/v1/solicitudes/{sol['id']}/estado", headers=auth(ganador["token"]), json={"estado": "en_atencion"})
verificar("Atendida no vuelve a en_atencion: 409 TRANSICION_INVALIDA", r.status_code == 409 and r.json()["error"]["codigo"] == "TRANSICION_INVALIDA", r.text)
r = requests.patch(f"{ASISTENCIA}/v1/solicitudes/{sol['id']}/severidad", headers=auth(ganador["token"]), json={"severidad": "bajo"})
verificar("Cerrada ya no se reclasifica: 409", r.status_code == 409, r.text)
r = requests.get(f"{ASISTENCIA}/v1/interno/ciudadanos/{yeimy['id']}/solicitud-activa", headers={"X-Clave-Servicio": CLAVE_SERVICIO})
verificar("Después de atendida, Yeimy no tiene solicitud activa", r.json() == {"activa": False}, r.text)
r = requests.get(f"{ASISTENCIA}/v1/solicitudes", headers=auth(diego["token"]), params={"estado": "atendido,descartado", "limite": 100})
verificar("La vista Historial la muestra", sol["id"] in [e["id"] for e in r.json()["elementos"]], r.text)

r = requests.patch(f"{ASISTENCIA}/v1/solicitudes/{diferida['id']}/estado", headers=auth(diego["token"]), json={"estado": "descartado"})
verificar("Descartar sin motivo: 422", r.status_code == 422, r.text)
r = requests.patch(f"{ASISTENCIA}/v1/solicitudes/{diferida['id']}/estado", headers=auth(diego["token"]),
                   json={"estado": "descartado", "motivo": "Falsa alarma confirmada por llamada al ciudadano."})
verificar("Descartar desde pendiente con motivo", r.status_code == 200 and r.json()["historial"][-1]["motivo"], r.text)

anotar("\n9. Límite de solicitudes por ciudadano")
respuestas = [requests.post(f"{ASISTENCIA}/v1/solicitudes", headers=auth(natalia["token"]), json=solicitud(tipo="inundacion")) for _ in range(4)]
verificar("Las 3 primeras pasan y la cuarta recibe 429 con Retry-After",
          [r.status_code for r in respuestas] == [201, 201, 201, 429] and "Retry-After" in respuestas[3].headers,
          [r.status_code for r in respuestas])

anotar("\n10. Cuentas desactivadas e intentos fallidos")
r = requests.patch(f"{IDENTIDAD}/v1/usuarios/{laura['id']}", headers=auth(admin), json={"activo": False})
verificar("El administrador desactiva a Laura", r.status_code == 200 and r.json()["activo"] is False, r.text)
r = requests.post(f"{IDENTIDAD}/v1/sesiones", json={"identificador": laura["correo"], "contrasena": "Turno-Seguro-1"})
verificar("Laura ya no puede iniciar sesión: 403 USUARIO_INACTIVO", r.status_code == 403 and r.json()["error"]["codigo"] == "USUARIO_INACTIVO", r.text)
r = requests.post(f"{IDENTIDAD}/v1/sesiones", json={"identificador": f"no.existe.{sufijo}@sentir.co", "contrasena": "x"})
r2 = requests.post(f"{IDENTIDAD}/v1/sesiones", json={"identificador": camilo["documento"], "contrasena": "equivocada"})
verificar("Cuenta inexistente y contraseña errada dan el mismo 401",
          r.status_code == r2.status_code == 401 and r.json()["error"]["mensaje"] == r2.json()["error"]["mensaje"], r.text + r2.text)
for _ in range(4):
    requests.post(f"{IDENTIDAD}/v1/sesiones", json={"identificador": camilo["documento"], "contrasena": "equivocada"})
r = requests.post(f"{IDENTIDAD}/v1/sesiones", json={"identificador": camilo["documento"], "contrasena": "Ciudadano-2026"})
verificar("Tras 5 intentos fallidos, hasta la contraseña correcta recibe 429", r.status_code == 429 and "Retry-After" in r.headers, r.text)

anotar("\n11. Registro de inicios de sesión")
r = requests.get(f"{IDENTIDAD}/v1/usuarios/{diego['id']}/inicios-sesion", headers=auth(admin))
verificar("El administrador ve los inicios de sesión de Diego",
          r.status_code == 200 and r.json()["total"] >= 1 and r.json()["elementos"][0]["resultado"] == "exitoso", r.text)
r = requests.get(f"{IDENTIDAD}/v1/usuarios/{laura['id']}/inicios-sesion", headers=auth(admin))
verificar("El intento de Laura con la cuenta desactivada queda registrado",
          r.status_code == 200 and any(e["resultado"] == "cuenta_inactiva" for e in r.json()["elementos"]), r.text)
r = requests.get(f"{IDENTIDAD}/v1/usuarios/{diego['id']}/inicios-sesion", headers=auth(diego["token"]))
verificar("Un operador no ve el registro: 403", r.status_code == 403, r.text)

creadas_extra = 0
if PERSONAS:
    anotar("\n12. Personas: autorización, perfil cifrado y acceso del operador")
    r = requests.get(f"{PERSONAS}/", timeout=30)
    verificar("Personas responde", r.status_code == 200 and r.json()["servicio"] == "personas", r.text)

    nacimiento = "1992-03-14"
    hoy = datetime.now(timezone(timedelta(hours=-5))).date()
    edad_esperada = hoy.year - 1992 - ((hoy.month, hoy.day) < (3, 14))
    perfil = {
        "nombre_completo": "Natalia Ascanio", "tipo_documento": "CC", "numero_documento": natalia["documento"],
        "fecha_nacimiento": nacimiento, "telefono": "+573154567890", "tipo_sangre": "O+",
        "alergias": ["Penicilina", "Polvo"], "condiciones_cronicas": ["Asma bronquial"], "medicamentos": ["Salbutamol"],
        "contactos_emergencia": [{"nombre_completo": "Andrés Ropero", "telefono": "+573209876543", "parentesco": "Hermano"}],
    }

    r = requests.put(f"{PERSONAS}/v1/perfiles/yo", headers=auth(natalia["token"]), json=perfil)
    verificar("Sin autorización no se guarda el perfil: 403 AUTORIZACION_PENDIENTE",
              r.status_code == 403 and r.json()["error"]["codigo"] == "AUTORIZACION_PENDIENTE", r.text)
    r = requests.post(f"{PERSONAS}/v1/autorizaciones", headers=auth(natalia["token"]), json={"version_texto": "2026-11-v1", "acepta": False})
    verificar("acepta=false no cuenta como autorización: 422", r.status_code == 422, r.text)
    r = requests.post(f"{PERSONAS}/v1/autorizaciones", headers=auth(natalia["token"]), json={"version_texto": "2026-11-v1", "acepta": True})
    verificar("Natalia acepta la autorización: 201", r.status_code == 201 and r.json()["version_texto"] == "2026-11-v1", r.text)
    r = requests.post(f"{PERSONAS}/v1/autorizaciones", headers=auth(natalia["token"]), json={"version_texto": "2026-11-v1", "acepta": True})
    verificar("Aceptar otra vez la misma versión: 200, sin duplicar", r.status_code == 200, r.text)

    r = requests.put(f"{PERSONAS}/v1/perfiles/yo", headers=auth(natalia["token"]),
                     json={**perfil, "contactos_emergencia": perfil["contactos_emergencia"] * 4})
    verificar("Más de 3 contactos: 422", r.status_code == 422, r.text)
    r = requests.put(f"{PERSONAS}/v1/perfiles/yo", headers=auth(natalia["token"]), json={**perfil, "tipo_sangre": "Z+"})
    verificar("Tipo de sangre inválido: 422", r.status_code == 422, r.text)
    r = requests.put(f"{PERSONAS}/v1/perfiles/yo", headers=auth(natalia["token"]), json={**perfil, "fecha_nacimiento": "2999-01-01"})
    verificar("Fecha de nacimiento futura: 422", r.status_code == 422, r.text)

    r = requests.put(f"{PERSONAS}/v1/perfiles/yo", headers=auth(natalia["token"]), json=perfil)
    verificar("Natalia guarda su perfil: 200 con la edad calculada",
              r.status_code == 200 and r.json()["edad"] == edad_esperada and r.json()["alergias"] == ["Penicilina", "Polvo"], r.text)
    verificar("La respuesta lleva Cache-Control: no-store", r.headers.get("Cache-Control") == "no-store", dict(r.headers))
    r = requests.get(f"{PERSONAS}/v1/perfiles/yo", headers=auth(natalia["token"]))
    verificar("Natalia ve su propio perfil", r.status_code == 200 and r.json()["contactos_emergencia"][0]["parentesco"] == "Hermano", r.text)

    r = requests.get(f"{PERSONAS}/v1/perfiles/{natalia['id']}", headers=auth(diego["token"]))
    activas_natalia = [x.json()["id"] for x in respuestas[:3]]
    verificar("El operador ve el perfil de Natalia, que tiene solicitudes activas",
              r.status_code == 200 and r.json()["tipo_sangre"] == "O+" and r.json()["solicitud_id"] in activas_natalia, r.text)
    r = requests.get(f"{PERSONAS}/v1/perfiles/{natalia['id']}", headers=auth(andres["token"]))
    verificar("Un ciudadano no ve perfiles ajenos: 403", r.status_code == 403 and r.json()["error"]["codigo"] == "ROL_NO_PERMITIDO", r.text)
    r = requests.get(f"{PERSONAS}/v1/perfiles/{camilo['id']}", headers=auth(diego["token"]))
    verificar("Sin solicitud activa no hay perfil: 403 SIN_SOLICITUD_ACTIVA",
              r.status_code == 403 and r.json()["error"]["codigo"] == "SIN_SOLICITUD_ACTIVA", r.text)

    r = requests.post(f"{ASISTENCIA}/v1/solicitudes", headers=auth(andres["token"]), json=solicitud(tipo="terremoto"))
    creadas_extra = 1 if r.status_code == 201 else 0
    r = requests.get(f"{PERSONAS}/v1/perfiles/{andres['id']}", headers=auth(diego["token"]))
    verificar("Con solicitud activa pero sin perfil: 404 PERFIL_NO_REGISTRADO",
              r.status_code == 404 and r.json()["error"]["codigo"] == "PERFIL_NO_REGISTRADO", r.text)

    r = requests.get(f"{PERSONAS}/v1/perfiles/{natalia['id']}/accesos", headers=auth(admin))
    verificar("El administrador ve quién consultó el perfil de Natalia",
              r.status_code == 200 and r.json()["total"] == 1 and r.json()["elementos"][0]["operador"]["id"] == diego["id"], r.text)
    r = requests.get(f"{PERSONAS}/v1/perfiles/{natalia['id']}/accesos", headers=auth(diego["token"]))
    verificar("Un operador no ve la auditoría: 403", r.status_code == 403, r.text)

    r = requests.delete(f"{PERSONAS}/v1/autorizaciones/actual", headers=auth(natalia["token"]))
    verificar("Natalia retira la autorización: 204", r.status_code == 204, r.text)
    r = requests.get(f"{PERSONAS}/v1/perfiles/yo", headers=auth(natalia["token"]))
    verificar("Al retirarla, su perfil se borra", r.status_code == 404, r.text)
    r = requests.get(f"{PERSONAS}/v1/perfiles/{natalia['id']}", headers=auth(diego["token"]))
    verificar("El operador ya no lo ve: 404", r.status_code == 404, r.text)
    r = requests.delete(f"{PERSONAS}/v1/autorizaciones/actual", headers=auth(natalia["token"]))
    verificar("Retirar otra vez: 404", r.status_code == 404, r.text)

if PUERTO_EVENTOS:
    anotar("\n13. Eventos publicados en Alertas")
    time.sleep(1)
    nombres = [e["nombre"] for e in eventos]
    verificar("Llegaron eventos con la clave de servicio", eventos and all(e["clave"] == CLAVE_SERVICIO for e in eventos))
    creadas = [e for e in eventos if e["nombre"] == "solicitud.creada"]
    esperadas = 5 + creadas_extra
    verificar(f"Un solicitud.creada por cada solicitud nueva ({esperadas}), sin repetir el reenvío", len(creadas) == esperadas, nombres)
    verificar("El evento no lleva datos personales", all("ciudadano" not in e["datos"] and "descripcion" not in e["datos"] for e in creadas))
    cambios = [e["datos"]["estado_nuevo"] for e in eventos if e["nombre"] == "solicitud.estado_cambiado"]
    verificar("Cambios de estado publicados: en_atencion, atendido, descartado", cambios == ["en_atencion", "atendido", "descartado"], cambios)
    verificar("Severidad reclasificada publicada", nombres.count("solicitud.severidad_cambiada") == 1, nombres)

anotar(f"\nResultado: {aprobadas} verificaciones aprobadas, {fallas} fallidas.")
if EVIDENCIA:
    with open(EVIDENCIA, "w", encoding="utf-8") as archivo:
        archivo.write("\n".join(lineas) + "\n")
sys.exit(1 if fallas else 0)
