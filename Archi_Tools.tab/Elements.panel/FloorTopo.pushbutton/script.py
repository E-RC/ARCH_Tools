# -*- coding: utf-8 -*-
"""Convierte floors en toposolids y toposolids en floors, conservando forma,
nivel, offset, Mark, Comments y fases."""
import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")

from pyrevit import revit, DB, UI, forms, script
from pyrevit.forms import WPFWindow
from System.Collections.Generic import List

doc = revit.doc
uidoc = revit.uidoc
config = script.get_config()

TOL = 1e-6
XY_TOL = 0.01  # pies, para emparejar vértices en planta
ARC_SEGMENTS = 8  # puntos por curva no recta en floors inclinados

FLOOR_CAT = int(DB.BuiltInCategory.OST_Floors)

COPY_PARAMS = [
    DB.BuiltInParameter.ALL_MODEL_MARK,
    DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS,
]


# ----------------------------------------------------------------------------
# Selección
# ----------------------------------------------------------------------------
def cat_id(el):
    return el.Category.Id.IntegerValue if el and el.Category else None


def is_floor(el):
    return isinstance(el, DB.Floor) and cat_id(el) == FLOOR_CAT


def is_topo(el):
    return isinstance(el, DB.Toposolid)


class FloorTopoFilter(UI.Selection.ISelectionFilter):
    def AllowElement(self, elem):
        return is_floor(elem) or is_topo(elem)

    def AllowReference(self, ref, pos):
        return False


def get_targets():
    sel = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
    sel = [e for e in sel if is_floor(e) or is_topo(e)]
    if not sel:
        try:
            refs = uidoc.Selection.PickObjects(
                UI.Selection.ObjectType.Element, FloorTopoFilter(),
                "Selecciona floors y/o toposolids y pulsa Finish")
        except Exception:
            return [], []
        sel = [doc.GetElement(r) for r in refs]
    return [e for e in sel if is_floor(e)], [e for e in sel if is_topo(e)]


# ----------------------------------------------------------------------------
# Geometría
# ----------------------------------------------------------------------------
def sketch_loops(el):
    """Contornos del sketch como List[CurveLoop] (incluye huecos)."""
    if el.SketchId == DB.ElementId.InvalidElementId:
        return None
    sketch = doc.GetElement(el.SketchId)
    loops = List[DB.CurveLoop]()
    for arr in sketch.Profile:
        loop = DB.CurveLoop()
        for c in arr:
            loop.Append(c)
        loops.Add(loop)
    return loops


def shape_editor(el):
    try:
        return el.GetSlabShapeEditor()
    except Exception:
        return getattr(el, "SlabShapeEditor", None)


def edited_points(el):
    """Vértices de edición de forma (Z absoluta), o [] si no hay edición."""
    ed = shape_editor(el)
    if ed is None or not ed.IsEnabled:
        return []
    return [v.Position for v in ed.SlabShapeVertices]


def top_faces(floor):
    return [floor.GetGeometryObjectFromReference(r)
            for r in DB.HostObjectUtils.GetTopFaces(floor)]


def is_flat(faces):
    for f in faces:
        if not isinstance(f, DB.PlanarFace) or abs(f.FaceNormal.Z - 1.0) > TOL:
            return False
    return True


def z_on_faces(faces, xy, zmin, zmax):
    line = DB.Line.CreateBound(DB.XYZ(xy.X, xy.Y, zmax), DB.XYZ(xy.X, xy.Y, zmin))
    best = None
    for f in faces:
        res = clr.Reference[DB.IntersectionResultArray]()
        if f.Intersect(line, res) == DB.SetComparisonResult.Overlap and res.Value:
            for i in range(res.Value.Size):
                z = res.Value.get_Item(i).XYZPoint.Z
                if best is None or z > best:
                    best = z
    return best


def z_on_plane(faces, xy):
    """Z sobre el plano de la cara plana más grande (sin límites de borde)."""
    planes = [f for f in faces if isinstance(f, DB.PlanarFace) and abs(f.FaceNormal.Z) > TOL]
    if not planes:
        return None
    f = max(planes, key=lambda pf: pf.Area)
    n, o = f.FaceNormal, f.Origin
    return o.Z - (n.X * (xy.X - o.X) + n.Y * (xy.Y - o.Y)) / n.Z


def boundary_samples(loops):
    pts = []
    for loop in loops:
        for c in loop:
            if isinstance(c, DB.Line):
                pts.append(c.GetEndPoint(0))
            else:
                for i in range(ARC_SEGMENTS):
                    pts.append(c.Evaluate(float(i) / ARC_SEGMENTS, True))
    return pts


def dedupe(points):
    out = []
    for p in points:
        if not any(abs(p.X - q.X) < XY_TOL and abs(p.Y - q.Y) < XY_TOL for q in out):
            out.append(p)
    return out


def floor_surface_points(floor, loops):
    """None si el floor es plano; si no, lista de XYZ de su cara superior."""
    pts = edited_points(floor)
    if pts:
        return dedupe(pts)
    faces = top_faces(floor)
    if is_flat(faces):
        return None
    bb = floor.get_BoundingBox(None)
    zmin, zmax = bb.Min.Z - 1.0, bb.Max.Z + 1.0
    out = []
    for p in boundary_samples(loops):
        z = z_on_faces(faces, p, zmin, zmax)
        if z is None:
            # Las uniones con muros recortan la cara superior: se extiende su plano.
            z = z_on_plane(faces, p)
        if z is not None:
            out.append(DB.XYZ(p.X, p.Y, z))
    return dedupe(out)


def apply_points_to_floor(floor, points):
    """Replica los puntos (Z absoluta) en el floor con edición de forma."""
    ed = shape_editor(floor)
    ed.Enable()
    doc.Regenerate()
    rest = List[DB.XYZ]()
    corners = [v for v in ed.SlabShapeVertices]
    for p in points:
        corner = None
        for v in corners:
            q = v.Position
            if abs(p.X - q.X) < XY_TOL and abs(p.Y - q.Y) < XY_TOL:
                corner = v
                break
        if corner is not None:
            # El offset de ModifySubElement es relativo a la cara superior sin editar.
            ed.ModifySubElement(corner, p.Z - corner.Position.Z)
        else:
            rest.Add(p)
    if rest.Count:
        ed.AddPoints(rest)


# ----------------------------------------------------------------------------
# Parámetros
# ----------------------------------------------------------------------------
def copy_common(src, dst):
    for bip in COPY_PARAMS:
        ps, pd = src.get_Parameter(bip), dst.get_Parameter(bip)
        if ps and pd and not pd.IsReadOnly and ps.HasValue:
            pd.Set(ps.AsString() or "")
    try:
        if src.ArePhasesModifiable() and dst.ArePhasesModifiable():
            dst.CreatedPhaseId = src.CreatedPhaseId
            dst.DemolishedPhaseId = src.DemolishedPhaseId
    except Exception:
        pass


def set_double(el, bip, value):
    p = el.get_Parameter(bip)
    if p and not p.IsReadOnly:
        p.Set(value)


def get_double(el, bip):
    p = el.get_Parameter(bip)
    return p.AsDouble() if p else 0.0


# ----------------------------------------------------------------------------
# Conversión
# ----------------------------------------------------------------------------
def floor_to_topo(floor, topo_type_id):
    loops = sketch_loops(floor)
    if not loops:
        raise Exception("sin contorno editable")
    pts = floor_surface_points(floor, loops)
    if pts:
        topo = DB.Toposolid.Create(doc, loops, List[DB.XYZ](pts), topo_type_id, floor.LevelId)
    else:
        topo = DB.Toposolid.Create(doc, loops, topo_type_id, floor.LevelId)
        set_double(topo, DB.BuiltInParameter.TOPOSOLID_HEIGHTABOVELEVEL_PARAM,
                   get_double(floor, DB.BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM))
    copy_common(floor, topo)
    return topo


def topo_to_floor(topo, floor_type_id):
    loops = sketch_loops(topo)
    if not loops:
        raise Exception("sin contorno editable")
    floor = DB.Floor.Create(doc, loops, floor_type_id, topo.LevelId)
    pts = edited_points(topo)
    if pts:
        # Los puntos son absolutos: la cara de referencia va al punto más alto.
        offset = max(p.Z for p in pts) - doc.GetElement(topo.LevelId).Elevation
    else:
        offset = get_double(topo, DB.BuiltInParameter.TOPOSOLID_HEIGHTABOVELEVEL_PARAM)
    set_double(floor, DB.BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM, offset)
    copy_common(topo, floor)
    if pts:
        doc.Regenerate()
        apply_points_to_floor(floor, pts)
    return floor


class SilentFailures(DB.IFailuresPreprocessor):
    """Oculta advertencias y aplica la resolución por defecto de Revit a los
    errores que la tienen (p. ej. desunir muros al eliminar el floor original)."""
    def PreprocessFailures(self, accessor):
        resolved = False
        for f in accessor.GetFailureMessages():
            if f.GetSeverity() == DB.FailureSeverity.Warning:
                accessor.DeleteWarning(f)
            elif f.HasResolutions():
                accessor.ResolveFailure(f)
                resolved = True
        if resolved:
            return DB.FailureProcessingResult.ProceedWithCommit
        return DB.FailureProcessingResult.Continue


def describe(el):
    return "{} {}".format("Floor" if is_floor(el) else "Toposolid", el.Id.IntegerValue)


def run(floors, topos, topo_type_id, floor_type_id, delete_originals):
    created, errors = [], []
    with DB.TransactionGroup(doc, "Floor <-> Toposolid") as tg:
        tg.Start()
        jobs = [(f, describe(f), floor_to_topo, topo_type_id) for f in floors] + \
               [(t, describe(t), topo_to_floor, floor_type_id) for t in topos]
        for el, label, fn, type_id in jobs:
            if not el.IsValidObject:
                errors.append("{}: ya no existe en el modelo".format(label))
                continue
            t = DB.Transaction(doc, "Convertir " + label)
            opts = t.GetFailureHandlingOptions()
            opts.SetFailuresPreprocessor(SilentFailures())
            t.SetFailureHandlingOptions(opts)
            t.Start()
            try:
                new = fn(el, type_id)
                if delete_originals:
                    doc.Delete(el.Id)
                if t.Commit() == DB.TransactionStatus.Committed:
                    created.append(new.Id)
                else:
                    errors.append("{}: Revit rechazó el resultado".format(label))
            except Exception as ex:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
                errors.append("{}: {}".format(label, ex))
        tg.Assimilate()
    return created, errors


# ----------------------------------------------------------------------------
# Ventana
# ----------------------------------------------------------------------------
def type_items(cls):
    types = DB.FilteredElementCollector(doc).OfClass(cls).ToElements()
    if cls is DB.FloorType:
        types = [t for t in types if t.Category and t.Category.Id.IntegerValue == FLOOR_CAT]
    items = sorted((DB.Element.Name.__get__(t), t.Id) for t in types)
    return items


class ConvertWindow(WPFWindow):
    def __init__(self, xaml, floors, topos):
        WPFWindow.__init__(self, xaml)
        self.result = None
        self.topo_items = type_items(DB.ToposolidType)
        self.floor_items = type_items(DB.FloorType)

        self.TbFloorCount.Text = "{} floors → toposolid".format(len(floors))
        self.TbTopoCount.Text = "{} toposolids → floor".format(len(topos))
        self._fill(self.CbTopoType, self.topo_items, "last_topo_type", bool(floors))
        self._fill(self.CbFloorType, self.floor_items, "last_floor_type", bool(topos))
        self.ChkDelete.IsChecked = config.get_option("delete_originals", True)

    def _fill(self, combo, items, key, enabled):
        for name, _ in items:
            combo.Items.Add(name)
        last = config.get_option(key, "")
        names = [n for n, _ in items]
        combo.SelectedIndex = names.index(last) if last in names else (0 if names else -1)
        combo.IsEnabled = enabled
        combo.Opacity = 1.0 if enabled else 0.45

    def BtnExecute_Click(self, sender, e):
        topo_id = floor_id = None
        if self.CbTopoType.IsEnabled:
            if self.CbTopoType.SelectedIndex < 0:
                forms.alert("No hay tipos de toposolid en el proyecto.")
                return
            name, topo_id = self.topo_items[self.CbTopoType.SelectedIndex]
            config.last_topo_type = name
        if self.CbFloorType.IsEnabled:
            if self.CbFloorType.SelectedIndex < 0:
                forms.alert("No hay tipos de floor en el proyecto.")
                return
            name, floor_id = self.floor_items[self.CbFloorType.SelectedIndex]
            config.last_floor_type = name
        delete = bool(self.ChkDelete.IsChecked)
        config.delete_originals = delete
        script.save_config()
        self.result = (topo_id, floor_id, delete)
        self.Close()

    def CloseButton_Click(self, sender, e):
        self.Close()


if __name__ == "__main__":
    floors, topos = get_targets()
    if not floors and not topos:
        script.exit()
    win = ConvertWindow(script.get_bundle_file("ui.xaml"), floors, topos)
    win.ShowDialog()
    if win.result:
        topo_id, floor_id, delete = win.result
        created, errors = run(floors, topos, topo_id, floor_id, delete)
        if created:
            uidoc.Selection.SetElementIds(List[DB.ElementId](created))
        msg = "Se convirtieron {} de {} elementos.".format(len(created), len(floors) + len(topos))
        if errors:
            msg += "\n\nNo convertidos:\n" + "\n".join(errors)
        forms.alert(msg, title="Floor ⇄ Toposolid")
