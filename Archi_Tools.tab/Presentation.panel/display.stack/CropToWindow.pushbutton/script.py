# -*- coding: utf-8 -*-
"""Ajusta la Crop Region de la vista activa a la porcion visible en pantalla.

La crop region se deja reducida respecto de la ventana (MARGIN_FACTOR) para
que los tiradores queden dentro del area visible y se puedan arrastrar.

GetZoomCorners() entrega las esquinas de la ventana visible en coordenadas de
mundo. Proyectadas al sistema local de la CropBox dan directamente los limites
en X e Y del recorte, sin necesidad de reconstruir el frustum de la camara
(calculo que no es fiable en vistas en perspectiva).

Solo se tocan Min/Max en X e Y: la profundidad (Z = near / far clip) y el
Transform se conservan, por lo que la camara no se mueve.
"""

from pyrevit import revit, DB, forms

# Fraccion de la ventana que ocupa la crop region.
# 0.85 -> ~7.5% de margen por lado. Bajar para dejar mas margen.
MARGIN_FACTOR = 0.85

UNSUPPORTED = (
    DB.ViewType.Schedule,
    DB.ViewType.ColumnSchedule,
    DB.ViewType.PanelSchedule,
    DB.ViewType.DrawingSheet,
    DB.ViewType.Legend,
    DB.ViewType.ProjectBrowser,
    DB.ViewType.SystemBrowser,
    DB.ViewType.Undefined,
    DB.ViewType.Internal,
)

doc = revit.doc
uidoc = revit.uidoc
view = uidoc.ActiveView

if view.IsTemplate or view.ViewType in UNSUPPORTED:
    forms.alert("La vista activa no admite Crop Region.",
                title="Crop a Ventana", exitscript=True)


def get_active_uiview():
    """UIView correspondiente a la vista activa."""
    for uiview in uidoc.GetOpenUIViews():
        if uiview.ViewId.IntegerValue == view.Id.IntegerValue:
            return uiview
    return None


active_uiview = get_active_uiview()
if active_uiview is None:
    forms.alert("No se encontro la ventana de la vista activa.",
                title="Crop a Ventana", exitscript=True)

crop = view.CropBox
xform = crop.Transform
origin = xform.Origin
basis_x = xform.BasisX
basis_y = xform.BasisY


def to_local(point):
    """Proyecta un punto de mundo al plano local de la CropBox."""
    delta = point - origin
    return delta.DotProduct(basis_x), delta.DotProduct(basis_y)


coords = [to_local(corner) for corner in active_uiview.GetZoomCorners()]
xs = [c[0] for c in coords]
ys = [c[1] for c in coords]

center_x = (min(xs) + max(xs)) / 2.0
center_y = (min(ys) + max(ys)) / 2.0
half_x = (max(xs) - min(xs)) / 2.0 * MARGIN_FACTOR
half_y = (max(ys) - min(ys)) / 2.0 * MARGIN_FACTOR

new_crop = DB.BoundingBoxXYZ()
new_crop.Transform = xform
new_crop.Min = DB.XYZ(center_x - half_x, center_y - half_y, crop.Min.Z)
new_crop.Max = DB.XYZ(center_x + half_x, center_y + half_y, crop.Max.Z)

with DB.Transaction(doc, "Crop region a ventana visible") as t:
    t.Start()
    view.CropBox = new_crop
    view.CropBoxActive = True
    view.CropBoxVisible = True
    t.Commit()
