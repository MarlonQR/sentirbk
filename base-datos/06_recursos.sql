-- ==================================================
-- SENTIR - Servicio de Recursos
-- Unidades de respuesta, sus asignaciones a solicitudes y los pedidos de
-- apoyo entre operadores (hallazgos H14 a H18, H21).
-- Se puede ejecutar varias veces sin romper nada.
-- ==================================================

CREATE SCHEMA IF NOT EXISTS recursos;

-- Una unidad es lo que se envía a una emergencia: una brigada, un vehículo
-- o un equipo de herramientas. El personal se cuenta dentro de cada unidad.
CREATE TABLE IF NOT EXISTS recursos.unidades (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre          TEXT NOT NULL UNIQUE CHECK (length(trim(nombre)) BETWEEN 3 AND 80),
    categoria       TEXT NOT NULL CHECK (categoria IN ('brigada', 'vehiculo_socorro', 'equipo_tactico', 'equipamiento_rescate')),
    especialidades  TEXT[] NOT NULL DEFAULT '{}' CHECK (
        especialidades <@ ARRAY['rescate_busqueda', 'atencion_medica', 'control_incendio', 'logistica_traslado']::TEXT[]
    ),
    personal        INT NOT NULL DEFAULT 0 CHECK (personal BETWEEN 0 AND 500),
    -- disponible y ocupada cambian solas al asignar y liberar;
    -- mantenimiento y fuera_servicio las pone el administrador.
    estado          TEXT NOT NULL DEFAULT 'disponible'
                    CHECK (estado IN ('disponible', 'ocupada', 'mantenimiento', 'fuera_servicio')),
    base_latitud    NUMERIC(9, 6) CHECK (base_latitud BETWEEN -90 AND 90),
    base_longitud   NUMERIC(9, 6) CHECK (base_longitud BETWEEN -180 AND 180),
    base_direccion  TEXT CHECK (length(base_direccion) <= 300),
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT base_completa CHECK ((base_latitud IS NULL) = (base_longitud IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_unidades_estado ON recursos.unidades (estado);

CREATE TABLE IF NOT EXISTS recursos.solicitudes_apoyo (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    solicitud_id        UUID NOT NULL,
    codigo_solicitud    TEXT NOT NULL,
    tipos_apoyo         TEXT[] NOT NULL CHECK (
        cardinality(tipos_apoyo) BETWEEN 1 AND 4
        AND tipos_apoyo <@ ARRAY['rescate_busqueda', 'atencion_medica', 'control_incendio', 'logistica_traslado']::TEXT[]
    ),
    personal_adicional  INT NOT NULL CHECK (personal_adicional BETWEEN 0 AND 100),
    prioridad           TEXT NOT NULL CHECK (prioridad IN ('critico', 'alto', 'medio', 'bajo')),
    mensaje             TEXT CHECK (length(mensaje) <= 500),
    estado              TEXT NOT NULL DEFAULT 'abierta' CHECK (estado IN ('abierta', 'atendida', 'cancelada')),
    solicitante_id      UUID NOT NULL,
    solicitante_nombre  TEXT NOT NULL,
    creada_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
    cerrada_por_id      UUID,
    cerrada_por_nombre  TEXT,
    cerrada_en          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_apoyo_estado ON recursos.solicitudes_apoyo (estado, creada_en);

CREATE TABLE IF NOT EXISTS recursos.asignaciones (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    solicitud_id         UUID NOT NULL,
    codigo_solicitud     TEXT NOT NULL,
    solicitud_apoyo_id   UUID REFERENCES recursos.solicitudes_apoyo (id),
    estado               TEXT NOT NULL DEFAULT 'activa' CHECK (estado IN ('activa', 'finalizada')),
    creada_por_id        UUID NOT NULL,
    creada_por_nombre    TEXT NOT NULL,
    creada_en            TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalizada_por_id    UUID,
    finalizada_por_nombre TEXT,
    finalizada_en        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_asignaciones_solicitud ON recursos.asignaciones (solicitud_id, estado);

CREATE TABLE IF NOT EXISTS recursos.asignacion_unidades (
    asignacion_id  UUID NOT NULL REFERENCES recursos.asignaciones (id),
    unidad_id      UUID NOT NULL REFERENCES recursos.unidades (id),
    PRIMARY KEY (asignacion_id, unidad_id)
);

ALTER TABLE recursos.unidades ENABLE ROW LEVEL SECURITY;
ALTER TABLE recursos.solicitudes_apoyo ENABLE ROW LEVEL SECURITY;
ALTER TABLE recursos.asignaciones ENABLE ROW LEVEL SECURITY;
ALTER TABLE recursos.asignacion_unidades ENABLE ROW LEVEL SECURITY;
