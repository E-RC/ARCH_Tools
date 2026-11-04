# -*- coding: utf-8 -*-
"""Geometria de Geometry Slope Multi: envolvente de pendientes, cumbreras y
deteccion de configuraciones irresolubles.

Python puro a proposito (sin API de Revit) para poder verificarlo en seco con
CPython: ver test_slope_solver.py.

Convenciones:
  - Todo en el sistema interno de Revit: pies, coordenadas XY del modelo.
  - Un porcentaje positivo sube al alejarse de su arista.
  - Las cotas devueltas son offsets sobre la losa plana (post ResetSlabShape),
    que es lo que espera SlabShapeEditor.ModifySubElement.

Idea central: cada arista define un campo lineal f_i(p) = ratio_i * d(p, recta_i).
Con pendientes que suben, la superficie correcta es la envolvente inferior de
esos planos (un techo a cuatro aguas); con pendientes que bajan, la superior.
Esa envolvente respeta siempre la cota de cada arista y quiebra en rectas: esas
rectas son las split lines que hay que dibujar.
"""
from __future__ import division

import math

FT_TO_M = 0.3048
TOL_FT = 0.003            # ~1 mm: cuanto se tolera que una arista se mueva
ACTIVE_TOL_FT = 1e-6      # igualdad exacta de campos, solo ruido de float
MIN_SEGMENT_FT = 0.16     # ~5 cm: bajo esto una split line no aporta
# Muestreo por cantidad, no por paso: el detalle mas chico que se detecta con
# fiabilidad es del orden de largo_del_tramo / SAMPLES. Subirlos solo si
# aparecen losas donde eso no alcanza.
SAMPLES_PER_EDGE = 9
SAMPLES_PER_SPAN = 24
_EPS = 1e-9


class SlopeEdge(object):
    """Una arista de apoyo con su pendiente propia."""

    def __init__(self, p0, p1, percent, name=None):
        self.p0 = (float(p0[0]), float(p0[1]))
        self.p1 = (float(p1[0]), float(p1[1]))
        self.percent = float(percent)
        self.ratio = self.percent / 100.0
        self.name = name
        dx = self.p1[0] - self.p0[0]
        dy = self.p1[1] - self.p0[1]
        self.length = math.hypot(dx, dy)
        if self.length < _EPS:
            raise ValueError("La arista no tiene longitud en planta.")
        self.ux, self.uy = dx / self.length, dy / self.length
        self.nx, self.ny = -self.uy, self.ux

    def signed_distance(self, p):
        return self.nx * (p[0] - self.p0[0]) + self.ny * (p[1] - self.p0[1])

    def projection(self, p):
        """Avance de p a lo largo de la arista, en pies desde p0."""
        return (p[0] - self.p0[0]) * self.ux + (p[1] - self.p0[1]) * self.uy

    def distance(self, p):
        """Distancia al tramo, no a su recta infinita.

        Una arista manda frente a su propio largo: mas alla de sus extremos su
        influencia gira en torno al extremo en vez de seguir recta hasta el
        infinito. Con la recta infinita, una arista corta de un entrante
        gobernaba la losa entera a decenas de metros de distancia."""
        t = self.projection(p)
        if t <= 0.0:
            return math.hypot(p[0] - self.p0[0], p[1] - self.p0[1])
        if t >= self.length:
            return math.hypot(p[0] - self.p1[0], p[1] - self.p1[1])
        return abs(self.signed_distance(p))

    def field(self, p):
        """Cota que esta arista, por si sola, exige en p."""
        return self.ratio * self.distance(p)

    def sample_points(self, count=SAMPLES_PER_EDGE):
        if count < 2:
            return [self.p0]
        out = []
        for i in range(count):
            t = i / (count - 1.0)
            out.append((self.p0[0] + (self.p1[0] - self.p0[0]) * t,
                        self.p0[1] + (self.p1[1] - self.p0[1]) * t))
        return out


# --------------------------------------------------------------------------
# Envolvente
# --------------------------------------------------------------------------

def envelope_mode(edges):
    """'min' para pendientes que suben, 'max' para las que bajan."""
    has_pos = any(e.ratio > _EPS for e in edges)
    has_neg = any(e.ratio < -_EPS for e in edges)
    if has_pos and has_neg:
        return "mixed"
    if has_neg:
        return "max"
    return "min"


def envelope(edges, p, mode=None):
    """(cota, indice de la arista que manda) en p."""
    if mode is None:
        mode = envelope_mode(edges)
    if mode == "mixed":
        # Con signos opuestos ni min ni max respetan a todas las aristas: no
        # hay superficie. Mejor reventar aqui que devolver un numero falso;
        # find_conflicts() bloquea antes de que se llegue a este punto.
        raise ValueError("Signos opuestos: no hay envolvente posible.")
    take_max = (mode == "max")
    best_i = 0
    best_v = edges[0].field(p)
    for i in range(1, len(edges)):
        v = edges[i].field(p)
        if (v > best_v) if take_max else (v < best_v):
            best_i, best_v = i, v
    return best_v, best_i


def offset_at(edges, p, mode=None):
    """Offset de cota a aplicar en p."""
    return envelope(edges, p, mode)[0]


# --------------------------------------------------------------------------
# Conflictos
# --------------------------------------------------------------------------

class Conflict(object):
    """kind: 'flat' | 'collinear' | 'violation'."""

    def __init__(self, kind, edge, other=None, gap_ft=0.0, point=None):
        self.kind = kind
        self.edge = edge
        self.other = other
        self.gap_ft = gap_ft
        self.point = point


def _are_collinear(a, b):
    """Dos aristas sobre la misma recta Y solapadas.

    Sobre la misma recta pero una a continuacion de la otra no se estorban:
    cada una manda frente a su propio tramo."""
    if abs(a.nx * b.ny - a.ny * b.nx) > 1e-7:
        return False
    if abs(a.signed_distance(b.p0)) >= TOL_FT:
        return False
    t0, t1 = a.projection(b.p0), a.projection(b.p1)
    return min(t0, t1) < a.length - _EPS and max(t0, t1) > _EPS


def find_conflicts(edges):
    """Todo lo que impide construir la superficie pedida.

    La regla de fondo: la envolvente tiene que valer 0 sobre cada arista,
    porque cada arista conserva su cota. Si otra arista la desplaza, no hay
    superficie posible y hay que avisar antes de tocar el modelo.
    """
    problems = []

    for i, e in enumerate(edges):
        if abs(e.ratio) < _EPS:
            problems.append(Conflict("flat", i))

    for i in range(len(edges)):
        for j in range(i + 1, len(edges)):
            if (_are_collinear(edges[i], edges[j])
                    and abs(edges[i].ratio - edges[j].ratio) > _EPS):
                problems.append(Conflict("collinear", i, j))

    mode = envelope_mode(edges)

    if mode == "mixed":
        # Sin envolvente que valga, cada par en desacuerdo se mide directo: asi
        # el aviso apunta a las dos aristas que de verdad se contradicen y no a
        # un tercero del mismo signo que pasaba por ahi.
        found, fallback = False, None
        for i in range(len(edges)):
            for j in range(i + 1, len(edges)):
                if edges[i].ratio * edges[j].ratio >= 0:
                    continue
                worst = None
                for (a, b) in ((i, j), (j, i)):
                    for p in edges[a].sample_points():
                        value = edges[b].field(p)
                        if worst is None or abs(value) > abs(worst.gap_ft):
                            worst = Conflict("violation", a, b, value, p)
                if worst is None:
                    continue
                if abs(worst.gap_ft) > TOL_FT:
                    problems.append(worst)
                    found = True
                elif fallback is None:
                    fallback = worst
        if not found and fallback is not None:
            # Dos aristas casi pegadas con signos opuestos dan un desfase bajo
            # tolerancia, pero siguen sin definir superficie. Se avisa igual:
            # sin esto la envolvente reventaria mas adelante.
            problems.append(fallback)
        return problems

    for i, e in enumerate(edges):
        worst = None
        for p in e.sample_points():
            value, winner = envelope(edges, p, mode)
            if abs(value) <= TOL_FT or winner == i:
                continue
            if worst is None or abs(value) > abs(worst.gap_ft):
                worst = Conflict("violation", i, winner, value, p)
        if worst is not None:
            problems.append(worst)

    return problems


def _label(edges, i):
    e = edges[i]
    name = e.name or "Arista {0}".format(i + 1)
    return "{0} ({1:+.2f}%)".format(name, e.percent)


def describe_conflict(c, edges):
    """Mensaje para el usuario: que pasa y por donde salir."""
    if c.kind == "flat":
        return ("{0} tiene pendiente 0%: obliga a que toda la superficie quede "
                "plana y anula las demas pendientes."
                .format(_label(edges, c.edge)))

    if c.kind == "collinear":
        return ("{0} y {1} estan sobre la misma recta con pendientes distintas: "
                "solo puede mandar una y la otra se ignora."
                .format(_label(edges, c.edge), _label(edges, c.other)))

    gap_m = abs(c.gap_ft) * FT_TO_M
    a, b = edges[c.edge], edges[c.other]
    if a.ratio * b.ratio < 0:
        return ("{0} y {1} se contradicen: cada una fija su propia cota y a la "
                "vez exige que la otra se mueva {2:.3f} m. No existe una "
                "superficie que cumpla ambas (haria falta un muro vertical).\n"
                "      - Para una caida continua entre ambas, usa una sola "
                "arista.\n"
                "      - Para una cumbrera o una canaleta entre ambas, ponles "
                "el mismo signo."
                .format(_label(edges, c.edge), _label(edges, c.other), gap_m))

    return ("{0} no conserva su cota: {1} la desplaza {2:.3f} m."
            .format(_label(edges, c.edge), _label(edges, c.other), gap_m))


# --------------------------------------------------------------------------
# Cumbreras (split lines)
# --------------------------------------------------------------------------

def point_in_polygon(p, polygon):
    x, y = p[0], p[1]
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if (yi > y) != (yj > y):
            xc = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < xc:
                inside = not inside
        j = i
    return inside


def clip_line_to_polygon(A, B, C, polygon):
    """(origen, direccion, tramos [t0,t1] de la recta dentro del poligono)."""
    norm = math.hypot(A, B)
    origin = (A * C / (norm * norm), B * C / (norm * norm))
    direction = (-B / norm, A / norm)

    hits = []
    n = len(polygon)
    for i in range(n):
        q0 = polygon[i]
        q1 = polygon[(i + 1) % n]
        g0 = A * q0[0] + B * q0[1] - C
        g1 = A * q1[0] + B * q1[1] - C
        if abs(g0 - g1) < _EPS:
            continue
        t = g0 / (g0 - g1)
        if t < -_EPS or t > 1.0 + _EPS:
            continue
        px = q0[0] + (q1[0] - q0[0]) * t
        py = q0[1] + (q1[1] - q0[1]) * t
        hits.append((px - origin[0]) * direction[0]
                    + (py - origin[1]) * direction[1])
    hits.sort()

    spans = []
    for i in range(len(hits) - 1):
        t0, t1 = hits[i], hits[i + 1]
        if t1 - t0 < MIN_SEGMENT_FT:
            continue
        tm = (t0 + t1) / 2.0
        mid = (origin[0] + direction[0] * tm, origin[1] + direction[1] * tm)
        if point_in_polygon(mid, polygon):
            spans.append((t0, t1))
    return origin, direction, spans


def _boundary(is_active, t_active, t_inactive, iterations=24):
    """Ultimo t activo entre un punto activo y uno que no lo esta."""
    for _ in range(iterations):
        mid = (t_active + t_inactive) / 2.0
        if is_active(mid):
            t_active = mid
        else:
            t_inactive = mid
    return t_active


def _active_spans(is_active, t0, t1, samples=SAMPLES_PER_SPAN):
    """Sub-tramos de [t0, t1] donde el par de aristas manda de verdad."""
    step = (t1 - t0) / float(samples)
    flags = [is_active(t0 + step * (k + 0.5)) for k in range(samples)]

    spans = []
    k = 0
    while k < samples:
        if not flags[k]:
            k += 1
            continue
        start = k
        while k + 1 < samples and flags[k + 1]:
            k += 1
        end = k
        a = t0 if start == 0 else _boundary(
            is_active, t0 + step * (start + 0.5), t0 + step * (start - 0.5))
        b = t1 if end == samples - 1 else _boundary(
            is_active, t0 + step * (end + 0.5), t0 + step * (end + 1.5))
        spans.append((a, b))
        k += 1
    return spans


def _same_segment(a, b):
    def close(p, q):
        return math.hypot(p[0] - q[0], p[1] - q[1]) < 0.02   # ~6 mm
    return ((close(a[0], b[0]) and close(a[1], b[1]))
            or (close(a[0], b[1]) and close(a[1], b[0])))


class Surface(object):
    """Las aristas mas el contorno de la losa: quien manda en cada punto.

    Cada arista aporta un plano que sube desde su recta. Dos aristas que
    comparten esquina se reparten el sector con la bisectriz que nace en esa
    esquina, y ahi esta la clave: en una esquina convexa se queda el plano mas
    bajo, y en una entrante el mas alto. Asi se arma cualquier techo, y es la
    unica forma de que un entrante se resuelva con una limatesa por esquina en
    vez de un abanico.

    Entre aristas que no comparten esquina no hay nada que repartir: gana la
    envolvente de siempre.
    """

    def __init__(self, edges, polygon):
        self.edges = edges
        self.polygon = polygon
        self.mode = envelope_mode(edges)
        self._prefer_lower = (self.mode != "max")
        self._sigma = [self._interior_sign(e) for e in edges]
        self._neighbours = [[] for _ in edges]
        self._corners = []
        self._link_edges()

    # -- geometria de apoyo ------------------------------------------------

    def _interior_sign(self, edge):
        """+1 o -1 segun de que lado de la arista esta la losa; 0 si esta a
        los dos lados (una arista interior, que sube hacia ambos)."""
        mid = ((edge.p0[0] + edge.p1[0]) / 2.0, (edge.p0[1] + edge.p1[1]) / 2.0)
        step = MIN_SEGMENT_FT
        plus = point_in_polygon((mid[0] + edge.nx * step,
                                 mid[1] + edge.ny * step), self.polygon)
        minus = point_in_polygon((mid[0] - edge.nx * step,
                                  mid[1] - edge.ny * step), self.polygon)
        if plus and minus:
            return 0.0
        return 1.0 if plus else -1.0

    def _link_edges(self):
        for i in range(len(self.edges)):
            for j in range(i + 1, len(self.edges)):
                corner = self._shared_corner(self.edges[i], self.edges[j])
                if corner is None:
                    continue
                reflex = not self._is_convex(corner, self.edges[i],
                                             self.edges[j])
                self._neighbours[i].append((j, reflex))
                self._neighbours[j].append((i, reflex))
                self._corners.append((i, j, corner))

    @staticmethod
    def _shared_corner(a, b):
        for pa in (a.p0, a.p1):
            for pb in (b.p0, b.p1):
                if _close_points(pa, pb):
                    return pa
        return None

    def _is_convex(self, corner, a, b):
        """La esquina es convexa si la bisectriz entre ambas aristas apunta
        hacia dentro de la losa."""
        ua = _away_from(a, corner)
        ub = _away_from(b, corner)
        dx, dy = ua[0] + ub[0], ua[1] + ub[1]
        norm = math.hypot(dx, dy)
        if norm < 1e-7:                       # aristas enfrentadas: no hay rincon
            return True
        step = MIN_SEGMENT_FT
        probe = (corner[0] + dx / norm * step, corner[1] + dy / norm * step)
        return point_in_polygon(probe, self.polygon)

    # -- cotas -------------------------------------------------------------

    def plane_height(self, i, p):
        """Cota del plano de la arista i extendido a toda la losa."""
        edge = self.edges[i]
        if self._sigma[i] == 0.0:
            return edge.ratio * abs(edge.signed_distance(p))
        return edge.ratio * self._sigma[i] * edge.signed_distance(p)

    def _in_domain(self, i, p):
        for (j, reflex) in self._neighbours[i]:
            mine, theirs = self.plane_height(i, p), self.plane_height(j, p)
            if (not reflex) == self._prefer_lower:
                if mine > theirs + _EPS:
                    return False
            elif mine < theirs - _EPS:
                return False
        return True

    def _reaches(self, value):
        """Una arista no manda detras de si misma.

        Su plano, extendido, da cotas negativas del lado por donde no se
        propaga la pendiente; sin este filtro esos negativos ganan la
        envolvente y hunden la losa justo donde tendria que subir."""
        return value >= -TOL_FT if self._prefer_lower else value <= TOL_FT

    def offset_at(self, p):
        best = None
        for i in range(len(self.edges)):
            if not self._in_domain(i, p):
                continue
            value = self.plane_height(i, p)
            if not self._reaches(value):
                continue
            if best is None or ((value < best) if self._prefer_lower
                                else (value > best)):
                best = value
        if best is None:
            # No llega ninguna pendiente: la losa se queda a la cota de las
            # aristas. El nivel de cada arista se proyecta a lo largo de la
            # losa y entre dos aristas que se dan la espalda queda plano.
            return 0.0
        return best

    def sigma(self, i):
        return self._sigma[i]

    def _rules_here(self, i, p):
        if not self._in_domain(i, p):
            return False
        value = self.plane_height(i, p)
        return (self._reaches(value)
                and abs(value - self.offset_at(p)) <= ACTIVE_TOL_FT)

    # -- quiebres ----------------------------------------------------------

    def ridge_segments(self):
        """Donde quiebra la superficie: la bisectriz entre dos aristas que se
        reparten un sector, y la propia recta de cada arista prolongada, que
        es donde su pendiente termina y empieza la zona plana."""
        out = []
        for i in range(len(self.edges)):
            for j in range(i + 1, len(self.edges)):
                for (A, B, C) in _pair_lines(self, i, j):
                    self._collect_line(A, B, C, i, out)
        for i, edge in enumerate(self.edges):
            A, B, C = edge.nx, edge.ny, edge.nx * edge.p0[0] + edge.ny * edge.p0[1]
            self._collect_line(A, B, C, i, out, only_beyond_edge=True)
        return out

    def _collect_line(self, A, B, C, i, out, only_beyond_edge=False):
        origin, direction, spans = clip_line_to_polygon(A, B, C, self.polygon)
        if not spans:
            return
        edge = self.edges[i]

        def is_active(t):
            p = (origin[0] + direction[0] * t, origin[1] + direction[1] * t)
            return self._rules_here(i, p)

        for (t0, t1) in spans:
            for (a, b) in _active_spans(is_active, t0, t1):
                if b - a < MIN_SEGMENT_FT:
                    continue
                start = (origin[0] + direction[0] * a,
                         origin[1] + direction[1] * a)
                end = (origin[0] + direction[0] * b,
                       origin[1] + direction[1] * b)
                if only_beyond_edge:
                    # Sobre la arista misma no hay nada que dibujar: eso ya es
                    # borde de la losa. Solo interesa la prolongacion.
                    t = edge.projection(((start[0] + end[0]) / 2.0,
                                         (start[1] + end[1]) / 2.0))
                    if -MIN_SEGMENT_FT <= t <= edge.length + MIN_SEGMENT_FT:
                        continue
                _add_segment(out, (start, end))

def _close_points(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1]) < 0.02      # ~6 mm


def _away_from(edge, corner):
    if _close_points(edge.p0, corner):
        return (edge.ux, edge.uy)
    return (-edge.ux, -edge.uy)


def _pair_lines(surface, i, j):
    """Rectas donde los planos de dos aristas se cortan.

    Con la losa a un lado conocido hay una sola. Una arista interior, que sube
    hacia ambos lados, no tiene lado definido y aporta las dos."""
    a, b = surface.edges[i], surface.edges[j]
    signs_a = (1.0, -1.0) if surface.sigma(i) == 0.0 else (surface.sigma(i),)
    signs_b = (1.0, -1.0) if surface.sigma(j) == 0.0 else (surface.sigma(j),)
    lines = []
    for ka in signs_a:
        for kb in signs_b:
            A = a.ratio * ka * a.nx - b.ratio * kb * b.nx
            B = a.ratio * ka * a.ny - b.ratio * kb * b.ny
            C = (a.ratio * ka * (a.nx * a.p0[0] + a.ny * a.p0[1])
                 - b.ratio * kb * (b.nx * b.p0[0] + b.ny * b.p0[1]))
            if math.hypot(A, B) > 1e-7:
                lines.append((A, B, C))
    return lines


def _add_segment(out, segment):
    if not any(_same_segment(segment, other) for other in out):
        out.append(segment)


def ridge_segments(edges, polygon):
    """Split lines a dibujar, para quien solo quiera el resultado.

    polygon: contorno de la losa en planta, lista de (x, y) sin repetir el
    primer punto al final.
    """
    if len(edges) < 2:
        return []
    return Surface(edges, polygon).ridge_segments()
