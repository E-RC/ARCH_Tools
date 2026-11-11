"""Geometria pura para unir dos losas con pendiente. Sin API de Revit.

Compatible con IronPython 2.7 y Python 3.x. Coordenadas en pies.
Un plano se guarda como (a, b, c) con z = a*x + b*y + c.
"""
from __future__ import division

TOL = 1e-9


def plano_desde_cara(nx, ny, nz, ox, oy, oz):
    """Plano z(x, y) de la cara superior, dada su normal y un punto."""
    if abs(nz) < TOL:
        raise ValueError("cara vertical: no define una pendiente")
    a, b = -nx / nz, -ny / nz
    return (a, b, oz - a * ox - b * oy)


def cruce(pa, pb):
    """Recta de cruce de dos planos: g(x, y) = A*x + B*y + C con g = za - zb.

    g < 0 donde la losa A queda mas baja que la B. None si son paralelos
    (misma pendiente y direccion): no hay arista que buscar.
    """
    A, B, C = pa[0] - pb[0], pa[1] - pb[1], pa[2] - pb[2]
    if abs(A) < TOL and abs(B) < TOL:
        return None
    return (A, B, C)


def quitar_cercanos(pts, tol=0.005):
    """Quita los vertices a menos de `tol` del anterior (astillas del booleano).

    Revit rechaza lineas menores a ShortCurveTolerance (~0.0026 pies).
    """
    out = []
    for p in pts:
        if not out or ((p[0] - out[-1][0]) ** 2 + (p[1] - out[-1][1]) ** 2) ** 0.5 >= tol:
            out.append(p)
    if len(out) > 1 and ((out[0][0] - out[-1][0]) ** 2 + (out[0][1] - out[-1][1]) ** 2) ** 0.5 < tol:
        out.pop()
    return out


def fusionar_colineales(pts, tol=1e-6):
    """Quita los vertices intermedios de un lazo cerrado que no cambian la direccion."""
    n = len(pts)
    out = []
    for i in range(n):
        p0, p1, p2 = pts[i - 1], pts[i], pts[(i + 1) % n]
        cruz = (p1[0] - p0[0]) * (p2[1] - p1[1]) - (p1[1] - p0[1]) * (p2[0] - p1[0])
        if abs(cruz) > tol:
            out.append(p1)
    return out


def subsegmento(p, q, a, b, tol=1e-6):
    """True si el segmento p-q yace sobre la recta que pasa por a-b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = (dx * dx + dy * dy) ** 0.5
    return all(abs(dx * (r[1] - a[1]) - dy * (r[0] - a[0])) / n < tol for r in (p, q))


def area(pts):
    """Area con signo (positiva si el lazo va en sentido antihorario)."""
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                     for i in range(len(pts)))


def semiplano(recta, lado, centro, radio=1.0e5):
    """Poligono grande (antihorario) del lado `lado` (-1: g<0, +1: g>0) de la recta.

    Es un cuadrado de lado 2*radio recortado por la recta, lo bastante grande
    para actuar como semiplano al intersectarlo con una losa.
    """
    A, B, C = recta
    cx, cy = centro
    sq = [(cx - radio, cy - radio), (cx + radio, cy - radio), (cx + radio, cy + radio), (cx - radio, cy + radio)]
    g = lambda p: lado * (A * p[0] + B * p[1] + C)
    out = []
    for i in range(4):
        p, q = sq[i], sq[(i + 1) % 4]
        gp, gq = g(p), g(q)
        if gp >= 0:
            out.append(p)
        if (gp > 0) != (gq > 0) and gp != 0 and gq != 0:
            t = gp / (gp - gq)
            out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
    return out
