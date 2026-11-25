-- ==================================================
-- SENTIR - Cuenta del primer administrador
--
-- ANTES DE EJECUTAR: cambie el correo y la contraseña de las dos
-- líneas marcadas. No suba este archivo con la contraseña real.
--
-- La contraseña se guarda como hash bcrypt (crypt + gen_salt('bf')),
-- el mismo formato que usa el servicio de Identidad.
-- Si el correo ya existe, no hace nada.
-- ==================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

INSERT INTO identidad.usuarios (rol, nombre_completo, correo, contrasena_hash)
SELECT
    'administrador',
    'Administrador SENTIR',
    'admin@sentir.co',                             -- <== CAMBIAR
    crypt('CAMBIAR-ESTA-CLAVE', gen_salt('bf', 12)) -- <== CAMBIAR
WHERE NOT EXISTS (
    SELECT 1 FROM identidad.usuarios WHERE correo = 'admin@sentir.co'  -- <== mismo correo
);
