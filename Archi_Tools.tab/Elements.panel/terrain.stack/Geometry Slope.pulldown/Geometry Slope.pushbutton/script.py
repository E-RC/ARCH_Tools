# -*- coding: utf-8 -*-
import clr
import math
from pyrevit import revit, DB, UI, forms, script
from Autodesk.Revit.Exceptions import OperationCanceledException

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

class Abort(Exception):
    """Aborta dentro de la transaccion: asi Revit hace RollBack y la losa
    conserva sus edits previos en vez de quedarse con el reset."""


class ElementSelectionFilter(UI.Selection.ISelectionFilter):
    def AllowElement(self, elem):
        if isinstance(elem, (DB.Floor, DB.Toposolid)):
            return True
        return False
        
    def AllowReference(self, reference, position):
        return False

class EdgeSelectionFilter(UI.Selection.ISelectionFilter):
    def __init__(self, element_id):
        self.element_id = element_id
        
    def AllowElement(self, elem):
        return elem.Id == self.element_id
        
    def AllowReference(self, reference, position):
        return reference.ElementId == self.element_id

class GeometrySlopeWindow(forms.WPFWindow):
    def __init__(self, xaml_file_name, default_slope=3.0):
        forms.WPFWindow.__init__(self, xaml_file_name)
        self.slope_percentage = None
        self.SlopeTextBox.Text = "{:.2f}".format(default_slope)

    def apply_click(self, sender, e):
        text = self.SlopeTextBox.Text
        text = text.replace(',', '.')
        try:
            val = float(text)
            val = round(val, 2)
            self.slope_percentage = val
            self.Close()
        except ValueError:
            forms.alert("Por favor ingresa un número válido.")

def distance_2d(p, p1, p2):
    x0, y0 = p.X, p.Y
    x1, y1 = p1.X, p1.Y
    x2, y2 = p2.X, p2.Y
    num = abs((y2 - y1) * x0 - (x2 - x1) * y0 + x2 * y1 - y2 * x1)
    den = math.sqrt((y2 - y1)**2 + (x2 - x1)**2)
    if den == 0:
        return 0
    return num / den

def main():
    # 1. Select Element
    try:
        elem_ref = uidoc.Selection.PickObject(UI.Selection.ObjectType.Element, ElementSelectionFilter(), "1. Selecciona un Floor o Toposolid")
        # Retrieve the element using the document owning the active view/context
        elem = uidoc.Document.GetElement(elem_ref)
    except OperationCanceledException:
        return
        
    # Retrieve the specific document owning the element (crucial for Group Edit Mode support)
    doc_to_use = elem.Document
        
    # 2. Persistent Memory (User Config)
    cfg = script.get_config()
    last_slope = getattr(cfg, 'last_slope', 3.0)
    try:
        last_slope = float(last_slope)
    except Exception:
        last_slope = 3.0

    # UI for Slope
    window = GeometrySlopeWindow("ui.xaml", default_slope=last_slope)
    window.ShowDialog()
    if window.slope_percentage is None:
        return
    slope = window.slope_percentage

    # Save the new value into the config file only after the user clicks "Apply"
    cfg.last_slope = slope
    script.save_config()

    # 3. Select Edge
    try:
        edge_filter = EdgeSelectionFilter(elem.Id)
        edge_ref = uidoc.Selection.PickObject(UI.Selection.ObjectType.Edge, edge_filter, "3. Selecciona la arista base")
    except OperationCanceledException:
        return

    edge_geom = elem.GetGeometryObjectFromReference(edge_ref)
    if not isinstance(edge_geom, DB.Edge):
        forms.alert("No se seleccionó una arista válida.")
        return
    curve = edge_geom.AsCurve()
    p1 = curve.GetEndPoint(0)
    p2 = curve.GetEndPoint(1)

    # 4. Transaction opened in the correct doc (the one owning the elements)
    try:
        with revit.Transaction("Geometry Slope", doc=doc_to_use):
            editor = elem.GetSlabShapeEditor()
            if not editor:
                raise Abort("No se pudo obtener el SlabShapeEditor del elemento.")
            
            # Reset First: Always call SlabShapeEditor.ResetSlabShape() to clear previous edits
            editor.ResetSlabShape()
            editor.Enable()
            doc_to_use.Regenerate()
        
            ratio = slope / 100.0
        
            # Internal Points: Ensure the loop iterates through SlabShapeEditor.SlabShapeVertices
            vertices = editor.SlabShapeVertices
            if not vertices or vertices.Size == 0:
                raise Abort("El elemento no tiene vértices modificables.")
             
            for v in vertices:
                pos = v.Position
                # Math: Project each vertex point to the infinite line of the base edge in the XY plane
                d = distance_2d(pos, p1, p2)
                # Always Up: d >= 0 and ratio is signed by slope. Since slope is upward relative to base edge,
                # new Z increases elevation relative to base edge Z-level.
                offset = d * ratio
                editor.ModifySubElement(v, offset)
    except Abort as ex:
        forms.alert(str(ex))

if __name__ == '__main__':
    main()
