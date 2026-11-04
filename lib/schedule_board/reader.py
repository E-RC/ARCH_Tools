# -*- coding: UTF-8 -*-
"""Lectura del modelo. ESTE MODULO NUNCA ABRE UNA TRANSACCION.

Implementa la regla 1 de `schedule_board/__init__.py`: la tabla NO se lee del render de
Revit, se reconstruye a partir de los elementos y de los campos de la
definicion del schedule.

    1. FilteredElementCollector(doc, schedule.Id)  -> los ElementId reales
    2. Definition.GetFieldOrder() + GetField()     -> las columnas
    3. Leer cada parametro directamente del elemento
"""

import re

from Autodesk.Revit import DB

from schedule_board import serializers as ser
from schedule_board import worksharing as ws


# --- Descubrimiento de schedules ------------------------------------------

def iter_schedules(doc):
    """Todos los ViewSchedule reales del documento.

    Excluye lo que `OfClass(ViewSchedule)` arrastra y no queremos:
      - plantillas de vista (IsTemplate)
      - el schedule interno de keynotes filtradas
      - las tablas de revision internas de los titleblocks
    """
    collector = DB.FilteredElementCollector(doc).OfClass(DB.ViewSchedule)
    for view in collector:
        try:
            if view.IsTemplate:
                continue
            if view.IsInternalKeynoteSchedule:
                continue
            if view.IsTitleblockRevisionSchedule:
                continue
        except Exception:
            # Si alguna de esas propiedades no existe en esta version,
            # es mejor incluir el schedule que perderlo silenciosamente.
            pass
        yield view


def find_schedule(doc, schedule_id_value):
    """Busca un ViewSchedule por el valor entero de su ElementId."""
    for view in iter_schedules(doc):
        if ser.element_id_value(view.Id) == int(schedule_id_value):
            return view
    return None


# --- Clasificacion --------------------------------------------------------

def schedule_kind(view):
    """Clasifica el schedule.

    Los tipos distintos de 'regular' no encajan en el modelo
    "una fila = un elemento" y se muestran como tarjetas de solo lectura.
    """
    definition = view.Definition
    try:
        if definition.IsKeySchedule:
            return "key"
        if definition.IsMaterialTakeoff:
            return "material_takeoff"
        if definition.HasEmbeddedSchedule:
            return "embedded_host"
    except Exception:
        pass
    if not ser.is_valid_id(definition.CategoryId):
        # Sheet lists, view lists, keynote legends: sin categoria.
        return "list"
    return "regular"


def is_editable_kind(kind):
    """True si el schedule admite el modelo 'una fila = un elemento'."""
    return kind == "regular"


# --- Campos ---------------------------------------------------------------

def field_payload(definition, field):
    """Representacion JSON de un ScheduleField."""
    try:
        field_id = int(field.FieldId.IntegerValue)
    except Exception:
        field_id = None

    payload = {
        "field_id": field_id,
        "index": None,
        "name": None,
        "heading": None,
        "field_type": None,
        "parameter_id": ser.element_id_value(field.ParameterId),
        "hidden": False,
        "calculated": False,
        "combined": False,
        # True cuando el valor lo produce Revit y nosotros NO lo
        # reconstruimos: formulas, porcentajes, campos combinados y
        # Count. Estas celdas salen vacias a proposito.
        #
        # Importa mas de lo que parece: sin esta bandera, el perfilado
        # de la Fase 2 leeria una columna Count vacia como "0% llena"
        # y reportaria una columna vacia que en Revit esta llena.
        "revit_computed": False,
        # Un campo de tipo lee y escribe en el TIPO, no en la instancia.
        # El frontend debe distinguirlo visualmente y advertir antes de
        # escribir, porque afecta a todas las instancias de ese tipo.
        "is_type_field": False,
    }

    try:
        payload["index"] = int(field.FieldIndex)
    except Exception:
        pass
    try:
        payload["name"] = field.GetName()
    except Exception:
        pass
    try:
        payload["heading"] = field.ColumnHeading
    except Exception:
        pass
    try:
        field_type = str(field.FieldType)
        payload["field_type"] = field_type
        payload["is_type_field"] = field_type == "ElementType"
    except Exception:
        pass
    try:
        payload["hidden"] = bool(field.IsHidden)
    except Exception:
        pass
    try:
        payload["calculated"] = bool(field.IsCalculatedField)
    except Exception:
        pass
    try:
        payload["combined"] = bool(field.IsCombinedParameterField)
    except Exception:
        pass

    # `Count` no es un parametro del elemento: es el numero de elementos
    # que Revit colapso en la fila. `IsCalculatedField` NO lo cubre
    # (devuelve False), asi que hay que detectarlo por FieldType o la
    # columna sale vacia sin explicacion.
    payload["revit_computed"] = bool(
        payload["calculated"]
        or payload["combined"]
        or payload["field_type"] == "Count"
    )

    # [Fase 26] Lo de la pestana Formatting que cambia lo que se LEE: como
    # resume el campo una fila no itemizada (`Calculation`) y con que texto
    # dice que el grupo no es homogeneo (`Multiple values`). Y la
    # alineacion, que la tabla del board respeta. Getters baratos, todos.
    payload["display_type"] = _enum_name(field, "DisplayType", "Standard")
    payload["multiple_values_text"] = _field_attr(field, "MultipleValuesText")
    payload["horizontal_alignment"] = _enum_name(field, "HorizontalAlignment",
                                                 None)
    return payload


def _field_attr(field, name):
    """Una propiedad del campo, o `None` si esta version de Revit no la tiene."""
    try:
        return getattr(field, name)
    except Exception:
        return None


def _enum_name(field, name, default):
    value = _field_attr(field, name)
    return default if value is None else str(value)


def visible_fields(definition):
    """Campos visibles, en orden, con su objeto ScheduleField.

    Devuelve una lista de tuplas (payload, field). Los campos ocultos se
    excluyen de la tabla: en Revit existen para filtrar y ordenar, no
    para mostrarse.
    """
    result = []
    try:
        field_ids = definition.GetFieldOrder()
    except Exception:
        return result

    for field_id in field_ids:
        try:
            field = definition.GetField(field_id)
        except Exception:
            continue
        payload = field_payload(definition, field)
        if payload["hidden"]:
            continue
        result.append((payload, field))
    return result


def all_fields(definition):
    """Todas las columnas en orden, INCLUIDAS las ocultas.  [Fase 5]

    `visible_fields` descarta las ocultas a proposito: para pintar la
    tabla no existen. Pero para GESTIONAR columnas hay que verlas. En
    Revit una columna oculta sigue viva y sigue sirviendo para filtrar y
    ordenar, asi que esconderla tambien
    del panel de gestion la volveria imposible de recuperar desde el
    board: quedaria afectando la tabla sin que nadie pueda verla.

    Solo lectura, como todo este modulo: nunca abre una transaccion.
    """
    result = []
    try:
        field_ids = definition.GetFieldOrder()
    except Exception:
        return result

    for field_id in field_ids:
        try:
            field = definition.GetField(field_id)
        except Exception:
            continue
        result.append(field_payload(definition, field))
    return result


def _field_reference(definition, field_id):
    """Como referirse a un campo de OTRO schedule.  [Fase 6]

    Un `ScheduleFieldId` solo tiene sentido dentro de su propia
    `ScheduleDefinition` (ver 6.5 del planteamiento): copiar un filtro
    entre schedules exige encontrar el campo EQUIVALENTE en el destino.
    Lo que viaja es el par (parameter_id, field_type), que si es
    comparable entre definiciones.
    """
    reference = {
        "field_id": None,
        "parameter_id": None,
        "field_type": None,
        "name": None,
    }
    try:
        reference["field_id"] = int(field_id.IntegerValue)
    except Exception:
        pass
    try:
        field = definition.GetField(field_id)
    except Exception:
        return reference
    reference["parameter_id"] = ser.element_id_value(field.ParameterId)
    try:
        reference["field_type"] = str(field.FieldType)
    except Exception:
        pass
    try:
        reference["name"] = field.GetName()
    except Exception:
        pass
    return reference


def schedule_filters(definition):
    """Filtros del schedule, con su campo resuelto.  [Fase 6]

    `value_kind` dice con que getter se leyo el valor. Un filtro puede
    no tener valor en absoluto (`HasParameter`, por ejemplo): ahi
    `value_kind` es "none" y `value` es None. Verificado contra el
    modelo real: de los 10 tipos de filtro presentes, los valores son
    string, elementid o ninguno.
    """
    result = []
    try:
        filters = list(definition.GetFilters())
    except Exception:
        return result

    for schedule_filter in filters:
        payload = {
            "field": _field_reference(definition, schedule_filter.FieldId),
            "filter_type": None,
            "value_kind": "none",
            "value": None,
        }
        try:
            payload["filter_type"] = str(schedule_filter.FilterType)
        except Exception:
            pass

        # Los getters lanzan si el valor no es de ese tipo, asi que hay
        # que preguntar antes con las banderas Is*.
        try:
            if schedule_filter.IsStringValue:
                payload["value_kind"] = "string"
                payload["value"] = schedule_filter.GetStringValue()
            elif schedule_filter.IsDoubleValue:
                payload["value_kind"] = "double"
                payload["value"] = schedule_filter.GetDoubleValue()
            elif schedule_filter.IsIntegerValue:
                payload["value_kind"] = "integer"
                payload["value"] = schedule_filter.GetIntegerValue()
            elif schedule_filter.IsElementIdValue:
                payload["value_kind"] = "element_id"
                payload["value"] = ser.element_id_value(
                    schedule_filter.GetElementIdValue())
        except Exception:
            payload["value_kind"] = "none"
            payload["value"] = None

        result.append(payload)
    return result


def schedule_sort_fields(definition):
    """Orden y agrupacion, con el campo resuelto igual que los filtros.  [Fase 6]"""
    result = []
    try:
        sort_fields = list(definition.GetSortGroupFields())
    except Exception:
        return result

    for sort_field in sort_fields:
        payload = {
            "field": _field_reference(definition, sort_field.FieldId),
            "ascending": True,
            "show_header": False,
            "show_footer": False,
            "show_footer_title": False,
            "show_footer_count": False,
            "show_blank_line": False,
        }
        try:
            payload["ascending"] = str(sort_field.SortOrder) == "Ascending"
        except Exception:
            pass
        for key, attr in (("show_header", "ShowHeader"),
                          ("show_footer", "ShowFooter"),
                          # [Fase 25] El desplegable de Footer del dialogo
                          # nativo: "Title, count, and totals", etc.
                          ("show_footer_title", "ShowFooterTitle"),
                          ("show_footer_count", "ShowFooterCount"),
                          ("show_blank_line", "ShowBlankLine")):
            try:
                payload[key] = bool(getattr(sort_field, attr))
            except Exception:
                pass
        result.append(payload)
    return result


# --- Schedule Properties  [Fase 25] -----------------------------------------
#
# Lo que el panel necesita para EDITAR filtros y orden como el dialogo
# nativo. Sondeado en vivo antes de escribirlo (Revit 2025):
#
#   - Los `CanFilterBy*` NO estan en el `ScheduleField` sino en la
#     definicion, y reciben el `ScheduleFieldId`. `Family` y `Type` son
#     `ElementId` y aun asi admiten los operadores de texto (`TC-Puertas`
#     filtra `Family Contains "ARCH-PU-"` con un valor string).
#   - `IsValidCategoryForFilterBySheet()` no recibe argumentos.
#   - `GetSpecTypeId()` devuelve `''` tanto para texto como para
#     `ElementId`: el tipo de almacenamiento sale de
#     `doc.get_TypeOfStorage` en los parametros de Revit, y del spec en
#     los compartidos (que son de texto cuando el spec es `''`).
#   - Un filtro sobre un `Double` guarda PIES internos; el usuario ve cm
#     o m2. La conversion usa la unidad del campo, o la del proyecto si
#     el campo usa la de por defecto.

FILTER_OPERATORS = (
    ("value", "CanFilterByValue",
     ("Equal", "NotEqual", "GreaterThan", "GreaterThanOrEqual",
      "LessThan", "LessThanOrEqual")),
    ("substring", "CanFilterBySubstring",
     ("Contains", "NotContains", "BeginsWith", "NotBeginsWith",
      "EndsWith", "NotEndsWith")),
    ("presence", "CanFilterByValuePresence", ("HasValue", "HasNoValue")),
    ("existence", "CanFilterByParameterExistence", ("HasParameter",)),
)


def filter_group(filter_type):
    """A que familia de operadores pertenece un `ScheduleFilterType`."""
    for group, _method, types in FILTER_OPERATORS:
        if filter_type in types:
            return group
    return None


def filter_value_kind(filter_type, storage):
    """Con que tipo de valor se arma un filtro. Pura.

    Los de texto van siempre con string -- aunque el campo sea un
    `ElementId`, como `Family Contains` --; los de presencia no llevan
    valor; los de comparacion, el del almacenamiento del campo.
    """
    group = filter_group(filter_type)
    if group == "substring":
        return "string"
    if group in ("presence", "existence"):
        return "none"
    return {"Integer": "integer", "Double": "double",
            "ElementId": "element_id"}.get(storage, "string")


def field_storage(doc, field):
    """`String`, `Integer`, `Double` o `ElementId`; `None` si no se sabe."""
    parameter_id = ser.element_id_value(field.ParameterId)
    if parameter_id is not None and parameter_id < 0:
        try:
            bip = DB.BuiltInParameter(parameter_id)
            return str(doc.get_TypeOfStorage(bip))
        except Exception:
            pass
    try:
        spec = field.GetSpecTypeId().TypeId or ""
    except Exception:
        return None
    if spec == "" or "spec.string" in spec:
        return "String"
    if "spec.bool" in spec or "spec.int" in spec:
        return "Integer"
    if "reference" in spec:
        return "ElementId"
    return "Double"


def _display_unit(doc, field):
    """La `ForgeTypeId` de la unidad en que se VE el campo, o `None`."""
    try:
        options = field.GetFormatOptions()
        if options is not None and not options.UseDefault:
            return options.GetUnitTypeId()
        spec = field.GetSpecTypeId()
        return doc.GetUnits().GetFormatOptions(spec).GetUnitTypeId()
    except Exception:
        return None


def to_display_number(doc, field, internal):
    """Un numero interno de Revit en la unidad que ve el usuario."""
    unit = _display_unit(doc, field)
    if unit is None or internal is None:
        return internal
    try:
        return DB.UnitUtils.ConvertFromInternalUnits(float(internal), unit)
    except Exception:
        return internal


def to_internal_number(doc, field, shown):
    """La inversa: lo que escribio el usuario, en unidades internas."""
    unit = _display_unit(doc, field)
    if unit is None:
        return float(shown)
    try:
        return DB.UnitUtils.ConvertToInternalUnits(float(shown), unit)
    except Exception:
        return float(shown)


def _unit_label(doc, field):
    unit = _display_unit(doc, field)
    if unit is None:
        return None
    try:
        return DB.LabelUtils.GetLabelForUnit(unit)
    except Exception:
        return None


def field_options(doc, definition, field):
    """Que se puede hacer con un campo en Filter y Sorting/Grouping."""
    field_id = field.FieldId
    operators = []
    for _group, method, types in FILTER_OPERATORS:
        try:
            if getattr(definition, method)(field_id):
                operators.extend(types)
        except Exception:
            pass
    try:
        can_sort = bool(definition.CanSortByField(field_id))
    except Exception:
        can_sort = False
    storage = field_storage(doc, field)
    return {
        "filter_operators": operators,
        "storage": storage,
        "unit": _unit_label(doc, field) if storage == "Double" else None,
        "can_sort": can_sort,
    }


# --- La pestana Formatting  [Fase 26] ---------------------------------------
#
# Los valores de los enums van por NOMBRE (`Totals`, `Center`), que es lo
# que el writer vuelve a convertir con `Enum.Parse`: el cliente nunca ve un
# entero de Revit.

def _accuracy(options):
    try:
        return float(options.Accuracy)
    except Exception:
        return None


def _symbols(unit):
    result = []
    try:
        symbols = DB.FormatOptions.GetValidSymbols(unit)
    except Exception:
        return result
    for symbol in symbols:
        type_id = symbol.TypeId or ""
        try:
            label = DB.LabelUtils.GetLabelForSymbol(symbol) if type_id else "None"
        except Exception:
            label = type_id or "None"
        result.append({"id": type_id, "label": label})
    return result


def field_format(doc, field):
    """El *Field Format* de un campo numerico con unidades, o `None`.

    Lo que ofrece el dialogo nativo: usar el formato del proyecto, o una
    unidad, un redondeo, un simbolo, suprimir ceros finales y agrupar
    digitos. `units` trae las unidades validas para el spec del campo, cada
    una con sus simbolos: son pocas (un area tiene ~10).
    """
    try:
        spec = field.GetSpecTypeId()
        if not spec.TypeId or not DB.UnitUtils.IsMeasurableSpec(spec):
            return None
        options = field.GetFormatOptions()
    except Exception:
        return None
    # Lo que se muestra cuando usa el del proyecto: el formato efectivo.
    effective = options
    if options.UseDefault:
        try:
            effective = doc.GetUnits().GetFormatOptions(spec)
        except Exception:
            return None
    units = []
    for unit in DB.UnitUtils.GetValidUnits(spec):
        try:
            label = DB.LabelUtils.GetLabelForUnit(unit)
        except Exception:
            label = unit.TypeId
        units.append({"id": unit.TypeId, "label": label,
                      "symbols": _symbols(unit)})
    result = {"use_default": bool(options.UseDefault), "units": units}
    for key, getter in (
            ("unit", lambda: effective.GetUnitTypeId().TypeId),
            ("symbol", lambda: effective.GetSymbolTypeId().TypeId or ""),
            ("accuracy", lambda: _accuracy(effective)),
            ("suppress_trailing_zeros",
             lambda: bool(effective.SuppressTrailingZeros)),
            ("digit_grouping", lambda: bool(effective.UseDigitGrouping))):
        try:
            result[key] = getter()
        except Exception:
            result[key] = None
    return result


def field_formatting(doc, field):
    """Lo que la pestana Formatting muestra de un campo."""
    def safe(getter, default=None):
        try:
            return getter()
        except Exception:
            return default

    return {
        "heading_orientation": _enum_name(field, "HeadingOrientation", None),
        "horizontal_alignment": _enum_name(field, "HorizontalAlignment", None),
        "vertical_alignment": _enum_name(field, "VerticalAlignment", None),
        "display_type": _enum_name(field, "DisplayType", "Standard"),
        "can_total": safe(lambda: bool(field.CanTotal()), False),
        "can_min_max": safe(lambda: bool(field.CanDisplayMinMax()), False),
        # None = esta version de Revit no tiene la opcion (anterior a 2025
        # no se verifico).
        "multiple_values": _enum_name(field, "MultipleValuesDisplayType", None),
        "multiple_values_custom_text": _field_attr(field,
                                                   "MultipleValuesCustomText"),
        "format": field_format(doc, field),
    }


def schedule_properties(definition):
    """Lo del dialogo que no es una lista: casillas y titulos."""
    result = {}
    for key, getter in (
            ("itemized", lambda: bool(definition.IsItemized)),
            ("filter_by_sheet", lambda: bool(definition.IsFilteredBySheet)),
            ("can_filter_by_sheet",
             lambda: bool(definition.IsValidCategoryForFilterBySheet())),
            ("include_links", lambda: bool(definition.IncludeLinkedFiles)),
            ("can_include_links",
             lambda: bool(definition.CanIncludeLinkedFiles())),
            ("grand_total", lambda: bool(definition.ShowGrandTotal)),
            ("grand_total_title_shown",
             lambda: bool(definition.ShowGrandTotalTitle)),
            ("grand_total_count", lambda: bool(definition.ShowGrandTotalCount)),
            ("grand_total_title", lambda: definition.GrandTotalTitle)):
        try:
            result[key] = getter()
        except Exception:
            result[key] = None
    return result


def definition_payload(doc, view):
    """La receta completa para el panel Schedule Properties.  [Fase 25]

    Es la de `GET /definition/` de la Fase 6 mas lo que hace falta para
    editar: los operadores y el tipo de valor de cada campo, el valor de
    los filtros numericos en la unidad que se ve (`display_value`), y
    las casillas del dialogo. Sigue siendo barata: solo la definicion.
    """
    definition = view.Definition
    fields = []
    by_id = {}
    for field_id in definition.GetFieldOrder():
        try:
            field = definition.GetField(field_id)
        except Exception:
            continue
        payload = field_payload(definition, field)
        payload.update(field_options(doc, definition, field))
        payload["formatting"] = field_formatting(doc, field)
        fields.append(payload)
        by_id[payload["field_id"]] = field

    filters = schedule_filters(definition)
    for item in filters:
        field = by_id.get(item["field"]["field_id"])
        item["display_value"] = item["value"]
        if item["value_kind"] == "double" and field is not None:
            item["display_value"] = to_display_number(doc, field, item["value"])

    return {
        "schedule_id": ser.element_id_value(view.Id),
        "name": ser.safe_name(view),
        "fields": fields,
        "filters": filters,
        "sort": schedule_sort_fields(definition),
        "properties": schedule_properties(definition),
    }


MAX_FIELD_VALUES = 500


def field_values(doc, view, field):
    """Los valores que puede tomar un campo `ElementId`, para un filtro.

    Es el desplegable del dialogo nativo cuando se filtra por `Level` o
    por `Family`. Un nivel sale de la lista de niveles del modelo,
    ordenada por elevacion (que es como Revit los compara: un filtro
    `Level LessThan` es "por debajo de"). Salen de los elementos de la
    categoria del schedule -- no solo de los que hoy pasan sus filtros,
    porque se esta editando justamente eso --, en el orden en que Revit
    los ordenaria (Fase 24): los niveles por elevacion, el resto natural.
    Tope de `MAX_FIELD_VALUES`.
    """
    values = {}
    truncated = False
    is_type = str(field.FieldType) == "ElementType"
    collector = DB.FilteredElementCollector(doc).OfCategoryId(
        view.Definition.CategoryId).WhereElementIsNotElementType()
    for element in collector:
        cell = ser.parameter_payload(ser.find_parameter(
            element, field.ParameterId, prefer_type=is_type))
        if cell["value"] is None or cell["value"] in values:
            continue
        if len(values) >= MAX_FIELD_VALUES:
            truncated = True
            break
        values[cell["value"]] = cell["display"] or u"{0}".format(cell["value"])
    cache = {}
    items = []
    keys = []
    for value, display in values.items():
        cell = {"storage": "ElementId", "value": value, "display": display,
                "present": True}
        items.append({"value": value, "display": display})
        keys.append((sort_value(cell, _level_elevation(doc, cell, cache)),
                     sort_tie(cell)))
    order = order_indices(keys, [True, True])
    return [items[i] for i in order], truncated


def sort_group_key_fields(definition):
    """Los campos por los que REVIT colapsa una tabla no itemizada.  [Fase 20]

    Devuelve `(campos, modo)`. `campos` es una lista de tuplas
    (payload, field) -- la misma forma que `visible_fields` -- o `None`
    cuando no se puede usar esa clave y hay que caer al comportamiento
    anterior. `modo` dice cual de los dos se usa, y es lo que la tarjeta
    le explica al usuario.

    POR QUE EXISTE, Y QUE ARREGLA. Hasta la Fase 19 el board colapsaba
    por TODOS los valores mostrados (`_grouping_key`), asi que cualquier
    diferencia en cualquier columna abria una fila nueva. Revit no hace
    eso: colapsa por los campos de *Sorting/Grouping* y resume el resto
    con `<varies>`. Medido sobre `TC-Tabiques` (7.285 elementos, 16
    columnas visibles): el board devolvia **2.452 filas** y ahora
    devuelve **29**, contra las ~30 que se contaron en una captura de la
    vista nativa. La diferencia era ~80x, y era nuestra.

    (Las 29 son medidas; el 30 sale de contar filas en una captura, asi
    que la fila de diferencia queda anotada como pendiente en vez de
    darse por buena.)

    EL DETALLE QUE EXPLICA POR QUE NADIE LO VIO EN 19 FASES: el unico
    campo de *Sorting/Grouping* de `TC-Tabiques` es `Type`, y esta
    OCULTO. `visible_fields` lo descarta a proposito -- para pintar la
    tabla no existe --, asi que mirando solo lo que se ve, el criterio
    de agrupacion de Revit es invisible. Por eso aca se recorre
    `GetFieldOrder()` completo y no la lista visible.

    CUANDO DEVUELVE None, Y POR QUE ESE CAMINO ES EL SEGURO:

      - `no_sort_fields`: el schedule no tiene Sorting/Grouping. Sin
        clave no hay nada que replicar, y el comportamiento anterior
        --agrupar por todos los valores-- es lo razonable.
      - `computed_sort_field`: uno de los campos de la clave es
        calculado (formula, porcentaje, `Count`, combinado), o sea que
        NO lo reconstruimos y no podemos armar su valor. Caer a la clave
        vieja da grupos mas FINOS que los de Revit; usar una clave
        incompleta los daria mas GRUESOS, que es lo peligroso: fusionaria
        elementos que Revit muestra separados. Fino es la direccion
        segura, y es la aproximacion que el proyecto ya tenia asumida.
      - `unreadable_sort_field`: la definicion no devolvio el campo.

    Solo lectura, como todo este modulo.
    """
    try:
        sort_fields = list(definition.GetSortGroupFields())
    except Exception:
        return None, "no_sort_fields"
    if not sort_fields:
        return None, "no_sort_fields"

    result = []
    for sort_field in sort_fields:
        try:
            field = definition.GetField(sort_field.FieldId)
        except Exception:
            return None, "unreadable_sort_field"
        if field is None:
            return None, "unreadable_sort_field"
        payload = field_payload(definition, field)
        if payload["revit_computed"]:
            return None, "computed_sort_field"
        result.append((payload, field))
    return result, "sort_fields"


# --- Orden de las filas  [Fase 24] -----------------------------------------
#
# Revit ORDENA por los campos de Sorting/Grouping, ademas de agrupar por
# ellos (Fase 20). Hasta la Fase 23 el board agrupaba como Revit pero
# dejaba las filas en el orden del collector. La regla de abajo no es una
# suposicion: salio de comparar, fila por fila, la reconstruccion contra
# la tabla que Revit pinta (`GetCellText`, solo como testigo, desde una
# ruta temporal) en 13 schedules del modelo de referencia. Con esta regla
# coinciden los 13, y cada pieza la exigio un caso:
#
#   - TEXTO: natural y sin mayusculas. `NPT_2` va antes que `NPT_10`
#     (`Room Schedule`: el orden ordinal fallaba en 949 de 976 filas, y
#     el natural CON mayusculas en 968).
#   - `Level`: por ELEVACION, no por nombre. `Parking Schedule` dibuja
#     NPT_-2z, NPT_-2, NPT_-1, NPT_1 -- el orden de sus elevaciones
#     (-21, -19,4, -10,5 y 0 pies) --, y por nombre el natural da otro.
#   - EMPATE de texto en un `ElementId`: por el id, el menor primero.
#     `Tabla de Ventanas` tiene dos tipos distintos llamados
#     `VC1-H241-W230`, y Revit pone primero el de id menor en los dos
#     niveles donde conviven. Por el primer elemento del grupo, fallaba.
#   - EMPATE completo: el orden del collector. `TC-Ventanas` (1.040
#     filas itemizadas) coincide asi; por id descendente, no.
#
#   - [Fase 25] VACIOS: primero en ascendente, ultimos en descendente.
#     NUMEROS: por el valor REDONDEADO que se ve, no por el interno --
#     dos "0.43 m2" empatan aunque difieran en la cuarta cifra. Medido
#     editando el orden de `Room Schedule` (976 filas, Asignacion + Area,
#     en los dos sentidos) y de `TO-Bajo NTN`; la Fase 24 no pudo porque
#     el modelo no tenia ningun campo de orden asi.
#
# Lo unico sin medir: el empate por id en DESCENDENTE.

_DIGITS = re.compile(r"(\d+)")


def sort_specs(definition):
    """Los campos de Sorting/Grouping con su sentido, para ORDENAR.

    Devuelve una lista de (payload, field, ascending). Corta en el
    primer campo que no se puede reconstruir (calculado o ilegible): los
    anteriores siguen ordenando bien, y un criterio inventado para el
    resto seria peor que dejar ese tramo en el orden del collector.

    Distinto de `sort_group_key_fields`, que ante un campo calculado
    renuncia entera: para AGRUPAR, una clave incompleta fusiona filas;
    para ORDENAR, un prefijo del criterio solo deja empates.
    """
    try:
        sort_fields = list(definition.GetSortGroupFields())
    except Exception:
        return []
    specs = []
    for sort_field in sort_fields:
        try:
            field = definition.GetField(sort_field.FieldId)
        except Exception:
            break
        if field is None:
            break
        payload = field_payload(definition, field)
        if payload["revit_computed"]:
            break
        try:
            ascending = str(sort_field.SortOrder) != "Descending"
        except Exception:
            ascending = True
        specs.append((payload, field, ascending))
    return specs


def _natural(text):
    """Clave de orden natural y sin mayusculas: `NPT_2` < `NPT_10`."""
    parts = []
    for part in _DIGITS.split(text.lower()):
        if not part:
            continue
        if part.isdigit():
            parts.append((0, int(part), u""))
        else:
            parts.append((1, 0, part))
    return tuple(parts)


def sort_value(cell, number=None):
    """Con que valor ordena Revit una celda. Pura: no toca Revit.

    `number` es un numero que manda sobre el texto -- hoy, la elevacion
    de un `Level` (ver `_level_elevation`). El primer elemento de la
    tupla separa las clases, para que nunca se comparen entre si un
    numero y un texto.
    """
    display = cell.get("display") or u""
    if not cell.get("present") or display == u"":
        return (0,)
    if number is not None:
        return (1, number)
    storage = cell.get("storage")
    value = cell.get("value")
    if storage == "Double" and isinstance(value, (int, float)):
        # [Fase 25] Por el numero REDONDEADO que se ve, no por el interno:
        # medido en `Room Schedule` ordenado por Area, dos locales de
        # "0.43 m2" (4,5972 y 4,5962 pies2 internos) EMPATAN para Revit y
        # quedan en el orden del collector.
        shown = _display_number(display)
        return (2, value if shown is None else shown)
    if storage == "Integer" and isinstance(value, (int, float)):
        return (2, value)
    return (3, _natural(display))


_NUMBER_SHOWN = re.compile(r"^\s*(-?[\d,]*\.?\d+)\s*[^\d\s]*\s*$")


def _display_number(display):
    """El numero de un texto como `0.43 m2` o `1,250.5`; `None` si no es eso.

    Pies y pulgadas (`5' - 6"`) no calzan y caen al valor interno: ahi el
    redondeo no se puede leer del texto.
    """
    match = _NUMBER_SHOWN.match(display)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def sort_tie(cell, payload=None):
    """El desempate de un `ElementId`: su id. 0 para todo lo demas.

    Va DESPUES de todos los campos, no pegado al suyo: `Family` guarda
    como valor el id del TIPO, asi que un desempate por campo cortaria
    antes de mirar `Type`. Asi falló la primera version en `TC-Puertas`
    y `TC-Ventanas` (Family, Type, los dos descendentes).

    [27-09-2026] Y `Family` no desempata: dos tipos de la misma familia
    son la MISMA familia para Revit (ver `_cell_identity`).
    """
    if _is_family_field(payload):
        return 0
    if cell.get("storage") != "ElementId" or cell.get("value") is None:
        return 0
    try:
        return int(cell.get("value"))
    except (TypeError, ValueError):
        return 0


def order_indices(keys, ascending):
    """El orden de las filas, como lista de indices. Pura.

    `keys[i]` es la tupla de orden de la fila i y `ascending` el sentido
    de cada nivel de la tupla. Se ordena del ultimo nivel al primero con
    `sort` estable -- tambien con `reverse`, que en Python conserva el
    orden de los empates --, asi que un empate completo queda en el
    orden de entrada: el del collector, que es lo que hace Revit.
    """
    order = list(range(len(keys)))
    for level in reversed(range(len(ascending))):
        order.sort(key=lambda i: keys[i][level], reverse=not ascending[level])
    return order


def _visible_prefix(specs, fields):
    """El tramo inicial de `specs` cuyos campos se muestran.

    Para ordenar filas colapsadas por la clave de RESPALDO
    (`_grouping_key`): ahi un grupo solo comparte las columnas visibles,
    asi que un campo de orden oculto puede diferir entre sus miembros y
    ordenar por el del primero seria inventar. Lo encontro la revision
    de codigo de la Fase 24, no una medicion.
    """
    visible = set(payload["field_id"] for payload, _field in fields)
    prefix = []
    for spec in specs:
        if spec[0]["field_id"] not in visible:
            break
        prefix.append(spec)
    return prefix


def _directions(specs):
    """Los sentidos de la tupla de `_sort_key`: los campos y sus desempates,
    cada desempate en el sentido de su campo."""
    return [ascending for _payload, _field, ascending in specs] * 2


def _level_elevation(doc, cell, cache):
    """La elevacion si la celda apunta a un `Level`; si no, `None`."""
    if doc is None or cell.get("storage") != "ElementId":
        return None
    value = cell.get("value")
    if value is None:
        return None
    if value not in cache:
        elevation = None
        try:
            element = doc.GetElement(ser.to_element_id(value))
            if isinstance(element, DB.Level):
                elevation = element.Elevation
        except Exception:
            elevation = None
        cache[value] = elevation
    return cache[value]


def _sort_key(doc, element, specs, positions, cells, cache):
    """La tupla de orden de un elemento.

    `positions` dice, por campo de orden, en que columna de `cells` ya
    esta leido (ver `_key_positions`); `None` -- o `cells=None` -- lo
    lee del elemento.
    """
    key = []
    ties = []
    for (payload, field, _ascending), position in zip(specs, positions):
        if cells is not None and position is not None:
            cell = cells[position]
        else:
            cell = _element_cell(element, payload, field)
        key.append(sort_value(cell, _level_elevation(doc, cell, cache)))
        ties.append(sort_tie(cell, payload))
    return tuple(key + ties)


# --- Elementos ------------------------------------------------------------

def schedule_elements(doc, view):
    """Los elementos que el schedule realmente contiene.

    Este es EL metodo que habilita todo el producto: el collector
    construido con (documento, id de vista) devuelve los elementos
    visibles en esa vista, ya con la categoria y los filtros del
    schedule aplicados, y cada uno con su ElementId.
    """
    collector = DB.FilteredElementCollector(doc, view.Id)
    return list(collector.WhereElementIsNotElementType())


# --- Presencia en laminas -------------------------------------------------

def schedule_instance_count(view):
    """En cuantas laminas esta puesto el schedule.

    OJO CON LA FIRMA. `GetScheduleInstances()` NO existe sin argumentos:
    es `GetScheduleInstances(segmentIndex)`. Hasta la Fase 7 esto se
    llamaba sin argumento dentro de un try/except, asi que `on_sheets`
    valia 0 para TODOS los schedules, en silencio, desde la Fase 1.

    Y el indice no es intercambiable con 0 (medido en el modelo de
    referencia, 240 schedules):

        segmentIndex = -1   los 182 que estan en laminas
        segmentIndex = 0    solo 2, los dos que estan PARTIDOS

    O sea: en un schedule sin partir la instancia no cuelga del segmento
    0, hay que pedir -1; y en uno partido cada segmento tiene su propia
    instancia y -1 devuelve 0. Por eso las dos ramas.
    """
    try:
        if view.IsSplit():
            total = 0
            for index in range(view.GetSegmentCount()):
                total += len(list(view.GetScheduleInstances(index)))
            return total
        return len(list(view.GetScheduleInstances(-1)))
    except Exception:
        return 0


# --- Resumen (Fase 0) -----------------------------------------------------

def schedule_summary(doc, view):
    """Resumen de un schedule, para la tarjeta en modo alejado (LOD).

    No recorre los elementos: es barato aunque haya muchos schedules.
    `element_count` sale en `None` y lo llena el detalle. [Fase 22] El
    conteo opcional (`?counts=1`) se saco: congelaba Revit minutos.
    """
    definition = view.Definition
    kind = schedule_kind(view)

    fields = visible_fields(definition)

    summary = {
        "id": ser.element_id_value(view.Id),
        "name": ser.safe_name(view),
        "kind": kind,
        "editable": is_editable_kind(kind),
        "category_id": ser.element_id_value(definition.CategoryId),
        "category": ser.category_name(doc, definition.CategoryId),
        "column_count": len(fields),
        "element_count": None,
        "itemized": True,
        "on_sheets": 0,
    }

    try:
        summary["itemized"] = bool(definition.IsItemized)
    except Exception:
        pass
    summary["on_sheets"] = schedule_instance_count(view)

    # Propiedad en modelo compartido [Fase 8]. Sale de `view.Id`, que ya
    # esta a mano: reconstruir un ElementId desde el entero seria pedir
    # la ambiguedad de sobrecargas que este proyecto evita.
    #
    # OJO con el nombre de las claves: `editable` ya existe arriba y
    # significa OTRA cosa -- si el board sabe pintar ese tipo de tabla.
    # La propiedad viaja como `checkout` y `owner`.
    if ws.is_workshared(doc):
        summary["checkout"] = ws.status_of(doc, view.Id)
        summary["owner"] = (
            ws.owner_of(doc, view.Id)
            if summary["checkout"] != ws.NOT_OWNED else None)

    return summary


def list_schedules(doc):
    """Resumen de todos los schedules del documento."""
    return [
        schedule_summary(doc, view)
        for view in iter_schedules(doc)
    ]


# --- Columnas actuales de varios schedules (Fase 6, cerrado en la 7) ------

def compatible_fields(doc, views):
    """Que campo aplica a cuantos de los schedules dados.  [Fase 6]

    Solo lectura. Vivia en `writer.py` junto a `apply_batch`, que es quien
    la necesita; se mudo aca en la Fase 27, al lado de su gemela
    `columns_across`, porque este es el modulo de la lectura.

    Devuelve la UNION, no la interseccion, con el conteo de a cuantos
    aplica cada campo. La interseccion es lo que 6.5 pide OFRECER, pero
    para poder decir "este campo aplica a 4 de los 7 seleccionados"
    —que es el otro requisito de 6.5, y el mas importante— hace falta
    saber tambien de los que no aplican a todos. El cliente ordena por
    ese conteo y avisa antes de ejecutar.

    `GetName` se llama UNA vez por campo distinto y no una por schedule:
    con ~2.500 campos por categoria y un lote de 15 schedules, la
    diferencia es 2.500 llamadas contra 37.500.
    """
    universe = {}
    for view in views:
        view_id = ser.element_id_value(view.Id)
        try:
            schedulable = list(view.Definition.GetSchedulableFields())
        except Exception:
            continue
        for field in schedulable:
            try:
                parameter_id = ser.element_id_value(field.ParameterId)
                field_type = str(field.FieldType)
            except Exception:
                continue
            key = (parameter_id, field_type)
            entry = universe.get(key)
            if entry is None:
                name = None
                try:
                    name = field.GetName(doc)
                except Exception:
                    pass
                entry = {
                    "parameter_id": parameter_id,
                    "field_type": field_type,
                    "name": name,
                    "is_shared_or_project": parameter_id is not None and parameter_id > 0,
                    "schedule_ids": [],
                }
                universe[key] = entry
            # UNA vez por schedule, aunque la categoria ofrezca la misma
            # clave dos veces: si no, `applies_to` puede superar el total
            # de seleccionados y el aviso "aplica a N de M" queda absurdo.
            # Delatado en la Fase 7 por su gemelo `reader.columns_across`,
            # que llego a reportar "en 27 de 20".
            if view_id not in entry["schedule_ids"]:
                entry["schedule_ids"].append(view_id)

    fields = list(universe.values())
    for entry in fields:
        entry["applies_to"] = len(entry["schedule_ids"])
    # Primero los que aplican a mas schedules; a igualdad, por nombre.
    fields.sort(key=lambda item: (-item["applies_to"], (item["name"] or "").lower()))
    return fields


def columns_across(views):
    """Las columnas que HOY tienen los schedules dados, con en cuantos esta.

    Contraparte de `compatible_fields`, y la lectura que faltaba
    para poder ofrecer "quitar una columna del lote" sin hacerlo a
    ciegas: esa funcion devuelve los campos que Revit OFRECE para la
    categoria (~2.500 por schedule), no los que el schedule realmente
    usa. Quitar hay que ofrecerlo sobre lo segundo.

    Barata: recorre la definicion de cada schedule, no sus elementos.
    Incluye las columnas ocultas, que tambien son columnas y tambien se
    pueden querer quitar.
    """
    universe = {}
    for view in views:
        view_id = ser.element_id_value(view.Id)
        try:
            payloads = all_fields(view.Definition)
        except Exception:
            continue
        for payload in payloads:
            parameter_id = payload.get("parameter_id")

            # LOS CAMPOS CALCULADOS NO SE PUEDEN IDENTIFICAR ENTRE
            # SCHEDULES, asi que se omiten. Una formula, un porcentaje o
            # un campo combinado no tienen Parameter detras: su
            # `parameter_id` es -1 (InvalidElementId) para TODOS, asi que
            # la clave (parameter_id, field_type) los colapsa entre si.
            #
            # Verificado en el modelo real: 'TC-Ancho Escaleras Sobre NTN
            # ... Departamentos' tiene DOS campos Formula distintos
            # (ARCH-Area-Redondeada y ARCH-Carga-ocupacion) con la misma
            # clave. Ofrecerlos aca no seria solo un conteo inflado
            # ("en 27 de 20", que es lo que delato el problema): quitar
            # el que dice un nombre podria tocar el otro.
            #
            # Ojo: NO alcanza con `parameter_id > 0`. Los parametros
            # built-in tienen ids NEGATIVOS y son perfectamente validos;
            # el unico invalido es -1.
            if parameter_id is None or parameter_id == -1:
                continue

            key = (parameter_id, payload.get("field_type"))
            entry = universe.get(key)
            if entry is None:
                entry = {
                    "parameter_id": parameter_id,
                    "field_type": payload.get("field_type"),
                    "name": payload.get("name"),
                    "schedule_ids": [],
                    "hidden_in": 0,
                }
                universe[key] = entry
            # Un mismo schedule cuenta UNA vez aunque la clave aparezca
            # dos veces en su definicion.
            if view_id not in entry["schedule_ids"]:
                entry["schedule_ids"].append(view_id)
                if payload.get("hidden"):
                    entry["hidden_in"] += 1

    columns = list(universe.values())
    for entry in columns:
        entry["present_in"] = len(entry["schedule_ids"])
    columns.sort(key=lambda item: (-item["present_in"], (item["name"] or "").lower()))
    return columns


# --- Categorias que admiten schedule (Fase 7) -----------------------------

def areas_category_id_value():
    """El valor entero del id de la categoria Areas.

    Se construye desde el ENUM (`ElementId(BuiltInCategory)`), que es la
    sobrecarga sin ambiguedad.
    Lo que este proyecto evita es `ElementId(int)`, que compite con las
    sobrecargas de enum.
    """
    try:
        return ser.element_id_value(DB.ElementId(DB.BuiltInCategory.OST_Areas))
    except Exception:
        return -2003200


def needs_area_scheme(category_id_value):
    """True si crear un schedule de esa categoria exige un AreaScheme.

    Es SOLO Areas, y no es una suposicion: se probaron las 187
    categorias validas creando un schedule de cada una dentro de una
    transaccion revertida. 186 crearon bien con dos argumentos y una
    fallo:

        Areas (-2003200): "The Areas category was specified but an area
        scheme ID was not provided."

    La misma excepcion la lanza `GetDefaultNameForSchedule`, por eso
    Areas no tiene nombre sugerido. Con el scheme, en cambio, Revit
    nombra la vista solo: "Area Schedule (VENTAS)".
    """
    try:
        return int(category_id_value) == areas_category_id_value()
    except (TypeError, ValueError):
        return False


def area_schemes(doc):
    """Los AreaScheme del documento, para poder crear schedules de area.

    El modelo de referencia tiene 4 (VENTAS, MUNICIPAL, Areas por
    tipologia, PASILLO PROTEGIDO), asi que no hay un default razonable:
    lo tiene que elegir el usuario.
    """
    schemes = []
    try:
        collector = DB.FilteredElementCollector(doc).OfClass(DB.AreaScheme)
    except Exception:
        return schemes

    for scheme in collector:
        schemes.append({
            "id": ser.element_id_value(scheme.Id),
            "name": ser.safe_name(scheme),
        })
    schemes.sort(key=lambda item: (item["name"] or "").lower())
    return schemes


def valid_categories(doc, with_default_names=False):
    """Las categorias que admiten un schedule, para el selector de crear.

    `GetValidCategoriesForSchedule()` es ESTATICA y no recibe argumentos.
    Devuelve 188 ids en el modelo de referencia, de los cuales uno
    (`-1`, InvalidElementId) no resuelve a ninguna `Category`: se
    descarta y quedan 187.

    `with_default_names` esta apagado por defecto porque los dos costos
    no se parecen (medido sobre las 188): resolver los NOMBRES cuesta
    11 ms, y `GetDefaultNameForSchedule` cuesta 1.643 ms, o sea 150
    veces mas. Quien solo necesita la lista no deberia pagar eso, y
    menos con la cola serial y el hilo de Revit de por medio.

    Cuando se piden, el nombre sugerido vale la espera: Revit ya evita
    las colisiones por su cuenta -- para Grids devuelve
    "Grid Schedule 2" si "Grid Schedule" ya existe.
    """
    try:
        category_ids = list(DB.ViewSchedule.GetValidCategoriesForSchedule())
    except Exception:
        return []

    categories = []
    for category_id in category_ids:
        try:
            category = DB.Category.GetCategory(doc, category_id)
        except Exception:
            continue
        if category is None:
            continue

        payload = {
            "category_id": ser.element_id_value(category_id),
            "name": category.Name,
            "default_name": None,
            "needs_area_scheme": needs_area_scheme(
                ser.element_id_value(category_id)),
        }
        # Para Areas no se pide: `GetDefaultNameForSchedule` lanza la
        # misma excepcion que `CreateSchedule` sin area scheme. Con el
        # scheme elegido, el nombre lo pone Revit al crear.
        if with_default_names and not payload["needs_area_scheme"]:
            try:
                payload["default_name"] = DB.ViewSchedule.GetDefaultNameForSchedule(
                    doc, category_id)
            except Exception:
                pass
        categories.append(payload)

    categories.sort(key=lambda item: (item["name"] or "").lower())
    return categories


# --- Detalle (base de Fase 1) ---------------------------------------------

def _read_cells(doc, element, fields):
    """Lee las celdas de un elemento para las columnas visibles.

    Los campos `revit_computed` (formulas, porcentajes, combinados,
    `Count`) salen vacios de aca: Revit los calcula y no los
    replicamos. `Count` se rellena despues, en `_apply_count`, porque
    su valor depende de cuantos elementos colapso el grupo, no del
    elemento individual.
    """
    cells = []
    for payload, field in fields:
        if payload["revit_computed"]:
            cells.append(ser.parameter_payload(None))
            continue
        cells.append(_element_cell(element, payload, field))
    return cells


def _element_cell(element, payload, field):
    """La celda de un campo, leida del elemento.

    [Fase 19] Con el formato del CAMPO, no solo el del proyecto: asi la
    celda dice lo mismo que la vista nativa de Revit. Ver
    `ser.parameter_payload`.
    """
    param = ser.find_parameter(
        element,
        field.ParameterId,
        prefer_type=payload["is_type_field"],
    )
    return ser.parameter_payload(param, _format_options(field))


def _format_options(field):
    """El `FormatOptions` del campo, o `None` si no se puede.  [Fase 19]

    Nunca lanza: sin opciones, `parameter_payload` cae al formato del
    proyecto, que es lo que hacia antes.
    """
    try:
        return field.GetFormatOptions()
    except Exception:
        return None


def _apply_count(cells, fields, count):
    """Devuelve una copia de `cells` con la columna `Count` poblada.

    `ScheduleFieldType.Count` no es un parametro del elemento: es
    cuantos elementos representa la fila. En una fila itemizada vale
    1; en una fila colapsada, el tamano del grupo. Sin esto la columna
    sale vacia siempre, aunque en Revit muestre un numero.
    """
    result = list(cells)
    for index, (payload, _field) in enumerate(fields):
        if payload["field_type"] == "Count":
            result[index] = {
                "value": count,
                "display": str(count),
                "storage": "Integer",
                "read_only": True,
                "present": True,
            }
    return result


# Cuantas celdas de la tabla de Revit se leen como mucho. Es un techo de
# seguridad, no una optimizacion: `TC-Tabiques` son 7.285 x 15 = 109.275
# celdas. Medido 0,0086 ms por celda en regimen, o sea ~1 s, que al lado
# de los ~45 s que cuesta reconstruir esa tabla no se nota. El techo esta
# para que un schedule patologico no sorprenda.
MAX_TABLE_CELLS = 250000


def _aligned_values(lines, keys):
    """Cruce POR ORDEN: a cada fila, la proxima linea de Revit con su clave.

    [Fase 26] Desde la Fase 24 las filas resumidas salen en el mismo orden
    que la tabla de Revit, y eso resuelve lo que la regla de ambiguedad no
    puede: en `TS-Superficie S1 y S2` el mismo texto visible se repite
    bajo niveles distintos (el nivel es un campo de agrupacion OCULTO), y
    cada uno con su area. Las lineas que no son de datos -- encabezados de
    grupo, pies, totales, blancos -- se saltean solas porque no coinciden
    con la fila que se busca.

    `keys` son TODAS las filas, en orden (no una pagina: una pagina
    arrancaria a buscar desde la primera linea). Si una sola fila no
    encuentra su linea, los ordenes no coinciden y el cruce por orden
    seria una mentira: devuelve `None` y se usa la regla de siempre.
    """
    found = []
    position = 0
    for key in keys:
        if key is None:
            found.append(None)
            continue
        while position < len(lines) and lines[position][0] != key:
            position += 1
        if position >= len(lines):
            return None
        found.append(lines[position][1])
        position += 1
    return found


def _fill_computed(view, fields, rows, ordered=False):
    """Rellena las celdas calculadas con lo que Revit ya computo.

    [Fase 26] Con `ordered=True` -- `rows` son TODAS las filas, en el orden
    de Revit -- cruza primero por orden (`_aligned_values`), que desempata
    las claves repetidas, y cae a la regla de abajo si no alinea. Lo usa el
    modo no itemizado, que tiene todas sus filas antes de paginar.

    LA EXCEPCION A LA REGLA 1, Y LA UNICA. La regla 1 prohibe leer la
    tabla de Revit como fuente de datos del board, con una excepcion
    escrita: los campos calculados -- formulas, porcentajes -- que Revit
    computa y nosotros no replicamos, "se leen con `GetCellText()` como
    fuente secundaria". Esto es eso, y nada mas: las columnas normales se
    siguen reconstruyendo elemento por elemento.

    POR QUE HACIA FALTA. Sin esto esas columnas salen VACIAS, y no es un
    caso borde: en el modelo de referencia **189 de los 240 schedules**
    tienen alguna (153 con formula, 43 con Count, 12 con porcentaje). En
    los schedules de Areas la columna que quedaba en blanco era la
    superficie, o sea la unica que importa.

    COMO SE CRUZA UNA FILA NUESTRA CON UNA DE REVIT, que es el problema
    real. La API no ofrece forma de saber a que elemento corresponde una
    fila de un schedule -- esa es justamente la regla 1 --, asi que NO se
    cruza por posicion: nuestras filas vienen en el orden del collector y
    las de Revit en el suyo, con encabezado, subtotales y totales
    intercalados.

    Se cruza por VALOR MOSTRADO: la clave es la tupla de las celdas NO
    calculadas, que son las que nosotros ya reconstruimos y Revit ya
    imprimio. Es la misma aproximacion que usa `_grouping_key` para
    replicar `IsItemized = False`, con el mismo costo asumido en 4.2 del
    planteamiento. Medido: 36/36, 10/10, 299/299, 1.040/1.040 y 15/15
    filas cruzadas en schedules itemizados y no itemizados.

    Y CUANDO NO CRUZA, NO CRUZA, Y ESO ESTA BIEN. Si una fila nuestra no
    corresponde a ninguna de Revit, traerle un valor calculado seria
    importarlo de otro conjunto de elementos: se deja vacia. (Hasta la
    Fase 19 pasaba en `TC-Puertas`, porque el board agrupaba por todas
    las columnas; desde la Fase 20 agrupa como Revit y cruza 46 de 46.)

    Y CUANDO LA CLAVE ES AMBIGUA, NO SE ADIVINA. Dos filas de la tabla
    con la misma clave y distinto valor calculado dejan esa clave
    anulada, y la celda se queda vacia como hasta ahora. Eso cubre el
    caso peligroso: una fila de SUBTOTAL comparte clave con la fila real
    cuando el grupo tiene el campo de agrupacion vacio, y sin esta regla
    se mostraria la suma del grupo como si fuera el valor de la fila.

    OJO CON EL FORMATO DE LOS NUMEROS. Revit imprime los `Double` con
    las opciones de formato del campo (`220.00`) y nosotros con las
    nuestras (`220.0`), asi que una columna numerica dentro de la clave
    hacia que esa fila no cruzara. NO se "arregla" normalizando: aflojar
    la clave llenaria celdas con valores de otro grupo. Se arreglo en la
    Fase 19 aplicando `ScheduleField.GetFormatOptions()` a nuestros
    valores (ver `_format_options`).

    NO PISA LO QUE YA ESTA LLENO: solo escribe donde `present` es False.
    Asi la columna `Count`, que `_apply_count` llena con su valor entero
    y su verdadero significado (cuantos elementos colapso la fila),
    queda intacta.

    Nunca lanza: si algo falla, las celdas se quedan como estaban, que es
    exactamente el comportamiento anterior.
    """
    calc_idx = [i for i, (payload, _f) in enumerate(fields)
                if payload["revit_computed"]]
    if not calc_idx or not rows:
        # El camino de costo cero, y es el de la mayoria: sin columnas
        # calculadas no se le pide NADA a Revit. Importa porque
        # `GetTableData()` obliga a Revit a computar el schedule entero.
        return

    key_idx = [i for i in range(len(fields)) if i not in calc_idx]
    if not key_idx:
        # Todas las columnas calculadas: no hay con que cruzar.
        return

    try:
        table = _table_text(view, len(fields))
    except Exception:
        return
    if not table:
        return

    headings = [(payload["heading"] or payload["name"] or "")
                for payload, _f in fields]

    lines = []
    index = {}
    for line in table:
        if line == headings:
            # La primera fila del cuerpo son los encabezados de columna.
            continue
        key = tuple(line[i] for i in key_idx)
        value = tuple(line[i] for i in calc_idx)
        lines.append((key, value))
        if key in index:
            if index[key] != value:
                index[key] = None       # ambigua: no se adivina
        else:
            index[key] = value

    def row_key(row):
        cells = row.get("cells") or []
        if len(cells) != len(fields):
            return None
        return tuple((cells[i].get("display") or "") for i in key_idx)

    def anchored(row):
        # Una fila sin ningun TEXTO en la clave (todo vacio, o solo numeros)
        # puede tener la misma clave que un pie de grupo, que es justo el
        # caso para el que existe la regla de ambiguedad. Esas no se cruzan
        # por orden: ni toman una linea ni corren el puntero.
        cells = row.get("cells") or []
        return any((cells[i].get("display") or "")
                   and cells[i].get("storage") not in ("Double", "Integer")
                   for i in key_idx)

    aligned = [row_key(row) if ordered and anchored(row) else None
               for row in rows]
    found_by_row = _aligned_values(lines, aligned) if ordered else None

    for position_in_rows, row in enumerate(rows):
        key = row_key(row)
        if key is None:
            continue
        cells = row["cells"]
        if found_by_row is not None and aligned[position_in_rows] is not None:
            found = found_by_row[position_in_rows]
        else:
            found = index.get(key)
        if not found:
            continue
        for position, i in enumerate(calc_idx):
            if cells[i].get("present"):
                continue                # `Count` ya lo lleno `_apply_count`
            text = found[position]
            if not text:
                continue
            cells[i] = {
                "value": text,
                "display": text,
                # Sin `storage`: no es un parametro, es lo que Revit
                # imprimio. `read_only` y el `revit_computed` de la
                # columna son los que bloquean la edicion.
                "storage": None,
                "read_only": True,
                "present": True,
            }


def _table_text(view, column_count):
    """El cuerpo de la tabla de Revit, como lista de listas de texto.

    Devuelve `None` si la tabla no tiene la forma esperada o si pasa el
    techo de celdas. Solo se llama cuando hay una columna calculada.
    """
    body = view.GetTableData().GetSectionData(DB.SectionType.Body)
    rows = int(body.NumberOfRows)
    cols = int(body.NumberOfColumns)
    if cols != column_count:
        # El cuerpo tiene una columna por campo VISIBLE, en orden. Si no
        # coincide, el cruce por posicion de columna seria una mentira:
        # mejor dejar las celdas vacias que llenarlas con la columna de
        # al lado.
        return None
    if rows * cols > MAX_TABLE_CELLS:
        return None
    text = []
    for r in range(rows):
        text.append([view.GetCellText(DB.SectionType.Body, r, c)
                     for c in range(cols)])
    return text


def _grouping_key(cells, fields):
    """Clave de agrupacion de RESPALDO, cuando no hay Sorting/Grouping.

    Se arma con (storage, value, display) de cada celda NO calculada,
    o sea que dos elementos van en la misma fila solo si TODO lo que se
    ve de ellos coincide.

    [Fase 20] Dejo de ser la clave principal. Revit colapsa por los
    campos de *Sorting/Grouping*, no por todas las columnas -- ver
    `sort_group_key_fields`, que es de donde sale la clave cuando el
    schedule tiene alguno. Esta sigue viva para el caso en que no lo
    tiene, y para cuando su clave es incalculable.

    Es una aproximacion por valor mostrado, no la comparacion interna
    exacta de Revit: el costo esta asumido conscientemente en la
    seccion 4.2 del planteamiento del proyecto ("tu tabla no sera
    identica: diferencias de redondeo, unidades, orden de empates").
    """
    key = []
    for (payload, _field), cell in zip(fields, cells):
        if payload["revit_computed"]:
            continue
        key.append(_cell_identity(cell, payload))
    return tuple(key)


# El texto con el que Revit resume una columna cuyo grupo NO es
# homogeneo. No se inventa: es lo que devuelve `GetCellText` en la
# tabla real, verificado en la Fase 17 sobre `TC-Puertas` (columna
# *Material marco* de la fila `ARCH-PU-PLACA-BANO / P1-H220-W75`).
#
# Va en ingles porque toda la aplicacion va en ingles y porque es la
# palabra de Revit -- la convencion es usar la de Revit
# cuando el texto nombra algo que existe ahi. Si alguna vez entra un
# usuario con Revit en espanol, esta constante es uno de los lugares a
# revisar junto con el selector de idioma.
VARIES_TEXT = "<varies>"


# `BuiltInParameter.ELEM_FAMILY_PARAM`, el campo `Family`.
FAMILY_PARAMETER_ID = -1002051


def _is_family_field(payload):
    return payload is not None and payload.get("parameter_id") == FAMILY_PARAMETER_ID


def _cell_identity(cell, payload=None):
    """Con que se decide que dos celdas son la misma.

    El mismo triple que usa `_grouping_key`, y a proposito: si una
    columna cuenta como "igual" para agrupar, tiene que contar como
    "igual" para no marcarse `<varies>`.

    [27-09-2026] SALVO `Family`, que se compara por el NOMBRE. Su valor es
    el id del TIPO, asi que por id el board
    partia una familia en tantas filas como tipos tuviera. Medido en
    `TE-Ex-Requeridos`: Revit dibuja UNA fila `ARCH-SITE-ESTACIONAMIENTO-DOM`
    con 389 y el board daba cuatro (320 + 2 + 49 + 18), una por tipo de la
    misma familia 46916162. Por nombre y no por el id de la familia porque
    las familias de sistema (`Basic Wall`) no tienen otro dato.
    """
    if _is_family_field(payload):
        return (cell.get("storage"), None, cell.get("display"))
    return (cell.get("storage"), cell.get("value"), cell.get("display"))


def _key_positions(fields, key_fields):
    """Para cada campo de la clave, su indice entre las columnas visibles.

    `None` donde el campo no se muestra, que es el caso importante: el
    unico campo de Sorting/Grouping de `TC-Tabiques` esta OCULTO, asi
    que su valor no esta en las celdas ya leidas y hay que ir a
    buscarlo al elemento.

    Sirve para no leer dos veces el mismo parametro: una columna de la
    clave que ademas se muestra ya se leyo en `_read_cells`, y a ~0,4 ms
    por celda releerla en un schedule de 7.285 elementos no es gratis.
    """
    by_id = {}
    for index, (payload, _field) in enumerate(fields):
        if payload["field_id"] is not None:
            by_id[payload["field_id"]] = index
    return [by_id.get(payload["field_id"]) for payload, _field in key_fields]


def _sort_group_key(element, key_fields, key_positions, cells):
    """La clave con la que Revit colapsa: los campos de Sorting/Grouping."""
    key = []
    for (payload, field), position in zip(key_fields, key_positions):
        if position is not None:
            cell = cells[position]
        else:
            cell = _element_cell(element, payload, field)
        key.append(_cell_identity(cell, payload))
    return tuple(key)


def _mark_varies(varies, stored, cells, fields):
    """Marca las columnas en las que este elemento difiere del grupo.

    Se acumula sobre `varies` en vez de guardar las celdas de todos los
    miembros: en `TC-Tabiques` un grupo puede tener cientos de elementos
    y no hace falta recordarlos para saber que una columna ya no es
    homogenea.
    """
    for index, (payload, _field) in enumerate(fields):
        if varies[index] or payload["revit_computed"]:
            continue
        if _cell_identity(stored[index], payload) != _cell_identity(cells[index], payload):
            varies[index] = True


def _apply_varies(cells, varies, fields):
    """Devuelve una copia de `cells` con `<varies>` donde el grupo difiere.

    Es lo que hace Revit, y no es cosmetico: sin esto una fila colapsada
    por Sorting/Grouping mostraria el valor del PRIMER elemento del
    grupo como si fuera el de todos, que es peor que no decir nada.

    La celda queda editable si lo era: escribir sobre un `<varies>` es
    legitimo -- pone el mismo valor en todos los elementos de la fila, y
    Revit hace lo mismo --, pero `value` queda en `None` para que el
    editor no arranque con el texto `<varies>` adentro.

    [Fase 26] El texto es el del campo (*Multiple values*: el del
    proyecto, `<varies>` o uno propio), que Revit ya resuelve en
    `MultipleValuesText`. `VARIES_TEXT` queda de respaldo.
    """
    if not any(varies):
        return cells
    result = list(cells)
    for index, differs in enumerate(varies):
        if not differs:
            continue
        base = result[index]
        text = fields[index][0].get("multiple_values_text")
        result[index] = {
            "value": None,
            "display": VARIES_TEXT if text is None else text,
            "storage": base.get("storage"),
            "read_only": base.get("read_only", False),
            "present": True,
            # Para que el frontend lo pinte distinto de un texto que el
            # usuario escribio, y para que el editor sepa que no hay un
            # valor actual que mostrar.
            "varies": True,
        }
    return result


# --- Calculation en una fila resumida  [Fase 26] -----------------------------
#
# Medido contra la tabla de Revit (ruta temporal TRACE-F26), en filas de
# un schedule no itemizado:
#
#   Totals   la SUMA del grupo, con el formato del campo.
#            `SUPERFICIE SOMBRA TEORICA`: 626.61 en una fila de dos
#            elementos; `TC-Pavimentos`: 703.28 m2 en una de dos.
#   MinMax   "min  ...  max" (dos espacios a cada lado), o el valor solo si
#            el grupo es homogeneo. `Area Schedule (MUNICIPAL) SUP. UTILES`:
#            "0  ...  0.5" en una fila de 299, "1" en otra.
#   Min/Max  no hay ninguno en el modelo: se asume el extremo solo.
#
# Solo para campos que el board reconstruye; un calculado con Totals lo
# trae `_fill_computed` de la tabla de Revit, ya sumado.

CALCULATIONS = ("Totals", "MinMax", "Min", "Max")
MIN_MAX_SEPARATOR = "  ...  "


def _calculation_columns(fields):
    return [index for index, (payload, _f) in enumerate(fields)
            if not payload["revit_computed"]
            and payload.get("display_type") in CALCULATIONS]


def _number_of(cell):
    if not cell.get("present") or cell.get("storage") not in ("Double",
                                                               "Integer"):
        return None
    value = cell.get("value")
    return value if isinstance(value, (int, float)) else None


def _format_number(doc, field, storage, value):
    """Un numero interno con el formato del campo, como lo imprime Revit.

    `None` si no se puede: la celda se queda como estaba antes.
    """
    try:
        options = DB.FormatValueOptions()
        format_options = field.GetFormatOptions()
        if format_options is not None and not format_options.UseDefault:
            options.SetFormatOptions(format_options)
        return DB.UnitFormatUtils.Format(doc.GetUnits(), field.GetSpecTypeId(),
                                         float(value), False, options)
    except Exception:
        if storage == "Integer":
            return str(int(value))
        return None


def _calculated_text(doc, field, storage, mode, numbers):
    def text(value):
        return _format_number(doc, field, storage, value)

    if mode == "Totals":
        return text(sum(numbers))
    if mode == "Min":
        return text(min(numbers))
    if mode == "Max":
        return text(max(numbers))
    low, high = text(min(numbers)), text(max(numbers))
    if low is None or high is None:
        return None
    return low if low == high else low + MIN_MAX_SEPARATOR + high


def _apply_calculation(doc, cells, fields, numbers, count):
    """Copia de `cells` con las columnas de Totals/Min/Max como Revit.

    Con un solo elemento no se toca nada: Revit imprime el valor, y el
    board ya lo tiene formateado. `value` queda en `None`, como en un
    `<varies>`: una suma no es el valor de ningun elemento, y el editor no
    tiene que arrancar con ella adentro.
    """
    if count < 2 or not numbers:
        return cells
    result = list(cells)
    for index, values in numbers.items():
        if not values:
            continue
        payload, field = fields[index]
        base = result[index]
        mode = payload["display_type"]
        shown = _calculated_text(doc, field, base.get("storage"), mode, values)
        if shown is None:
            continue
        result[index] = {
            "value": None,
            "display": shown,
            "storage": base.get("storage"),
            "read_only": base.get("read_only", False),
            "present": True,
            "calculation": mode,
        }
    return result


def _paginate(items, offset, limit):
    """Recorta una lista ya materializada por offset/limit."""
    offset = max(0, offset or 0)
    if limit is None:
        return items[offset:], offset
    return items[offset:offset + max(0, limit)], offset


def _itemized_rows(doc, elements, fields, offset, limit, specs=None):
    """Una fila por elemento.

    Aca offset/limit SI reducen el costo: solo se leen las celdas de
    los elementos de la pagina pedida, no de todos.

    [Fase 24] Salvo los campos de orden: para saber que elementos caen
    en la pagina hay que ordenarlos TODOS antes, asi que esos campos se
    leen de cada elemento. Es el costo nuevo de la fase.
    """
    if specs:
        cache = {}
        blind = [None] * len(specs)
        keys = [_sort_key(doc, element, specs, blind, None, cache)
                for element in elements]
        elements = [elements[i] for i in order_indices(keys, _directions(specs))]
    subset, offset = _paginate(elements, offset, limit)
    rows = []
    for element in subset:
        cells = _read_cells(doc, element, fields)
        rows.append({
            "element_ids": [ser.element_id_value(element.Id)],
            "cells": _apply_count(cells, fields, 1),
        })
    return rows, len(elements), offset


def _collapsed_rows(doc, elements, fields, offset, limit, key_fields=None,
                    specs=None, fill=None):
    """Colapsa filas como Revit cuando `IsItemized` es False.

    [Fase 20] LA CLAVE SALE DE SORTING/GROUPING, no de todas las
    columnas. `key_fields` son los campos de *Sorting/Grouping* --
    incluidos los OCULTOS, ver `sort_group_key_fields` --, y una columna
    cuyo grupo no es homogeneo sale como `<varies>`, igual que Revit.
    Con `key_fields=None` se agrupa por todos los valores mostrados, que
    es lo que hacia el board hasta la Fase 19 y sigue siendo el camino
    de un schedule sin Sorting/Grouping.

    El cambio es mas SIMPLE que lo que reemplaza, no mas complejo: una
    clave de uno o dos campos en vez de una de dieciseis. Lo unico que
    se agrega es marcar las columnas que dejaron de ser homogeneas, que
    es una comparacion por elemento sin memoria extra.

    A DIFERENCIA DEL CASO ITEMIZADO, ACA NO SE PUEDE PAGINAR LA LECTURA:
    para saber si dos elementos van en la misma fila hay que leer las
    celdas de TODOS antes de poder devolver la primera pagina. offset/
    limit acotan cuantas filas YA COLAPSADAS se devuelven, no cuanto se
    lee. No es una limitacion de esta implementacion, es una propiedad
    del problema: colapsar exige conocimiento global. Y esta fase NO lo
    cambia: `TC-Tabiques` sigue costando lo mismo, porque hay que leer
    los 7.285 elementos igual para poder agruparlos. Arregla las filas y
    el payload, no la espera.

    [Fase 24] Y los grupos salen ORDENADOS por `specs` (ver
    `sort_specs`), con la tupla de orden del primer elemento de cada
    grupo: en el modo `sort_fields` todos los miembros comparten esos
    valores, porque son la clave. Cuesta una tupla por grupo, no por
    elemento.
    """
    key_positions = (
        _key_positions(fields, key_fields) if key_fields is not None else None
    )
    sort_positions = (
        _key_positions(fields, [(p, f) for p, f, _asc in specs])
        if specs else None
    )
    cache = {}
    # [Fase 26] Las columnas con Totals/Min/Max acumulan sus numeros por
    # grupo; el resto no guarda nada de los miembros.
    calc_columns = _calculation_columns(fields)

    groups = []
    index_by_key = {}
    for element in elements:
        cells = _read_cells(doc, element, fields)
        if key_fields is None:
            key = _grouping_key(cells, fields)
        else:
            key = _sort_group_key(element, key_fields, key_positions, cells)
        index = index_by_key.get(key)
        if index is None:
            index = len(groups)
            index_by_key[key] = index
            groups.append({
                "element_ids": [],
                "cells": cells,
                "varies": [False] * len(cells),
                "numbers": dict((i, []) for i in calc_columns),
                "sort": (_sort_key(doc, element, specs, sort_positions,
                                   cells, cache) if specs else None),
            })
        else:
            _mark_varies(groups[index]["varies"], groups[index]["cells"],
                         cells, fields)
        for column in calc_columns:
            number = _number_of(cells[column])
            if number is not None:
                groups[index]["numbers"][column].append(number)
        groups[index]["element_ids"].append(ser.element_id_value(element.Id))

    if specs:
        groups = [groups[i] for i in order_indices(
            [group["sort"] for group in groups], _directions(specs))]
    # [Fase 26] Las filas se arman TODAS antes de paginar -- son grupos, un
    # punado -- para que `fill` (las columnas calculadas) cruce por orden
    # contra la tabla de Revit.
    rows = []
    for group in groups:
        count = len(group["element_ids"])
        cells = _apply_count(group["cells"], fields, count)
        cells = _apply_varies(cells, group["varies"], fields)
        rows.append({
            "element_ids": group["element_ids"],
            "cells": _apply_calculation(doc, cells, fields, group["numbers"],
                                        count),
        })
    if fill is not None:
        fill(rows)
    subset, offset = _paginate(rows, offset, limit)
    return subset, len(groups), offset


def schedule_detail(doc, view, offset=0, limit=None):
    """Campos y filas de un schedule, reconstruidos desde los elementos.

    Cada fila lleva `element_ids`: una lista, no un id suelto, porque
    una fila colapsada (`IsItemized = False`) representa varios
    elementos a la vez. En una fila itemizada la lista siempre tiene
    longitud 1. Es lo que hace posible, mas adelante, la seleccion en
    Revit (Fase 3): seleccionar una fila colapsada selecciona TODOS
    sus elementos, igual que hace Revit.

    `offset` y `limit` paginan lo que se DEVUELVE. En un schedule
    itemizado tambien acotan lo que se LEE (mas barato). En uno no
    itemizado no: colapsar exige leer todos los elementos igual, sin
    importar `limit`. Ver `_collapsed_rows`.

    [Fase 24] Las filas salen en el orden de Sorting/Grouping, como en
    Revit. Ver el bloque "Orden de las filas" y `sort_specs`.

    LIMITACIONES DE ESTA VERSION, a resolver en la fase que corresponda:
      - Los campos calculados (formulas y porcentajes) los computa
        Revit y aqui NO se replican, pero desde la Fase 17 sus celdas se
        rellenan leyendo la tabla que Revit ya pinto, cruzando por valor
        mostrado. Ver `_fill_computed`. Siguen siendo de solo lectura.
      - Los material takeoffs generan varias filas por elemento; su
        modelo no encaja aqui y se marcan como solo lectura.
    """
    ser.clear_param_cache()

    definition = view.Definition
    kind = schedule_kind(view)
    fields = visible_fields(definition)
    columns = [payload for payload, _field in fields]

    detail = schedule_summary(doc, view)
    itemized = detail["itemized"]

    all_elements = schedule_elements(doc, view)
    total_elements = len(all_elements)

    specs = sort_specs(definition)

    collapse_mode = None
    collapse_names = []
    if itemized:
        rows, total_rows, used_offset = _itemized_rows(
            doc, all_elements, fields, offset, limit, specs)
        # Las columnas calculadas, con lo que Revit ya computo. Va DESPUES
        # de paginar: solo se rellena lo que se devuelve. Sin columnas
        # calculadas no cuesta nada; ver su docstring.
        _fill_computed(view, fields, rows)
    else:
        # [Fase 20] Colapsar por los campos de Sorting/Grouping, que es
        # lo que hace Revit, y caer a la clave vieja solo si no hay.
        key_fields, collapse_mode = sort_group_key_fields(definition)
        if key_fields:
            collapse_names = [(payload["heading"] or payload["name"] or "")
                              for payload, _field in key_fields]
        else:
            specs = _visible_prefix(specs, fields)
        # [Fase 26] Aca sobre TODAS las filas y antes de paginar, para
        # poder cruzar por orden. Son grupos: pocos.
        rows, total_rows, used_offset = _collapsed_rows(
            doc, all_elements, fields, offset, limit, key_fields, specs,
            fill=lambda all_rows: _fill_computed(view, fields, all_rows,
                                                 ordered=bool(specs)))

    ser.clear_param_cache()

    # `element_count`: cuantos elementos TIENE el schedule en Revit,
    # sin colapsar. `row_count`: cuantas filas LOGICAS hay para pintar
    # (igual a element_count si es itemizado, menor si se colapso).
    # `returned`: cuantas trae esta respuesta puntual.
    detail["element_count"] = total_elements
    detail["row_count"] = total_rows
    detail["offset"] = used_offset
    detail["returned"] = len(rows)
    detail["columns"] = columns
    detail["rows"] = rows
    detail["truncated"] = (used_offset + len(rows)) < total_rows
    # [Fase 24] Por que campos salen ordenadas las filas. Vacia = el orden
    # del collector (sin Sorting/Grouping, o con el primero calculado).
    detail["sort"] = [
        {"field": payload["heading"] or payload["name"] or "",
         "ascending": ascending}
        for payload, _field, ascending in specs
    ]
    detail["notes"] = []
    if not is_editable_kind(kind):
        detail["notes"].append(
            "This is a '{0}' schedule and the board shows it read-only: "
            "its data model does not fit 'one row = one "
            "element'.".format(kind)
        )
    if not itemized:
        # [Fase 20] `collapse` dice COMO se colapso, no solo que se
        # colapso. Importa porque los dos modos dan tablas de tamano muy
        # distinto -- `TC-Tabiques` daba 2.452 filas con el viejo y da
        # ~30 con el nuevo -- y quien mire la tarjeta al lado de la vista
        # nativa tiene que poder saber cual esta viendo.
        detail["collapse"] = {"mode": collapse_mode, "fields": collapse_names}
        if collapse_mode == "sort_fields":
            detail["notes"].append(
                "Itemize every instance is off, so Revit collapses rows. "
                "This view collapses them the same way: by the "
                "Sorting/Grouping fields ({0}), including hidden ones. A "
                "field whose value is not the same across the whole group "
                "shows <varies>, like Revit. Every row carries "
                "element_ids with all the elements behind it, and Count "
                "(when the column exists) shows the real size of the "
                "group.".format(", ".join(collapse_names) or "none")
            )
        else:
            detail["notes"].append(
                "Itemize every instance is off, so Revit collapses rows. "
                "This schedule has no Sorting/Grouping field the board "
                "can rebuild, so rows are collapsed only where every "
                "field matches. That can give more rows than the native "
                "view. Every row carries element_ids with all the "
                "elements behind it, and Count (when the column exists) "
                "shows the real size of the group."
            )
        detail["notes"].append(
            "The grouping is an approximation by displayed value, so it "
            "can differ from Revit where rounding or units are involved."
        )
    return detail


# --- Fase 9: apoyo para la edicion de celda --------------------------------

def find_visible_field(definition, field_id_value):
    """El (payload, field) visible cuyo `field_id` coincide, o (None, None).

    La edicion de celda recibe un `field_id` del cliente y NO se fia de
    lo que el cliente diga de esa columna: `revit_computed` y
    `is_type_field` se resuelven aca, contra la definicion real. De ahi
    depende que la escritura vaya a la instancia o al tipo, que es
    justamente la decision que 6.1 llama el riesgo de producto mas serio
    de la herramienta.
    """
    try:
        fields = visible_fields(definition)
    except Exception:
        return None, None
    for payload, field in fields:
        if payload.get("field_id") is None:
            continue
        try:
            if int(payload["field_id"]) == int(field_id_value):
                return payload, field
        except Exception:
            continue
    return None, None


def parameter_options(doc, element, param):
    """Los valores que un parametro `ElementId` puede tomar.

    La API de Revit NO tiene un "dame las opciones" universal, asi que
    hay dos caminos y en ese orden:

    1. Si el valor actual ES el tipo del elemento (columnas `Type` y
       `Family`), las opciones son `element.GetValidTypes()`. Es la
       respuesta de Revit a "que puede ser este elemento", asi que no
       hay que inventar ninguna regla propia. Medido: 57 tipos de puerta
       en 61 ms, 86 de muro en 54 ms.
    2. Si no, se colectan los elementos de la MISMA CLASE que el valor
       actual (un Material, un Level...). Es una heuristica, pero es la
       unica senal disponible: `Definition.GetDataType()` vuelve VACIO
       para `Type` y `Family`, asi que no sirve para decidir.

    Sin valor actual no hay clase de la que partir y se devuelve vacio:
    la celda queda sin selector y hay que editarla en Revit.
    """
    if element is None or param is None:
        return []

    try:
        current = param.AsElementId()
    except Exception:
        return []

    ids = []
    try:
        type_id = element.GetTypeId()
    except Exception:
        type_id = None

    es_el_tipo = (ser.is_valid_id(current) and ser.is_valid_id(type_id)
                  and ser.element_id_value(current) == ser.element_id_value(type_id))

    if es_el_tipo:
        try:
            ids = list(element.GetValidTypes())
        except Exception:
            ids = []
    elif ser.is_valid_id(current):
        actual = doc.GetElement(current)
        if actual is not None:
            try:
                ids = list(DB.FilteredElementCollector(doc)
                           .OfClass(actual.GetType())
                           .ToElementIds())
            except Exception:
                ids = []

    options = []
    for element_id in ids:
        options.append({
            "id": ser.element_id_value(element_id),
            "name": ser.safe_name(doc.GetElement(element_id)),
        })
    options.sort(key=lambda o: (o["name"] or u"").lower())
    return options


def instances_of_type(doc, type_id, category_id):
    """Los elementos del modelo que usan ese tipo. Lista de ids.

    Sirve dos cosas de la Fase 9 con UN solo recorrido:

    1. `len()` es el "affects: afectara a N instancias" que pide el
       entregable de la fase para las columnas de parametro de tipo.
    2. La lista misma viaja en la respuesta de la escritura, para que el
       board pueda repintar las OTRAS filas que comparten el tipo. Sin
       ella el cliente tendria que adivinar cuales son -- comparar por
       valor mostrado seria una aproximacion, y ya hay bastante de eso
       en `_collapsed_rows`.

    Se acota a la CATEGORIA del schedule a proposito: recorrer los
    382.033 elementos del modelo de referencia cuesta minutos, y un tipo
    solo puede usarlo un elemento de su propia categoria.
    """
    if not ser.is_valid_id(type_id):
        return []
    wanted = ser.element_id_value(type_id)
    found = []
    try:
        collector = DB.FilteredElementCollector(doc).WhereElementIsNotElementType()
        if ser.is_valid_id(category_id):
            collector = collector.OfCategoryId(category_id)
        for element in collector:
            try:
                if ser.element_id_value(element.GetTypeId()) == wanted:
                    found.append(ser.element_id_value(element.Id))
            except Exception:
                continue
        return found
    except Exception:
        return []
