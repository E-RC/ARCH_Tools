# -*- coding: UTF-8 -*-
"""Conversion segura de objetos de Revit a tipos JSON.

Todo lo que cruza el limite HTTP pasa por aqui. Ningun modulo debe
tocar `.IntegerValue` ni `.Value` de un ElementId directamente:
usar `element_id_value()`.
"""

import json

from Autodesk.Revit import DB


# --- ElementId ------------------------------------------------------------

def element_id_value(element_id):
    """Valor entero de un ElementId, compatible entre versiones.

    Revit 2025+ usa `.Value` (int64). Versiones anteriores `.IntegerValue`
    (int32). Revit 2026 elimino `.IntegerValue` por completo.
    """
    if element_id is None:
        return None
    try:
        return int(element_id.Value)
    except AttributeError:
        return int(element_id.IntegerValue)


def to_element_id(value):
    """Entero -> ElementId. El unico lugar donde se construye uno.

    Existe para que `api_routes` no tenga que importar `DB` solo para
    convertir lo que llega del JSON. OJO: NO usar esto con ids de
    CATEGORIA, que son negativos y por lo tanto valores validos de
    `BuiltInCategory` -- ahi `ElementId(int)` es justo la ambiguedad de
    sobrecargas que este proyecto evita, y hay que buscarlos entre los
    que devuelve la API (ver `writer._find_valid_category`).

    Un `None` FALLA aca (`int(None)`), a proposito [Fase 27]: todos los
    llamadores lo filtran antes, y un `null` que se cuele desde un JSON
    tiene que fallar fuerte, no llegar a Revit como referencia nula.
    """
    return DB.ElementId(int(value))


def is_valid_id(element_id):
    """True si el ElementId apunta a algo real."""
    if element_id is None:
        return False
    return element_id != DB.ElementId.InvalidElementId


# --- Nombres --------------------------------------------------------------

def safe_name(element):
    """Nombre de un elemento, sin reventar si no lo tiene.

    OJO CON `element.Name` BAJO IRONPYTHON: para un `FamilySymbol` (y en
    general para los tipos) lanza `Exception: Name`, porque la propiedad
    se declara en varios niveles de la jerarquia y el motor no resuelve
    la ambiguedad. `DB.Element.Name.GetValue(element)` si funciona.

    Hasta la Fase 8 esto no molesto porque `safe_name` solo se usaba
    sobre vistas, donde `Name` es inequivoco. Lo destapo la Fase 9 al
    necesitar el nombre de los tipos para el selector de valores: sin
    este fallback, TODAS las opciones salian sin etiqueta.
    """
    if element is None:
        return None
    try:
        return element.Name
    except Exception:
        pass
    try:
        return DB.Element.Name.GetValue(element)
    except Exception:
        return None


def category_name(doc, category_id):
    """Nombre de la categoria, o None si el id no es valido.

    Los sheet lists y view lists no tienen categoria: su CategoryId es
    InvalidElementId. No es un error, es su forma normal.
    """
    if not is_valid_id(category_id):
        return None
    try:
        category = DB.Category.GetCategory(doc, category_id)
        if category is not None:
            return category.Name
    except Exception:
        pass
    return None


# --- Parametros -----------------------------------------------------------

# Cache por documento para no recorrer Parameters en cada celda.
# Clave: (id de documento, id de elemento) -> {id de parametro: Parameter}
_PARAM_CACHE = {}

# Cache del elemento de tipo resuelto para cada elemento. Ver
# `_resolve_type_element`: sin esto, `GetTypeId()` + `doc.GetElement()`
# se repetian una vez POR CAMPO en vez de una vez por elemento.
_TYPE_ELEMENT_CACHE = {}
_NO_TYPE = object()


def clear_param_cache():
    """Vacia los caches de parametros y de tipo resuelto.

    Llamar al inicio de CADA peticion que lea parametros -- lectura,
    perfilado, contexto de celda y escritura --: el modelo pudo cambiar
    entre una peticion y otra. [Fase 22] Decia "de lectura", y por eso
    las dos rutas de celda no lo llamaban (bug B1).
    """
    _PARAM_CACHE.clear()
    _TYPE_ELEMENT_CACHE.clear()


def _cache_key(element):
    """Clave estable de cache para un elemento.

    OJO CON `id(element.Document)`: NO SIRVE. Bajo IronPython cada acceso
    a una propiedad de interop devuelve un wrapper nuevo, asi que
    `id(element.Document)` da un entero distinto en cada llamada
    (medido: 310438, 310439, 310440 en tres accesos seguidos). Con esa
    clave los dos caches de este modulo NUNCA acertaban:

      - cada `find_parameter` reconstruia el mapa de parametros del
        elemento (0,94 ms de los 1,29 ms que costaba la llamada)
      - `_PARAM_CACHE` crecia 2 entradas POR LLAMADA sin reusar ninguna:
        perfilar 25 elementos x 2.658 campos lo dejaba en 26.580
        entradas, cada una con objetos Parameter vivos

    Ese era el origen del "~1,35 ms por celda" documentado y de los
    103-135 s de un perfilado. `Document.GetHashCode()` si es estable
    entre accesos y coincide con el `doc` del handler: con eso el costo
    por campo baja de 1,29 ms a 0,0035 ms, unas 369 veces.

    Se mantiene el documento en la clave, y no solo el id del elemento,
    porque un schedule puede incluir elementos de archivos vinculados
    (`IncludeLinkedFiles`) y ahi los ids se repiten entre documentos.
    """
    return (element.Document.GetHashCode(), element_id_value(element.Id))


def _param_map(element):
    """Mapa {valor entero del id de parametro: Parameter} para un elemento."""
    key = _cache_key(element)
    cached = _PARAM_CACHE.get(key)
    if cached is not None:
        return cached

    mapping = {}
    try:
        for param in element.Parameters:
            try:
                mapping[element_id_value(param.Id)] = param
            except Exception:
                continue
    except Exception:
        pass
    _PARAM_CACHE[key] = mapping
    return mapping


def _resolve_type_element(element):
    """Resuelve y cachea el elemento de tipo de `element`.

    `GetTypeId()` + `doc.GetElement()` son dos llamadas de interop.
    Sin cachearlas, `find_parameter` las repetia una vez POR CAMPO
    (una por celda) en vez de una vez por elemento -- invisible con
    las ~15 columnas visibles de una tabla, pero un perfilado recorre
    decenas de campos schedulable por elemento (`profiler.py`), y ahi
    la misma resolucion redundante se multiplica en serio. El
    resultado no cambia entre campos de un mismo elemento dentro de
    la misma peticion, asi que cachea igual que `_param_map`.

    Comparte `_cache_key`, o sea que hasta la Fase 7 tampoco acertaba.
    """
    key = _cache_key(element)
    cached = _TYPE_ELEMENT_CACHE.get(key, _NO_TYPE)
    if cached is not _NO_TYPE:
        return cached

    type_element = None
    try:
        type_id = element.GetTypeId()
        if is_valid_id(type_id):
            type_element = element.Document.GetElement(type_id)
    except Exception:
        type_element = None

    _TYPE_ELEMENT_CACHE[key] = type_element
    return type_element


def find_parameter(element, parameter_id, prefer_type=False):
    """Busca un parametro en la instancia y, si no esta, en su tipo.

    `prefer_type=True` invierte el orden: se usa cuando el campo del
    schedule es de tipo `ScheduleFieldType.ElementType`, porque ese valor
    vive en el tipo y no en la instancia.

    OJO: escribir en un parametro de tipo afecta a TODAS las instancias
    de ese tipo. Quien escriba debe advertirlo.
    """
    if element is None or not is_valid_id(parameter_id):
        return None

    param_key = element_id_value(parameter_id)
    type_element = _resolve_type_element(element)

    if prefer_type:
        sources = [type_element, element]
    else:
        sources = [element, type_element]

    for source in sources:
        if source is None:
            continue
        param = _param_map(source).get(param_key)
        if param is not None:
            return param
    return None


def parameter_payload(param, format_options=None):
    """Representacion JSON de un parametro, para una celda de la tabla.

    Devuelve tanto el valor crudo (para ordenar y comparar) como el
    texto formateado (para mostrar). El frontend usa `display`; el
    ordenamiento usa `value`.

    `format_options` es el `FormatOptions` del CAMPO del schedule, y
    llega desde `ScheduleField.GetFormatOptions()`.  [Fase 19]

    POR QUE HACE FALTA. `AsValueString()` a secas formatea con las
    unidades del PROYECTO, y un campo de schedule puede tener su propio
    formato encima. Medido en `TC-Puertas`, columna `Alto (cm)`, que
    lleva `Accuracy = 0.01`:

        AsValueString()              -> '220.0'      <- lo que mostrabamos
        AsValueString(FormatOptions) -> '220.00'     <- lo que muestra Revit

    Sin esto, el board imprime un numero distinto del que el usuario ve
    en la vista nativa que esta al lado, que es justamente lo que el
    board tiene que facilitar. Y tiene un segundo efecto: el texto
    mostrado es la CLAVE con la que `_grouping_key` colapsa filas y con
    la que `_fill_computed` cruza contra la tabla de Revit.

    Es opcional y por defecto `None`, asi que el perfilado -- que
    recorre los ~2.500 campos schedulable, donde no hay un campo de
    schedule del que sacar opciones -- sigue igual que antes.

    Cuesta 0,009 ms por llamada contra ~0,4 ms de la celda, asi que no
    se cachea: no vale la complejidad.
    """
    if param is None:
        return {
            "value": None,
            "display": None,
            "storage": None,
            "read_only": True,
            "present": False,
        }

    storage = None
    value = None
    try:
        storage = str(param.StorageType)
    except Exception:
        storage = None

    try:
        if storage == "String":
            value = param.AsString()
        elif storage == "Integer":
            value = param.AsInteger()
        elif storage == "Double":
            value = param.AsDouble()
        elif storage == "ElementId":
            value = element_id_value(param.AsElementId())
        else:
            value = None
    except Exception:
        value = None

    display = None
    if format_options is not None:
        try:
            # El formato del CAMPO, que puede pisar el del proyecto.
            display = param.AsValueString(format_options)
        except Exception:
            display = None
    if display is None:
        try:
            # AsValueString aplica unidades y formato del proyecto.
            display = param.AsValueString()
        except Exception:
            display = None
    if display is None:
        try:
            display = param.AsString()
        except Exception:
            display = None
    if display is None and value is not None:
        display = str(value)

    read_only = True
    try:
        read_only = bool(param.IsReadOnly)
    except Exception:
        read_only = True

    return {
        "value": value,
        "display": display,
        "storage": storage,
        "read_only": read_only,
        # `present` distingue "el parametro no existe en este elemento"
        # de "existe pero esta vacio". El perfilado de la Fase 2 necesita
        # esa diferencia para calcular el % de llenado.
        "present": True,
    }


# En IronPython 2.7 un texto puede llegar como `str` o como `unicode`.
# En CPython 3 solo existe `str`. Este par cubre ambos casos.
try:
    _TEXT_TYPES = (str, unicode)  # noqa: F821  (IronPython 2.7)
except NameError:
    _TEXT_TYPES = (str,)


def is_empty_payload(payload):
    """True si la celda cuenta como vacia para el calculo de % llenado."""
    if not payload or not payload.get("present"):
        return True
    value = payload.get("value")
    if value is None:
        return True
    if isinstance(value, _TEXT_TYPES) and value.strip() == "":
        return True
    return False


# --- JSON en ASCII puro -----------------------------------------------------
#
# [20-10-2026, auditoria ponytail] Antes vivia duplicada letra por letra en
# `http.ascii_json` y `audit._ascii_json` -- el comentario de `http.py`
# incluso decia "igual que audit._ascii_json" sin resolverlo. Los dos siguen
# existiendo como envoltorios finos que importan esta funcion DE ADENTRO de
# si misma (no a nivel de modulo): `http.py` y `audit.py` tienen que poder
# importarse sin Revit (los ejercitan `checks/serializer.check.py` y el
# `_self_check` de `audit.py`), y este modulo si trae `Autodesk.Revit.DB`.

def ascii_json(obj, sort_keys=False, allow_nan=True):
    """JSON en ASCII puro, para el motor de arranque de ScheduleBoard.

    Ahi `sys.getdefaultencoding()` es `'ascii'` y `json.dumps(obj,
    ensure_ascii=True)` revienta con cualquier texto no ASCII ("'unknown'
    codec can't decode byte ..."), asi que toda linea con tilde o `ñ`
    desaparecia en silencio (Fase 14/22). `ensure_ascii=False`
    no decodifica nada y si funciona; el escape a `\\uXXXX` se hace despues,
    a mano, y solo puede tocar el interior de los strings.

    Bajo CPython 3 (el otro motor con el que el proyecto declara
    compatibilidad, `__init__.py`) un caracter fuera del plano basico
    multilingue -- un emoji -- tiene `ord(ch) > 0xFFFF`, y un solo `\\uXXXXX`
    de 5 digitos NO es un escape JSON valido: hace falta el PAR SUBROGADO
    UTF-16 que pide la especificacion (`_escape_non_ascii` lo arma). Bajo
    IronPython 2.7 (.NET, strings UTF-16) esto nunca se disparaba: un emoji
    ya llega partido en dos code units de a lo sumo 0xFFFF cada uno.
    """
    text = json.dumps(obj, ensure_ascii=False, sort_keys=sort_keys,
                      allow_nan=allow_nan)
    return "".join(_escape_non_ascii(ch) for ch in text)


def _escape_non_ascii(ch):
    code = ord(ch)
    if code < 128:
        return ch
    if code <= 0xFFFF:
        return "\\u%04x" % code
    code -= 0x10000
    high = 0xD800 + (code >> 10)
    low = 0xDC00 + (code & 0x3FF)
    return "\\u%04x\\u%04x" % (high, low)
