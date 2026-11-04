# -*- coding: utf-8 -*-
from pyrevit import revit, DB, UI, forms
import System
import clr
import os, json
from System.Collections.Generic import List
from collections import Counter
import wpf
from archi_common.paths import user_data_path
from System import Windows

doc   = revit.doc
uidoc = revit.uidoc

CM_TO_FT       = 1.0 / 30.48
TURN_RADIUS_FT = 75.0 * CM_TO_FT
ARROW_EXTRA_FT = 30.0 * CM_TO_FT

# Rutas del plugin
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_XAML_PATH  = os.path.join(_SCRIPT_DIR, "MainWindow.xaml")
_CFG_PATH   = user_data_path("RutaAccesible", "config.json")

def _load_cfg():
    try:
        if os.path.exists(_CFG_PATH):
            with open(_CFG_PATH, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_cfg(data):
    try:
        with open(_CFG_PATH, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

def _family_loaded_in_doc(document, family_name):
    for s in DB.FilteredElementCollector(document).OfClass(DB.FamilySymbol).OfCategory(DB.BuiltInCategory.OST_DetailComponents).ToElements():
        if s.Family and s.Family.Name == family_name:
            return True
    return False

def _load_family_rfa(document, rfa_path):
    with DB.Transaction(document, "Cargar familia {}".format(os.path.basename(rfa_path))) as t:
        t.Start()
        out_fam = clr.Reference[DB.Family]()
        loaded = document.LoadFamily(rfa_path, out_fam)
        if loaded:
            fam = out_fam.Value
            for sym_id in fam.GetFamilySymbolIds():
                sym = document.GetElement(sym_id)
                if not sym.IsActive:
                    sym.Activate()
        t.Commit()

# ─────────────────────────────────────────────────────────────────
class Wrapper(object):
    def __init__(self, el, name):
        self.element = el
        self.Name = name

import math as _math

class LineFilter(UI.Selection.ISelectionFilter):
    """Permite seleccionar lineas y curvas de detalle o de modelo."""
    def AllowElement(self, e):
        return isinstance(e, (DB.DetailLine, DB.DetailArc, DB.DetailCurve,
                              DB.ModelLine, DB.ModelArc, DB.ModelCurve,
                              DB.CurveElement))
    def AllowReference(self, r, p):
        return False

def _key(pt, d=3):
    return (round(pt.X, d), round(pt.Y, d))

def _flat(pt, z):
    return DB.XYZ(pt.X, pt.Y, z)

# ─── Constructores de loops ───────────────────────────────────────
def loop_rect(s, e, half_w, z, clip_s=0.0, clip_e=0.0):
    """Rectángulo de s a e. clip_s/clip_e recortan los extremos hacia adentro."""
    s = _flat(s, z); e = _flat(e, z)
    d = (e - s).Normalize()
    n = DB.XYZ(-d.Y, d.X, 0.0)
    s2 = s + d * clip_s
    e2 = e - d * clip_e
    if s2.DistanceTo(e2) < 1e-6:
        raise ValueError("Segmento demasiado corto")
    p1 = s2 + n*half_w; p2 = e2 + n*half_w
    p3 = e2 - n*half_w; p4 = s2 - n*half_w
    lp = DB.CurveLoop()
    lp.Append(DB.Line.CreateBound(p1, p2))
    lp.Append(DB.Line.CreateBound(p2, p3))
    lp.Append(DB.Line.CreateBound(p3, p4))
    lp.Append(DB.Line.CreateBound(p4, p1))
    return lp

def loop_arc_band(arc, half_w, z):
    """Banda anular alrededor de un arco (sector anular)."""
    c  = DB.XYZ(arc.Center.X, arc.Center.Y, z)
    R  = arc.Radius
    Ro = R + half_w
    Ri = max(R - half_w, 0.001)
    xAx = DB.XYZ(arc.XDirection.X, arc.XDirection.Y, 0).Normalize()
    yAx = DB.XYZ(arc.YDirection.X, arc.YDirection.Y, 0).Normalize()
    t0 = arc.GetEndParameter(0)
    t1 = arc.GetEndParameter(1)
    ao = DB.Arc.Create(c, Ro, t0, t1, xAx, yAx)
    ai = DB.Arc.Create(c, Ri, t0, t1, xAx, yAx)
    ps = _flat(ao.GetEndPoint(0), z); pe = _flat(ao.GetEndPoint(1), z)
    qs = _flat(ai.GetEndPoint(0), z); qe = _flat(ai.GetEndPoint(1), z)
    lp = DB.CurveLoop()
    lp.Append(ao)
    lp.Append(DB.Line.CreateBound(pe, qe))
    lp.Append(ai.CreateReversed())
    lp.Append(DB.Line.CreateBound(qs, ps))
    return lp

def loop_spline_band(curve, half_w, z):
    """Banda poligonal alrededor de un spline por teselacion."""
    raw_pts = list(curve.Tessellate())
    # Forzar Z plano y eliminar puntos duplicados
    pts = []
    for p in raw_pts:
        fp = _flat(p, z)
        if not pts or pts[-1].DistanceTo(fp) > 1e-6:
            pts.append(fp)
    if len(pts) < 2:
        raise ValueError("Spline demasiado corto para teselar")

    def _seg_dir(a, b):
        v = b - a
        L = v.GetLength()
        return DB.XYZ(v.X / L, v.Y / L, 0.0) if L > 1e-9 else DB.XYZ(1, 0, 0)

    def _perp(d):
        return DB.XYZ(-d.Y, d.X, 0.0)

    # Normal en cada punto: promedio de los segmentos adyacentes
    n = len(pts)
    normals = []
    for i in range(n):
        if i == 0:
            d = _seg_dir(pts[0], pts[1])
        elif i == n - 1:
            d = _seg_dir(pts[-2], pts[-1])
        else:
            d1 = _seg_dir(pts[i - 1], pts[i])
            d2 = _seg_dir(pts[i], pts[i + 1])
            avg = DB.XYZ(d1.X + d2.X, d1.Y + d2.Y, 0.0)
            L = avg.GetLength()
            d = DB.XYZ(avg.X / L, avg.Y / L, 0.0) if L > 1e-9 else d1
        normals.append(_perp(d))

    right = [pts[i] + normals[i] * half_w for i in range(n)]
    left  = [pts[i] - normals[i] * half_w for i in range(n)]

    polygon = right + list(reversed(left))
    lp = DB.CurveLoop()
    for i in range(len(polygon)):
        a = polygon[i]
        b = polygon[(i + 1) % len(polygon)]
        if a.DistanceTo(b) > 1e-6:
            lp.Append(DB.Line.CreateBound(a, b))
    return lp

def loop_for_curve(curve, half_w, z):
    """Delega al constructor de loop segun el tipo de curva."""
    if isinstance(curve, DB.Arc):
        return loop_arc_band(curve, half_w, z)
    elif isinstance(curve, (DB.HermiteSpline, DB.NurbSpline)):
        return loop_spline_band(curve, half_w, z)
    else:
        s = _flat(curve.GetEndPoint(0), z)
        e = _flat(curve.GetEndPoint(1), z)
        return loop_rect(s, e, half_w, z)

def loop_circle(center, radius, z):
    cx, cy = center.X, center.Y
    L = DB.XYZ(cx-radius, cy, z); R = DB.XYZ(cx+radius, cy, z)
    T = DB.XYZ(cx, cy+radius, z); B = DB.XYZ(cx, cy-radius, z)
    lp = DB.CurveLoop()
    lp.Append(DB.Arc.Create(L, R, T))
    lp.Append(DB.Arc.Create(R, L, B))
    return lp

def loop_arrow(tip, outdir, half_w, z):
    OVERLAP_FT = 0.02
    tip = _flat(tip, z)
    d = DB.XYZ(outdir.X, outdir.Y, 0.0).Normalize()
    n = DB.XYZ(-d.Y, d.X, 0.0)
    base_half = half_w + ARROW_EXTRA_FT
    base_pt = tip - d * OVERLAP_FT
    b1 = base_pt + n*base_half; b2 = base_pt - n*base_half
    apex = tip + d * base_half
    lp = DB.CurveLoop()
    lp.Append(DB.Line.CreateBound(apex, b1))
    lp.Append(DB.Line.CreateBound(b1, b2))
    lp.Append(DB.Line.CreateBound(b2, apex))
    return lp


def _curve_tangent(curve, at_start, z):
    """Tangente unitaria 2D en el inicio (at_start=True) o fin del curve."""
    t = 0.0 if at_start else 1.0
    deriv = curve.ComputeDerivatives(t, True)
    tan = deriv.BasisX
    v = DB.XYZ(tan.X, tan.Y, 0.0)
    L = v.GetLength()
    return DB.XYZ(v.X/L, v.Y/L, 0.0) if L > 1e-9 else DB.XYZ(1, 0, 0)

def _is_tangent_junction(c1, ep1_at_start, c2, ep2_at_start, z, angle_tol_deg=15.0):
    """
    True si dos curvas se unen de forma tangente (sin quiebra brusca).
    ep1_at_start: la union es en el inicio de c1 (True) o en el fin (False).
    """
    # Tangente de c1 SALIENDO del nodo
    t1 = _curve_tangent(c1, ep1_at_start, z)
    if not ep1_at_start:          # si estamos en el fin, invertir
        t1 = DB.XYZ(-t1.X, -t1.Y, 0.0)
    # Tangente de c2 SALIENDO del nodo
    t2 = _curve_tangent(c2, ep2_at_start, z)
    if not ep2_at_start:
        t2 = DB.XYZ(-t2.X, -t2.Y, 0.0)
    # Angulo entre las dos tangentes salientes (continuacion suave = ~180 grados)
    dot = t1.X*t2.X + t1.Y*t2.Y
    dot = max(-1.0, min(1.0, dot))
    angle_deg = _math.degrees(_math.acos(dot))
    # Continuacion suave: las tangentes son antiparalelas (una entra, otra sale)
    return abs(angle_deg - 180.0) < angle_tol_deg

def split_at_midpoints(curves_info, z, TOL=1e-3):
    """
    Divide curvas cuando el extremo de OTRA cae en su interior.
    curves_info: lista de DB.Curve (Line o Arc) en el plano z.
    Devuelve nueva lista de DB.Curve divididas.
    """
    all_endpoints = []
    for c in curves_info:
        all_endpoints.append(_flat(c.GetEndPoint(0), z))
        all_endpoints.append(_flat(c.GetEndPoint(1), z))

    result = []
    for c in curves_info:
        if isinstance(c, DB.Line):
            s = _flat(c.GetEndPoint(0), z)
            e = _flat(c.GetEndPoint(1), z)
            seg_vec = e - s
            seg_len = seg_vec.GetLength()
            if seg_len < TOL:
                result.append(c)
                continue
            seg_dir = seg_vec * (1.0 / seg_len)
            split_ts = []
            for pt in all_endpoints:
                if pt.DistanceTo(s) < TOL or pt.DistanceTo(e) < TOL:
                    continue
                v  = pt - s
                t  = v.X*seg_dir.X + v.Y*seg_dir.Y
                if t <= TOL or t >= seg_len - TOL:
                    continue
                proj = DB.XYZ(s.X + seg_dir.X*t, s.Y + seg_dir.Y*t, z)
                if proj.DistanceTo(pt) < TOL:
                    split_ts.append(t)
            if not split_ts:
                result.append(c)
            else:
                split_ts = sorted(set([round(t, 6) for t in split_ts]))
                prev = s
                for t in split_ts:
                    mid = DB.XYZ(s.X + seg_dir.X*t, s.Y + seg_dir.Y*t, z)
                    if prev.DistanceTo(mid) > TOL:
                        result.append(DB.Line.CreateBound(prev, mid))
                    prev = mid
                if prev.DistanceTo(e) > TOL:
                    result.append(DB.Line.CreateBound(prev, e))
        elif isinstance(c, DB.Arc):
            # Para arcos: verificar si algun endpoint cae sobre el arco
            center = DB.XYZ(c.Center.X, c.Center.Y, z)
            R      = c.Radius
            t0     = c.GetEndParameter(0)
            t1     = c.GetEndParameter(1)
            arc_s  = _flat(c.GetEndPoint(0), z)
            arc_e  = _flat(c.GetEndPoint(1), z)
            xAx    = DB.XYZ(c.XDirection.X, c.XDirection.Y, 0).Normalize()
            yAx    = DB.XYZ(c.YDirection.X, c.YDirection.Y, 0).Normalize()
            split_params = []
            for pt in all_endpoints:
                if pt.DistanceTo(arc_s) < TOL or pt.DistanceTo(arc_e) < TOL:
                    continue
                # Distancia al centro del arco
                dp = pt - center
                if abs(dp.GetLength() - R) > TOL:
                    continue
                # Angulo del punto en el sistema de coordenadas del arco
                ang = _math.atan2(
                    dp.X*yAx.X + dp.Y*yAx.Y,
                    dp.X*xAx.X + dp.Y*xAx.Y)
                # Normalizar al rango [t0, t1]
                while ang < t0: ang += 2*_math.pi
                if ang <= t1 - TOL/R:
                    split_params.append(ang)
            if not split_params:
                result.append(c)
            else:
                split_params = sorted(set([round(a, 8) for a in split_params]))
                ta = t0
                for ang in split_params:
                    sub = DB.Arc.Create(center, R, ta, ang, xAx, yAx)
                    result.append(sub)
                    ta = ang
                result.append(DB.Arc.Create(center, R, ta, t1, xAx, yAx))
        else:
            result.append(c)
    return result

# ─── Ventana ──────────────────────────────────────────────────────
class RutaWindow(Windows.Window):
    def __init__(self, xaml_path, pre_sel):
        wpf.LoadComponent(self, xaml_path)
        self.selected_lines = list(pre_sel)
        self.action = "cancel"
        # Guardar ruta de config como atributo de instancia (evita acceso a globals en handlers WPF)
        self._cfg_path = user_data_path("RutaAccesible", "config.json")

        lines_cat = doc.Settings.Categories.get_Item(DB.BuiltInCategory.OST_Lines)
        self._ls = []
        if lines_cat:
            for sub in lines_cat.SubCategories:
                gs = sub.GetGraphicsStyle(DB.GraphicsStyleType.Projection)
                self._ls.append(Wrapper(gs, sub.Name))
        self._ls = sorted(self._ls, key=lambda x: x.Name)
        self.LineStyleCmb.ItemsSource = self._ls

        self._fr = []
        for frt in DB.FilteredElementCollector(doc).OfClass(DB.FilledRegionType).ToElements():
            p = frt.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)
            self._fr.append(Wrapper(frt, p.AsString() if p else "?"))
        self._fr = sorted(self._fr, key=lambda x: x.Name)
        self.FilledRegionTypeCmb.ItemsSource = self._fr

        TARGET_FAMILY = "ARCH-DET-SIMB-GIRO-DISCAP"
        _rfa_path = os.path.join(_SCRIPT_DIR, "families", TARGET_FAMILY + ".rfa")

        # Cargar familia si no está en el modelo y el .rfa está disponible
        if not _family_loaded_in_doc(doc, TARGET_FAMILY):
            if os.path.exists(_rfa_path):
                try:
                    _load_family_rfa(doc, _rfa_path)
                except Exception as _ex:
                    forms.alert(
                        "No se pudo cargar la familia automáticamente:\n{}\n\n"
                        "Puedes cargarla manualmente y volver a abrir la herramienta.".format(_ex),
                        title="Error al cargar familia", warn_icon=True)
            else:
                forms.alert(
                    "La familia \"{}\" no está en el modelo y no se encontró el archivo .rfa "
                    "en la carpeta de la herramienta.\n\n"
                    "Carga la familia manualmente y vuelve a abrir la herramienta.".format(TARGET_FAMILY),
                    title="Familia no encontrada", warn_icon=True)

        self._sym = [Wrapper(None, "Ninguno")]
        target_sym_idx = 0
        for s in DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol).OfCategory(DB.BuiltInCategory.OST_DetailComponents).ToElements():
            p  = s.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)
            sn = p.AsString() if p else "?"
            fn = s.Family.Name if s.Family else "?"
            self._sym.append(Wrapper(s, "{} : {}".format(fn, sn)))
        self._sym = sorted(self._sym, key=lambda x: x.Name)
        self.TurningRadiusSymbolCmb.ItemsSource = self._sym

        for i, w in enumerate(self._sym):
            if w.element is not None and getattr(w.element, "Family", None) and w.element.Family.Name == TARGET_FAMILY:
                target_sym_idx = i
                break
        self._target_sym_idx = target_sym_idx

        # Restaurar config
        cfg = _load_cfg()
        self.WidthTxt.Text = str(cfg.get("width", "120"))
        self.IncludeEndRadiusChk.IsChecked = (cfg.get("include_radius", "False") == "True")
        self.LineStyleCmb.SelectedIndex = next(
            (i for i, v in enumerate(self._ls) if v.Name == cfg.get("line_style", "")), 0)
        self.FilledRegionTypeCmb.SelectedIndex = next(
            (i for i, v in enumerate(self._fr) if v.Name == cfg.get("fr_type", "")), 0)
        self.TurningRadiusSymbolCmb.SelectedIndex = self._target_sym_idx
        self._refresh_btn()

    def _refresh_btn(self):
        n = len(self.selected_lines)
        self.btn_select.Content = ("Seleccionar Líneas de Ruta" if n == 0
                                   else "Seleccionadas: {} línea(s)".format(n))

    def _save_cfg_ui(self):
        try:
            import json as _json
            _data = {
                "width": self.WidthTxt.Text,
                "include_radius": str(self.IncludeEndRadiusChk.IsChecked == True)
            }
            if self.LineStyleCmb.SelectedItem:
                _data["line_style"] = self.LineStyleCmb.SelectedItem.Name
            if self.FilledRegionTypeCmb.SelectedItem:
                _data["fr_type"] = self.FilledRegionTypeCmb.SelectedItem.Name
            if self.TurningRadiusSymbolCmb.SelectedItem:
                _data["symbol"] = self.TurningRadiusSymbolCmb.SelectedItem.Name
            with open(self._cfg_path, "w") as _f:
                _json.dump(_data, _f)
        except Exception:
            pass

    def Btn_Select_Click(self, sender, e):
        # Guardar estado actual antes de cerrar para que se restaure al reabrir
        self._save_cfg_ui()
        self.action = "select"
        self.Close()

    def GenerateBtn_Click(self, sender, e):
        try:
            self.r_width = float(self.WidthTxt.Text.replace(",", "."))
            self.r_ls         = self.LineStyleCmb.SelectedItem.element if self.LineStyleCmb.SelectedItem else None
            self.r_fr         = self.FilledRegionTypeCmb.SelectedItem.element if self.FilledRegionTypeCmb.SelectedItem else None
            sw                = self.TurningRadiusSymbolCmb.SelectedItem
            self.r_sym        = sw.element if sw else None
            self.r_use_radius = (self.IncludeEndRadiusChk.IsChecked == True)
            self.action       = "generate"
            self._save_cfg_ui()
            self.Close()
        except ValueError:
            pass
        except Exception:
            pass

    def PreviewBtn_Click(self, sender, e):
        try:
            self.r_width     = float(self.WidthTxt.Text.replace(",", "."))
            self.r_ls        = self.LineStyleCmb.SelectedItem.element if self.LineStyleCmb.SelectedItem else None
            self.r_fr        = self.FilledRegionTypeCmb.SelectedItem.element if self.FilledRegionTypeCmb.SelectedItem else None
            sw               = self.TurningRadiusSymbolCmb.SelectedItem
            self.r_sym       = sw.element if sw else None
            self.r_use_radius = (self.IncludeEndRadiusChk.IsChecked == True)
            self.action      = "preview"
            self._save_cfg_ui()
            self.Close()
        except ValueError:
            pass
        except Exception:
            pass

    def CancelBtn_Click(self, sender, e):
        self.action = "cancel"
        self.Close()


# ─── Helpers de preview ───────────────────────────────────────────
_preview_ids = []

def _delete_preview(document):
    global _preview_ids
    if not _preview_ids:
        return
    with DB.Transaction(document, "Limpiar preview ruta") as t:
        t.Start()
        for eid in list(_preview_ids):
            try:
                if document.GetElement(eid) is not None:
                    document.Delete(eid)
            except Exception:
                pass
        t.Commit()
    _preview_ids = []


def _build_geometry(lines, view_z, half_w, use_rad_ends):
    """Construye loops y clasifica nodos. Devuelve (loops, intersections, free_ends)."""
    curves = []
    for ln in lines:
        try:
            gc = ln.GeometryCurve
            if isinstance(gc, DB.Arc):
                s   = _flat(gc.GetEndPoint(0), view_z)
                e   = _flat(gc.GetEndPoint(1), view_z)
                mid = _flat(gc.Evaluate(0.5, True), view_z)
                if s.DistanceTo(e) > 1e-6:
                    curves.append(DB.Arc.Create(s, e, mid))
            elif isinstance(gc, (DB.HermiteSpline, DB.NurbSpline)):
                curves.append(gc)
            else:
                s = _flat(gc.GetEndPoint(0), view_z)
                e = _flat(gc.GetEndPoint(1), view_z)
                if s.DistanceTo(e) > 1e-6:
                    curves.append(DB.Line.CreateBound(s, e))
        except Exception:
            pass

    if not curves:
        return None, [], []

    curves = split_at_midpoints(curves, view_z)

    node_map = {}
    for c in curves:
        for at_start in (True, False):
            pt = _flat(c.GetEndPoint(0 if at_start else 1), view_z)
            k  = _key(pt)
            if k not in node_map:
                node_map[k] = []
            node_map[k].append((c, at_start))

    intersections = []
    free_ends     = []
    for k, members in node_map.items():
        pt = DB.XYZ(k[0], k[1], view_z)
        if len(members) == 1:
            c, at_start = members[0]
            tan = _curve_tangent(c, at_start, view_z)
            if at_start:
                tan = DB.XYZ(-tan.X, -tan.Y, 0.0)
            free_ends.append((pt, tan))
        elif len(members) == 2:
            (c1, s1), (c2, s2) = members[0], members[1]
            if not _is_tangent_junction(c1, s1, c2, s2, view_z):
                intersections.append(pt)
        else:
            intersections.append(pt)

    loops = []
    for c in curves:
        try:
            loops.append(loop_for_curve(c, half_w, view_z))
        except Exception:
            pass
    for pt in intersections:
        try:
            loops.append(loop_circle(pt, TURN_RADIUS_FT, view_z))
        except Exception:
            pass
    for pt, d in free_ends:
        try:
            if use_rad_ends:
                loops.append(loop_circle(pt, TURN_RADIUS_FT, view_z))
            else:
                loops.append(loop_arrow(pt, d, half_w, view_z))
        except Exception:
            pass

    return loops, intersections, free_ends


def _loops_to_region_sets(loops):
    """Union booleana de loops → lista de CurveLoop sets para FilledRegion."""
    extr_dir = DB.XYZ(0, 0, 1)
    solids = []
    for lp in loops:
        try:
            lp_list = List[DB.CurveLoop]()
            lp_list.Add(lp)
            solids.append(DB.GeometryCreationUtilities.CreateExtrusionGeometry(lp_list, extr_dir, 1.0))
        except Exception:
            pass

    if not solids:
        return []

    merged = [solids[0]]
    for s in solids[1:]:
        joined = False
        for i in range(len(merged)):
            try:
                merged[i] = DB.BooleanOperationsUtils.ExecuteBooleanOperation(
                    merged[i], s, DB.BooleanOperationsType.Union)
                joined = True
                break
            except Exception:
                pass
        if not joined:
            merged.append(s)

    def reproject_loop(edge_loop, z):
        new_lp = DB.CurveLoop()
        try:
            for curve in edge_loop:
                p0 = DB.XYZ(curve.GetEndPoint(0).X, curve.GetEndPoint(0).Y, z)
                p1 = DB.XYZ(curve.GetEndPoint(1).X, curve.GetEndPoint(1).Y, z)
                if isinstance(curve, DB.Line):
                    new_lp.Append(DB.Line.CreateBound(p0, p1))
                elif isinstance(curve, DB.Arc):
                    mid = curve.Evaluate(0.5, True)
                    new_lp.Append(DB.Arc.Create(p0, p1, DB.XYZ(mid.X, mid.Y, z)))
                else:
                    new_lp.Append(DB.Line.CreateBound(p0, p1))
        except Exception:
            return None
        return new_lp

    z = loops[0].GetCurveLoopIterator().Current.GetEndPoint(0).Z if loops else 0.0
    region_loop_sets = []
    for solid in merged:
        lp_set = List[DB.CurveLoop]()
        for face in solid.Faces:
            if isinstance(face, DB.PlanarFace) and face.FaceNormal.Z < -0.9:
                for edge_loop in face.GetEdgesAsCurveLoops():
                    reprojected = reproject_loop(edge_loop, z)
                    if reprojected is not None:
                        lp_set.Add(reprojected)
        if lp_set.Count > 0:
            region_loop_sets.append(lp_set)

    if not region_loop_sets:
        for lp in loops:
            s = List[DB.CurveLoop]()
            s.Add(lp)
            region_loop_sets.append(s)

    return region_loop_sets


def _do_generate(document, active_view, lines, win_params, is_preview=False):
    """
    Crea las FilledRegions (y símbolos) en el documento.
    Devuelve lista de ElementId de los elementos creados.
    is_preview=True aplica halftone y omite el grupo de líneas.
    """
    half_w       = win_params.r_width * CM_TO_FT / 2.0
    fr_type      = win_params.r_fr
    line_style   = win_params.r_ls
    fam_sym      = win_params.r_sym
    use_rad_ends = win_params.r_use_radius

    view_z = 0.0
    try:
        level = active_view.GenLevel
        view_z = level.Elevation if level else active_view.Origin.Z
    except Exception:
        pass

    loops, intersections, free_ends = _build_geometry(lines, view_z, half_w, use_rad_ends)
    if not loops:
        return []

    region_loop_sets = _loops_to_region_sets(loops)
    if not region_loop_sets:
        return []

    view_id   = active_view.Id
    created   = []
    tx_name   = "Preview Ruta Accesible" if is_preview else "Generar Ruta Accesible"

    with DB.Transaction(document, tx_name) as t:
        t.Start()

        if fam_sym:
            try:
                if not fam_sym.IsActive:
                    fam_sym.Activate()
                    document.Regenerate()
            except Exception:
                pass

        for idx, lp_set in enumerate(region_loop_sets):
            try:
                fr = DB.FilledRegion.Create(document, fr_type.Id, view_id, lp_set)
                if line_style:
                    try:
                        fr.SetLineStyleId(line_style.Id)
                    except Exception:
                        pass
                if is_preview:
                    try:
                        ogs = DB.OverrideGraphicSettings()
                        ogs.SetHalftone(True)
                        active_view.SetElementOverrides(fr.Id, ogs)
                    except Exception:
                        pass
                created.append(fr.Id)
            except Exception:
                pass

        if fam_sym:
            sym_pts = list(intersections)
            if use_rad_ends:
                sym_pts += [pt for pt, _ in free_ends]
            for pt in sym_pts:
                sym_el = _place_sym_ret(document, active_view, pt, fam_sym)
                if sym_el:
                    if is_preview:
                        try:
                            ogs = DB.OverrideGraphicSettings()
                            ogs.SetHalftone(True)
                            active_view.SetElementOverrides(sym_el.Id, ogs)
                        except Exception:
                            pass
                    created.append(sym_el.Id)

        if not is_preview and created:
            try:
                view_name = active_view.Name
                base_name = "ARCH_Ruta Accesible-{}-".format(view_name)
                existing_names = []
                for _g in DB.FilteredElementCollector(document).OfClass(DB.GroupType):
                    try:
                        _n = _g.Name
                        if _n and _n.startswith(base_name):
                            existing_names.append(_n)
                    except Exception:
                        pass
                grp_idx = 1
                while "{}{:02d}".format(base_name, grp_idx) in existing_names:
                    grp_idx += 1
                potential_name = "{}{:02d}".format(base_name, grp_idx)

                line_ids = List[DB.ElementId]()
                for ln in lines:
                    try:
                        if document.GetElement(ln.Id) is not None:
                            line_ids.Add(ln.Id)
                    except Exception:
                        pass
                if line_ids.Count > 0:
                    new_group = document.Create.NewGroup(line_ids)
                    if new_group:
                        document.Regenerate()
                        gt = new_group.GroupType
                        try:
                            gt.Name = potential_name
                        except Exception:
                            p = gt.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_NAME)
                            if p and not p.IsReadOnly:
                                p.Set(potential_name)
            except Exception:
                pass

        t.Commit()

    return created


# ─── Main ─────────────────────────────────────────────────────────
def main():
    global _preview_ids

    pre = []
    for eid in revit.get_selection().element_ids:
        el = doc.GetElement(eid)
        if isinstance(el, (DB.DetailLine, DB.DetailArc, DB.DetailCurve,
                            DB.ModelLine, DB.ModelArc, DB.ModelCurve,
                            DB.CurveElement)):
            pre.append(el)

    win = RutaWindow(_XAML_PATH, pre)
    win.ShowDialog()

    while win.action in ("select", "preview"):
        _delete_preview(doc)
        if win.action == "preview":
            if win.selected_lines and win.r_fr:
                _preview_ids = _do_generate(doc, doc.ActiveView, win.selected_lines, win, is_preview=True)
                uidoc.UpdateAllOpenViews()
            else:
                if not win.selected_lines:
                    forms.alert("Selecciona líneas antes de previsualizar.", title="Sin selección")
                elif not win.r_fr:
                    forms.alert("Selecciona un Tipo de Región de Relleno.", title="Falta tipo")
        elif win.action == "select":
            try:
                refs = uidoc.Selection.PickObjects(
                    UI.Selection.ObjectType.Element, LineFilter(),
                    "Seleccione líneas → ENTER para confirmar")
                pre = [doc.GetElement(r.ElementId) for r in refs]
            except Exception:
                pass
        win = RutaWindow(_XAML_PATH, pre)
        win.ShowDialog()

    _delete_preview(doc)

    if win.action != "generate":
        return

    if not win.selected_lines:
        forms.alert("No hay líneas seleccionadas.", title="Sin selección")
        return
    if win.r_fr is None:
        forms.alert("Seleccione un Tipo de Región de Relleno.", title="Falta tipo")
        return

    created = _do_generate(doc, doc.ActiveView, win.selected_lines, win, is_preview=False)
    msg = "Regiones creadas: {}".format(len(created))
    forms.alert(msg, title="Ruta Accesible")


def _place_sym_ret(document, active_view, pt, fam_sym):
    try:
        ptype = fam_sym.Family.FamilyPlacementType
        if ptype == DB.FamilyPlacementType.ViewBased:
            return document.Create.NewFamilyInstance(pt, fam_sym, active_view)
        else:
            level = active_view.GenLevel
            if level:
                return document.Create.NewFamilyInstance(pt, fam_sym, level, DB.Structure.StructuralType.NonStructural)
            else:
                return document.Create.NewFamilyInstance(pt, fam_sym, DB.Structure.StructuralType.NonStructural)
    except Exception:
        return None


if __name__ == "__main__":
    main()
