# -*- coding: utf-8 -*-
"""Modifica la silueta de un pavimento contorneando y eliminando automáticamente el perímetro de muros que lo intersecten, manteniendo el pavimento continuo bajo las puertas."""
import clr
clr.AddReference('RevitAPI')
from Autodesk.Revit.DB import *
import System

from pyrevit import revit, DB, UI, forms

__title__ = "Floor\nSilhouette"
__author__ = "Archiplan"

doc = revit.doc
uidoc = revit.uidoc

def get_solid_from_element(element):
    """Returns the first non-empty Solid from the element's geometry."""
    opt = Options()
    opt.ComputeReferences = False
    opt.DetailLevel = ViewDetailLevel.Fine
    geom_elem = element.get_Geometry(opt)
    if not geom_elem:
        return None
    for geom_obj in geom_elem:
        if isinstance(geom_obj, Solid) and geom_obj.Faces.Size > 0 and geom_obj.Volume > 0:
            return geom_obj
        elif isinstance(geom_obj, GeometryInstance):
            inst_geom = geom_obj.GetInstanceGeometry()
            for inst_obj in inst_geom:
                if isinstance(inst_obj, Solid) and inst_obj.Faces.Size > 0 and inst_obj.Volume > 0:
                    return inst_obj
    return None

def project_curve_to_z0(curve):
    """Projects a curve to the Z=0 plane."""
    if isinstance(curve, Line):
        return Line.CreateBound(XYZ(curve.GetEndPoint(0).X, curve.GetEndPoint(0).Y, 0),
                                XYZ(curve.GetEndPoint(1).X, curve.GetEndPoint(1).Y, 0))
    elif isinstance(curve, Arc):
        return Arc.Create(curve.GetEndPoint(0).Add(XYZ(0,0,-curve.GetEndPoint(0).Z)),
                          curve.GetEndPoint(1).Add(XYZ(0,0,-curve.GetEndPoint(1).Z)),
                          curve.Evaluate(0.5, True).Add(XYZ(0,0,-curve.Evaluate(0.5, True).Z)))
    return curve

def project_curveloop_to_z0(curve_loop):
    """Projects a CurveLoop to the Z=0 plane."""
    new_loop = CurveLoop()
    for curve in curve_loop:
        new_loop.Append(project_curve_to_z0(curve))
    return new_loop

def get_bottom_curveloops_from_solid(solid):
    """Extracts the outermost CurveLoops from horizontal faces facing downwards."""
    loops = []
    for face in solid.Faces:
        if isinstance(face, PlanarFace):
            if face.FaceNormal.IsAlmostEqualTo(XYZ.BasisZ.Negate()):
                for loop in face.GetEdgesAsCurveLoops():
                    loops.append(loop)
    return loops

def create_extrusion_solid(curve_loops, height=1.0):
    """Creates a 3D solid by extruding a list of CurveLoops along the Z axis."""
    try:
        return GeometryCreationUtilities.CreateExtrusionGeometry(curve_loops, XYZ.BasisZ, height)
    except:
        return None

def copy_parameters(source_elem, target_elem):
    """Copies writable parameters from source element to target element."""
    for param in source_elem.Parameters:
        if not param.IsReadOnly and param.Definition.Name != "Level":
            target_param = target_elem.get_Parameter(param.Definition)
            if target_param and not target_param.IsReadOnly:
                try:
                    if param.StorageType == StorageType.Double:
                        target_param.Set(param.AsDouble())
                    elif param.StorageType == StorageType.Integer:
                        target_param.Set(param.AsInteger())
                    elif param.StorageType == StorageType.String:
                        target_param.Set(param.AsString())
                    elif param.StorageType == StorageType.ElementId:
                        target_param.Set(param.AsElementId())
                except:
                    pass

def get_door_width(door):
    """Extracts the width of the door reliably."""
    param_names = ["Width", "Ancho", "Anchura", "Width (Rough)", "Rough Width", "GENERIC_WIDTH"]
    
    # Check instance parameters first
    for name in param_names:
        p = door.LookupParameter(name)
        if p and p.AsDouble() > 0: return p.AsDouble()
        
    p = door.get_Parameter(BuiltInParameter.DOOR_WIDTH)
    if p and p.AsDouble() > 0: return p.AsDouble()
    
    # Check type parameters
    door_type = doc.GetElement(door.GetTypeId())
    for name in param_names:
        p = door_type.LookupParameter(name)
        if p and p.AsDouble() > 0: return p.AsDouble()
        
    p = door_type.get_Parameter(BuiltInParameter.DOOR_WIDTH)
    if p and p.AsDouble() > 0: return p.AsDouble()
    
    return 3.28084 # Default to ~1m

def merge_intervals(intervals):
    """Merges overlapping normalized intervals."""
    if not intervals: return []
    intervals.sort(key=lambda x: x[0])
    merged = [intervals[0]]
    for current in intervals[1:]:
        last = merged[-1]
        if current[0] <= last[1]:
            merged[-1] = (last[0], max(last[1], current[1]))
        else:
            merged.append(current)
    return merged

def clamp(val, min_val, max_val):
    return max(min_val, min(val, max_val))

def main():
    # A. Selección y Filtro Inicial
    selected_ids = uidoc.Selection.GetElementIds()
    if len(selected_ids) != 1:
        forms.alert("Por favor, seleccione exactamente un Suelo (Floor).", exitscript=True)
        return
    
    floor = doc.GetElement(selected_ids[0])
    if not isinstance(floor, Floor):
        forms.alert("El elemento seleccionado no es un Suelo (Floor).", exitscript=True)
        return

    # B. Colectar Elementos Intersectantes
    bb = floor.get_BoundingBox(None)
    outline = Outline(bb.Min, bb.Max)
    bb_filter = BoundingBoxIntersectsFilter(outline)
    intersect_filter = ElementIntersectsElementFilter(floor)
    
    walls = FilteredElementCollector(doc, doc.ActiveView.Id) \
            .OfClass(Wall) \
            .WherePasses(intersect_filter) \
            .ToElements()
            
    # CRITICAL FIX: Removed bb_filter for doors. The floor's bounding box might not intersect 
    # the door if the floor was drawn strictly to the interior face of the wall.
    all_doors = FilteredElementCollector(doc, doc.ActiveView.Id) \
            .OfCategory(BuiltInCategory.OST_Doors) \
            .OfClass(FamilyInstance) \
            .ToElements()

    if not walls:
        forms.alert("No se encontraron muros intersectando el suelo seleccionado en la vista activa.", exitscript=True)
        return

    # C. Procesamiento Geométrico del Suelo Base
    floor_solid = get_solid_from_element(floor)
    if not floor_solid:
        forms.alert("No se pudo obtener la geometría sólida del Suelo.", exitscript=True)
        return
        
    floor_loops = get_bottom_curveloops_from_solid(floor_solid)
    if not floor_loops:
        forms.alert("No se pudo extraer el contorno inferior del Suelo.", exitscript=True)
        return
        
    z0_floor_loops = [project_curveloop_to_z0(loop) for loop in floor_loops]
    base_floor_solid = create_extrusion_solid(System.Collections.Generic.List[CurveLoop](z0_floor_loops), 1.0)
    if not base_floor_solid:
        forms.alert("Error al crear la geometría base del Suelo para operaciones booleanas.", exitscript=True)
        return

    # D. Segmentación de Huellas de Muro y Extracción de Vano
    wall_solids_to_subtract = []
    door_solids_to_add = [] # NEW: Explicitly add door areas to guarantee continuity
    
    if len(walls) > 10:
        pb = forms.ProgressBar(title="Procesando Muros y Puertas...")
    else:
        pb = None

    for i, wall in enumerate(walls):
        if pb: pb.update_progress(i, len(walls))
        
        loc = wall.Location
        if not isinstance(loc, LocationCurve):
            continue
            
        wall_curve = loc.Curve
        # Solo procesamos muros rectos para la sustracción segmentada
        if not isinstance(wall_curve, Line):
            continue
            
        wall_curve_z0 = project_curve_to_z0(wall_curve)
        wall_dir = wall_curve_z0.Direction.Normalize()
        wall_start_pt = wall_curve_z0.GetEndPoint(0)
        wall_len = wall_curve_z0.Length
        
        # 1. Detección de Puertas Hospedadas
        hosted_doors = []
        for d in all_doors:
            # Validar si el Host de la puerta es el muro actual
            if hasattr(d, "Host") and d.Host and d.Host.Id == wall.Id:
                hosted_doors.append(d)
                
        # 2. Extracción de Intervalos del Vano
        door_intervals = []
        for door in hosted_doors:
            loc_pt = door.Location
            if not isinstance(loc_pt, LocationPoint):
                continue
                
            # Usar el centro del BoundingBox es mucho más seguro que LocationPoint 
            # porque algunas puertas tienen su origen (LocationPoint) en un extremo (jamb)
            bb = door.get_BoundingBox(None)
            if bb:
                bb_center = (bb.Min + bb.Max) / 2.0
                door_pt_z0 = XYZ(bb_center.X, bb_center.Y, 0)
            else:
                door_pt_z0 = XYZ(loc_pt.Point.X, loc_pt.Point.Y, 0)
            
            # Validación vectorial de distancia ortogonal
            v_door = door_pt_z0 - wall_start_pt
            dist_to_line = (v_door - v_door.DotProduct(wall_dir) * wall_dir).GetLength()
            if dist_to_line > 10.0:
                continue
                
            width = get_door_width(door)
            half_w = (width / 2.0) + 0.03 # Margen
            
            p_start = door_pt_z0 - wall_dir * half_w
            p_end = door_pt_z0 + wall_dir * half_w
            
            t1 = (p_start - wall_start_pt).DotProduct(wall_dir) / wall_len
            t2 = (p_end - wall_start_pt).DotProduct(wall_dir) / wall_len
            
            t_min = clamp(min(t1, t2), 0.0, 1.0)
            t_max = clamp(max(t1, t2), 0.0, 1.0)
            
            if t_min < t_max:
                door_intervals.append((t_min, t_max))
                    
        # Unir intervalos
        door_intervals = merge_intervals(door_intervals)
        
        # Preparar anchura del muro para los polígonos
        wall_width = wall.Width
        half_ww = wall_width / 2.0
        up = XYZ.BasisZ
        right = wall_dir.CrossProduct(up).Normalize() * half_ww
        
        # 3. Creación de Sólidos de PUERTAS (Para añadir al suelo)
        for (t_start, t_end) in door_intervals:
            p0_door = wall_curve_z0.Evaluate(t_start, True)
            p1_door = wall_curve_z0.Evaluate(t_end, True)
            
            c1 = p0_door + right
            c2 = p1_door + right
            c3 = p1_door - right
            c4 = p0_door - right
            
            loop = CurveLoop()
            loop.Append(Line.CreateBound(c1, c2))
            loop.Append(Line.CreateBound(c2, c3))
            loop.Append(Line.CreateBound(c3, c4))
            loop.Append(Line.CreateBound(c4, c1))
            
            d_solid = create_extrusion_solid(System.Collections.Generic.List[CurveLoop]([loop]), 1.0)
            if d_solid:
                door_solids_to_add.append(d_solid)
        
        # 4. Segmentación de la Huella del Muro (Para restar al suelo)
        wall_segments = []
        current_t = 0.0
        for interval in door_intervals:
            if current_t < interval[0]:
                wall_segments.append((current_t, interval[0]))
            current_t = interval[1]
            
        if current_t < 1.0:
            wall_segments.append((current_t, 1.0))
            
        for (t_start, t_end) in wall_segments:
            if abs(t_end - t_start) < 0.01:
                continue
                
            p0 = wall_curve_z0.Evaluate(t_start, True)
            p1 = wall_curve_z0.Evaluate(t_end, True)
            
            c1 = p0 + right
            c2 = p1 + right
            c3 = p1 - right
            c4 = p0 - right
            
            loop = CurveLoop()
            loop.Append(Line.CreateBound(c1, c2))
            loop.Append(Line.CreateBound(c2, c3))
            loop.Append(Line.CreateBound(c3, c4))
            loop.Append(Line.CreateBound(c4, c1))
            
            temp_solid = create_extrusion_solid(System.Collections.Generic.List[CurveLoop]([loop]), 1.0)
            if temp_solid:
                wall_solids_to_subtract.append(temp_solid)

    # E. Operación Booleana Final con el Suelo
    final_floor_solid = base_floor_solid
    
    # PRIMERO: Añadimos (Union) los vanos de las puertas para asegurar que el suelo exista ahí
    for d_solid in door_solids_to_add:
        try:
            final_floor_solid = BooleanOperationsUtils.ExecuteBooleanOperation(final_floor_solid, d_solid, BooleanOperationsType.Union)
        except:
            pass
            
    # SEGUNDO: Restamos (Difference) los muros segmentados
    for w_solid in wall_solids_to_subtract:
        try:
            final_floor_solid = BooleanOperationsUtils.ExecuteBooleanOperation(final_floor_solid, w_solid, BooleanOperationsType.Difference)
        except:
            pass

    # Extraer bucles finales
    final_loops = get_bottom_curveloops_from_solid(final_floor_solid)
    if not final_loops:
        forms.alert("Error geométrico: No se pudo resolver la silueta final.", exitscript=True)
        return

    list_loops = System.Collections.Generic.List[CurveLoop]()
    for loop in final_loops:
        list_loops.Add(loop)

    # F. Recreación del Elemento (Transacción)
    floor_type_id = floor.GetTypeId()
    level_id = floor.LevelId

    with revit.TransactionGroup("Floor Silhouette"):
        try:
            t = DB.Transaction(doc, "Crear Nuevo Suelo")
            t.Start()
            
            try:
                new_floor = Floor.Create(doc, list_loops, floor_type_id, level_id)
            except Exception as e:
                t.RollBack()
                forms.alert("Fallo al crear el nuevo suelo: {}".format(e), exitscript=True)
                return
                
            copy_parameters(floor, new_floor)
            doc.Delete(floor.Id)
            
            t.Commit()
            forms.toast("Silueta optimizada: Muros contorneados y pasos de puertas respetados.", title="Éxito")
        except Exception as ex:
            forms.alert("Error inesperado durante la transacción: {}".format(ex))

if __name__ == '__main__':
    main()
