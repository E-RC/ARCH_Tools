# -*- coding: utf-8 -*-
import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
import math
import os
import System

from pyrevit import revit, DB, UI, forms, script
from pyrevit.forms import WPFWindow
from System.Collections.Generic import List

def to_xyz_list(pts):
    lst = List[DB.XYZ]()
    for pt in pts: lst.Add(pt)
    return lst

doc = revit.doc
uidoc = revit.uidoc
app = doc.Application

class WallSelectionFilter(UI.Selection.ISelectionFilter):
    def AllowElement(self, elem):
        return elem.Category and elem.Category.Id.IntegerValue == int(DB.BuiltInCategory.OST_Walls)
    def AllowReference(self, ref, pos):
        return False

class ToposolidSelectionFilter(UI.Selection.ISelectionFilter):
    def AllowElement(self, elem):
        return elem.Category and elem.Category.Id.IntegerValue == int(DB.BuiltInCategory.OST_Toposolid)
    def AllowReference(self, ref, pos):
        return False

class TopocutWindow(WPFWindow):
    def __init__(self, xaml_file_name):
        WPFWindow.__init__(self, xaml_file_name)
        self.selected_walls = []
        self.selected_toposolids = []
        
    def BtnSelectWalls_Click(self, sender, e):
        self.Hide()
        try:
            refs = uidoc.Selection.PickObjects(UI.Selection.ObjectType.Element, WallSelectionFilter(), "Selecciona muros en cadena")
            self.selected_walls = [doc.GetElement(r) for r in refs]
            self.TbSelectedWalls.Text = "{} Muros seleccionados".format(len(self.selected_walls))
            brush_conv = System.Windows.Media.BrushConverter()
            terracotta = brush_conv.ConvertFromString("#B26E63")
            self.TbSelectedWalls.Foreground = terracotta
            self.BtnSelectWalls.BorderBrush = terracotta
        except Exception:
            pass
        self.ShowDialog()
        
    def BtnSelectToposolid_Click(self, sender, e):
        self.Hide()
        try:
            refs = uidoc.Selection.PickObjects(UI.Selection.ObjectType.Element, ToposolidSelectionFilter(), "Selecciona los Toposolids")
            self.selected_toposolids = [doc.GetElement(r) for r in refs]
            self.TbSelectedToposolid.Text = "{} seleccionados".format(len(self.selected_toposolids))
            brush_conv = System.Windows.Media.BrushConverter()
            terracotta = brush_conv.ConvertFromString("#B26E63")
            self.TbSelectedToposolid.Foreground = terracotta
            self.BtnSelectToposolid.BorderBrush = terracotta
        except Exception:
            pass
        self.ShowDialog()
        
    def BtnExecute_Click(self, sender, e):
        if not self.selected_walls:
            forms.alert("Debes seleccionar al menos un muro.")
            return
        if not self.selected_toposolids:
            forms.alert("Debes seleccionar al menos un Toposolid.")
            return
            
        try:
            offset_m = float(self.TbOffset.Text.replace(',', '.'))
        except ValueError:
            forms.alert("El valor de desfase debe ser numérico.")
            return
            
        self.Close()
        execute_cut(self.selected_walls, self.selected_toposolids, offset_m)

    def CloseButton_Click(self, sender, e):
        self.Close()

def get_toposolid_z_at_point(toposolids, pt):
    highest_z = [None]
    
    for toposolid in toposolids:
        opts = DB.Options()
        opts.ComputeReferences = True
        geom_elem = toposolid.get_Geometry(opts)
        if not geom_elem: continue
        
        bb = toposolid.get_BoundingBox(None)
        if bb:
            max_z = bb.Max.Z + 1000.0
            min_z = bb.Min.Z - 1000.0
        else:
            max_z = pt.Z + 5000.0
            min_z = pt.Z - 5000.0
            
        p1 = DB.XYZ(pt.X, pt.Y, max_z)
        p2 = DB.XYZ(pt.X, pt.Y, min_z)
        try:
            ray = DB.Line.CreateBound(p1, p2)
        except Exception:
            continue
    
        def process_geometry(geom_obj):
            if isinstance(geom_obj, DB.Solid) and geom_obj.Faces.Size > 0:
                for face in geom_obj.Faces:
                    hit_found = False
                    
                    try:
                        res = face.Intersect(ray)
                        status = res[0] if isinstance(res, tuple) else res
                        results = res[1] if isinstance(res, tuple) and len(res) > 1 else None
                        if status == DB.SetComparisonResult.Overlap and results:
                            for i in range(results.Size):
                                hit_z = results.get_Item(i).XYZPoint.Z
                                if highest_z[0] is None or hit_z > highest_z[0]:
                                    highest_z[0] = hit_z
                            hit_found = True
                    except Exception:
                        pass
                        
                    if hit_found: continue
                    
                    try:
                        res_array = clr.Reference[DB.IntersectionResultArray]()
                        res = face.Intersect(ray, res_array)
                        if res == DB.SetComparisonResult.Overlap and res_array.Value:
                            for i in range(res_array.Value.Size):
                                hit_z = res_array.Value.get_Item(i).XYZPoint.Z
                                if highest_z[0] is None or hit_z > highest_z[0]:
                                    highest_z[0] = hit_z
                            hit_found = True
                    except Exception:
                        pass
                        
            elif isinstance(geom_obj, DB.GeometryInstance):
                inst_geom = geom_obj.GetInstanceGeometry()
                for obj in inst_geom:
                    process_geometry(obj)

        for obj in geom_elem:
            process_geometry(obj)
            
    return highest_z[0]

def create_void_solid_for_wall(wall, toposolids, offset_m):
    offset_ft = offset_m / 0.3048
    loc_curve = wall.Location.Curve
    if not loc_curve:
        return None
        
    sample_dist = 0.20 / 0.3048
    length = loc_curve.Length
    points = []
    
    wall_type = doc.GetElement(wall.GetTypeId())
    wall_width = wall_type.Width if hasattr(wall_type, 'Width') else 1.0
    half_width = (wall_width / 2.0) + 1.0 # Margin to ensure cut completely overlaps width
    margin = half_width * 3.0 + 2.0 # Extra extension to cover sharp mitered corners
    
    raw_zs = []
    
    # Start Extension
    pt0 = loc_curve.Evaluate(0.0, True)
    deriv0 = loc_curve.ComputeDerivatives(0.0, True).BasisX.Normalize()
    perp0 = DB.XYZ.BasisZ.CrossProduct(deriv0).Normalize()
    pt_ext_start = pt0 - deriv0 * margin
    pt_left_start = pt_ext_start + perp0 * half_width
    pt_right_start = pt_ext_start - perp0 * half_width
    
    raw_zs.append([pt_ext_start, pt_left_start, pt_right_start, None])
    
    d = 0.0
    while True:
        if d > length: d = length
        normalized_param = d / length if length > 0 else 0.0
        pt = loc_curve.Evaluate(normalized_param, True)
        deriv = loc_curve.ComputeDerivatives(normalized_param, True).BasisX.Normalize()
        perp = DB.XYZ.BasisZ.CrossProduct(deriv).Normalize()
        
        pt_left = pt + perp * half_width
        pt_right = pt - perp * half_width
        
        z_center = get_toposolid_z_at_point(toposolids, pt)
        if z_center is None:
            z_left = get_toposolid_z_at_point(toposolids, pt_left)
            z_right = get_toposolid_z_at_point(toposolids, pt_right)
            zs = [z for z in [z_left, z_right] if z is not None]
            if zs: z_center = max(zs)
            
        raw_zs.append([pt, pt_left, pt_right, z_center])
        
        if d == length: break
        if d + sample_dist > length and d < length:
            d = length
        else:
            d += sample_dist
            
    # End Extension
    pt1 = loc_curve.Evaluate(1.0, True)
    deriv1 = loc_curve.ComputeDerivatives(1.0, True).BasisX.Normalize()
    perp1 = DB.XYZ.BasisZ.CrossProduct(deriv1).Normalize()
    pt_ext_end = pt1 + deriv1 * margin
    pt_left_end = pt_ext_end + perp1 * half_width
    pt_right_end = pt_ext_end - perp1 * half_width
    
    raw_zs.append([pt_ext_end, pt_left_end, pt_right_end, None])
            
    # Forward fill
    last_valid = None
    for i in range(len(raw_zs)):
        if raw_zs[i][3] is not None:
            last_valid = raw_zs[i][3]
        elif last_valid is not None:
            raw_zs[i][3] = last_valid
            
    # Backward fill
    last_valid = None
    for i in range(len(raw_zs)-1, -1, -1):
        if raw_zs[i][3] is not None:
            last_valid = raw_zs[i][3]
        elif last_valid is not None:
            raw_zs[i][3] = last_valid
            
    for item in raw_zs:
        pt, pt_left, pt_right, z_center = item
        if z_center is None: z_center = pt.Z
        z_target = z_center + offset_ft
        points.append((DB.XYZ(pt_left.X, pt_left.Y, z_target), DB.XYZ(pt_right.X, pt_right.Y, z_target)))

    if len(points) < 2:
        return None

    bb = wall.get_BoundingBox(None)
    top_z = bb.Max.Z + 10.0 # High enough to fully cover top
    
    # 1. Intentar generar un sólido NURBS fluido (Loft) para evitar faceteado en curvas
    try:
        loops = List[DB.CurveLoop]()
        filtered_zs = []
        
        # Filtrar perfiles para no sobrecargar el motor de loft (máx ~20-30 perfiles), 
        # pero conservando estrictamente los extremos y extensiones.
        for i, item in enumerate(raw_zs):
            if i < 2 or i >= len(raw_zs) - 2 or i % 5 == 0:
                filtered_zs.append(item)
                
        for item in filtered_zs:
            pt, pt_left, pt_right, z_center = item
            if z_center is None: z_center = pt.Z
            z_target = z_center + offset_ft
            
            p1 = DB.XYZ(pt_left.X, pt_left.Y, z_target)
            p2 = DB.XYZ(pt_right.X, pt_right.Y, z_target)
            p3 = DB.XYZ(p2.X, p2.Y, top_z)
            p4 = DB.XYZ(p1.X, p1.Y, top_z)
            
            loop = DB.CurveLoop()
            loop.Append(DB.Line.CreateBound(p1, p2))
            loop.Append(DB.Line.CreateBound(p2, p3))
            loop.Append(DB.Line.CreateBound(p3, p4))
            loop.Append(DB.Line.CreateBound(p4, p1))
            loops.Add(loop)
            
        opts = DB.SolidOptions(DB.ElementId.InvalidElementId, DB.ElementId.InvalidElementId)
        smooth_solid = DB.GeometryCreationUtilities.CreateLoftGeometry(loops, opts)
        if smooth_solid:
            return smooth_solid
    except Exception:
        pass # Si falla el loft por radios muy cerrados, usar el sistema facetado de emergencia
    
    # 2. Sistema de Respaldo: Sólido Facetado
    builder = DB.TessellatedShapeBuilder()
    builder.OpenConnectedFaceSet(True)
    
    for i in range(len(points) - 1):
        l1, r1 = points[i]
        l2, r2 = points[i+1]
        
        tl1 = DB.XYZ(l1.X, l1.Y, top_z)
        tl2 = DB.XYZ(l2.X, l2.Y, top_z)
        tr1 = DB.XYZ(r1.X, r1.Y, top_z)
        tr2 = DB.XYZ(r2.X, r2.Y, top_z)
        
        # Bottom faces
        builder.AddFace(DB.TessellatedFace(to_xyz_list([l1, r1, r2]), DB.ElementId.InvalidElementId))
        builder.AddFace(DB.TessellatedFace(to_xyz_list([l1, r2, l2]), DB.ElementId.InvalidElementId))
        
        # Left side
        builder.AddFace(DB.TessellatedFace(to_xyz_list([l1, l2, tl2]), DB.ElementId.InvalidElementId))
        builder.AddFace(DB.TessellatedFace(to_xyz_list([l1, tl2, tl1]), DB.ElementId.InvalidElementId))
        
        # Right side
        builder.AddFace(DB.TessellatedFace(to_xyz_list([r2, r1, tr1]), DB.ElementId.InvalidElementId))
        builder.AddFace(DB.TessellatedFace(to_xyz_list([r2, tr1, tr2]), DB.ElementId.InvalidElementId))
        
        # Top faces
        builder.AddFace(DB.TessellatedFace(to_xyz_list([tl1, tr1, tr2]), DB.ElementId.InvalidElementId))
        builder.AddFace(DB.TessellatedFace(to_xyz_list([tl1, tr2, tl2]), DB.ElementId.InvalidElementId))
        
    # Front cap
    l1, r1 = points[0]
    tl1 = DB.XYZ(l1.X, l1.Y, top_z)
    tr1 = DB.XYZ(r1.X, r1.Y, top_z)
    builder.AddFace(DB.TessellatedFace(to_xyz_list([l1, r1, tr1]), DB.ElementId.InvalidElementId))
    builder.AddFace(DB.TessellatedFace(to_xyz_list([l1, tr1, tl1]), DB.ElementId.InvalidElementId))
    
    # Back cap
    ln, rn = points[-1]
    tln = DB.XYZ(ln.X, ln.Y, top_z)
    trn = DB.XYZ(rn.X, rn.Y, top_z)
    builder.AddFace(DB.TessellatedFace(to_xyz_list([ln, tln, trn]), DB.ElementId.InvalidElementId))
    builder.AddFace(DB.TessellatedFace(to_xyz_list([ln, trn, rn]), DB.ElementId.InvalidElementId))
    
    builder.CloseConnectedFaceSet()
    builder.Target = DB.TessellatedShapeBuilderTarget.Solid
    builder.Fallback = DB.TessellatedShapeBuilderFallback.Abort
    builder.Build()
    
    result = builder.GetBuildResult()
    if result.Outcome == DB.TessellatedShapeBuilderOutcome.Solid:
        return result.GetGeometricalObjects()[0]
    return None

class FamilyLoadOption(DB.IFamilyLoadOptions):
    def OnFamilyFound(self, familyInUse, overwriteParameterValues):
        return True, True
    def OnSharedFamilyFound(self, sharedFamily, familyInUse, source, overwriteParameterValues):
        return True, True

def execute_cut(walls, toposolids, offset_m):
    with DB.TransactionGroup(doc, "Corte de Muro con Toposolid") as tg:
        tg.Start()
        
        template_path = ""
        template_dirs_to_check = []
        if app.FamilyTemplatePath and os.path.exists(app.FamilyTemplatePath):
            template_dirs_to_check.append(app.FamilyTemplatePath)
        
        fallback_dir = r"C:\ProgramData\Autodesk\RVT " + app.VersionNumber + r"\Family Templates"
        if os.path.exists(fallback_dir):
            template_dirs_to_check.append(fallback_dir)
            
        for d in template_dirs_to_check:
            for root, dirs, files in os.walk(d):
                for f in files:
                    if f.lower() in ["metric generic model.rft", "modelo genérico métrico.rft", "generic model.rft"]:
                        template_path = os.path.join(root, f)
                        break
                if template_path: break
            if template_path: break
                
        if not template_path:
            forms.alert("No se encontró la plantilla de familia 'Modelo genérico métrico'.")
            tg.RollBack()
            return
            
        success_count = 0
        
        for wall in walls:
            solid = create_void_solid_for_wall(wall, toposolids, offset_m)
            if not solid: continue
            
            fam_doc = app.NewFamilyDocument(template_path)
            if not fam_doc: continue
            
            with DB.Transaction(fam_doc, "Create Void") as t_fam:
                t_fam.Start()
                allow_cut_param = fam_doc.OwnerFamily.get_Parameter(DB.BuiltInParameter.FAMILY_ALLOW_CUT_WITH_VOIDS)
                if allow_cut_param: allow_cut_param.Set(1)
                
                freeform = DB.FreeFormElement.Create(fam_doc, solid)
                param = freeform.get_Parameter(DB.BuiltInParameter.ELEMENT_IS_CUTTING)
                if param: param.Set(1)
                
                t_fam.Commit()
                
            family = fam_doc.LoadFamily(doc, FamilyLoadOption())
            fam_doc.Close(False)
            
            with DB.Transaction(doc, "Aplicar Cortes") as t:
                t.Start()
                
                symbol = None
                for symId in family.GetFamilySymbolIds():
                    symbol = doc.GetElement(symId)
                    break
                    
                if not symbol.IsActive:
                    symbol.Activate()
                    doc.Regenerate()
                    
                instance = doc.Create.NewFamilyInstance(DB.XYZ.Zero, symbol, DB.Structure.StructuralType.NonStructural)
                
                try:
                    DB.InstanceVoidCutUtils.AddInstanceVoidCut(doc, wall, instance)
                    success_count += 1
                except Exception as e:
                    pass
                    
                t.Commit()
                
        tg.Assimilate()
        forms.alert("Se cortaron {} muros exitosamente.".format(success_count))

if __name__ == '__main__':
    xaml_path = script.get_bundle_file('ui.xaml')
    window = TopocutWindow(xaml_path)
    window.ShowDialog()
