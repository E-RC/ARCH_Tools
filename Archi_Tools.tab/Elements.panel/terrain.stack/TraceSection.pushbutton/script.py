# -*- coding: utf-8 -*-
"""Trace Section - Archiplan BIM Tool

Calca de forma automatizada la silueta de contorno bidimensional de un Suelo (Floor)
o Toposólido (Toposolid) en el plano exacto donde una vista de Sección o Elevación
los interseca. Las aristas resultantes se dibujan como líneas de detalle nativas y se agrupan.
"""

import sys
import os
import clr
import System

import Autodesk.Revit.DB as DB
import Autodesk.Revit.Exceptions
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from pyrevit import revit, forms, script
from System.Collections.Generic import List

doc = revit.doc
uidoc = revit.uidoc


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def extract_solids(geom_element):
    """Extrae todos los sólidos válidos (con volumen > 0) de un elemento geométrico."""
    solids = []
    if geom_element is None:
        return solids
    for geom_obj in geom_element:
        if isinstance(geom_obj, DB.Solid):
            if geom_obj.Faces.Size > 0 and geom_obj.Volume > 0:
                solids.append(geom_obj)
        elif isinstance(geom_obj, DB.GeometryInstance):
            inst_geom = geom_obj.GetInstanceGeometry()
            solids.extend(extract_solids(inst_geom))
    return solids


def are_curves_equal(c1, c2, tol=0.001):
    """Compara si dos curvas son geométricamente iguales dentro de una tolerancia."""
    if abs(c1.Length - c2.Length) > tol:
        return False
    
    p1_start = c1.GetEndPoint(0)
    p1_end = c1.GetEndPoint(1)
    p2_start = c2.GetEndPoint(0)
    p2_end = c2.GetEndPoint(1)
    
    # Comprobar extremos
    match_same = p1_start.DistanceTo(p2_start) < tol and p1_end.DistanceTo(p2_end) < tol
    match_reverse = p1_start.DistanceTo(p2_end) < tol and p1_end.DistanceTo(p2_start) < tol
    
    if not (match_same or match_reverse):
        return False
    
    # Comprobar punto medio (importante para arcos y splines)
    mid1 = c1.Evaluate(0.5, True)
    mid2 = c2.Evaluate(0.5, True)
    if mid1.DistanceTo(mid2) > tol:
        return False
        
    return True


def is_curve_on_cut_plane(curve, view_origin, view_direction, tolerance=0.01):
    """Verifica si una curva se encuentra exactamente en el plano de corte de la vista."""
    p_start = curve.GetEndPoint(0)
    p_end = curve.GetEndPoint(1)
    p_mid = curve.Evaluate(0.5, True)
    
    d_start = (p_start - view_origin).DotProduct(view_direction)
    d_end = (p_end - view_origin).DotProduct(view_direction)
    d_mid = (p_mid - view_origin).DotProduct(view_direction)
    
    return abs(d_start) < tolerance and abs(d_end) < tolerance and abs(d_mid) < tolerance


def sanitize_group_name(name):
    """Sanea el nombre para evitar caracteres no permitidos en nombres de grupos de Revit."""
    forbidden = ['\\', ':', '{', '}', '[', ']', '|', ';', '<', '>', '?', '`', '~', '"']
    clean = name
    for char in forbidden:
        clean = clean.replace(char, '_')
    return clean.strip()


# =============================================================================
# SELECTION FILTER
# =============================================================================

class TopoFloorSelectionFilter(ISelectionFilter):
    """Filtra la selección permitiendo únicamente Suelos (Floors) y Toposólidos (Toposolids)."""
    def AllowElement(self, elem):
        if elem and elem.Category:
            cat_id = elem.Category.Id.IntegerValue
            return cat_id in [int(DB.BuiltInCategory.OST_Floors), int(DB.BuiltInCategory.OST_Toposolid)]
        return False

    def AllowReference(self, ref, point):
        return False


# =============================================================================
# MAIN PROCESS
# =============================================================================

def main():
    active_view = doc.ActiveView

    # 1. Guardia de Contexto de Vista
    if active_view.ViewType not in [DB.ViewType.Section, DB.ViewType.Elevation]:
        forms.alert(
            "La herramienta requiere estar en una vista activa de Sección o Elevación.",
            title="Vista Inválida",
            exitscript=True
        )

    # 2. Selección del Elemento
    selected_element = None
    try:
        with forms.WarningBar(title="Seleccione un Suelo o Toposólido cortado por la vista"):
            ref_ref = uidoc.Selection.PickObject(
                ObjectType.Element, 
                TopoFloorSelectionFilter(), 
                "Seleccione un Suelo o Toposólido"
            )
            selected_element = doc.GetElement(ref_ref)
    except Autodesk.Revit.Exceptions.OperationCanceledException:
        forms.toast("Selección cancelada por el usuario.", title="Trace Section")
        script.exit()
    except Exception as e:
        forms.alert("Error al seleccionar elemento: {}".format(str(e)), exitscript=True)

    if not selected_element:
        script.exit()

    # 3. Configuración de Opciones de Geometría
    geo_options = DB.Options()
    geo_options.View = active_view
    geo_options.ComputeReferences = True

    # 4. Extracción y Procesamiento Vectorial de Aristas
    geom_elem = selected_element.get_Geometry(geo_options)
    solids = extract_solids(geom_elem)

    if not solids:
        forms.alert(
            "No se pudo extraer geometría sólida del elemento en esta vista.\n"
            "Verifique si el elemento está visible o interseca el plano de corte.",
            title="Sin Geometría"
        )
        script.exit()

    # Extraer aristas convirtiendo a curvas y deduplicando
    raw_curves = []
    for solid in solids:
        if solid.Volume == 0:
            continue
        for face in solid.Faces:
            for loop in face.EdgeLoops:
                for edge in loop:
                    curve = edge.AsCurve()
                    if curve:
                        raw_curves.append(curve)

    # Filtrar curvas duplicadas
    unique_curves = []
    for curve in raw_curves:
        is_duplicate = False
        for uc in unique_curves:
            if are_curves_equal(curve, uc):
                is_duplicate = True
                break
        if not is_duplicate:
            unique_curves.append(curve)

    # 5. Filtrar solo aristas en el plano exacto de corte de la vista
    view_origin = active_view.Origin
    view_direction = active_view.ViewDirection
    
    cut_curves = []
    for curve in unique_curves:
        if is_curve_on_cut_plane(curve, view_origin, view_direction, tolerance=0.01):
            cut_curves.append(curve)

    if not cut_curves:
        forms.alert(
            "No se encontraron aristas que intersequen el plano de corte de la vista.",
            title="Sin Aristas de Corte"
        )
        script.exit()

    # 6. Creación de Curvas de Detalle y Agrupación (Transacción)
    created_detail_curves = []
    
    with revit.Transaction("Trace Section - Crear Líneas"):
        for curve in cut_curves:
            try:
                # Intenta crear la curva de detalle en la vista activa.
                # Las aristas no coplanares levantarán una ArgumentException de Revit
                # y serán descartadas limpiamente en este bloque try/except.
                detail_curve = doc.Create.NewDetailCurve(active_view, curve)
                if detail_curve:
                    created_detail_curves.append(detail_curve)
            except Exception:
                pass

        if not created_detail_curves:
            forms.alert(
                "Ninguna de las aristas del elemento es coplanar con el plano de la vista.",
                title="Líneas no coplanares"
            )
            script.exit()

        # Recolectar ElementIds
        group_elements_ids = List[DB.ElementId]()
        for dc in created_detail_curves:
            group_elements_ids.Add(dc.Id)

        # Crear y nombrar el grupo de detalle
        new_group = doc.Create.NewGroup(group_elements_ids)
        if new_group:
            elem_name = selected_element.Name
            elem_id_str = str(selected_element.Id.IntegerValue)
            clean_name = sanitize_group_name(elem_name)
            base_group_name = "TraceSection_{}_{}".format(clean_name, elem_id_str)
            
            # Asignar nombre único de tipo de grupo para evitar colisiones
            group_name = base_group_name
            counter = 1
            while True:
                try:
                    new_group.GroupType.Name = group_name
                    break
                except Exception:
                    group_name = "{}_{}".format(base_group_name, counter)
                    counter += 1

    # 6. Notificación final
    forms.toast(
        "Se crearon {} líneas de detalle agrupadas con éxito.".format(len(created_detail_curves)),
        title="Trace Section Completado"
    )


if __name__ == '__main__':
    main()
