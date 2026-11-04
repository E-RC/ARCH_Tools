# -*- coding: utf-8 -*-
import traceback
from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
import Autodesk.Revit.Exceptions

class TopoLevelDialog(forms.WPFWindow):
    def __init__(self, xaml_file_name):
        forms.WPFWindow.__init__(self, xaml_file_name)
        self.prec_str = None

    def OkBtn_Click(self, sender, args):
        self.prec_str = self.PrecisionTxt.Text
        self.Close()

    def CancelBtn_Click(self, sender, args):
        self.prec_str = None
        self.Close()

def main():
    doc = revit.doc
    uidoc = revit.uidoc

    # Paso 1: Validación de Pre-selección
    selection = revit.get_selection()
    selected_elements = selection.elements

    dest_elem = None
    if len(selected_elements) == 1:
        elem = selected_elements[0]
        if elem.Category.Id.IntegerValue in [int(DB.BuiltInCategory.OST_Floors), int(DB.BuiltInCategory.OST_Toposolid)]:
            dest_elem = elem

    if not dest_elem:
        forms.alert("Debe seleccionar el elemento destino a modificar (Suelo o Toposólido) antes de iniciar la herramienta.", exitscript=True)

    # Input de Precisión
    try:
        xaml_path = script.get_bundle_file('MainWindow.xaml')
        dialog = TopoLevelDialog(xaml_path)
        dialog.ShowDialog()
        prec_str = dialog.prec_str
    except Exception as e:
        prec_str = '5'
        forms.alert("No se pudo cargar la ventana de precisión. Usando precisión 5 por defecto.\n" + str(e))

    if prec_str is None:
        script.exit()
    try:
        precision = int(prec_str)
        if precision < 0 or precision > 10:
            raise ValueError()
    except:
        forms.alert('Precisión inválida. Debe ser un número entero del 0 al 10.', exitscript=True)

    prec_map = {
        0: 0, 1: 0, 2: 4.0 / 0.3048, 3: 3.0 / 0.3048, 4: 2.0 / 0.3048,
        5: 1.0 / 0.3048, 6: 0.8 / 0.3048, 7: 0.6 / 0.3048, 8: 0.4 / 0.3048,
        9: 0.2 / 0.3048, 10: 0.1 / 0.3048
    }
    sample_distance = prec_map.get(precision, 0)

    class TopoFloorFilter(ISelectionFilter):
        def AllowElement(self, elem):
            return elem.Category.Id.IntegerValue in [int(DB.BuiltInCategory.OST_Floors), int(DB.BuiltInCategory.OST_Toposolid)]
        def AllowReference(self, ref, point):
            return False

    try:
        with forms.WarningBar(title="Seleccione el Suelo o Toposólido de referencia"):
            ref_ref = uidoc.Selection.PickObject(ObjectType.Element, TopoFloorFilter(), "Seleccione el Suelo o Toposólido de referencia")
            ref_elem = doc.GetElement(ref_ref)
    except Autodesk.Revit.Exceptions.OperationCanceledException:
        script.exit()
    except Exception as e:
        forms.alert("Error en la selección: " + str(e), exitscript=True)

    if ref_elem.Id == dest_elem.Id:
        forms.alert("El elemento de referencia no puede ser el mismo que el destino.", exitscript=True)

    # Geometry Helpers
    import clr
    try:
        from clr import StrongBox
        def intersect_face_curve(face, curve):
            res_arr = StrongBox[DB.IntersectionResultArray](DB.IntersectionResultArray())
            res = face.Intersect(curve, res_arr)
            return res, res_arr.Value
    except ImportError:
        def intersect_face_curve(face, curve):
            res_arr = clr.Reference[DB.IntersectionResultArray]()
            res = face.Intersect(curve, res_arr)
            return res, res_arr

    def flatten_curve(curve):
        try:
            if isinstance(curve, DB.Line):
                p0 = DB.XYZ(curve.GetEndPoint(0).X, curve.GetEndPoint(0).Y, 0)
                p1 = DB.XYZ(curve.GetEndPoint(1).X, curve.GetEndPoint(1).Y, 0)
                if p0.DistanceTo(p1) > 0.001:
                    return DB.Line.CreateBound(p0, p1)
            elif isinstance(curve, DB.Arc):
                p0 = DB.XYZ(curve.GetEndPoint(0).X, curve.GetEndPoint(0).Y, 0)
                p1 = DB.XYZ(curve.GetEndPoint(1).X, curve.GetEndPoint(1).Y, 0)
                p2 = DB.XYZ(curve.Evaluate(0.5, True).X, curve.Evaluate(0.5, True).Y, 0)
                if p0.DistanceTo(p1) > 0.001:
                    return DB.Arc.Create(p0, p1, p2)
        except:
            pass
        return None

    def is_on_perimeter(pt, flat_perimeter_curves, tol=0.01):
        test_pt = DB.XYZ(pt.X, pt.Y, 0)
        for c in flat_perimeter_curves:
            res = c.Project(test_pt)
            if res and res.Distance < tol:
                return True
        return False

    def get_elevation_at_point(ref_solid, xy_pt):
        if not ref_solid: return None
        pt_high = DB.XYZ(xy_pt.X, xy_pt.Y, xy_pt.Z + 10000)
        pt_low = DB.XYZ(xy_pt.X, xy_pt.Y, xy_pt.Z - 10000)
        ray_curve = DB.Line.CreateBound(pt_high, pt_low)
        
        max_z = -1e9
        found = False
        
        for face in ref_solid.Faces:
            res, res_arr = intersect_face_curve(face, ray_curve)
            if res == DB.SetComparisonResult.Overlap and res_arr:
                for i in range(res_arr.Size):
                    pt = res_arr.get_Item(i).XYZPoint
                    if pt.Z > max_z:
                        max_z = pt.Z
                        found = True
        return max_z if found else None

    # Extracción de Sólido de la Referencia
    geom_options = DB.Options()
    geom_options.ComputeReferences = True
    geom_elem = ref_elem.get_Geometry(geom_options)
    ref_solid = None
    if geom_elem:
        for geom_obj in geom_elem:
            if isinstance(geom_obj, DB.Solid) and geom_obj.Faces.Size > 0:
                ref_solid = geom_obj
                break
            elif isinstance(geom_obj, DB.GeometryInstance):
                inst_geom = geom_obj.GetInstanceGeometry()
                for inst_obj in inst_geom:
                    if isinstance(inst_obj, DB.Solid) and inst_obj.Faces.Size > 0:
                        ref_solid = inst_obj
                        break

    if not ref_solid:
        forms.alert("No se pudo extraer la geometría sólida del elemento de referencia.", exitscript=True)

    with revit.Transaction("Topo Level: Perimeter Match"):
        dest_editor = dest_elem.GetSlabShapeEditor()
        if not dest_editor:
            forms.alert("El elemento destino no soporta SlabShapeEditor.", exitscript=True)
        dest_editor.Enable()
            
        dest_level = doc.GetElement(dest_elem.LevelId)
        level_elev = dest_level.Elevation if dest_level else 0.0
        
        offset_param = dest_elem.get_Parameter(DB.BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
        base_offset = offset_param.AsDouble() if offset_param else 0.0
        
        try:
            sketch = doc.GetElement(dest_elem.SketchId)
        except AttributeError:
            forms.alert("Revit no soporta SketchId directamente. Contacta soporte.", exitscript=True)

        flat_perimeter_curves = []
        if sketch:
            for profile in sketch.Profile:
                for curve in profile:
                    fc = flatten_curve(curve)
                    if fc: flat_perimeter_curves.append(fc)
        else:
            forms.alert("No se pudo obtener el boceto (Sketch) del destino.", exitscript=True)
        
        points_to_process = []
        
        # 2. Vértices existentes
        for vertex in dest_editor.SlabShapeVertices:
            try:
                pos = vertex.Position
            except Exception:
                continue
            if precision == 0 or is_on_perimeter(pos, flat_perimeter_curves):
                points_to_process.append({'vertex': vertex, 'pt': pos, 'is_new': False})

        # 3. Sampling
        if sample_distance > 0:
            for c in flat_perimeter_curves:
                length = c.Length
                if length > sample_distance:
                    num_segments = int(length / sample_distance)
                    for i in range(1, num_segments):
                        param = float(i) / num_segments
                        pt_3d = c.Evaluate(param, True)
                        pt_2d = DB.XYZ(pt_3d.X, pt_3d.Y, 0)
                        
                        exists = False
                        for existing in points_to_process:
                            if DB.XYZ(existing['pt'].X, existing['pt'].Y, 0).DistanceTo(pt_2d) < 0.05:
                                exists = True
                                break
                        if not exists:
                            points_to_process.append({'vertex': None, 'pt': pt_2d, 'is_new': True})

        stats = {
            'raycast_miss': 0,
            'points_added': 0,
            'draw_failed': 0,
            'points_modified': 0,
            'points_ok': 0
        }

        # 4. Procesamiento
        def process_point(item):
            pt_2d = item['pt']
            z = get_elevation_at_point(ref_solid, pt_2d)
            if z is None:
                stats['raycast_miss'] += 1
            else:
                offset_z = z - (level_elev + base_offset)
                if item['is_new']:
                    # DrawPoint creates a point at the Z elevation we provide
                    try:
                        new_v = dest_editor.DrawPoint(DB.XYZ(pt_2d.X, pt_2d.Y, z))
                    except Exception:
                        new_v = None
                        
                    if new_v:
                        stats['points_added'] += 1
                        # Si DrawPoint lo coloco en el Z correcto, la diferencia es 0
                        try:
                            new_v_z = new_v.Position.Z
                        except Exception:
                            new_v_z = z + 100 # forzar modificación si no se puede leer
                            
                        if abs(new_v_z - z) > 0.00328:
                            try:
                                dest_editor.ModifySubElement(new_v, offset_z)
                                stats['points_modified'] += 1
                            except Exception:
                                stats['draw_failed'] += 1
                        else:
                            # Ya esta bien
                            stats['points_ok'] += 1
                    else:
                        stats['draw_failed'] += 1
                else:
                    vertex = item['vertex']
                    try:
                        v_z = vertex.Position.Z
                    except Exception:
                        v_z = z + 100 # forzar modificación
                        
                    if abs(v_z - z) > 0.00328:
                        try:
                            dest_editor.ModifySubElement(vertex, offset_z)
                            stats['points_modified'] += 1
                        except Exception:
                            pass
                    else:
                        stats['points_ok'] += 1

        if precision >= 8:
            try:
                with forms.ProgressBar(title='Procesando muestreo perimetral...', step=len(points_to_process)) as pb:
                    for i, item in enumerate(points_to_process):
                        process_point(item)
                        pb.update_progress(i + 1, len(points_to_process))
            except:
                for item in points_to_process:
                    process_point(item)
        else:
            for item in points_to_process:
                process_point(item)

    msg = "Proceso finalizado.\n\n"
    msg += "Puntos evaluados: {}\n".format(len(points_to_process))
    msg += "Raycast fallidos (Puntos fuera del área de la referencia): {}\n".format(stats['raycast_miss'])
    msg += "Nuevos puntos añadidos: {}\n".format(stats['points_added'])
    msg += "Puntos ignorados al añadir (Error Revit): {}\n".format(stats['draw_failed'])
    msg += "Puntos existentes modificados: {}\n".format(stats['points_modified'])
    msg += "Puntos con elevación ya correcta: {}".format(stats['points_ok'])

    forms.alert(msg, title="Topo Level - Reporte")

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        forms.alert('Error crítico de ejecución en Topo Level:\n{}\n\nDetalles técnicos:\n{}'.format(str(e), traceback.format_exc()))
