# -*- coding: utf-8 -*-
"""Alinea visualmente los Room Tags y Area Tags con la rotación de un muro o una línea de contorno de área."""

import math
from pyrevit import revit, DB, UI, forms
import sys

class ReferenceSelectionFilter(UI.Selection.ISelectionFilter):
    def __init__(self, walls=True, boundaries=True):
        self.walls = walls
        self.boundaries = boundaries

    def AllowElement(self, element):
        if element and element.Category:
            cat_id = element.Category.Id.IntegerValue
            if self.walls and cat_id == int(DB.BuiltInCategory.OST_Walls):
                return True
            if self.boundaries and cat_id == int(DB.BuiltInCategory.OST_AreaSchemeLines):
                return True
        return False

    def AllowReference(self, reference, position):
        return False

def main():
    # Obtener selección actual
    selection = revit.get_selection()

    # Filtrar Room Tags y Area Tags de la selección
    supported_types = (DB.Architecture.RoomTag, DB.AreaTag)
    selected_tags = [
        el for el in selection.elements
        if isinstance(el, supported_types)
    ]

    if not selected_tags:
        forms.alert(
            "No se encontraron Room Tags o Area Tags válidos en la selección. "
            "Asegúrate de seleccionarlos antes de ejecutar la herramienta.",
            exitscript=True
        )

    # Determinar qué tipos de referencias permitir según la selección
    has_rooms = any(isinstance(t, DB.Architecture.RoomTag) for t in selected_tags)
    has_areas = any(isinstance(t, DB.AreaTag) for t in selected_tags)

    allow_walls = has_rooms or not has_areas
    allow_boundaries = has_areas or not has_rooms

    if allow_walls and allow_boundaries:
        prompt = "Selecciona el muro o la línea de contorno de área (Area Boundary) de referencia para el ángulo"
    elif allow_boundaries:
        prompt = "Selecciona la línea de contorno de área (Area Boundary) de referencia para el ángulo"
    else:
        prompt = "Selecciona el muro de referencia para el ángulo"

    # Solicitar al usuario que seleccione el elemento de referencia
    ref_filter = ReferenceSelectionFilter(walls=allow_walls, boundaries=allow_boundaries)
    try:
        ref_element_ref = revit.uidoc.Selection.PickObject(
            UI.Selection.ObjectType.Element,
            ref_filter,
            prompt
        )
        ref_element = revit.doc.GetElement(ref_element_ref)
    except Exception:
        # El usuario canceló la selección con ESC
        sys.exit()

    # Obtener la curva del elemento de referencia para calcular el ángulo
    curve = None
    if hasattr(ref_element, "Location") and ref_element.Location and hasattr(ref_element.Location, "Curve") and ref_element.Location.Curve:
        curve = ref_element.Location.Curve
    elif hasattr(ref_element, "GeometryCurve") and ref_element.GeometryCurve:
        curve = ref_element.GeometryCurve

    if not curve:
        forms.alert("El elemento seleccionado no tiene una curva de ubicación o geometría válida.", exitscript=True)

    start_pt = curve.GetEndPoint(0)
    end_pt = curve.GetEndPoint(1)
    direction = (end_pt - start_pt).Normalize()

    # Calcular ángulo respecto al eje X
    target_angle = math.atan2(direction.Y, direction.X)

    # Normalizar el ángulo para que el texto no quede de cabeza
    if target_angle > math.pi / 2:
        target_angle -= math.pi
    elif target_angle <= -math.pi / 2:
        target_angle += math.pi

    # Ejecutar Transaction para aplicar la rotación
    with revit.Transaction("Rotate A/R Tags"):
        for tag in selected_tags:
            # Cambiar orientación a Model para permitir rotación libre
            try:
                tag.TagOrientation = DB.SpatialElementTagOrientation.Model
            except Exception:
                pass
            
            loc = tag.Location
            if isinstance(loc, DB.LocationPoint):
                point = loc.Point
                current_angle = loc.Rotation
                rotation_diff = target_angle - current_angle
                
                # Crear eje Z para la rotación
                axis = DB.Line.CreateBound(point, point.Add(DB.XYZ(0, 0, 1)))
                
                # Rotar el tag sobre su Location Point
                if rotation_diff != 0:
                    DB.ElementTransformUtils.RotateElement(revit.doc, tag.Id, axis, rotation_diff)

if __name__ == '__main__':
    main()
