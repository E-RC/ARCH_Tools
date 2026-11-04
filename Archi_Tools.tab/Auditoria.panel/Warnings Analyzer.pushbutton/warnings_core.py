# -*- coding: utf-8 -*-
"""Lado Revit del Warnings Analyzer: leer los warnings y actuar sobre el modelo.

Todo lo de este modulo corre dentro de un ExternalEvent (contexto valido de la
API). Cada accion recibe `(uiapp, payload)` y devuelve datos serializables a
JSON. La clasificacion de los warnings (prioridad, grupo, como resolverlos)
vive en el lado web (`web/rules.js`): aqui solo hay datos crudos y acciones.

IronPython 2.7: sin f-strings.
"""
import datetime
import hashlib
import io
import json
import os
import re

import System
from System.Collections.Generic import List

from Autodesk.Revit import DB

INVALID_ID = DB.ElementId.InvalidElementId

DATA_DIR = os.path.join(os.environ.get("APPDATA", ""), "ARCHIPLAN",
                        "WarningsAnalyzer")
HISTORY_LIMIT = 365          # un registro por dia, un anio de tendencia
BOX_MARGIN_FT = 3.0          # holgura de la caja de seccion (~0,9 m)
LINE_TOL_FT = 0.003          # ~1 mm para decidir si una linea esta contenida
VIEW3D_PREFIX = "WA - Warnings 3D"

PRIORITY_COLORS = {
    "high": DB.Color(192, 57, 43),
    "medium": DB.Color(230, 145, 56),
    "low": DB.Color(214, 196, 74),
}
def _rgb(color):
    # Los canales son System.Byte: sin int() no hashean igual dentro de un set.
    return (int(color.Red), int(color.Green), int(color.Blue))


_PRIORITY_RGB = set(_rgb(c) for c in PRIORITY_COLORS.values())


# --- Utilidades ---------------------------------------------------------------

def id_value(eid):
    """Entero de un ElementId en Revit 2024+ (Value) y anteriores."""
    value = getattr(eid, "Value", None)
    return int(value) if value is not None else eid.IntegerValue


def to_eid(number):
    try:
        return DB.ElementId(System.Int64(number))
    except Exception:
        return DB.ElementId(int(number))


def _safe(fn, default=""):
    try:
        value = fn()
        return default if value is None else value
    except Exception:
        return default


def _doc(uiapp):
    uidoc = uiapp.ActiveUIDocument
    if uidoc is None:
        raise ValueError("No hay un documento activo en Revit.")
    return uidoc, uidoc.Document


def _existing_ids(doc, numbers):
    """ElementIds que siguen existiendo en el modelo (el resto se ignora)."""
    ids = List[DB.ElementId]()
    for number in numbers or []:
        eid = to_eid(number)
        if doc.GetElement(eid) is not None:
            ids.Add(eid)
    if ids.Count == 0:
        raise ValueError("Ninguno de esos elementos existe ya en el modelo. "
                         "Pulsa Actualizar.")
    return ids


def _run_in_transaction(doc, name, fn):
    t = DB.Transaction(doc, name)
    t.Start()
    try:
        result = fn()
        # Commit no lanza si Revit no pudo resolver un fallo: devuelve
        # RolledBack. Sin esto se reportaria exito sin haber cambiado nada.
        if t.Commit() != DB.TransactionStatus.Committed:
            raise RuntimeError(u"Revit canceló la operación «{0}».".format(name))
        return result
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        raise


# --- Lectura de warnings ------------------------------------------------------

def _element_name(el):
    name = _safe(lambda: DB.Element.Name.GetValue(el))
    family = _safe(lambda: el.Symbol.FamilyName)
    if not family:
        type_el = _safe(lambda: el.Document.GetElement(el.GetTypeId()), None)
        family = _safe(lambda: type_el.FamilyName) if type_el else ""
    number = _safe(lambda: el.get_Parameter(
        DB.BuiltInParameter.ROOM_NUMBER).AsString())
    if number:
        return "{0} {1}".format(number, name).strip()
    if family and family != name:
        return "{0}: {1}".format(family, name)
    return name


_LEVEL_PARAMS = (
    DB.BuiltInParameter.SCHEDULE_LEVEL_PARAM,
    DB.BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM,
    DB.BuiltInParameter.WALL_BASE_CONSTRAINT,
    DB.BuiltInParameter.STAIRS_BASE_LEVEL_PARAM,
)


def _level_name(doc, el):
    level_id = _safe(lambda: el.LevelId, INVALID_ID)
    if level_id != INVALID_ID:
        return _safe(lambda: doc.GetElement(level_id).Name)
    for bip in _LEVEL_PARAMS:
        value = _safe(lambda: el.get_Parameter(bip).AsValueString())
        if value:
            return value
    return ""


def _element_info(doc, el, workset_table):
    info = {
        "cat": _safe(lambda: el.Category.Name, "(sin categoría)"),
        "name": _element_name(el),
        "level": _level_name(doc, el),
        "view": "",
    }
    owner = _safe(lambda: el.OwnerViewId, INVALID_ID)
    if owner != INVALID_ID:
        info["view"] = _safe(lambda: doc.GetElement(owner).Name, "(vista)")
    if workset_table is not None:
        info["ws"] = _safe(
            lambda: workset_table.GetWorkset(el.WorksetId).Name)
        tip = _safe(lambda: DB.WorksharingUtils.GetWorksharingTooltipInfo(
            doc, el.Id), None)
        if tip is not None:
            info["creator"] = tip.Creator
            info["editor"] = tip.LastChangedBy
    return info


def scan(uiapp, payload):
    uidoc, doc = _doc(uiapp)
    workset_table = doc.GetWorksetTable() if doc.IsWorkshared else None
    warnings, elements = [], {}

    def collect(ids):
        out = []
        for eid in ids:
            key = id_value(eid)
            out.append(key)
            if key in elements:
                continue
            el = doc.GetElement(eid)
            if el is not None:
                elements[key] = _element_info(doc, el, workset_table)
        return out

    for w in doc.GetWarnings():
        warnings.append({
            "d": w.GetDescriptionText(),
            "g": str(w.GetFailureDefinitionId().Guid),
            "f": collect(w.GetFailingElements()),
            "a": collect(w.GetAdditionalElements()),
        })

    store = _load_store(doc)
    element_count = (DB.FilteredElementCollector(doc)
                     .WhereElementIsNotElementType().GetElementCount())
    return {
        "doc": {
            "title": doc.Title,
            "key": model_key(doc),
            "workshared": doc.IsWorkshared,
            "elementCount": element_count,
            "view": _safe(lambda: doc.ActiveView.Name),
            "user": doc.Application.Username,
        },
        # Las claves JSON tienen que ser texto.
        "elements": dict((str(k), v) for k, v in elements.items()),
        "warnings": warnings,
        "history": store.get("history", []),
        "accepted": store.get("accepted", []),
        "scannedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# --- Historial y aceptados (un JSON por modelo, fuera del modelo) --------------

def model_key(doc):
    path = ""
    if doc.IsWorkshared:
        path = _safe(lambda: DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(
            doc.GetWorksharingCentralModelPath()))
    return path or doc.PathName or doc.Title


def _store_path(doc):
    key = model_key(doc)
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:10]
    title = re.sub(r"[^\w\-]+", "_", doc.Title, flags=re.UNICODE)[:40]
    return os.path.join(DATA_DIR, u"{0}_{1}.json".format(title, digest))


def _load_store(doc, strict=False):
    """Lee el JSON del modelo. `strict`: si existe pero no se puede leer,
    falla en vez de devolver vacio (para no sobrescribir el historial)."""
    path = _store_path(doc)
    if not os.path.isfile(path):
        return {}
    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as ex:
        if strict:
            raise ValueError(u"No se pudo leer el historial ({0}): {1}. "
                             u"No se guardó nada para no perderlo."
                             .format(path, ex))
        return {}  # un historial ilegible no debe impedir abrir el panel


def _save_store(doc, store):
    if not os.path.isdir(DATA_DIR):
        os.makedirs(DATA_DIR)
    text = json.dumps(store, ensure_ascii=False, indent=1)
    with io.open(_store_path(doc), "w", encoding="utf-8") as handle:
        handle.write(u"" + text)


def snapshot(uiapp, payload):
    """Guarda el resumen del dia (sobrescribe el del mismo dia)."""
    _, doc = _doc(uiapp)
    store = _load_store(doc, strict=True)
    today = datetime.date.today().isoformat()
    entry = dict((k, v) for k, v in payload.items() if k != "docKey")
    entry["date"] = today
    history = [h for h in store.get("history", []) if h.get("date") != today]
    history.append(entry)
    store["history"] = history[-HISTORY_LIMIT:]
    _save_store(doc, store)
    return store["history"]


def accept(uiapp, payload):
    """Marca o desmarca warnings como aceptados (intencionales)."""
    _, doc = _doc(uiapp)
    store = _load_store(doc, strict=True)
    accepted = set(store.get("accepted", []))
    keys = set(payload.get("keys") or [])
    if payload.get("accepted"):
        accepted |= keys
    else:
        accepted -= keys
    store["accepted"] = sorted(accepted)
    _save_store(doc, store)
    return store["accepted"]


# --- Acciones sobre la vista ----------------------------------------------------

def select(uiapp, payload):
    uidoc, doc = _doc(uiapp)
    ids = _existing_ids(doc, payload.get("ids"))
    uidoc.Selection.SetElementIds(ids)
    return {"count": ids.Count}


def show(uiapp, payload):
    uidoc, doc = _doc(uiapp)
    ids = _existing_ids(doc, payload.get("ids"))
    uidoc.Selection.SetElementIds(ids)
    try:
        uidoc.ShowElements(ids)
    except Exception as ex:
        return {"count": ids.Count,
                "note": u"Seleccionados, pero Revit no encontró una vista "
                        u"donde mostrarlos: {0}".format(ex)}
    return {"count": ids.Count}


def _temporary_view(doc):
    view = doc.ActiveView
    if not view.CanUseTemporaryVisibilityModes():
        raise ValueError(u"La vista activa ({0}) no admite aislar u ocultar "
                         u"temporalmente. Abre una planta, corte o 3D."
                         .format(view.Name))
    return view


def _in_view(doc, view, ids, hideable=False, required=True):
    """Los ids que pueden actuar en `view`: los de modelo y los 2D de ESA
    vista. Un elemento 2D de otra vista hace fallar el lote completo."""
    out = List[DB.ElementId]()
    for eid in ids:
        el = doc.GetElement(eid)
        owner = _safe(lambda: el.OwnerViewId, INVALID_ID)
        if owner != INVALID_ID and owner != view.Id:
            continue
        if hideable and not _safe(lambda: el.CanBeHidden(view), False):
            continue
        out.Add(eid)
    if out.Count == 0 and required:
        raise ValueError(u"Ninguno de esos elementos puede mostrarse en la "
                         u"vista activa ({0}). Usa Mostrar para ir a su "
                         u"vista.".format(view.Name))
    return out


def isolate(uiapp, payload):
    """Aislar u ocultar temporalmente (modo 'isolate' o 'hide')."""
    uidoc, doc = _doc(uiapp)
    view = _temporary_view(doc)
    requested = _existing_ids(doc, payload.get("ids"))
    ids = _in_view(doc, view, requested, hideable=True)
    hide = payload.get("mode") == "hide"

    def apply():
        if hide:
            view.HideElementsTemporary(ids)
        else:
            view.IsolateElementsTemporary(ids)

    _run_in_transaction(doc, "Warnings Analyzer: aislar", apply)
    if not hide:
        uidoc.Selection.SetElementIds(ids)
    result = {"count": ids.Count, "view": view.Name}
    if ids.Count < requested.Count:
        result["note"] = u"{0} elementos omitidos: son de otra vista o no se " \
                         u"pueden ocultar.".format(requested.Count - ids.Count)
    return result


def reset_view(uiapp, payload):
    _, doc = _doc(uiapp)
    view = _temporary_view(doc)
    _run_in_transaction(
        doc, "Warnings Analyzer: restablecer vista",
        lambda: view.DisableTemporaryViewMode(
            DB.TemporaryViewMode.TemporaryHideIsolate))
    return {"view": view.Name}


def _warnings_3d_view(doc):
    name = u"{0} - {1}".format(VIEW3D_PREFIX, doc.Application.Username)
    for view in DB.FilteredElementCollector(doc).OfClass(DB.View3D):
        if not view.IsTemplate and view.Name == name:
            return view
    vft = next((v for v in DB.FilteredElementCollector(doc)
                .OfClass(DB.ViewFamilyType)
                if v.ViewFamily == DB.ViewFamily.ThreeDimensional), None)
    if vft is None:
        raise ValueError(u"El proyecto no tiene un tipo de vista 3D.")
    view = DB.View3D.CreateIsometric(doc, vft.Id)
    view.Name = name
    return view


def section_box(uiapp, payload):
    """Vista 3D propia del usuario recortada a los elementos con problemas."""
    uidoc, doc = _doc(uiapp)
    ids = _existing_ids(doc, payload.get("ids"))
    boxes = []
    for eid in ids:
        el = doc.GetElement(eid)
        if _safe(lambda: el.OwnerViewId, INVALID_ID) != INVALID_ID:
            continue  # elementos 2D: no tienen sentido en una caja 3D
        bb = _safe(lambda: el.get_BoundingBox(None), None)
        if bb is not None:
            boxes.append(bb)
    if not boxes:
        raise ValueError(u"Esos elementos son de vista (2D) o no tienen "
                         u"geometría: usa Mostrar.")
    m = BOX_MARGIN_FT
    box = DB.BoundingBoxXYZ()
    box.Min = DB.XYZ(min(b.Min.X for b in boxes) - m,
                     min(b.Min.Y for b in boxes) - m,
                     min(b.Min.Z for b in boxes) - m)
    box.Max = DB.XYZ(max(b.Max.X for b in boxes) + m,
                     max(b.Max.Y for b in boxes) + m,
                     max(b.Max.Z for b in boxes) + m)

    def apply():
        view = _warnings_3d_view(doc)
        view.SetSectionBox(box)
        view.IsSectionBoxActive = True
        return view

    view = _run_in_transaction(doc, "Warnings Analyzer: caja 3D", apply)
    uidoc.ActiveView = view
    uidoc.Selection.SetElementIds(ids)
    return {"count": len(boxes), "view": view.Name}


def _solid_fill_id(doc):
    for fp in DB.FilteredElementCollector(doc).OfClass(DB.FillPatternElement):
        pattern = fp.GetFillPattern()
        if pattern.IsSolidFill and pattern.Target == DB.FillPatternTarget.Drafting:
            return fp.Id
    return INVALID_ID


def colorize(uiapp, payload):
    """Colorea en la vista activa cada elemento segun la prioridad de su warning.

    Parte de los overrides que el elemento ya tenga (medio tono, grosor,
    transparencia) y solo cambia colores y relleno, que es lo que
    `clear_colors` despues deshace.
    """
    uidoc, doc = _doc(uiapp)
    view = doc.ActiveView
    fill_id = _solid_fill_id(doc)
    colored = set()
    # Solo lo visible: colorear algo oculto no sirve y deja overrides
    # escondidos que despues nadie ve para limpiar.
    visible = set(_visible_in_view(doc, view))

    def apply():
        for priority in ("low", "medium", "high"):  # alta al final: gana
            color = PRIORITY_COLORS[priority]
            numbers = payload.get(priority) or []
            existing = [to_eid(n) for n in numbers if n in visible]
            for eid in _in_view(doc, view, existing, required=False):
                ogs = view.GetElementOverrides(eid)
                ogs.SetProjectionLineColor(color)
                ogs.SetCutLineColor(color)
                if fill_id != INVALID_ID:
                    ogs.SetSurfaceForegroundPatternId(fill_id)
                    ogs.SetSurfaceForegroundPatternColor(color)
                    ogs.SetCutForegroundPatternId(fill_id)
                    ogs.SetCutForegroundPatternColor(color)
                view.SetElementOverrides(eid, ogs)
                colored.add(id_value(eid))

    _run_in_transaction(doc, "Warnings Analyzer: colorear", apply)
    uidoc.RefreshActiveView()
    return {"count": len(colored), "view": view.Name}


def _has_priority_color(ogs):
    color = ogs.ProjectionLineColor
    return color.IsValid and _rgb(color) in _PRIORITY_RGB


def clear_colors(uiapp, payload):
    """Quita los colores del analizador en la vista activa.

    Revisa lo visible en la vista y los ids de warnings que manda la pagina,
    y limpia los que tengan un color de prioridad (no depende de memoria:
    funciona tambien despues de reiniciar Revit). Solo quita colores y
    relleno; el resto de sus overrides se conserva.
    """
    uidoc, doc = _doc(uiapp)
    view = doc.ActiveView
    count = [0]

    def apply():
        invalid = DB.Color.InvalidColorValue
        candidates = set(_visible_in_view(doc, view)) | set(
            payload.get("ids") or [])
        for number in candidates:
            eid = to_eid(number)
            if doc.GetElement(eid) is None:
                continue
            ogs = view.GetElementOverrides(eid)
            if not _has_priority_color(ogs):
                continue
            ogs.SetProjectionLineColor(invalid)
            ogs.SetCutLineColor(invalid)
            ogs.SetSurfaceForegroundPatternId(INVALID_ID)
            ogs.SetSurfaceForegroundPatternColor(invalid)
            ogs.SetCutForegroundPatternId(INVALID_ID)
            ogs.SetCutForegroundPatternColor(invalid)
            view.SetElementOverrides(eid, ogs)
            count[0] += 1

    _run_in_transaction(doc, "Warnings Analyzer: quitar colores", apply)
    uidoc.RefreshActiveView()
    return {"count": count[0], "view": view.Name}


def _visible_in_view(doc, view):
    return [id_value(eid) for eid in DB.FilteredElementCollector(doc, view.Id)
            .WhereElementIsNotElementType().ToElementIds()]


def view_ids(uiapp, payload):
    """De los ids pedidos, los que se ven en la vista activa."""
    _, doc = _doc(uiapp)
    view = doc.ActiveView
    wanted = set(payload.get("ids") or [])
    return {"ids": [n for n in _visible_in_view(doc, view) if n in wanted],
            "view": view.Name}


# --- Correcciones automaticas ---------------------------------------------------

def _current_failing_sets(doc, description):
    """Grupos de elementos que HOY siguen en un warning con esa descripcion.

    Las correcciones solo actuan sobre grupos que siguen vigentes: si el
    usuario movio o cambio algo desde el ultimo escaneo, ese grupo se salta.
    """
    return set(frozenset(id_value(e) for e in w.GetFailingElements())
               for w in doc.GetWarnings()
               if w.GetDescriptionText() == description)


def _owned_by_other(doc, eid):
    if not doc.IsWorkshared:
        return False
    status = DB.WorksharingUtils.GetCheckoutStatus(doc, eid)
    return status == DB.CheckoutStatus.OwnedByOtherUser


def _delete(doc, numbers, report):
    for number in numbers:
        eid = to_eid(number)
        if doc.GetElement(eid) is None:
            continue  # ya lo borro una eliminacion anterior en cascada
        if _owned_by_other(doc, eid):
            report["skipped"].append(number)
            continue
        removed = doc.Delete(eid)  # incluye etiquetas, cotas y alojados
        report["deleted"].append(number)
        report["cascade"] += max(0, removed.Count - 1)


def _fix(uiapp, payload, name, plan):
    """Esqueleto comun: valida grupos vigentes y borra lo que `plan` decida."""
    _, doc = _doc(uiapp)
    report = {"deleted": [], "skipped": [], "cascade": 0, "partial": 0,
              "stale": 0}
    current = _current_failing_sets(doc, payload.get("description"))

    def apply():
        for group in payload.get("groups") or []:
            if frozenset(group) not in current:
                report["stale"] += 1
                continue
            _delete(doc, plan(doc, group, report), report)

    _run_in_transaction(doc, name, apply)
    return report


def _plan_duplicates(doc, group, report):
    return sorted(group)[1:]  # conserva el menor ID


def fix_duplicates(uiapp, payload):
    """Instancias identicas: conserva el menor ID de cada grupo."""
    return _fix(uiapp, payload, u"Warnings Analyzer: eliminar duplicados",
                _plan_duplicates)


def _line(doc, number):
    el = doc.GetElement(to_eid(number))
    if isinstance(el, DB.CurveElement):
        curve = _safe(lambda: el.GeometryCurve, None)
        if isinstance(curve, DB.Line):
            return curve
    return None


def contained(inner, outer, tol=LINE_TOL_FT):
    """True si la recta `inner` cae entera sobre la recta `outer`.

    Solo rectas: en una recta acotada basta con los dos extremos. En arcos no
    (un arco puede tener ambos extremos sobre otro y no estar contenido), por
    eso los arcos nunca se borran automaticamente.
    """
    for i in (0, 1):
        projection = outer.Project(inner.GetEndPoint(i))
        if projection is None or projection.Distance > tol:
            return False
    return True


def _plan_lines(doc, group, report):
    lines = [(n, _line(doc, n)) for n in group]
    if any(curve is None for _, curve in lines):
        report["partial"] += 1  # arcos u otras curvas: a mano
        return []
    lines.sort(key=lambda item: -item[1].Length)
    kept, doomed = [], []
    for number, line in lines:
        if any(contained(line, other) for _, other in kept):
            doomed.append(number)
        else:
            kept.append((number, line))
    if not doomed:
        report["partial"] += 1
    return doomed


def fix_overlap_lines(uiapp, payload):
    """Lineas superpuestas: borra solo la recta que esta entera dentro de otra.

    Una superposicion parcial no se toca: borrar cualquiera de las dos dejaria
    un tramo sin linea.
    """
    return _fix(uiapp, payload, u"Warnings Analyzer: eliminar líneas contenidas",
                _plan_lines)


# --- Exportacion -----------------------------------------------------------------

def export_csv(uiapp, payload):
    from Microsoft.Win32 import SaveFileDialog
    dialog = SaveFileDialog()
    dialog.Title = "Exportar warnings"
    dialog.Filter = "CSV (*.csv)|*.csv"
    dialog.FileName = payload.get("filename") or "warnings.csv"
    if dialog.ShowDialog() != True:  # noqa: E712 (Nullable<bool> de .NET)
        return {"path": None}
    # utf-8-sig: Excel lee bien las tildes solo con BOM.
    with io.open(dialog.FileName, "w", encoding="utf-8-sig",
                 newline="") as handle:
        handle.write(u"" + payload.get("text", ""))
    return {"path": dialog.FileName}


ACTIONS = {
    "scan": scan,
    "snapshot": snapshot,
    "accept": accept,
    "select": select,
    "show": show,
    "isolate": isolate,
    "reset_view": reset_view,
    "section_box": section_box,
    "colorize": colorize,
    "clear_colors": clear_colors,
    "view_ids": view_ids,
    "fix_duplicates": fix_duplicates,
    "fix_overlap_lines": fix_overlap_lines,
    "export_csv": export_csv,
}

# Acciones que no dependen de que el modelo activo sea el escaneado.
_NO_DOC_CHECK = ("scan", "export_csv")


def dispatch(uiapp, action, payload):
    """Punto de entrada del puente. Rechaza actuar sobre otro modelo.

    La ventana no esta atada a un documento: si el usuario cambio de modelo
    en Revit desde el ultimo escaneo, los ids de la pagina son de OTRO modelo
    y podrian coincidir con elementos ajenos. Por eso toda accion (salvo
    escanear y exportar) trae la clave del modelo escaneado y se compara.
    """
    handler = ACTIONS[action]
    if action not in _NO_DOC_CHECK:
        _, doc = _doc(uiapp)
        if payload.get("docKey") != model_key(doc):
            raise ValueError(u"El modelo activo en Revit ({0}) no es el que "
                             u"se analizó. Pulsa Actualizar.".format(doc.Title))
    return handler(uiapp, payload)
