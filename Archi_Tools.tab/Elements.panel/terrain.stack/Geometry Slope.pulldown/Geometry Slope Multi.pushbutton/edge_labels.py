# -*- coding: utf-8 -*-
"""Numeros flotando junto a cada arista mientras se piden los porcentajes.

En una vista 3D no se pueden poner TextNote, asi que esto usa el Analysis
Visualization Framework: resultados puntuales, cada uno con su valor escrito
al lado. Es una capa de visualizacion de la vista, no elementos del modelo,
asi que limpiar es quitar los primitivos y devolver el estilo que hubiera.

Todo va envuelto en try: identificar la arista es una ayuda, no un requisito.
Si la vista no admite analisis o la API se comporta distinto, la herramienta
sigue igual, sin numeros y sin molestar.
"""

from pyrevit import revit, DB

from System.Collections.Generic import List

from Autodesk.Revit.DB.Analysis import (AnalysisDisplayColorSettings,
                                        AnalysisDisplayLegendSettings,
                                        AnalysisDisplayMarkersAndTextSettings,
                                        AnalysisDisplayStyle,
                                        AnalysisDisplayStyleMarkerTextLabelType,
                                        AnalysisResultSchema,
                                        FieldDomainPointsByXYZ, FieldValues,
                                        SpatialFieldManager, ValueAtPoint)

STYLE_NAME = "Geometry Slope Multi - aristas"
SCHEMA_NAME = "Aristas Geometry Slope Multi"
MARKER_FT = 0.8           # ~25 cm de marca en el modelo: solo ancla el numero
TEXT_TYPE_NAME = "Geometry Slope Multi"
TEXT_SIZE_FT = 0.0328     # 10 mm de papel: a 1:50 son 50 cm en el modelo
DEFAULT_SCALE = 100.0


class EdgeLabels(object):
    """Numeros de apoyo sobre la vista activa. Usar show() y despues hide()."""

    def __init__(self, uidoc):
        self.uidoc = uidoc
        self.doc = uidoc.Document
        self.view = uidoc.ActiveView
        self.visible = False
        self._manager = None
        self._primitives = []
        self._previous_style = None

    # -- publico -----------------------------------------------------------

    def show(self, points):
        """points: XYZ donde va cada numero, en el orden de seleccion."""
        if not points:
            return False
        try:
            with revit.Transaction("Numerar aristas", doc=self.doc):
                self._build(points)
            self.uidoc.RefreshActiveView()
            self.visible = True
        except Exception:
            self._forget()
        return self.visible

    def hide(self):
        if not self.visible:
            return
        try:
            with revit.Transaction("Quitar numeros", doc=self.doc):
                for primitive in self._primitives:
                    try:
                        self._manager.RemoveSpatialFieldPrimitive(primitive)
                    except Exception:
                        pass
                if self._previous_style is not None:
                    self.view.AnalysisDisplayStyleId = self._previous_style
            self.uidoc.RefreshActiveView()
        except Exception:
            pass
        finally:
            self._forget()

    # -- interno -----------------------------------------------------------

    def _forget(self):
        self.visible = False
        self._manager = None
        self._primitives = []
        self._previous_style = None

    def _build(self, points):
        manager = SpatialFieldManager.GetSpatialFieldManager(self.view)
        if manager is None:
            manager = SpatialFieldManager.CreateSpatialFieldManager(
                self.view, 1)
        self._manager = manager

        result = _result_index(manager)

        for number, point in enumerate(points, 1):
            primitive = manager.AddSpatialFieldPrimitive()
            manager.UpdateSpatialFieldPrimitive(
                primitive, _point_domain(point), _single_value(number), result)
            self._primitives.append(primitive)

        self._previous_style = self.view.AnalysisDisplayStyleId
        style = _find_style(self.doc)
        if style is not None:
            # El estilo guarda la escala con que se creo; si la vista cambio de
            # escala, las marcas saldrian de otro tamano.
            _refresh_style(style, self.doc, self.view)
        else:
            style = _create_style(self.doc, self.view)
        if style is not None:
            self.view.AnalysisDisplayStyleId = style.Id


def _result_index(manager):
    """El indice del schema donde escribir los numeros.

    El nombre tiene que ser unico dentro de la vista, y el schema sobrevive a
    la corrida: registrarlo de nuevo revienta con 'name of result schema is not
    unique in view'. Si ya esta, se reutiliza."""
    try:
        for index in manager.GetRegisteredResults():
            if manager.GetResultSchema(index).Name == SCHEMA_NAME:
                return index
    except Exception:
        pass
    return manager.RegisterResult(
        AnalysisResultSchema(SCHEMA_NAME, "Numero de arista"))


def _point_domain(point):
    points = List[DB.XYZ]()
    points.Add(point)
    return FieldDomainPointsByXYZ(points)


def _single_value(number):
    doubles = List[float]()
    doubles.Add(float(number))
    values = List[ValueAtPoint]()
    values.Add(ValueAtPoint(doubles))
    return FieldValues(values)


def _find_style(doc):
    """El estilo se reutiliza entre corridas: crearlo dos veces con el mismo
    nombre es un error."""
    for style in DB.FilteredElementCollector(doc).OfClass(AnalysisDisplayStyle):
        if _name_of(style) == STYLE_NAME:
            return style
    return None


def _name_of(element):
    """Element.Name.GetValue en vez de element.Name: en las clases donde el
    tipo redefine Name, leerlo como atributo puede fallar bajo IronPython."""
    try:
        return DB.Element.Name.GetValue(element)
    except Exception:
        return getattr(element, "Name", None)


def _flat_colors():
    """Un solo color: el degradado por valor pinta cada numero distinto y aqui
    no significa nada, los valores son 1, 2, 3."""
    colors = AnalysisDisplayColorSettings()
    naranja = DB.Color(255, 140, 0)
    _try_set(colors, "MinColor", naranja)
    _try_set(colors, "MaxColor", naranja)
    return colors


def _marker_size(view):
    """MarkerSize se multiplica por la escala de la vista: a 1:50, un 0.5 se
    convierte en 25 pies de circulo tapando el modelo entero."""
    scale = getattr(view, "Scale", None) or DEFAULT_SCALE
    return MARKER_FT / float(scale)


def _text_type_id(doc):
    """Tipo de texto para el numero.

    El tamano esta en unidades de papel y se multiplica por la escala de la
    vista, asi que el tipo por defecto del proyecto, pensado para planos,
    dibuja un numero ilegible al lado de la marca. Se usa uno propio, con el
    tamano de este modulo, creado una sola vez y reutilizado."""
    mine = _own_text_type(doc)
    if mine is None:
        try:
            base = doc.GetElement(
                doc.GetDefaultElementTypeId(DB.ElementTypeGroup.TextNoteType))
            mine = base.Duplicate(TEXT_TYPE_NAME)
        except Exception:
            mine = None
    if mine is None:
        return _any_text_type_id(doc)

    # El tamano se fija en cada corrida, no solo al crearlo: asi un tipo que
    # quedo de una version anterior, o con otro tamano, se arregla solo.
    try:
        size = mine.get_Parameter(DB.BuiltInParameter.TEXT_SIZE)
        if size is not None and not size.IsReadOnly:
            size.Set(TEXT_SIZE_FT)
    except Exception:
        pass
    return mine.Id


def _own_text_type(doc):
    """El tipo propio, si ya esta en el proyecto.

    El nombre se lee con Element.Name.GetValue: leerlo como atributo falla en
    algunas clases bajo IronPython, y si falla aqui se termina cayendo al tipo
    por defecto del proyecto, que dibuja el numero ilegible."""
    try:
        for text_type in DB.FilteredElementCollector(doc).OfClass(
                DB.TextNoteType):
            if _name_of(text_type) == TEXT_TYPE_NAME:
                return text_type
    except Exception:
        pass
    return None


def _any_text_type_id(doc):
    """Sin tipo propio, cualquiera sirve: el numero sale chico pero sale."""
    try:
        default = doc.GetDefaultElementTypeId(DB.ElementTypeGroup.TextNoteType)
        if default is not None and AnalysisDisplayStyle.IsTextTypeIdValid(
                default, doc):
            return default
    except Exception:
        pass
    try:
        for text_type in DB.FilteredElementCollector(doc).OfClass(
                DB.TextNoteType):
            if AnalysisDisplayStyle.IsTextTypeIdValid(text_type.Id, doc):
                return text_type.Id
    except Exception:
        pass
    return None


def _markers_settings(doc, view, settings=None):
    markers = settings if settings is not None \
        else AnalysisDisplayMarkersAndTextSettings()
    _try_set(markers, "MarkerSize", _marker_size(view))
    _try_set(markers, "Rounding", 1.0)
    _try_set(markers, "TextLabelType",
             AnalysisDisplayStyleMarkerTextLabelType.ShowAll)
    text_type = _text_type_id(doc)
    if text_type is not None:
        _try_set(markers, "TextTypeId", text_type)
    return markers


def _refresh_style(style, doc, view):
    """Un estilo de una corrida anterior trae la configuracion de entonces: el
    tamano de otra escala, o los numeros apagados."""
    try:
        style.SetMarkersAndTextSettings(
            _markers_settings(doc, view, style.GetMarkersAndTextSettings()))
    except Exception:
        pass
    try:
        style.SetColorSettings(_flat_colors())
    except Exception:
        pass


def _create_style(doc, view):
    legend = AnalysisDisplayLegendSettings()
    _try_set(legend, "ShowLegend", False)
    try:
        return AnalysisDisplayStyle.CreateAnalysisDisplayStyle(
            doc, STYLE_NAME, _markers_settings(doc, view), _flat_colors(),
            legend)
    except Exception:
        return None


def _try_set(settings, name, value):
    """Los nombres de estas propiedades cambian entre versiones y ninguna es
    imprescindible: si no esta, se usa el default de Revit."""
    try:
        setattr(settings, name, value)
    except Exception:
        pass
