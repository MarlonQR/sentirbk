-- ==================================================
-- SENTIR - Recursos: pedidos de apoyo por cupos (respuesta del equipo de
-- Tópicos al hallazgo H21).
--
-- Un pedido de apoyo pide N personas. Cada operador que acepta suma las
-- personas que lleva; el pedido sigue abierto mientras falten y pasa a
-- "cubierta" al completarse. Un operador no puede aceptar dos veces.
--
-- Se ejecuta DESPUÉS de 06_recursos.sql. Se puede ejecutar varias veces.
-- ==================================================

ALTER TABLE recursos.solicitudes_apoyo
    ADD COLUMN IF NOT EXISTS personal_asignado INT NOT NULL DEFAULT 0;

-- Ubicación de la emergencia que pide apoyo, copiada de Asistencia al crear el
-- pedido. Sirve para mostrar a cada operador a qué distancia está.
ALTER TABLE recursos.solicitudes_apoyo ADD COLUMN IF NOT EXISTS solicitud_latitud NUMERIC(9, 6);
ALTER TABLE recursos.solicitudes_apoyo ADD COLUMN IF NOT EXISTS solicitud_longitud NUMERIC(9, 6);
ALTER TABLE recursos.solicitudes_apoyo ADD COLUMN IF NOT EXISTS solicitud_direccion TEXT;

-- Estados: "atendida" pasa a llamarse "cubierta".
ALTER TABLE recursos.solicitudes_apoyo DROP CONSTRAINT IF EXISTS solicitudes_apoyo_estado_check;
UPDATE recursos.solicitudes_apoyo SET estado = 'cubierta' WHERE estado = 'atendida';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'apoyo_estado_valido') THEN
        ALTER TABLE recursos.solicitudes_apoyo
            ADD CONSTRAINT apoyo_estado_valido CHECK (estado IN ('abierta', 'cubierta', 'cancelada'));
    END IF;
END $$;

-- Un pedido pide al menos una persona. NOT VALID: no revisa los pedidos viejos.
ALTER TABLE recursos.solicitudes_apoyo DROP CONSTRAINT IF EXISTS solicitudes_apoyo_personal_adicional_check;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'apoyo_personal_minimo') THEN
        ALTER TABLE recursos.solicitudes_apoyo
            ADD CONSTRAINT apoyo_personal_minimo CHECK (personal_adicional BETWEEN 1 AND 100) NOT VALID;
    END IF;
END $$;

-- Cada operador que se suma a un pedido. La llave única impide aceptar dos veces.
CREATE TABLE IF NOT EXISTS recursos.aceptaciones_apoyo (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    apoyo_id         UUID NOT NULL REFERENCES recursos.solicitudes_apoyo (id),
    operador_id      UUID NOT NULL,
    operador_nombre  TEXT NOT NULL,
    personas         INT NOT NULL CHECK (personas BETWEEN 1 AND 500),
    asignacion_id    UUID REFERENCES recursos.asignaciones (id),
    fecha            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT una_aceptacion_por_operador UNIQUE (apoyo_id, operador_id)
);

ALTER TABLE recursos.aceptaciones_apoyo ENABLE ROW LEVEL SECURITY;
