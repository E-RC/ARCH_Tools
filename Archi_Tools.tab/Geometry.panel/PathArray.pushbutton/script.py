# -*- coding: utf-8 -*-
"""
Path Array - ARCHIPLAN
Clona y alinea elementos a lo largo de un recorrido de lineas o curvas.

FLUJO:
  1. El usuario selecciona el elemento host ANTES de que aparezca la UI.
  2. El usuario selecciona las curvas del recorrido ANTES de que aparezca la UI.
  3. La UI permite re-seleccionar cada uno (cierra -> selecciona -> reabre).
  4. El usuario configura los parametros y ejecuta el array.
"""
import math
import os
import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
import System.Windows

from pyrevit import revit, DB, UI, forms, script
from Autodesk.Revit.DB.Structure import StructuralType

logger = script.get_logger()

# Estado global compartido entre reaperturas de la ventana
_state = {
    "host":    None,
    "curves":  [],
    "action":  None,  # "execute" | "reselect_host" | "reselect_path" | "cancel"
    "params":  None,
}


# ──────────────────────────────────────────────────────────────────────────────
# SELECCION EN REVIT (hilo principal, fuera de la UI)
# ──────────────────────────────────────────────────────────────────────────────

def pick_host():
    try:
        ref = revit.uidoc.Selection.PickObject(
            UI.Selection.ObjectType.Element,
            "Selecciona el elemento HOST a replicar (ESC para cancelar)"
        )
        elem = revit.doc.GetElement(ref)
        if isinstance(elem, DB.FamilyInstance):
            return elem
        else:
            forms.alert("Selecciona un FamilyInstance (mobiliario, pilar, vegetacion, etc.)", title="Path Array")
            return None
    except Exception:
        return None


def pick_path():
    try:
        refs = revit.uidoc.Selection.PickObjects(
            UI.Selection.ObjectType.Element,
            "Selecciona las lineas del recorrido (finaliza con clic derecho o barra de opciones)"
        )
        curves = []
        for ref in refs:
            try:
                elem = revit.doc.GetElement(ref)
                if hasattr(elem, "GeometryCurve") and elem.GeometryCurve:
                    curves.append(elem.GeometryCurve)
                elif hasattr(elem, "Location") and isinstance(elem.Location, DB.LocationCurve):
                    curves.append(elem.Location.Curve)
            except Exception as ex:
                logger.debug("Curva no extraible: {}".format(ex))
        if not curves:
            forms.alert("Ninguno de los elementos tiene curva valida.\nUsa Detail Lines o Model Lines.", title="Path Array")
        return curves
    except Exception:
        return []


# ──────────────────────────────────────────────────────────────────────────────
# VENTANA WPF
# ──────────────────────────────────────────────────────────────────────────────

class PathArrayWindow(forms.WPFWindow):
    def __init__(self, xaml_file_name):
        forms.WPFWindow.__init__(self, xaml_file_name)

        # Cargar configuracion anterior
        self.config = script.get_config()
        self.txtDistance.Text = self.config.get_option("distance", "1.0")
        self.txtCount.Text = self.config.get_option("count", "10")
        self.chkAlignTangential.IsChecked = self.config.get_option("align", True)
        if self.config.get_option("is_count", False):
            self.optCount.IsChecked = True
        else:
            self.optDistance.IsChecked = True

        self._refresh_status()

        self.btnSelectHost.Click   += self._on_select_host
        self.btnSelectPath.Click   += self._on_select_path
        self.btnExecute.Click      += self._on_execute
        self.btnCancel.Click       += self._on_cancel
        self.txtCount.TextChanged  += self._on_param_changed
        self.optCount.Checked      += self._on_param_changed
        self.optDistance.Checked   += self._on_param_changed

    def _m_to_ft(self, m):
        try:
            return DB.UnitUtils.ConvertToInternalUnits(m, DB.UnitTypeId.Meters)
        except AttributeError:
            return DB.UnitUtils.ConvertToInternalUnits(m, DB.DisplayUnitType.DUT_METERS)

    def _ft_to_m(self, ft):
        try:
            return DB.UnitUtils.ConvertFromInternalUnits(ft, DB.UnitTypeId.Meters)
        except AttributeError:
            return DB.UnitUtils.ConvertFromInternalUnits(ft, DB.DisplayUnitType.DUT_METERS)

    def _refresh_status(self):
        green = System.Windows.Media.Brushes.ForestGreen
        gray  = System.Windows.Media.Brushes.Gray
        Collapsed = System.Windows.Visibility.Collapsed
        Visible   = System.Windows.Visibility.Visible

        host = _state["host"]
        if host:
            self.lblHostStatus.Text       = u"\u2714  {} (ID: {})".format(host.Name, host.Id.IntegerValue)
            self.lblHostStatus.Foreground = green
        else:
            self.lblHostStatus.Text       = u"\u2014  Sin seleccionar"
            self.lblHostStatus.Foreground = gray

        curves = _state["curves"]
        if curves:
            lengths   = [c.Length for c in curves]
            total_ft  = sum(lengths)
            total_m   = self._ft_to_m(total_ft)
            self.lblPathStatus.Text       = u"\u2714  {} segmento(s) seleccionado(s)".format(len(curves))
            self.lblPathStatus.Foreground = green
            # Panel azul con longitud total
            self.lblTotalLength.Text = u"Largo total del recorrido: {:.2f} m ({:.2f} ft)".format(total_m, total_ft)
            self.pnlPathInfo.Visibility = Visible
            # Guardar longitudes en estado para reuso
            _state["curve_lengths"] = lengths
            _state["total_ft"]      = total_ft
        else:
            self.lblPathStatus.Text       = u"\u2014  Sin seleccionar"
            self.lblPathStatus.Foreground = gray
            self.pnlPathInfo.Visibility = Collapsed

        self._update_dist_info()

    def _update_dist_info(self):
        """Actualiza el panel verde con la distancia calculada segun los parametros actuales."""
        Collapsed = System.Windows.Visibility.Collapsed
        Visible   = System.Windows.Visibility.Visible

        total_ft = _state.get("total_ft", 0)
        if not total_ft or not _state.get("curves"):
            self.pnlDistInfo.Visibility = Collapsed
            return

        try:
            is_count = bool(self.optCount.IsChecked)
            if is_count:
                count = int(self.txtCount.Text)
                if count < 2:
                    self.pnlDistInfo.Visibility = Collapsed
                    return
                spacing_ft = total_ft / float(count - 1)
                spacing_m  = self._ft_to_m(spacing_ft)
                total_m    = self._ft_to_m(total_ft)
                self.lblDistInfo.Text = (
                    u"{} copias  \u2022  Separacion: {:.3f} m  \u2022  Recorrido: {:.2f} m\n"
                    u"Primero en el inicio  \u2192  Ultimo en el final del recorrido"
                ).format(count, spacing_m, total_m)
                self.pnlDistInfo.Visibility = Visible
            else:
                self.pnlDistInfo.Visibility = Collapsed
        except Exception:
            self.pnlDistInfo.Visibility = Collapsed

    def _on_param_changed(self, sender, e):
        """Actualiza el panel de diagnostico cuando cambian los parametros."""
        self._update_dist_info()

    def _on_select_host(self, sender, e):
        _state["action"] = "reselect_host"
        self.Close()

    def _on_select_path(self, sender, e):
        _state["action"] = "reselect_path"
        self.Close()

    def _on_execute(self, sender, e):
        if not _state["host"]:
            forms.alert("Selecciona primero el elemento host.", title="Path Array")
            return
        if not _state["curves"]:
            forms.alert("Selecciona primero el recorrido.", title="Path Array")
            return

        is_count = bool(self.optCount.IsChecked)
        align    = bool(self.chkAlignTangential.IsChecked)

        if is_count:
            try:
                count = int(self.txtCount.Text)
                if count < 2:
                    raise ValueError()
            except Exception:
                forms.alert("La cantidad debe ser un entero >= 2.", title="Path Array")
                return
            # Reutilizar longitudes ya calculadas en _refresh_status (mismos valores, sin nuevo drift)
            lengths   = _state.get("curve_lengths") or [c.Length for c in _state["curves"]]
            total_len = sum(lengths)
            # Posiciones absolutas: 0 → total_len exacto
            positions = [i * total_len / float(count - 1) for i in range(count)]
            positions[-1] = total_len  # ultimo siempre en el extremo exacto
            spacing_m = self._ft_to_m(total_len / float(count - 1))
            print("  Count mode: {} copias, separacion={:.4f} m, total={:.4f} ft".format(
                count, spacing_m, total_len))
            print("  Pos[0]={:.6f}  Pos[-1]={:.6f}  total_len={:.6f}".format(
                positions[0], positions[-1], total_len))
            _state["params"] = {
                "mode":      "count",
                "positions": positions,
                "lengths":   lengths,
                "align":     align,
            }
        else:
            try:
                dist_m = float(self.txtDistance.Text.replace(",", "."))
                if dist_m <= 0:
                    raise ValueError()
            except Exception:
                forms.alert("La distancia debe ser un numero decimal positivo.", title="Path Array")
                return
            try:
                distance_ft = DB.UnitUtils.ConvertToInternalUnits(dist_m, DB.UnitTypeId.Meters)
            except AttributeError:
                distance_ft = DB.UnitUtils.ConvertToInternalUnits(dist_m, DB.DisplayUnitType.DUT_METERS)
            _state["params"] = {
                "mode":        "distance",
                "distance_ft": distance_ft,
                "align":       align,
            }
        # Guardar configuracion
        self.config.distance = self.txtDistance.Text
        self.config.count = self.txtCount.Text
        self.config.align = align
        self.config.is_count = is_count
        script.save_config()

        _state["action"] = "execute"
        self.Close()

    def _on_cancel(self, sender, e):
        _state["action"] = "cancel"
        self.Close()


def show_window():
    """Muestra la ventana de forma bloqueante usando ShowDialog() nativo de WPF."""
    xaml_path = os.path.join(os.path.dirname(__file__), "ui.xaml")
    win = PathArrayWindow(xaml_path)
    # ShowDialog() es el metodo WPF nativo que bloquea hasta que la ventana se cierra
    win.ShowDialog()


# ──────────────────────────────────────────────────────────────────────────────
# LOGICA DEL ARRAY
# ──────────────────────────────────────────────────────────────────────────────

SAMPLES_PER_CURVE = 500  # mayor = mas precision en curvas complejas


def sort_curves(curves, tol=1e-6):
    """
    Ordena y ORIENTA las curvas en una cadena continua.

    El usuario puede seleccionar los segmentos en cualquier orden y cada
    curva puede estar dibujada en sentido inverso. Esta funcion los encadena
    emparejando extremos (con tolerancia) y revierte (CreateReversed) las que
    vengan al reves, de modo que GetEndPoint(1) de cada curva coincida con
    GetEndPoint(0) de la siguiente. Asi el extremo final de la cadena es el
    extremo VISUAL real del recorrido (y el ultimo elemento cae ahi).

    Devuelve (cadena_ordenada, n_segmentos_no_conectados).
    """
    remaining = list(curves)
    if not remaining:
        return [], 0
    chain = [remaining.pop(0)]

    progress = True
    while remaining and progress:
        progress  = False
        end_pt    = chain[-1].GetEndPoint(1)
        start_pt  = chain[0].GetEndPoint(0)
        for i, c in enumerate(remaining):
            c0, c1 = c.GetEndPoint(0), c.GetEndPoint(1)
            # Anexar al final de la cadena
            if c0.IsAlmostEqualTo(end_pt, tol):
                chain.append(remaining.pop(i)); progress = True; break
            if c1.IsAlmostEqualTo(end_pt, tol):
                chain.append(remaining.pop(i).CreateReversed()); progress = True; break
            # Anteponer al inicio de la cadena
            if c1.IsAlmostEqualTo(start_pt, tol):
                chain.insert(0, remaining.pop(i)); progress = True; break
            if c0.IsAlmostEqualTo(start_pt, tol):
                chain.insert(0, remaining.pop(i).CreateReversed()); progress = True; break

    return chain, len(remaining)


def build_path_table(curves):
    """
    Construye una tabla de muestreo sin entradas duplicadas en las uniones.
    El punto de INICIO de cada curva (excepto la primera) NO se agrega porque
    ya existe como el punto FINAL de la curva anterior.
    """
    table = []
    total = 0.0
    n     = SAMPLES_PER_CURVE

    for cidx, curve in enumerate(curves):
        start_pt = curve.Evaluate(0.0, True)

        # Solo agregar el punto de inicio para la primera curva.
        # Para las siguientes, el inicio ya fue agregado como el final de la anterior.
        if cidx == 0:
            table.append((total, cidx, 0.0, start_pt))

        prev_pt = start_pt

        for s in range(1, n + 1):
            norm    = s / float(n)
            pt      = curve.Evaluate(norm, True)
            seg_len = prev_pt.DistanceTo(pt)
            total  += seg_len
            table.append((total, cidx, norm, pt))
            prev_pt = pt

    return table


def sample_at_arc_length(table, curves, target_len):
    """
    Devuelve (punto, tangente) para una posicion de arco dada.
    Usa busqueda binaria en la tabla de teselacion.
    CRITICO: cuando lo y hi pertenecen a curvas distintas (union de segmentos),
    NO interpola entre parametros de curvas diferentes (producian posiciones erroneas).
    En ese caso usa el extremo mas cercano al target.
    """
    if target_len <= 0.0:
        _, cidx, norm, _ = table[0]
        d = curves[cidx].ComputeDerivatives(norm, True)
        return d.Origin, d.BasisX.Normalize()

    if target_len >= table[-1][0]:
        _, cidx, norm, _ = table[-1]
        d = curves[cidx].ComputeDerivatives(norm, True)
        return d.Origin, d.BasisX.Normalize()

    # Busqueda binaria
    lo, hi = 0, len(table) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if table[mid][0] <= target_len:
            lo = mid
        else:
            hi = mid

    l0, cidx0, n0, _ = table[lo]
    l1, cidx1, n1, _ = table[hi]

    if cidx0 != cidx1:
        # Union entre dos curvas: no interpolar entre parametros de curvas distintas.
        # Elegir el extremo mas cercano al target.
        span = l1 - l0
        t    = (target_len - l0) / span if span > 1e-12 else 0.0
        if t < 0.5:
            curve, norm = curves[cidx0], n0  # extremo final de curva anterior
        else:
            curve, norm = curves[cidx1], n1  # extremo inicial de curva siguiente
    else:
        # Mismo segmento: interpolacion lineal normal
        span = l1 - l0
        t    = (target_len - l0) / span if span > 1e-12 else 0.0
        curve = curves[cidx0]
        norm  = n0 + t * (n1 - n0)

    d = curve.ComputeDerivatives(min(max(norm, 0.0), 1.0), True)
    return d.Origin, d.BasisX.Normalize()


def run_array():
    host_elem = _state["host"]
    params    = _state["params"]
    align     = params["align"]
    mode      = params["mode"]

    # ── 0. Ordenar y orientar las curvas en una cadena continua ───────────────
    # Sin esto, con varios segmentos el "final" del recorrido depende del orden
    # de seleccion y de la direccion en que se dibujo cada linea, y el ultimo
    # elemento no cae en el extremo real del path (el "delta" reportado).
    curves, n_unlinked = sort_curves(_state["curves"])
    if not curves:
        forms.alert("No se pudo construir el recorrido a partir de las curvas seleccionadas.",
                    title="Path Array")
        return
    if n_unlinked > 0:
        forms.alert(
            u"{} segmento(s) no se pudieron conectar al recorrido y fueron ignorados.\n"
            u"Verifica que las lineas formen una cadena continua (extremos coincidentes).".format(
                n_unlinked),
            title="Path Array")

    # ── 1. Construir tabla de teselacion (longitud de arco real) ──────────────
    print("ARCH Path Array: construyendo tabla ({} curvas x {} muestras)...".format(
        len(curves), SAMPLES_PER_CURVE))
    path_table   = build_path_table(curves)
    total_arc_ft = path_table[-1][0]

    try:
        total_arc_m = DB.UnitUtils.ConvertFromInternalUnits(total_arc_ft, DB.UnitTypeId.Meters)
    except AttributeError:
        total_arc_m = DB.UnitUtils.ConvertFromInternalUnits(total_arc_ft, DB.DisplayUnitType.DUT_METERS)

    print("  Longitud total (arco real): {:.4f} ft / {:.4f} m".format(total_arc_ft, total_arc_m))

    # ── 2. Generar lista de posiciones absolutas (en longitud de arco real) ───
    if mode == "count":
        count = len(params["positions"])
        abs_positions = [i * total_arc_ft / float(count - 1) for i in range(count)]
        abs_positions[-1] = total_arc_ft
        try:
            spacing_m = DB.UnitUtils.ConvertFromInternalUnits(
                total_arc_ft / float(count - 1), DB.UnitTypeId.Meters)
        except AttributeError:
            spacing_m = DB.UnitUtils.ConvertFromInternalUnits(
                total_arc_ft / float(count - 1), DB.DisplayUnitType.DUT_METERS)
        print("  Modo CANTIDAD: {} copias, separacion real = {:.4f} m".format(count, spacing_m))
    else:
        distance_ft   = params["distance_ft"]
        abs_positions = []
        t = 0.0
        while t <= total_arc_ft + 1e-9:
            abs_positions.append(min(t, total_arc_ft))
            t += distance_ft

    print("  Insertar: {} posiciones | primera={:.4f} ultima={:.4f} ft".format(
        len(abs_positions), abs_positions[0], abs_positions[-1]))

    # ── 3. Insertar elementos usando la tabla de teselacion ───────────────────
    family_symbol = host_elem.Symbol
    host_level_id = host_elem.LevelId
    host_level    = revit.doc.GetElement(host_level_id) \
                    if host_level_id != DB.ElementId.InvalidElementId else None

    count_created = 0

    with revit.Transaction("ARCH - Path Array"):
        if not family_symbol.IsActive:
            family_symbol.Activate()
            revit.doc.Regenerate()

        prev_tangent  = None
        host_consumed = False  # True cuando el host ya fue movido a posicion 0

        for i, abs_t in enumerate(abs_positions):
            try:
                point, tangent = sample_at_arc_length(path_table, curves, abs_t)

                # Correccion de inversion de 180° entre segmentos
                if prev_tangent is not None and tangent.DotProduct(prev_tangent) < 0:
                    tangent = tangent.Negate()
                prev_tangent = tangent

                # Posicion 0: mover el host en vez de crear uno nuevo (evita duplicado)
                if i == 0 and not host_consumed:
                    host_consumed = True
                    count_created += 1
                    # Mover host a la posicion 0
                    host_loc = host_elem.Location
                    if hasattr(host_loc, "Point"):
                        delta = point - host_loc.Point
                        DB.ElementTransformUtils.MoveElement(
                            revit.doc, host_elem.Id, delta)
                    # Rotar host si corresponde
                    if align:
                        angle = math.atan2(tangent.Y, tangent.X)
                        axis  = DB.Line.CreateBound(point, point + DB.XYZ.BasisZ)
                        DB.ElementTransformUtils.RotateElement(
                            revit.doc, host_elem.Id, axis, angle)
                    continue

                # Resto de posiciones: crear nuevas instancias
                if host_level:
                    inst = revit.doc.Create.NewFamilyInstance(
                        point, family_symbol, host_level, StructuralType.NonStructural)
                else:
                    inst = revit.doc.Create.NewFamilyInstance(
                        point, family_symbol, StructuralType.NonStructural)

                if not inst:
                    print("  Advertencia: instancia nula en t={:.3f}".format(abs_t))
                    continue

                count_created += 1

                if align:
                    angle = math.atan2(tangent.Y, tangent.X)
                    axis  = DB.Line.CreateBound(point, point + DB.XYZ.BasisZ)
                    DB.ElementTransformUtils.RotateElement(
                        revit.doc, inst.Id, axis, angle)

            except Exception as ex:
                print("  Error en t={:.3f}: {}".format(abs_t, ex))

    print("ARCH Path Array: {} elementos creados.".format(count_created))

    if count_created > 0:
        forms.toast("{} elementos generados".format(count_created), title="Path Array")
    else:
        forms.alert("No se generaron elementos.\nRevisa la consola de pyRevit.", title="Path Array")


# ──────────────────────────────────────────────────────────────────────────────
# PUNTO DE ENTRADA - BUCLE PRINCIPAL
# ──────────────────────────────────────────────────────────────────────────────

# Seleccion inicial antes de abrir la UI
_state["host"]   = pick_host()
if _state["host"]:
    _state["curves"] = pick_path()

# Bucle: la ventana puede cerrarse para re-seleccionar y luego reabrirse
while True:
    _state["action"] = None
    show_window()

    action = _state["action"]

    if action == "execute":
        run_array()
        break
    elif action == "reselect_host":
        new_host = pick_host()
        if new_host:
            _state["host"] = new_host
        # Si ESC, conserva el host anterior y reabre la ventana
    elif action == "reselect_path":
        new_path = pick_path()
        if new_path:
            _state["curves"] = new_path
        # Si ESC, conserva el path anterior y reabre la ventana
    else:
        # "cancel" o ventana cerrada con X
        break
