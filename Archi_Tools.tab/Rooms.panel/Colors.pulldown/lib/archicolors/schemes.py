# -*- coding: utf-8 -*-
"""Lectura de Color Fill Schemes de Rooms y Areas.

Todo lo que ambas herramientas necesitan saber del modelo: que schemes hay,
que elementos los usan, y como leer el valor de un parametro o de una entrada
sin importar su StorageType.
"""

from pyrevit import revit, DB

# Label, BuiltInCategory, sustantivo singular, sustantivo plural.
CATEGORY_OPTIONS = [
    ("Rooms", DB.BuiltInCategory.OST_Rooms, "room", "rooms"),
    ("Areas", DB.BuiltInCategory.OST_Areas, "area", "areas"),
]


def _doc():
    """El documento activo AL MOMENTO DE LA LLAMADA.

    Nunca cachear revit.doc en una global de este modulo: vive en lib/ y el
    motor de IronPython lo mantiene en sys.modules entre ejecuciones, asi que
    una referencia tomada al importar quedaria obsoleta en cuanto el usuario
    cambie de documento.
    """
    return revit.doc


def label_for_bic(bic):
    return next(l for l, b, _s, _p in CATEGORY_OPTIONS if b == bic)


def category_for_label(label):
    """(BuiltInCategory, singular, plural) a partir de "Rooms" o "Areas"."""
    return next((b, s, p) for l, b, s, p in CATEGORY_OPTIONS if l == label)


def get_category_id(bic):
    return DB.Category.GetCategory(_doc(), bic).Id


def get_schemes_for_category(bic, area_scheme_id=None):
    """Todos los Color Fill Schemes definidos para una categoria.

    ColorFillScheme es un elemento normal del documento (no existe un
    'manager' separado que lo administre) por lo que se recolecta
    directamente y se filtra por su CategoryId. Para Areas, un mismo
    proyecto puede tener varios Area Schemes (ej. "MUNICIPAL", "VENTAS")
    cada uno con sus propios Color Fill Schemes independientes -- exactamente
    como los agrupa el dialogo nativo "Edit Color Scheme" de Revit -- por
    eso ademas se filtra por AreaSchemeId cuando se indica.
    """
    doc = _doc()
    cat_id = get_category_id(bic)
    all_schemes = DB.FilteredElementCollector(doc).OfClass(DB.ColorFillScheme).ToElements()
    schemes = [s for s in all_schemes if s.CategoryId == cat_id]
    if area_scheme_id is not None:
        schemes = [s for s in schemes if s.AreaSchemeId == area_scheme_id]
    return schemes


def get_area_schemes():
    return list(DB.FilteredElementCollector(_doc()).OfClass(DB.AreaScheme).ToElements())


def get_elements(bic, area_scheme_id=None, view_only=False):
    """Rooms o Areas de la categoria.

    Con view_only el collector se ata a la vista activa, asi respeta view
    range, crop box, filtros y fase de esa vista; sin el, es todo el
    documento. Para Areas se acota ademas al Area Scheme elegido, ya que un
    Area de "VENTAS" no debe mezclarse con una de "MUNICIPAL".

    Devuelve [] si la vista activa no admite un collector (plantillas de
    vista, hojas): reventar ahi no le sirve a nadie, y quien llama puede
    ofrecer el alcance de proyecto como alternativa.
    """
    doc = _doc()
    if view_only:
        try:
            collector = DB.FilteredElementCollector(doc, doc.ActiveView.Id)
        except Exception:
            return []
    else:
        collector = DB.FilteredElementCollector(doc)

    elements = list(collector.OfCategory(bic).WhereElementIsNotElementType().ToElements())
    if area_scheme_id is not None:
        elements = [el for el in elements
                    if el.AreaScheme is not None and el.AreaScheme.Id == area_scheme_id]
    return elements


def view_scheme_id(bic):
    """Id del Color Scheme aplicado a la vista activa para esa categoria.

    Devuelve None cuando la vista no admite Color Schemes de esa categoria
    (una vista 3D, o una planta comun consultada por Areas), e
    InvalidElementId cuando los admite pero no tiene ninguno puesto.
    """
    try:
        return _doc().ActiveView.GetColorFillSchemeId(get_category_id(bic))
    except Exception:
        return None


def get_param_by_id(element, param_id):
    if param_id.IntegerValue < 0:
        try:
            return element.get_Parameter(DB.BuiltInParameter(param_id.IntegerValue))
        except Exception:
            return None
    for p in element.Parameters:
        if p.Id.IntegerValue == param_id.IntegerValue:
            return p
    return None


def describe_param(param_id):
    if param_id.IntegerValue < 0:
        try:
            return str(DB.BuiltInParameter(param_id.IntegerValue))
        except Exception:
            return "Parametro nativo"
    el = _doc().GetElement(param_id)
    return el.Name if el else "Parametro"


def raw_value_from_storage(storage_type, get_string, get_integer, get_double, get_element_id):
    if storage_type == DB.StorageType.String:
        return get_string()
    if storage_type == DB.StorageType.Integer:
        return get_integer()
    if storage_type == DB.StorageType.Double:
        return get_double()
    if storage_type == DB.StorageType.ElementId:
        eid = get_element_id()
        return eid.IntegerValue if eid else None
    return None


def entry_raw_value(entry):
    return raw_value_from_storage(
        entry.StorageType,
        entry.GetStringValue,
        entry.GetIntegerValue,
        entry.GetDoubleValue,
        entry.GetElementIdValue,
    )


def entry_display_value(entry):
    if entry.StorageType == DB.StorageType.ElementId:
        eid = entry.GetElementIdValue()
        el = _doc().GetElement(eid) if eid and eid.IntegerValue > 0 else None
        return el.Name if el else "(sin valor)"
    value = entry_raw_value(entry)
    return "(vacio)" if value in (None, "") else str(value)


def param_raw_value(param):
    if param is None or not param.HasValue:
        return None
    return raw_value_from_storage(
        param.StorageType,
        param.AsString,
        param.AsInteger,
        param.AsDouble,
        param.AsElementId,
    )


def param_display_value(param):
    if param is None or not param.HasValue:
        return None
    value = param.AsValueString()
    if value:
        return value
    return param.AsString()


def scheme_label(scheme):
    """Etiqueta para los combos: nombre del scheme y parametro que usa."""
    return "{}  -  {}".format(scheme.Name, describe_param(scheme.ParameterDefinition))


# La trama de una entrada decide como se dibuja el relleno en la vista: el
# color sin trama no pinta nada. FillPatternId con InvalidElementId es
# justamente eso, "sin trama".
NO_PATTERN_LABEL = "(sin trama)"


def get_drafting_fill_patterns():
    """Patrones validos para un Color Fill Scheme, ordenados por nombre.

    Solo los de target Drafting: la API rechaza los de modelo para este uso
    (RevitAPI: "Only fill pattern elements whose fill pattern target is
    drafting is valid for color fill scheme").
    """
    patterns = DB.FilteredElementCollector(_doc()).OfClass(DB.FillPatternElement).ToElements()
    drafting = []
    for pattern_element in patterns:
        try:
            if pattern_element.GetFillPattern().Target == DB.FillPatternTarget.Drafting:
                drafting.append(pattern_element)
        except Exception:
            continue
    drafting.sort(key=lambda p: p.Name)
    return drafting


def fill_pattern_name(pattern_id):
    """Nombre legible de un FillPatternId, incluido el caso "sin trama"."""
    if not pattern_id or pattern_id == DB.ElementId.InvalidElementId:
        return NO_PATTERN_LABEL
    element = _doc().GetElement(pattern_id)
    return element.Name if element else NO_PATTERN_LABEL
