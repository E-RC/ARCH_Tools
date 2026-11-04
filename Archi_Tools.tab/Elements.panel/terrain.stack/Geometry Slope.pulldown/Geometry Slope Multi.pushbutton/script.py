# -*- coding: utf-8 -*-
__title__ = "Geometry Slope\nMulti"
__doc__ = ("Modela pendientes en un Floor o Toposolid a partir de 2 o mas "
           "aristas, cada una con su propio porcentaje.")

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")

import math
import os
import sys

from pyrevit import revit, DB, UI, forms, script
from Autodesk.Revit.Exceptions import OperationCanceledException

from System.Windows import Thickness, VerticalAlignment
from System.Windows.Controls import Orientation, StackPanel, TextBlock, TextBox

# slope_solver.py vive junto a este script. pyRevit agrega la carpeta al search
# path por su cuenta, pero se asegura aqui para no depender de ese detalle.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)

import slope_solver

try:
    import edge_labels
except Exception:
    edge_labels = None      # sin numeros de apoyo, pero la herramienta corre

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

FT_TO_M = slope_solver.FT_TO_M
XY_TOL_FT = 1e-6          # bajo esto una arista no tiene direccion en planta
SAME_POINT_FT = 0.0033    # ~1 mm: para descartar aristas repetidas
VERTEX_TOL_FT = 0.01      # ~3 mm: para reconocer un vertice que ya existe
MIN_SPLIT_FT = 0.05       # ~15 mm: bajo esto la split line es degenerada
MAX_SPLIT_LINES = 200     # tope de cordura: mas que esto es un caso patologico


class Abort(Exception):
    """Aborta dentro de la transaccion: asi Revit hace RollBack y la losa
    conserva sus edits previos en vez de quedarse con el reset."""


class ElementSelectionFilter(UI.Selection.ISelectionFilter):
    def AllowElement(self, elem):
        return isinstance(elem, (DB.Floor, DB.Toposolid))

    def AllowReference(self, reference, position):
        return False


class EdgeSelectionFilter(UI.Selection.ISelectionFilter):
    def __init__(self, element_id):
        self.element_id = element_id

    def AllowElement(self, elem):
        return elem.Id == self.element_id

    def AllowReference(self, reference, position):
        return reference.ElementId == self.element_id


class MultiSlopeWindow(forms.WPFWindow):
    """Una fila por arista: las filas se generan aqui, no en el XAML, porque
    su cantidad depende de lo que el usuario acabe de seleccionar."""

    def __init__(self, xaml_file_name, rows, default_slope):
        forms.WPFWindow.__init__(self, xaml_file_name)
        self.percentages = None
        self._boxes = []

        for row_text in rows:
            row = StackPanel()
            row.Orientation = Orientation.Horizontal
            row.Margin = Thickness(0, 0, 0, 6)

            caption = TextBlock()
            caption.Text = row_text
            caption.Width = 210
            caption.VerticalAlignment = VerticalAlignment.Center

            box = TextBox()
            box.Text = "{0:.2f}".format(default_slope)
            box.Width = 80
            box.Height = 24
            box.VerticalContentAlignment = VerticalAlignment.Center

            row.Children.Add(caption)
            row.Children.Add(box)
            self.RowsPanel.Children.Add(row)
            self._boxes.append(box)

        if self._boxes:
            self._boxes[0].Focus()
            self._boxes[0].SelectAll()

        self._move_aside()

    def _move_aside(self):
        """A un costado de la pantalla: los numeros de las aristas estan detras,
        en la vista, y una ventana centrada los tapa justo."""
        try:
            from System.Windows import SystemParameters, WindowStartupLocation
            self.WindowStartupLocation = WindowStartupLocation.Manual
            self.Left = SystemParameters.PrimaryScreenWidth - self.Width - 60
            self.Top = 140
        except Exception:
            pass

    def apply_click(self, sender, e):
        values = []
        for i, box in enumerate(self._boxes):
            text = box.Text.replace(',', '.').strip()
            try:
                values.append(round(float(text), 2))
            except ValueError:
                forms.alert("La arista {0} no tiene un numero valido: "
                            "'{1}'".format(i + 1, box.Text))
                box.Focus()
                box.SelectAll()
                return
        self.percentages = values
        self.Close()


def pick_element():
    ref = uidoc.Selection.PickObject(
        UI.Selection.ObjectType.Element, ElementSelectionFilter(),
        "1. Selecciona un Floor o Toposolid")
    return uidoc.Document.GetElement(ref)


def pick_edges(elem):
    """Aristas como (p0, p1) en planta, donde rotular cada una, y los avisos.

    La curva se lee ahora, antes de la transaccion: despues de ResetSlabShape
    las referencias de las aristas ya no sirven. Solo importa la proyeccion en
    planta, que el editor de sub-elementos nunca mueve."""
    refs = uidoc.Selection.PickObjects(
        UI.Selection.ObjectType.Edge, EdgeSelectionFilter(elem.Id),
        "2. Selecciona 2 o mas aristas de apoyo y termina con Finish")

    lines, anchors, notes = [], [], []
    for ref in refs:
        geom = elem.GetGeometryObjectFromReference(ref)
        if not isinstance(geom, DB.Edge):
            continue
        curve = geom.AsCurve()
        if not isinstance(curve, DB.Line):
            notes.append("Se ignoro una arista curva: solo se admiten rectas.")
            continue

        a, b = curve.GetEndPoint(0), curve.GetEndPoint(1)
        p0, p1 = (a.X, a.Y), (b.X, b.Y)
        if math.hypot(p1[0] - p0[0], p1[1] - p0[1]) < XY_TOL_FT:
            notes.append("Se ignoro un canto vertical: no define pendiente.")
            continue
        if any(_same_line(p0, p1, q0, q1) for (q0, q1) in lines):
            notes.append("Se ignoro una arista repetida (canto superior e "
                         "inferior de un mismo borde).")
            continue
        lines.append((p0, p1))
        anchors.append(curve.Evaluate(0.5, True))   # ahi va su numero

    return lines, anchors, notes


def _same_line(p0, p1, q0, q1):
    def close(u, v):
        return math.hypot(u[0] - v[0], u[1] - v[1]) < SAME_POINT_FT
    return ((close(p0, q0) and close(p1, q1))
            or (close(p0, q1) and close(p1, q0)))


def top_face_polygon(elem):
    """(contorno exterior en planta, cota de la cara, cantidad de loops).

    Se llama con la losa ya reseteada: asi la cara superior es una sola y su
    contorno es el real."""
    opts = DB.Options()
    opts.DetailLevel = DB.ViewDetailLevel.Fine
    best = None
    for gobj in elem.get_Geometry(opts):
        if not isinstance(gobj, DB.Solid):
            continue
        for face in gobj.Faces:
            if not isinstance(face, DB.PlanarFace):
                continue
            if face.FaceNormal.Z < 0.99:
                continue
            if best is None or face.Area > best.Area:
                best = face
    if best is None:
        return None, None, 0

    polygons = [_loop_to_polygon(loop)
                for loop in best.GetEdgesAsCurveLoops()]
    polygons = [p for p in polygons if len(p) >= 3]
    if not polygons:
        return None, None, 0

    return max(polygons, key=_polygon_area), best.Origin.Z, len(polygons)


def _loop_to_polygon(loop):
    pts = []
    for curve in loop:
        tess = curve.Tessellate()
        for i in range(tess.Count - 1):   # el ultimo punto abre la curva siguiente
            pts.append((tess[i].X, tess[i].Y))
    return pts


def _polygon_area(polygon):
    total = 0.0
    n = len(polygon)
    for i in range(n):
        x0, y0 = polygon[i]
        x1, y1 = polygon[(i + 1) % n]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2.0


def _vertex_at(editor, point, z):
    """El vertice que ya hay en ese punto, o uno nuevo si no hay ninguno.

    Las cumbreras nacen muchas veces en una esquina de la losa, que ya es
    vertice: duplicarlo deja dos puntos de control encima del mismo sitio."""
    for vertex in editor.SlabShapeVertices:
        position = vertex.Position
        if math.hypot(position.X - point[0],
                      position.Y - point[1]) < VERTEX_TOL_FT:
            return vertex
    return editor.DrawPoint(DB.XYZ(point[0], point[1], z))


def draw_split_line(editor, segment, z):
    """Dibuja la linea de quiebre entre los dos extremos del tramo.

    DrawSplitLine solo acepta SlabShapeVertex: no existe una sobrecarga que
    tome puntos, asi que primero hay que materializar los extremos.

    El editor de sub-elementos es fragil: un tramo degenerado o dos extremos
    que caen sobre el mismo vertice pueden tumbar Revit entero, asi que se
    revisa antes de pasarselo."""
    (x0, y0), (x1, y1) = segment
    for value in (x0, y0, x1, y1):
        if not _is_finite(value):
            return False
    if math.hypot(x1 - x0, y1 - y0) < MIN_SPLIT_FT:
        return False

    try:
        start = _vertex_at(editor, segment[0], z)
        end = _vertex_at(editor, segment[1], z)
        if start is None or end is None:
            return False
        if math.hypot(start.Position.X - end.Position.X,
                      start.Position.Y - end.Position.Y) < MIN_SPLIT_FT:
            return False        # los dos extremos cayeron en el mismo vertice
        editor.DrawSplitLine(start, end)
        return True
    except Exception:
        return False


def _is_finite(value):
    return value == value and abs(value) < 1e12


def apply_slopes(elem, doc_to_use, edges):
    """Resetea, dibuja las cumbreras y sube cada vertice a su cota.

    Devuelve (split lines fallidas, loops de la cara superior, esquinas que
    hubo que facetear)."""
    failed = 0

    editor = elem.GetSlabShapeEditor()
    if not editor:
        raise Abort("No se pudo obtener el SlabShapeEditor del elemento.")

    editor.ResetSlabShape()
    editor.Enable()
    doc_to_use.Regenerate()

    polygon, z, loop_count = top_face_polygon(elem)
    if polygon is None:
        raise Abort("No se encontro la cara superior del elemento.")

    surface = slope_solver.Surface(edges, polygon)
    segments = surface.ridge_segments()
    if len(segments) > MAX_SPLIT_LINES:
        raise Abort("El calculo pide {0} lineas de quiebre, muy por encima de "
                    "lo razonable ({1}). No se toca el elemento: revisa la "
                    "seleccion de aristas.".format(len(segments),
                                                   MAX_SPLIT_LINES))
    for segment in segments:
        if not draw_split_line(editor, segment, z):
            failed += 1
    doc_to_use.Regenerate()

    vertices = list(editor.SlabShapeVertices)
    if not vertices:
        raise Abort("El elemento no tiene vertices modificables.")
    for vertex in vertices:
        position = vertex.Position
        editor.ModifySubElement(
            vertex, surface.offset_at((position.X, position.Y)))

    return failed, loop_count


def main():
    try:
        elem = pick_element()
    except OperationCanceledException:
        return
    doc_to_use = elem.Document      # el dueno real, para editar dentro de grupos

    try:
        lines, anchors, notes = pick_edges(elem)
    except OperationCanceledException:
        return

    if len(lines) < 2:
        # Las notas explican por que una seleccion de 2 o 3 aristas puede
        # quedar corta: aqui son el diagnostico, no un aviso secundario.
        detail = ("\n\n- " + "\n- ".join(notes)) if notes else ""
        forms.alert("Selecciona al menos 2 aristas.\n\nPara una sola arista usa "
                    "Geometry Slope." + detail, title="Geometry Slope Multi")
        return

    cfg = script.get_config()
    try:
        last_slope = float(getattr(cfg, 'last_slope', 3.0))
    except Exception:
        last_slope = 3.0

    rows = ["Arista {0}   ({1:.2f} m)".format(
        i + 1, math.hypot(p1[0] - p0[0], p1[1] - p0[1]) * FT_TO_M)
        for i, (p0, p1) in enumerate(lines)]

    # Los mismos numeros, flotando junto a cada arista en la vista, para no
    # tener que adivinar cual fila es cual.
    labels = edge_labels.EdgeLabels(uidoc) if edge_labels else None
    if labels is not None:
        labels.show(anchors)
    window = None
    try:
        window = MultiSlopeWindow("ui.xaml", rows, last_slope)
        window.ShowDialog()
    finally:
        if labels is not None:
            labels.hide()
    if window is None or window.percentages is None:
        return

    edges = [slope_solver.SlopeEdge(p0, p1, pct, "Arista {0}".format(i + 1))
             for i, ((p0, p1), pct) in enumerate(zip(lines, window.percentages))]

    conflicts = slope_solver.find_conflicts(edges)
    if conflicts:
        detail = "\n\n".join(
            "- " + slope_solver.describe_conflict(c, edges) for c in conflicts)
        forms.alert("Las pendientes pedidas no definen una superficie posible, "
                    "asi que no se modifico nada:\n\n" + detail,
                    title="Geometria irresoluble", warn_icon=True)
        return

    cfg.last_slope = window.percentages[0]
    script.save_config()

    try:
        with revit.Transaction("Geometry Slope Multi", doc=doc_to_use):
            failed, loop_count = apply_slopes(elem, doc_to_use, edges)
    except Abort as ex:
        forms.alert(str(ex), title="Geometry Slope Multi", warn_icon=True)
        return

    warnings = list(notes)
    if failed:
        warnings.append("{0} linea(s) de quiebre no se pudieron dibujar: la "
                        "superficie quedo aproximada ahi.".format(failed))
    if loop_count > 1:
        warnings.append("La losa tiene aberturas: las lineas de quiebre se "
                        "calcularon sobre el contorno exterior.")
    if warnings:
        forms.alert("Pendientes aplicadas, con avisos:\n\n- "
                    + "\n- ".join(warnings), title="Geometry Slope Multi")


if __name__ == '__main__':
    main()
