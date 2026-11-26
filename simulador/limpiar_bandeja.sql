-- ==================================================
-- SENTIR - Limpiar la bandeja antes de una demostración
--
-- Descarta los reportes que siguen pendientes o en atención y que llegaron
-- hace más de una hora (los que dejaron las pruebas). No borra nada: quedan
-- en la pestaña Atendidos / Archivados con el motivo en su historial.
--
-- Se corre en Supabase > SQL Editor, ANTES de ejecutar el simulador.
-- Para cambiar el margen, edite '1 hour' (por ejemplo '30 minutes').
-- ==================================================

WITH viejos AS (
    UPDATE asistencia.solicitudes
    SET estado = 'descartado', actualizado_en = now()
    WHERE estado IN ('pendiente', 'en_atencion')
      AND fecha_hora_recepcion < now() - interval '1 hour'
    RETURNING id
)
INSERT INTO asistencia.historial_estados (solicitud_id, estado, motivo)
SELECT id, 'descartado', 'Datos de prueba retirados antes de la demostración'
FROM viejos;

-- Cuántos quedan activos (debería dar 0 si nadie ha reportado en la última hora)
SELECT count(*) AS reportes_activos
FROM asistencia.solicitudes
WHERE estado IN ('pendiente', 'en_atencion');
