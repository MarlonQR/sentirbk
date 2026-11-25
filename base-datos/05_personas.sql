-- ==================================================
-- SENTIR - Servicio de Personas
-- Datos de cada ciudadano, su perfil médico cifrado, la autorización de
-- tratamiento de datos de salud (Ley 1581 de 2012) y el registro de quién
-- consultó cada perfil. Se puede ejecutar varias veces sin romper nada.
-- ==================================================

CREATE SCHEMA IF NOT EXISTS personas;

-- Autorización de tratamiento de datos sensibles. Se conserva el historial:
-- una autorización retirada queda con fecha de retiro y no se borra.
CREATE TABLE IF NOT EXISTS personas.autorizaciones (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ciudadano_id   UUID NOT NULL,
    version_texto  TEXT NOT NULL CHECK (length(version_texto) BETWEEN 1 AND 40),
    aceptada_en    TIMESTAMPTZ NOT NULL DEFAULT now(),
    retirada_en    TIMESTAMPTZ
);

-- Solo una autorización vigente por ciudadano.
CREATE UNIQUE INDEX IF NOT EXISTS uq_autorizacion_vigente
    ON personas.autorizaciones (ciudadano_id) WHERE retirada_en IS NULL;

-- datos_protegidos guarda, cifrado con AES-256-GCM desde el servicio, un JSON con
-- tipo de sangre, alergias, condiciones crónicas, medicamentos y contactos de
-- emergencia. La base de datos nunca ve esos datos en claro.
CREATE TABLE IF NOT EXISTS personas.perfiles (
    ciudadano_id        UUID PRIMARY KEY,
    nombre_completo     TEXT NOT NULL CHECK (length(trim(nombre_completo)) BETWEEN 3 AND 120),
    tipo_documento      TEXT NOT NULL CHECK (tipo_documento IN ('CC', 'TI', 'CE', 'PA')),
    numero_documento    TEXT NOT NULL,
    fecha_nacimiento    DATE NOT NULL CHECK (fecha_nacimiento >= DATE '1900-01-01'),
    telefono            TEXT NOT NULL,
    datos_protegidos    TEXT NOT NULL,
    creado_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
    fecha_actualizacion TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Auditoría: cada vez que un operador ve un perfil (contrato, sección 4).
CREATE TABLE IF NOT EXISTS personas.accesos (
    id               BIGSERIAL PRIMARY KEY,
    ciudadano_id     UUID NOT NULL,
    operador_id      UUID NOT NULL,
    operador_nombre  TEXT NOT NULL,
    solicitud_id     UUID NOT NULL,
    fecha            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_accesos_ciudadano_fecha ON personas.accesos (ciudadano_id, fecha DESC);

ALTER TABLE personas.autorizaciones ENABLE ROW LEVEL SECURITY;
ALTER TABLE personas.perfiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE personas.accesos ENABLE ROW LEVEL SECURITY;
