# -*- coding: utf-8 -*-
import os
import sys
import clr
import math

from pyrevit import revit, DB, UI, forms
from pyrevit import script

clr.AddReference('RevitAPIUI')
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

import System
clr.AddReference('PresentationCore')
clr.AddReference('PresentationFramework')
clr.AddReference('WindowsBase')

doc = revit.doc
uidoc = revit.uidoc

__window__ = 'ui.xaml'

class LineSelectionFilter(ISelectionFilter):
    def AllowElement(self, elem):
        return isinstance(elem, (DB.ModelLine, DB.DetailLine))
    def AllowReference(self, ref, point):
        return False

class HostSelectionFilter(ISelectionFilter):
    def AllowElement(self, elem):
        cat_id = elem.Category.Id.IntegerValue
        return cat_id in [int(DB.BuiltInCategory.OST_Toposolid), 
                          int(DB.BuiltInCategory.OST_Floors),
                          int(DB.BuiltInCategory.OST_Topography)]
    def AllowReference(self, ref, point):
        return False

def sort_curves(curves):
    if not curves: return []
    # simple chaining algorithm
    sorted_curves = [curves.pop(0)]
    
    while curves:
        last_curve = sorted_curves[-1]
        last_pt = last_curve.GetEndPoint(1)
        
        found = False
        for i, c in enumerate(curves):
            if c.GetEndPoint(0).IsAlmostEqualTo(last_pt):
                sorted_curves.append(curves.pop(i))
                found = True
                break
            elif c.GetEndPoint(1).IsAlmostEqualTo(last_pt):
                rev_c = c.CreateReversed()
                sorted_curves.append(rev_c)
                curves.pop(i)
                found = True
                break
                
        if not found:
            # Try prepending
            first_curve = sorted_curves[0]
            first_pt = first_curve.GetEndPoint(0)
            
            for i, c in enumerate(curves):
                if c.GetEndPoint(1).IsAlmostEqualTo(first_pt):
                    sorted_curves.insert(0, curves.pop(i))
                    found = True
                    break
                elif c.GetEndPoint(0).IsAlmostEqualTo(first_pt):
                    rev_c = c.CreateReversed()
                    sorted_curves.insert(0, rev_c)
                    curves.pop(i)
                    found = True
                    break
                    
        if not found:
            break
            
    return sorted_curves

def get_points_along_curves(curves, distance, justification="Inicio"):
    points = []
    
    for i, c in enumerate(curves):
        L = c.Length
        num_segments = int(L / distance)
        remainder = L - (num_segments * distance)
        
        curve_points = []
        
        if num_segments == 0:
            curve_points.append(c.Evaluate(0.0, True))
            curve_points.append(c.Evaluate(1.0, True))
        else:
            if justification == "Inicio":
                for j in range(num_segments + 1):
                    param = (j * distance) / L
                    curve_points.append(c.Evaluate(param, True))
                if remainder > 0.001:
                    curve_points.append(c.Evaluate(1.0, True))
                    
            elif justification == "Centro":
                start_offset = remainder / 2.0
                curve_points.append(c.Evaluate(0.0, True))
                if start_offset > 0.001:
                    param = start_offset / L
                    curve_points.append(c.Evaluate(param, True))
                for j in range(1, num_segments + 1):
                    dist = start_offset + j * distance
                    param = dist / L
                    curve_points.append(c.Evaluate(param, True))
                if L - (start_offset + num_segments * distance) > 0.001:
                    curve_points.append(c.Evaluate(1.0, True))
                    
            elif justification == "Final":
                curve_points.append(c.Evaluate(0.0, True))
                if remainder > 0.001:
                    param = remainder / L
                    curve_points.append(c.Evaluate(param, True))
                for j in range(1, num_segments + 1):
                    dist = remainder + j * distance
                    param = dist / L
                    curve_points.append(c.Evaluate(param, True))
        
        for pt in curve_points:
            if not points or not pt.IsAlmostEqualTo(points[-1]):
                points.append(pt)
                
    return points

def get_elevation_at_point(point, host_elem, view3d):
    start_pt = DB.XYZ(point.X, point.Y, point.Z + 1000.0)
    direction = DB.XYZ(0, 0, -1)
    
    from System.Collections.Generic import List
    id_list = List[DB.ElementId]()
    id_list.Add(host_elem.Id)
    
    intersector = DB.ReferenceIntersector(id_list, DB.FindReferenceTarget.Face, view3d)
    
    context = intersector.FindNearest(start_pt, direction)
    if context:
        ref_pt = context.GetReference().GlobalPoint
        return ref_pt.Z
    
    return point.Z

def get_angle_at_point(i, points):
    if len(points) < 2:
        return 0.0
    if i == 0:
        p1 = points[0]
        p2 = points[1]
        dx = p2.X - p1.X
        dy = p2.Y - p1.Y
        return math.atan2(dy, dx)
    elif i == len(points) - 1:
        p1 = points[-2]
        p2 = points[-1]
        dx = p2.X - p1.X
        dy = p2.Y - p1.Y
        return math.atan2(dy, dx)
    else:
        p_prev = points[i-1]
        p_curr = points[i]
        p_next = points[i+1]
        
        v_in_x = p_curr.X - p_prev.X
        v_in_y = p_curr.Y - p_prev.Y
        len_in = math.sqrt(v_in_x**2 + v_in_y**2)
        if len_in > 0:
            v_in_x /= len_in
            v_in_y /= len_in
            
        v_out_x = p_next.X - p_curr.X
        v_out_y = p_next.Y - p_curr.Y
        len_out = math.sqrt(v_out_x**2 + v_out_y**2)
        if len_out > 0:
            v_out_x /= len_out
            v_out_y /= len_out
            
        bisect_x = v_in_x + v_out_x
        bisect_y = v_in_y + v_out_y
        len_bisect = math.sqrt(bisect_x**2 + bisect_y**2)
        
        if len_bisect < 0.0001:
            return math.atan2(v_in_y, v_in_x) + math.pi/2
        
        return math.atan2(bisect_y, bisect_x)

class PartyWallWindow(forms.WPFWindow):
    def __init__(self, xaml_file_name):
        forms.WPFWindow.__init__(self, xaml_file_name)
        self.path_curves = []
        self.host_elem = None
        
        columns = DB.FilteredElementCollector(doc)\
                    .OfCategory(DB.BuiltInCategory.OST_Columns)\
                    .WhereElementIsElementType().ToElements()
        struct_columns = DB.FilteredElementCollector(doc)\
                           .OfCategory(DB.BuiltInCategory.OST_StructuralColumns)\
                           .WhereElementIsElementType().ToElements()
                           
        all_columns = list(columns) + list(struct_columns)
        all_columns = sorted(all_columns, key=lambda x: DB.Element.Name.__get__(x))
        self.CmbColumns.ItemsSource = all_columns
        if all_columns:
            self.CmbColumns.SelectedIndex = 0
            
        walls = DB.FilteredElementCollector(doc)\
                  .OfClass(DB.WallType)\
                  .ToElements()
        walls = sorted(walls, key=lambda x: DB.Element.Name.__get__(x))
        self.CmbWalls.ItemsSource = walls
        if walls:
            self.CmbWalls.SelectedIndex = 0

    def BtnSelectPath_Click(self, sender, e):
        self.Hide()
        try:
            sel = uidoc.Selection.PickObjects(ObjectType.Element, LineSelectionFilter(), "Selecciona las líneas del recorrido")
            if sel:
                curves = []
                for ref in sel:
                    elem = doc.GetElement(ref)
                    if isinstance(elem, (DB.ModelLine, DB.DetailLine)):
                        curves.append(elem.GeometryCurve)
                
                sorted_curves = sort_curves(curves)
                self.path_curves = sorted_curves
                self.LblPathStatus.Text = "{} líneas seleccionadas".format(len(sorted_curves))
                self.LblPathStatus.Foreground = System.Windows.Media.Brushes.Green
        except Exception as ex:
            pass
        finally:
            self.CheckEnableGenerate()
            self.ShowDialog()

    def BtnSelectHost_Click(self, sender, e):
        self.Hide()
        try:
            ref = uidoc.Selection.PickObject(ObjectType.Element, HostSelectionFilter(), "Selecciona el terreno o suelo (Host)")
            if ref:
                self.host_elem = doc.GetElement(ref)
                self.LblHostStatus.Text = self.host_elem.Name
                self.LblHostStatus.Foreground = System.Windows.Media.Brushes.Green
        except Exception as ex:
            pass
        finally:
            self.CheckEnableGenerate()
            self.ShowDialog()

    def CheckEnableGenerate(self):
        if self.path_curves and self.host_elem:
            self.BtnGenerate.IsEnabled = True
        else:
            self.BtnGenerate.IsEnabled = False

    def BtnGenerate_Click(self, sender, e):
        try:
            dist_val = float(self.TxtDistance.Text)
            dist_feet = dist_val / 0.3048
            height_val = float(self.TxtWallHeight.Text)
            height_feet = height_val / 0.3048
        except ValueError:
            self.LblError.Text = "La distancia y la altura deben ser números válidos."
            return
            
        selected_item = self.CmbJustification.SelectedItem
        just_str = selected_item.Content if selected_item else "Inicio"
        
        selected_col_type = self.CmbColumns.SelectedItem
        selected_wall_type = self.CmbWalls.SelectedItem
        
        if not selected_col_type or not selected_wall_type:
            self.LblError.Text = "Selecciona tipos de columna y muro."
            return
            
        self.Hide()
        
        view3d = None
        collector = DB.FilteredElementCollector(doc).OfClass(DB.View3D)
        for v in collector:
            if not v.IsTemplate and v.Name == "{3D}":
                view3d = v
                break
        if not view3d:
            for v in collector:
                if not v.IsTemplate:
                    view3d = v
                    break
                    
        if not view3d:
            forms.alert("No se encontró una vista 3D válida para calcular la elevación. Por favor, crea una vista {3D}.")
            self.Close()
            return

        points = get_points_along_curves(self.path_curves, dist_feet, just_str)
        
        if len(points) < 2:
            forms.alert("La longitud del recorrido es muy corta para generar al menos dos postes.")
            self.Close()
            return
            
        levels = DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements()
        if not levels:
            forms.alert("No hay niveles en el proyecto.")
            self.Close()
            return
        base_level = levels[0]
        
        with revit.Transaction("Activate Column Type"):
            if not selected_col_type.IsActive:
                selected_col_type.Activate()
        
        with revit.TransactionGroup("Party Wall Generation"):
            with revit.Transaction("Create Party Wall Components"):
                
                elevations = []
                for pt in points:
                    z = get_elevation_at_point(pt, self.host_elem, view3d)
                    elevations.append(z)
                    
                # Calculate Wall bases and tops
                wall_bases = []
                wall_tops = []
                
                for i in range(len(points) - 1):
                    pt_start = points[i]
                    pt_end = points[i+1]
                    z_start = elevations[i]
                    z_end = elevations[i+1]
                    
                    mid_pt = DB.XYZ((pt_start.X + pt_end.X)/2, (pt_start.Y + pt_end.Y)/2, (pt_start.Z + pt_end.Z)/2)
                    z_mid = get_elevation_at_point(mid_pt, self.host_elem, view3d)
                    
                    z_max = max(z_start, z_end, z_mid)
                    wall_bases.append(z_max)
                    wall_tops.append(z_max + height_feet)
                
                # Create Walls
                for i in range(len(points) - 1):
                    pt_start = points[i]
                    pt_end = points[i+1]
                    wall_line = DB.Line.CreateBound(DB.XYZ(pt_start.X, pt_start.Y, 0), DB.XYZ(pt_end.X, pt_end.Y, 0))
                    
                    try:
                        wall = DB.Wall.Create(doc, wall_line, selected_wall_type.Id, base_level.Id, height_feet, 0.0, False, False)
                        base_offset_wall = wall.get_Parameter(DB.BuiltInParameter.WALL_BASE_OFFSET)
                        if base_offset_wall:
                            base_offset_wall.Set(wall_bases[i] - base_level.Elevation)
                        unconnected_height = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                        if unconnected_height:
                            unconnected_height.Set(wall_tops[i] - wall_bases[i])
                    except Exception as e:
                        print("Error al crear muro: ", e)
                
                # Create Columns
                for i in range(len(points)):
                    pt = points[i]
                    
                    if i == 0:
                        col_base = wall_bases[0]
                        col_top = wall_tops[0]
                    elif i == len(points) - 1:
                        col_base = wall_bases[-1]
                        col_top = wall_tops[-1]
                    else:
                        col_base = min(wall_bases[i-1], wall_bases[i])
                        col_top = max(wall_tops[i-1], wall_tops[i])
                        
                    location = DB.XYZ(pt.X, pt.Y, col_base)
                    try:
                        col = doc.Create.NewFamilyInstance(location, selected_col_type, base_level, DB.Structure.StructuralType.NonStructural)
                        
                        base_offset_param = col.get_Parameter(DB.BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM)
                        if base_offset_param:
                            base_offset_param.Set(col_base - base_level.Elevation)
                            
                        # Column top
                        top_offset_param = col.get_Parameter(DB.BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM)
                        if top_offset_param:
                            top_level_param = col.get_Parameter(DB.BuiltInParameter.FAMILY_TOP_LEVEL_PARAM)
                            if top_level_param and top_level_param.AsElementId() != DB.ElementId.InvalidElementId:
                                top_lvl_id = top_level_param.AsElementId()
                                top_lvl = doc.GetElement(top_lvl_id)
                                if top_lvl:
                                    top_offset_param.Set(col_top - top_lvl.Elevation)
                            else:
                                top_offset_param.Set(col_top - base_level.Elevation)
                                
                        # Rotate column
                        angle = get_angle_at_point(i, points)
                        if angle != 0.0:
                            axis_start = DB.XYZ(pt.X, pt.Y, col_base)
                            axis_end = DB.XYZ(pt.X, pt.Y, col_base + 10.0)
                            axis = DB.Line.CreateBound(axis_start, axis_end)
                            DB.ElementTransformUtils.RotateElement(doc, col.Id, axis, angle)
                            
                    except Exception as e:
                        print("Error al crear columna: ", e)
                            
        self.Close()

if __name__ == '__main__':
    window = PartyWallWindow('ui.xaml')
    window.ShowDialog()
