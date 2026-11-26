"""
Simulador de la app del ciudadano de SENTIR (versión mínima).

El equipo de Tópicos construye solo la app del operador, así que los reportes
de emergencia los genera este script (hallazgo H13). Hace lo mismo que haría
la app del ciudadano y siempre por la API: registra un ciudadano en Identidad y
envía su solicitud a Asistencia. No necesita la contraseña del administrador
ni acceso a la base de datos.

Uso en PowerShell, desde la carpeta del proyecto:

    python simulador/simular.py
    python simulador/simular.py --cantidad 5
    python simulador/simular.py --intervalo 60      (un reporte por minuto)

Por defecto apunta a producción. Para probar en local:

    python simulador/simular.py --identidad http://127.0.0.1:8001 --asistencia http://127.0.0.1:8002
"""
import argparse
import random
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone

import requests

IDENTIDAD = "https://sentirbk.vercel.app"
ASISTENCIA = "https://sentirbk-y7ar.vercel.app"

# Reportes de ejemplo en Cúcuta. severidad None = la del tipo en el catálogo.
REPORTES = [
    ("incendio", "critico", "Sale humo negro de una vivienda de dos pisos. Los vecinos dicen que adentro quedó una señora mayor.",
     "Av. 5 # 8-24, barrio La Playa, Cúcuta", 7.885100, -72.502100),
    ("derrumbe", None, "Se vino abajo parte del talud sobre la vía y dejó un carro atrapado. El conductor está consciente.",
     "Anillo Vial Occidental, sector Atalaya, Cúcuta", 7.883500, -72.535200),
    ("inundacion", None, "El caño se desbordó y el agua está entrando a las casas de la cuadra. Hay niños pequeños.",
     "Calle 2N con Av. 4E, barrio Prados del Este, Cúcuta", 7.902600, -72.488000),
    ("terremoto", "alto", "Después del temblor se agrietó una pared del colegio. Sacaron a los estudiantes al patio.",
     "Av. Libertadores, barrio Colsag, Cúcuta", 7.900100, -72.493000),
    ("otro", None, "Un árbol grande cayó sobre los cables de energía y quedó echando chispas en la esquina del parque.",
     "Parque de la Calle 10, barrio La Cabrera, Cúcuta", 7.893900, -72.507800),
    ("incendio", None, "Se prendió el pastizal del lote junto al barrio y el viento lo está acercando a las casas.",
     "Lote de la Calle 28, barrio Antonia Santos, Cúcuta", 7.865000, -72.515000),
    ("inundacion", "bajo", "La alcantarilla se tapó y el agua ya llega a la acera. Todavía no entra a las casas.",
     "Calle 21 # 6-15, barrio San Luis, Cúcuta", 7.878500, -72.492000),
    ("derrumbe", "medio", "Se cayó el techo de una bodega abandonada. No se ven personas, pero hay niños jugando cerca.",
     "Av. 7 # 3-50, barrio El Llano, Cúcuta", 7.889000, -72.510000),
    ("otro", "critico", "Fuga fuerte de gas en un edificio de apartamentos. Los vecinos están bajando por las escaleras.",
     "Calle 16 # 1E-40, barrio Caobos, Cúcuta", None, None),
]

NOMBRES = ["Yeimy Carrascal", "Andrés Ropero", "Natalia Ascanio", "Camilo Quintero", "Laura Sanjuán",
           "Jhon Fredy Ortega", "Marta Lucía Pabón", "Wilmer Jaimes", "Sandra Milena Duarte", "Édgar Peñaranda"]


def registrar_ciudadano(identidad: str, nombre: str) -> str:
    """Crea un ciudadano de prueba y devuelve su token."""
    for _ in range(3):
        documento = "10" + "".join(random.choices("0123456789", k=8))
        r = requests.post(f"{identidad}/v1/ciudadanos", timeout=40, json={
            "nombre_completo": nombre, "tipo_documento": "CC", "numero_documento": documento,
            "contrasena": secrets.token_urlsafe(12),
        })
        if r.status_code == 201:
            return r.json()["token"]
        if r.status_code != 409:  # 409: documento repetido, se intenta con otro
            raise RuntimeError(f"Identidad respondió {r.status_code}: {r.text[:200]}")
    raise RuntimeError("No se pudo registrar el ciudadano después de 3 intentos.")


def enviar_reporte(asistencia: str, token: str, reporte) -> dict:
    tipo, severidad, descripcion, direccion, lat, lon = reporte
    ubicacion = {"direccion": direccion}
    if lat is not None:
        # Unos metros de variación para que dos corridas no caigan en el mismo punto.
        ubicacion.update(latitud=round(lat + random.uniform(-0.0015, 0.0015), 6),
                         longitud=round(lon + random.uniform(-0.0015, 0.0015), 6),
                         precision_metros=random.choice([8, 12, 15, 25]))
    cuerpo = {
        "id": str(uuid.uuid4()),
        "codigo_tipo": tipo,
        "descripcion": descripcion,
        "ubicacion": ubicacion,
        "fecha_hora_dispositivo": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if severidad:
        cuerpo["severidad"] = severidad
    r = requests.post(f"{asistencia}/v1/solicitudes", headers={"Authorization": f"Bearer {token}"}, json=cuerpo, timeout=40)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Asistencia respondió {r.status_code}: {r.text[:200]}")
    return r.json()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Genera reportes de emergencia como lo haría la app del ciudadano.")
    parser.add_argument("--identidad", default=IDENTIDAD, help="URL del servicio de Identidad")
    parser.add_argument("--asistencia", default=ASISTENCIA, help="URL del servicio de Asistencia")
    parser.add_argument("--cantidad", type=int, default=len(REPORTES), help=f"Cuántos reportes enviar (máximo {len(REPORTES)})")
    parser.add_argument("--intervalo", type=float, default=0, help="Segundos de espera entre reportes (0 = todos seguidos)")
    args = parser.parse_args()

    identidad, asistencia = args.identidad.strip().rstrip("/"), args.asistencia.strip().rstrip("/")
    reportes = random.sample(REPORTES, k=max(1, min(args.cantidad, len(REPORTES))))
    nombres = random.sample(NOMBRES, k=len(reportes))

    print(f"Enviando {len(reportes)} reportes a {asistencia}")
    enviados = 0
    for i, (reporte, nombre) in enumerate(zip(reportes, nombres), start=1):
        try:
            token = registrar_ciudadano(identidad, nombre)
            sol = enviar_reporte(asistencia, token, reporte)
        except (RuntimeError, requests.RequestException) as error:
            print(f"  {i}. ERROR: {error}")
            continue
        enviados += 1
        print(f"  {i}. {sol['codigo']}  {sol['tipo']['nombre']:<11} {sol['severidad']:<8} {nombre:<22} {sol['ubicacion']['direccion']}")
        if args.intervalo and i < len(reportes):
            time.sleep(args.intervalo)
    print(f"Listo: {enviados} de {len(reportes)} reportes enviados.")
    sys.exit(0 if enviados == len(reportes) else 1)


if __name__ == "__main__":
    main()
