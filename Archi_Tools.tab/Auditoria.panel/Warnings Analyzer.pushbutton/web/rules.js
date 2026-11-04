// Clasificación de warnings: prioridad, grupo, explicación, cómo resolver y esquema.
// La primera regla cuyo patrón calza con la descripción gana; 'other' recoge el resto.
// Los patrones cubren Revit en inglés y en español.

const PRIORITY = {
  high: { label: 'Alta', color: '#C0392B', weight: 3 },
  medium: { label: 'Media', color: '#E69138', weight: 1.5 },
  low: { label: 'Baja', color: '#D6C44A', weight: 0.5 },
};

// Esquemas: trazo grafito para lo correcto, terracota para el problema.
const G = '#333333', C = '#B26E63', SV = '#A1A1A0';
const svg = body => `<svg viewBox="0 0 160 100" xmlns="http://www.w3.org/2000/svg" fill="none" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
const label = (x, y, t, c = SV) => `<text x="${x}" y="${y}" font-size="8" fill="${c}" stroke="none" font-family="Lato, Segoe UI">${t}</text>`;

const SCHEMAS = {
  duplicates: svg(`
    <rect x="44" y="22" width="44" height="44" stroke="${G}" stroke-width="2"/>
    <rect x="50" y="28" width="44" height="44" stroke="${C}" stroke-width="2" stroke-dasharray="4 3"/>
    <path d="M100 30 h22 M100 70 h22" stroke="${SV}"/>
    ${label(104, 26, '×2 en tablas', C)}${label(36, 90, 'misma posición, mismo tipo')}`),
  overlapWalls: svg(`
    <rect x="14" y="36" width="96" height="14" stroke="${G}" stroke-width="2"/>
    <rect x="62" y="40" width="84" height="14" stroke="${C}" stroke-width="2"/>
    <rect x="62" y="40" width="48" height="10" fill="${C}" fill-opacity=".25" stroke="none"/>
    ${label(70, 70, 'zona duplicada', C)}${label(14, 88, 'límite de habitación ambiguo')}`),
  lines: svg(`
    <path d="M14 50 H146" stroke="${G}" stroke-width="2"/>
    <path d="M52 50 H108" stroke="${C}" stroke-width="5" stroke-opacity=".55"/>
    <circle cx="52" cy="50" r="3" fill="${C}" stroke="none"/><circle cx="108" cy="50" r="3" fill="${C}" stroke="none"/>
    ${label(44, 40, 'línea contenida → sobra', C)}${label(14, 80, 'la línea larga ya cubre el tramo')}`),
  roomOpen: svg(`
    <path d="M30 20 H130 V80 H86 M66 80 H30 V20" stroke="${G}" stroke-width="2.5"/>
    <path d="M68 80 H84" stroke="${C}" stroke-width="2" stroke-dasharray="3 3"/>
    <path d="M36 26 L124 74 M124 26 L36 74" stroke="${C}" stroke-opacity=".35"/>
    ${label(62, 94, 'perímetro abierto', C)}${label(60, 54, 'Área = 0', G)}`),
  tag: svg(`
    <rect x="22" y="20" width="70" height="60" stroke="${G}" stroke-width="2"/>
    <circle cx="57" cy="50" r="2" fill="${G}" stroke="none"/>
    <rect x="104" y="40" width="44" height="18" rx="3" stroke="${C}" stroke-width="2"/>
    ${label(110, 52, '101 Sala', C)}<path d="M104 49 L60 50" stroke="${C}" stroke-dasharray="2 3"/>`),
  offAxis: svg(`
    <path d="M14 64 H146" stroke="${SV}" stroke-dasharray="4 4"/>
    <path d="M14 64 L146 55" stroke="${C}" stroke-width="2.5"/>
    <path d="M118 64 A 28 28 0 0 0 117.6 57" stroke="${G}"/>
    ${label(122, 50, '0,2°', C)}${label(14, 86, 'casi ortogonal ≠ ortogonal')}`),
  joins: svg(`
    <rect x="16" y="36" width="56" height="26" stroke="${G}" stroke-width="2"/>
    <rect x="88" y="36" width="56" height="26" stroke="${G}" stroke-width="2"/>
    <path d="M72 49 H88" stroke="${C}" stroke-width="2" stroke-dasharray="3 3"/>
    ${label(54, 26, 'unidos sin tocarse', C)}${label(36, 84, 'Unjoin Geometry o acercar')}`),
  dupValues: svg(`
    <rect x="20" y="30" width="50" height="22" rx="4" stroke="${G}" stroke-width="2"/>
    <rect x="90" y="30" width="50" height="22" rx="4" stroke="${C}" stroke-width="2"/>
    ${label(34, 44, 'P-01', G)}${label(104, 44, 'P-01', C)}
    ${label(30, 76, 'mismo Mark en dos elementos')}`),
  stairs: svg(`
    <path d="M20 80 H40 V66 H60 V52 H80 V38 H100 V24 H120" stroke="${G}" stroke-width="2"/>
    <path d="M120 24 H142" stroke="${C}" stroke-width="2" stroke-dasharray="3 3"/>
    <path d="M142 14 V34" stroke="${C}"/>
    ${label(102, 46, 'no llega al nivel', C)}`),
  cut: svg(`
    <rect x="14" y="38" width="132" height="22" stroke="${G}" stroke-width="2"/>
    <rect x="64" y="30" width="30" height="38" stroke="${C}" stroke-width="2" stroke-dasharray="4 3"/>
    ${label(46, 84, 'el vano no corta el host', C)}`),
  generic: svg(`
    <path d="M80 18 L140 82 H20 Z" stroke="${G}" stroke-width="2"/>
    <path d="M80 42 V62" stroke="${C}" stroke-width="3"/><circle cx="80" cy="71" r="2" fill="${C}" stroke="none"/>`),
};

const RULES = [
  {
    key: 'duplicates', re: /identical instances|instancias id[eé]nticas/i, prio: 'high', group: 'Duplicados',
    title: 'Instancias idénticas en el mismo lugar', schema: 'duplicates', autofix: 'duplicates',
    why: 'Se cuentan dos veces en tablas y cubicaciones, y aumentan el peso del modelo sin aportar nada.',
    fix: ['Usa «Eliminar duplicados»: conserva el de menor ID de cada grupo y borra con los demás sus etiquetas y cotas (se deshace con Ctrl+Z).', 'Antes, confirma que no son variantes de fase u opción de diseño.'],
  },
  {
    key: 'walls_overlap', re: /walls?(\(s\))? overlap|muros? .*(superpon|solap)/i, prio: 'high', group: 'Geometría',
    title: 'Muros superpuestos', schema: 'overlapWalls',
    why: 'Revit puede ignorar uno de los muros al calcular los límites de habitación; además duplica volumen en la cubicación.',
    fix: ['Aísla el par y elimina o recorta (Trim/Extend) el muro sobrante.', 'Si son dos capas de un mismo muro, unifícalas en un solo tipo compuesto.', 'Si el solape nace en una unión, prueba «Disallow Join» en ese extremo.'],
  },
  {
    key: 'lines_overlap', re: /lines?(\(s\))? overlap|l[ií]neas? .*(superpon|solap)/i, prio: 'medium', group: 'Líneas y separadores',
    title: 'Líneas superpuestas', schema: 'lines', autofix: 'lines',
    why: 'Separadores de habitación o área duplicados vuelven ambiguo el límite y generan warnings en cadena.',
    fix: ['«Eliminar líneas contenidas» borra solo la línea que queda completamente dentro de otra.', 'Las superposiciones parciales se resuelven a mano: recorta una de las dos.'],
  },
  {
    key: 'room_enclosed',
    re: /not in a properly enclosed region|not enclosed|same enclosed region|regi[oó]n .*cerrada|no est[aá] .*cerrad/i,
    prio: 'high', group: 'Habitaciones y áreas', title: 'Habitación o área sin cerrar o duplicada', schema: 'roomOpen',
    why: 'La superficie queda en 0 o se duplica: rompe cuadros de superficies, tablas y verificaciones normativas.',
    fix: ['Cierra el perímetro con muros «Room Bounding» o líneas de separación.', 'Si la habitación sobra, bórrala y luego elimina las «Not Placed» de la tabla.', 'Revisa holguras pequeñas en vanos de puertas y encuentros de muros.'],
  },
  {
    key: 'tag_outside', re: /tag .*outside|etiqueta .*fuera/i, prio: 'low', group: 'Habitaciones y áreas',
    title: 'Etiqueta fuera de su habitación', schema: 'tag',
    why: 'La etiqueta puede leerse como si perteneciera a otro recinto en los planos.',
    fix: ['Mueve la etiqueta dentro del recinto o activa «Leader».', 'Si el recinto es muy chico, documenta la excepción y márcala como aceptada.'],
  },
  {
    key: 'off_axis', re: /off axis|fuera de(l)? eje/i, prio: 'medium', group: 'Precisión',
    title: 'Elemento ligeramente fuera de eje', schema: 'offAxis',
    why: 'Los ángulos casi ortogonales provocan uniones fallidas, cotas imprecisas y nuevos warnings en cadena.',
    fix: ['Usa «Mostrar» y revisa el ángulo en la vista.', 'Rótalo con Rotate tomando un eje como referencia, o redibújalo con snaps.', 'Si viene de un CAD vinculado, corrige el origen del CAD.'],
  },
  {
    key: 'joins', re: /joined but do not intersect|keep .*joined|unid[oa]s? pero no|mantener unid/i, prio: 'medium', group: 'Uniones',
    title: 'Uniones de geometría inválidas', schema: 'joins',
    why: 'Uniones que ya no se tocan dejan cortes y cubicaciones incorrectas.',
    fix: ['Selecciona el par y usa «Unjoin Geometry».', 'Si deben unirse, ajusta la geometría para que se toquen y vuelve a unir.'],
  },
  {
    key: 'dup_values', re: /duplicate .*values?|valor(es)? .*duplicad/i, prio: 'low', group: 'Datos',
    title: 'Valores duplicados (Mark, Number, Type Mark…)', schema: 'dupValues',
    why: 'No afecta la geometría, pero confunde tablas, etiquetas y listados de cantidades.',
    fix: ['Renumera desde una tabla ordenada por el parámetro duplicado.', 'Si la duplicidad es intencional (tipos compartidos), márcala como «Aceptada».'],
  },
  {
    key: 'stairs', re: /stair|railing|\brail|escalera|baranda/i, prio: 'medium', group: 'Escaleras y barandas',
    title: 'Escaleras y barandas', schema: 'stairs',
    why: 'Escaleras que no llegan a su nivel o barandas con perfil inválido no se dibujan o cubican bien.',
    fix: ['Revisa «Relative Top Height» y la cantidad de contrahuellas.', 'Para perfiles de baranda: el perfil debe ser un único lazo cerrado.'],
  },
  {
    key: 'cut', re: /insert conflicts|doesn'?t cut|can'?t cut|no corta|no puede cortar/i, prio: 'medium', group: 'Huecos e inserciones',
    title: 'Inserciones que no cortan su host', schema: 'cut',
    why: 'Puertas o ventanas que no cortan el muro dejan vanos inexistentes en planos y cubicaciones.',
    fix: ['Revisa que la inserción esté alojada en el muro correcto.', 'Ajusta altura/ancho si choca con otra inserción o con un muro unido.'],
  },
  {
    key: 'create', re: /could not create|can'?t create|cannot be created|no se pudo crear|no se puede crear/i, prio: 'medium', group: 'Geometría',
    title: 'Geometría que no se pudo generar', schema: 'generic',
    why: 'El elemento existe pero parte de su geometría (barrido, unión, recorte) no se generó.',
    fix: ['Aísla el elemento y revisa el perfil o la operación que falló.', 'Si hay dos barridos superpuestos, elimina uno.'],
  },
  {
    key: 'short', re: /too short|demasiado cort/i, prio: 'medium', group: 'Precisión',
    title: 'Elementos demasiado cortos', schema: 'offAxis',
    why: 'Segmentos minúsculos suelen ser restos de dibujo y causan uniones y cotas defectuosas.',
    fix: ['Elimina el tramo corto y extiende el elemento vecino.'],
  },
  {
    key: 'constraints', re: /constraint|restricci/i, prio: 'medium', group: 'Restricciones',
    title: 'Restricciones no satisfechas', schema: 'generic',
    why: 'Restricciones rotas (cotas bloqueadas, alineaciones) impiden mover elementos y ralentizan la regeneración.',
    fix: ['Quita los candados de las cotas involucradas o elimina la restricción.'],
  },
  {
    key: 'other', re: /[\s\S]/, prio: 'low', group: 'Otros', title: null, schema: 'generic',
    why: 'Warning sin regla específica. Revisa la descripción de Revit.',
    fix: ['Usa «Mostrar» para ubicar los elementos y lee el mensaje de Revit.', 'Si es inofensivo e intencional, márcalo como «Aceptado».'],
  },
];

function classify(description) {
  return RULES.find(r => r.re.test(description));
}
