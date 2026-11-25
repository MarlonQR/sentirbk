-- ==================================================
-- SENTIR - Servicio de Identidad: registro de inicios de sesión
-- Guarda cada inicio de sesión con contraseña correcta, incluidos los de
-- cuentas desactivadas. Los fallidos ya quedan en identidad.intentos_fallidos.
-- Se puede ejecutar varias veces sin romper nada.
-- ==================================================

CREATE TABLE IF NOT EXISTS identidad.inicios_sesion (
    id          BIGSERIAL PRIMARY KEY,
    usuario_id  UUID NOT NULL REFERENCES identidad.usuarios (id),
    fecha       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resultado   TEXT NOT NULL CHECK (resultado IN ('exitoso', 'cuenta_inactiva')),
    ip          TEXT,
    agente      TEXT
);

CREATE INDEX IF NOT EXISTS idx_inicios_usuario_fecha ON identidad.inicios_sesion (usuario_id, fecha DESC);

ALTER TABLE identidad.inicios_sesion ENABLE ROW LEVEL SECURITY;
