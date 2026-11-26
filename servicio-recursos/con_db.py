import os
from contextlib import contextmanager

import psycopg2

from seguridad import ErrorApi


def obtener_conexion():
    # En Supabase se pega la cadena del Transaction pooler (puerto 6543)
    # en DATABASE_URL. strip() quita espacios o saltos de línea pegados por error.
    url = os.getenv("DATABASE_URL", "").strip()
    try:
        if url:
            return psycopg2.connect(url, connect_timeout=10)
        return psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            database=os.getenv("DB_NAME", "postgres"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", ""),
            port=os.getenv("DB_PORT", "5432"),
            sslmode=os.getenv("DB_SSLMODE", "prefer"),
            connect_timeout=10,
        )
    except psycopg2.Error as error:
        # Queda en los logs. El mensaje no incluye la contraseña.
        print(f"Error de conexión a PostgreSQL (DATABASE_URL definida: {bool(url)}): {error}")
        raise ErrorApi(503, "BASE_DATOS_NO_DISPONIBLE", "No fue posible conectar con la base de datos.")


@contextmanager
def transaccion():
    """Abre una conexión, confirma si todo sale bien y deshace si algo falla."""
    conexion = obtener_conexion()
    try:
        with conexion.cursor() as cursor:
            yield cursor
        conexion.commit()
    except Exception:
        conexion.rollback()
        raise
    finally:
        conexion.close()
