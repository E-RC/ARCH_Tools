# -*- coding: utf-8 -*-
__title__ = "Center A/R"
__doc__ = "Centra el punto de ubicacion de las habitaciones y areas seleccionadas."

import math
from pyrevit import revit, forms
from Autodesk.Revit.DB import (
    SpatialElement, SpatialElementBoundaryOptions,
    SpatialElementBoundaryLocation, XYZ
)

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS GEOMÉTRICOS
# ═══════════════════════════════════════════════════════════════════════════════

def shoelace_centroid(pts):
    n  = len(pts)
    A  = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(n):
        j     = (i + 1) % n
        cross = pts[i].X * pts[j].Y - pts[j].X * pts[i].Y
        A  += cross
        cx += (pts[i].X + pts[j].X) * cross
        cy += (pts[i].Y + pts[j].Y) * cross
    A *= 0.5
    if abs(A) < 1e-9:
        return None
    return (cx / (6.0 * A), cy / (6.0 * A))


def point_in_polygon(px, py, pts):
    n      = len(pts)
    inside = False
    j      = n - 1
    for i in range(n):
        xi, yi = pts[i].X, pts[i].Y
        xj, yj = pts[j].X, pts[j].Y
        if ((yi > py) != (yj > py)) and \
           (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def dist_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.sqrt((px - ax) ** 2 + (py - ay) ** 2)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.sqrt((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2)


def pole_of_inaccessibility(pts, divisions=40):
    """Punto interior más alejado de todas las aristas (siempre dentro del polígono)."""
    min_x = min(p.X for p in pts)
    max_x = max(p.X for p in pts)
    min_y = min(p.Y for p in pts)
    max_y = max(p.Y for p in pts)

    dx = (max_x - min_x) / divisions
    dy = (max_y - min_y) / divisions

    n     = len(pts)
    edges = [
        (pts[i].X, pts[i].Y, pts[(i + 1) % n].X, pts[(i + 1) % n].Y)
        for i in range(n)
    ]

    best_pt = None
    best_d  = -1.0

    for i in range(divisions):
        for j in range(divisions):
            sx = min_x + (i + 0.5) * dx
            sy = min_y + (j + 0.5) * dy

            if not point_in_polygon(sx, sy, pts):
                continue

            min_d = min(
                dist_to_segment(sx, sy, ax, ay, bx, by)
                for ax, ay, bx, by in edges
            )

            if min_d > best_d:
                best_d  = min_d
                best_pt = (sx, sy)

    return best_pt


def get_safe_center(element):
    """
    1. Shoelace centroid → si está dentro del polígono, úsalo.
    2. Si cae fuera (forma cóncava) → Polo de Inaccesibilidad.
    """
    opts = SpatialElementBoundaryOptions()
    opts.SpatialElementBoundaryLocation = SpatialElementBoundaryLocation.Center

    try:
        loops = element.GetBoundarySegments(opts)
    except Exception:
        return None

    if not loops or len(loops) == 0:
        return None

    pts = [seg.GetCurve().GetEndPoint(0) for seg in loops[0]]
    if len(pts) < 3:
        return None

    c = shoelace_centroid(pts)
    if c and point_in_polygon(c[0], c[1], pts):
        return (c[0], c[1])

    pole = pole_of_inaccessibility(pts, divisions=40)
    if pole:
        return (pole[0], pole[1])

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# LÓGICA PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════════

selection        = revit.get_selection()
spatial_elements = [el for el in selection if isinstance(el, SpatialElement)]

if not spatial_elements:
    forms.alert(
        "Por favor, pre-seleccione al menos una Habitacion (Room) o un Area "
        "antes de ejecutar la herramienta.",
        title="Seleccion Vacia"
    )
else:
    with revit.Transaction("ARCH - Center Rooms and Areas"):
        count = 0
        for element in spatial_elements:
            result = get_safe_center(element)
            if result is None:
                continue
            loc = element.Location
            if loc and hasattr(loc, "Point"):
                loc.Point = XYZ(result[0], result[1], loc.Point.Z)
                count += 1

    forms.toast("Centrados: {} elemento(s).".format(count))
