-- ==================================================
-- SENTIR - Servicio de Identidad
-- Cuentas de usuario, roles e intentos de inicio de sesión.
-- Se puede ejecutar varias veces sin romper nada.
-- ==================================================

CREATE SCHEMA IF NOT EXISTS identidad;

CREATE TABLE IF NOT EXISTS identidad.usuarios (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rol               TEXT NOT NULL CHECK (rol IN ('ciudadano', 'operador', 'administrador')),
    nombre_completo   TEXT NOT NULL CHECK (length(trim(nombre_completo)) BETWEEN 3 AND 120),
    correo            TEXT UNIQUE,
    tipo_documento    TEXT CHECK (tipo_documento IN ('CC', 'TI', 'CE', 'PA')),
    numero_documento  TEXT UNIQUE,
    contrasena_hash   TEXT NOT NULL,
    activo            BOOLEAN NOT NULL DEFAULT TRUE,
    creado_en         TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en    TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- El ciudadano entra con su documento; operador y administrador, con su correo.
    CONSTRAINT identificador_segun_rol CHECK (
        (rol = 'ciudadano' AND tipo_documento IS NOT NULL AND numero_documento IS NOT NULL)
        OR (rol IN ('operador', 'administrador') AND correo IS NOT NULL)
    )
);

-- Registro de intentos fallidos para bloquear después de 5 en 15 minutos.
CREATE TABLE IF NOT EXISTS identidad.intentos_fallidos (
    id             BIGSERIAL PRIMARY KEY,
    identificador  TEXT NOT NULL,
    fecha          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_intentos_identificador_fecha
    ON identidad.intentos_fallidos (identificador, fecha);

-- RLS sin políticas: la API pública de Supabase (anon, authenticated) no puede
-- leer ni escribir. El servicio se conecta como dueño de las tablas y no lo afecta.
ALTER TABLE identidad.usuarios ENABLE ROW LEVEL SECURITY;
ALTER TABLE identidad.intentos_fallidos ENABLE ROW LEVEL SECURITY;
