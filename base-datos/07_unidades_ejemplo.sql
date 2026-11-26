-- ==================================================
-- SENTIR - Unidades de ejemplo para el inventario (Cúcuta)
-- Datos de demostración para las pantallas 4 y 5 de la app.
-- Si una unidad con el mismo nombre ya existe, no la toca.
-- ==================================================

INSERT INTO recursos.unidades (nombre, categoria, especialidades, personal, estado, base_latitud, base_longitud, base_direccion) VALUES
    ('Brigada Central',            'brigada',              '{rescate_busqueda,atencion_medica}',    6, 'disponible',     7.888611, -72.496944, 'Estación de Bomberos, Av. 0, Cúcuta'),
    ('Brigada Norte',              'brigada',              '{rescate_busqueda}',                    5, 'disponible',     7.921500, -72.505300, 'Barrio Prados del Norte, Cúcuta'),
    ('Brigada Atalaya',            'brigada',              '{rescate_busqueda,logistica_traslado}', 5, 'disponible',     7.884900, -72.530800, 'Ciudadela Atalaya, Cúcuta'),
    ('Brigada Sur',                'brigada',              '{atencion_medica}',                     4, 'mantenimiento',  7.857200, -72.511900, 'Barrio Belén, Cúcuta'),
    ('Ambulancia 12',              'vehiculo_socorro',     '{atencion_medica,logistica_traslado}',  3, 'disponible',     7.890900, -72.502100, 'Hospital Erasmo Meoz, Cúcuta'),
    ('Ambulancia 15',              'vehiculo_socorro',     '{atencion_medica,logistica_traslado}',  3, 'disponible',     7.900300, -72.488400, 'Clínica Santa Ana, Cúcuta'),
    ('Ambulancia 21',              'vehiculo_socorro',     '{atencion_medica}',                     2, 'fuera_servicio', 7.873300, -72.497000, 'Barrio La Libertad, Cúcuta'),
    ('Máquina de bomberos 3',      'vehiculo_socorro',     '{control_incendio}',                    5, 'disponible',     7.888611, -72.496944, 'Estación de Bomberos, Av. 0, Cúcuta'),
    ('Máquina de bomberos 7',      'vehiculo_socorro',     '{control_incendio,rescate_busqueda}',   5, 'disponible',     7.909000, -72.520500, 'Estación Norte, Cúcuta'),
    ('Camión de logística 1',      'vehiculo_socorro',     '{logistica_traslado}',                  2, 'disponible',     7.878100, -72.507900, 'Defensa Civil, Cúcuta'),
    ('Equipo táctico Alfa',        'equipo_tactico',       '{rescate_busqueda}',                    4, 'disponible',     7.893200, -72.511300, 'Comando de Policía, Cúcuta'),
    ('Equipo táctico Bravo',       'equipo_tactico',       '{rescate_busqueda,control_incendio}',   4, 'mantenimiento',  7.893200, -72.511300, 'Comando de Policía, Cúcuta'),
    ('Kit de corte hidráulico 1',  'equipamiento_rescate', '{rescate_busqueda}',                    0, 'disponible',     7.888611, -72.496944, 'Estación de Bomberos, Av. 0, Cúcuta'),
    ('Kit de corte hidráulico 2',  'equipamiento_rescate', '{rescate_busqueda}',                    0, 'disponible',     7.909000, -72.520500, 'Estación Norte, Cúcuta'),
    ('Motobomba de achique 1',     'equipamiento_rescate', '{logistica_traslado}',                  0, 'disponible',     7.878100, -72.507900, 'Defensa Civil, Cúcuta'),
    ('Planta eléctrica portátil',  'equipamiento_rescate', '{logistica_traslado}',                  0, 'fuera_servicio', 7.878100, -72.507900, 'Defensa Civil, Cúcuta')
ON CONFLICT (nombre) DO NOTHING;
