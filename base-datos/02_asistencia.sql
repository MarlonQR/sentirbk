-- ==================================================
-- SENTIR - Servicio de Asistencia
-- Solicitudes de auxilio, su historial de estados y las observaciones
-- del operador. Se puede ejecutar varias veces sin romper nada.
-- ==================================================

CREATE SCHEMA IF NOT EXISTS asistencia;

CREATE TABLE IF NOT EXISTS asistencia.tipos_emergencia (
    codigo                 TEXT PRIMARY KEY,
    nombre                 TEXT NOT NULL,
    severidad_por_defecto  TEXT NOT NULL CHECK (severidad_por_defecto IN ('critico', 'alto', 'medio', 'bajo')),
    orden                  INT NOT NULL,
    activo                 BOOLEAN NOT NULL DEFAULT TRUE
);

-- Catálogo acordado con el equipo móvil (hallazgo H8).
INSERT INTO asistencia.tipos_emergencia (codigo, nombre, severidad_por_defecto, orden) VALUES
    ('terremoto',  'Terremoto',  'critico', 1),
    ('tsunami',    'Tsunami',    'critico', 2),
    ('incendio',   'Incendio',   'alto',    3),
    ('derrumbe',   'Derrumbe',   'alto',    4),
    ('inundacion', 'Inundación', 'medio',   5),
    ('otro',       'Otro',       'medio',   6)
ON CONFLICT (codigo) DO NOTHING;

-- Código corto para dictar por teléfono: SEN-0001, SEN-0002...
CREATE SEQUENCE IF NOT EXISTS asistencia.codigo_solicitud_seq;

CREATE TABLE IF NOT EXISTS asistencia.solicitudes (
    -- El id lo genera el cliente y sirve como clave de idempotencia (H10).
    id                      UUID PRIMARY KEY,
    codigo                  TEXT NOT NULL UNIQUE
                            DEFAULT 'SEN-' || lpad(nextval('asistencia.codigo_solicitud_seq')::TEXT, 4, '0'),
    ciudadano_id            UUID NOT NULL,
    ciudadano_nombre        TEXT NOT NULL,
    codigo_tipo             TEXT NOT NULL REFERENCES asistencia.tipos_emergencia (codigo),
    severidad               TEXT NOT NULL CHECK (severidad IN ('critico', 'alto', 'medio', 'bajo')),
    estado                  TEXT NOT NULL DEFAULT 'pendiente'
                            CHECK (estado IN ('pendiente', 'en_atencion', 'atendido', 'descartado')),
    descripcion             TEXT NOT NULL CHECK (length(trim(descripcion)) BETWEEN 1 AND 1000),
    latitud                 NUMERIC(9, 6) CHECK (latitud BETWEEN -90 AND 90),
    longitud                NUMERIC(9, 6) CHECK (longitud BETWEEN -180 AND 180),
    precision_metros        NUMERIC(8, 1) CHECK (precision_metros >= 0),
    direccion               TEXT CHECK (length(direccion) <= 300),
    fecha_hora_dispositivo  TIMESTAMPTZ NOT NULL,
    fecha_hora_recepcion    TIMESTAMPTZ NOT NULL DEFAULT now(),
    responsable_id          UUID,
    responsable_nombre      TEXT,
    actualizado_en          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Latitud y longitud van juntas.
    CONSTRAINT coordenadas_completas CHECK ((latitud IS NULL) = (longitud IS NULL)),
    -- Hace falta al menos coordenadas o dirección escrita.
    CONSTRAINT alguna_ubicacion CHECK (latitud IS NOT NULL OR direccion IS NOT NULL),
    -- En atención siempre hay responsable.
    CONSTRAINT responsable_en_atencion CHECK (estado = 'pendiente' OR responsable_id IS NOT NULL OR estado = 'descartado')
);

CREATE INDEX IF NOT EXISTS idx_solicitudes_estado ON asistencia.solicitudes (estado);
CREATE INDEX IF NOT EXISTS idx_solicitudes_ciudadano_recepcion
    ON asistencia.solicitudes (ciudadano_id, fecha_hora_recepcion);

CREATE TABLE IF NOT EXISTS asistencia.historial_estados (
    id               BIGSERIAL PRIMARY KEY,
    solicitud_id     UUID NOT NULL REFERENCES asistencia.solicitudes (id),
    estado           TEXT NOT NULL,
    fecha            TIMESTAMPTZ NOT NULL DEFAULT now(),
    operador_id      UUID,
    operador_nombre  TEXT,
    motivo           TEXT
);

CREATE INDEX IF NOT EXISTS idx_historial_solicitud ON asistencia.historial_estados (solicitud_id, fecha);

-- Las observaciones no se editan ni se borran (contrato, sección 3).
CREATE TABLE IF NOT EXISTS asistencia.observaciones (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    solicitud_id     UUID NOT NULL REFERENCES asistencia.solicitudes (id),
    texto            TEXT NOT NULL CHECK (length(trim(texto)) BETWEEN 1 AND 500),
    operador_id      UUID NOT NULL,
    operador_nombre  TEXT NOT NULL,
    fecha            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_observaciones_solicitud ON asistencia.observaciones (solicitud_id, fecha);

-- RLS sin políticas: la API pública de Supabase (anon, authenticated) no puede
-- leer ni escribir. El servicio se conecta como dueño de las tablas y no lo afecta.
ALTER TABLE asistencia.tipos_emergencia ENABLE ROW LEVEL SECURITY;
ALTER TABLE asistencia.solicitudes ENABLE ROW LEVEL SECURITY;
ALTER TABLE asistencia.historial_estados ENABLE ROW LEVEL SECURITY;
ALTER TABLE asistencia.observaciones ENABLE ROW LEVEL SECURITY;
