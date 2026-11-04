# -*- coding: utf-8 -*-
"""Self-check de slope_solver.py.  Correr con CPython:  python test_slope_solver.py

Sin framework a proposito: el resto de la herramienta solo existe dentro de
Revit, asi que esto es lo unico verificable en seco.

Escenario base: losa rectangular de 20 x 10 pies, esquinas (0,0) y (20,10).
"""

import math
from slope_solver import (Surface, SlopeEdge, clip_line_to_polygon,
                          describe_conflict, envelope_mode, find_conflicts,
                          point_in_polygon, ridge_segments)

RECT = [(0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0)]

SUR = ((0.0, 0.0), (20.0, 0.0))
NORTE = ((0.0, 10.0), (20.0, 10.0))
ESTE = ((20.0, 0.0), (20.0, 10.0))
OESTE = ((0.0, 0.0), (0.0, 10.0))


def _edges(*spec):
    return [SlopeEdge(p[0], p[1], pct) for (p, pct) in spec]


def _offset(polygon, edges, p):
    """Cota en p: la superficie depende de la losa, no solo de las aristas."""
    return Surface(edges, polygon).offset_at(p)


def _norm(segment):
    """Segmento con los extremos ordenados, para comparar sin importar sentido."""
    a, b = segment
    a = (round(a[0], 3), round(a[1], 3))
    b = (round(b[0], 3), round(b[1], 3))
    return (a, b) if a <= b else (b, a)


def _has_segment(segments, p, q, tol=0.01):
    target = _norm((p, q))
    for s in segments:
        a, b = _norm(s)
        if (abs(a[0] - target[0][0]) < tol and abs(a[1] - target[0][1]) < tol
                and abs(b[0] - target[1][0]) < tol
                and abs(b[1] - target[1][1]) < tol):
            return True
    return False


# --------------------------------------------------------------------------
# Envolvente
# --------------------------------------------------------------------------

def test_una_arista_da_el_mismo_plano_de_siempre():
    # Compatibilidad con Geometry Slope: cota = distancia * ratio.
    edges = _edges((SUR, 5.0))
    assert abs(_offset(RECT, edges, (7.0, 0.0)) - 0.0) < 1e-9
    assert abs(_offset(RECT, edges, (7.0, 4.0)) - 0.20) < 1e-9


def test_dos_paralelas_mismo_signo_hacen_cumbrera_al_medio():
    edges = _edges((SUR, 5.0), (NORTE, 5.0))
    assert find_conflicts(edges) == []
    assert abs(_offset(RECT, edges, (10.0, 5.0)) - 0.25) < 1e-9   # 5% * 5 pies
    assert abs(_offset(RECT, edges, (10.0, 0.0))) < 1e-9          # arista intacta
    assert abs(_offset(RECT, edges, (10.0, 10.0))) < 1e-9


def test_pendientes_distintas_corren_la_cumbrera():
    # Cumbrera donde 5%*d = 1%*(10-d)  ->  d = 10/6 pies desde el sur.
    edges = _edges((SUR, 5.0), (NORTE, 1.0))
    assert find_conflicts(edges) == []
    segments = ridge_segments(edges, RECT)
    assert len(segments) == 1
    (a, b) = segments[0]
    assert abs(a[1] - 10.0 / 6.0) < 0.01
    assert abs(b[1] - 10.0 / 6.0) < 0.01
    # y cada arista sigue mandando su propia pendiente
    assert abs(_offset(RECT, edges, (10.0, 10.0 / 6.0)) - 0.05 * 10.0 / 6.0) < 1e-9


def test_pendientes_negativas_usan_la_envolvente_superior():
    edges = _edges((SUR, -5.0), (NORTE, -5.0))
    assert envelope_mode(edges) == "max"
    assert find_conflicts(edges) == []
    assert abs(_offset(RECT, edges, (10.0, 5.0)) + 0.25) < 1e-9   # canaleta al medio


def test_cuatro_aristas_dan_un_techo_a_cuatro_aguas():
    edges = _edges((SUR, 5.0), (NORTE, 5.0), (ESTE, 5.0), (OESTE, 5.0))
    assert find_conflicts(edges) == []
    assert abs(_offset(RECT, edges, (10.0, 5.0)) - 0.25) < 1e-9   # centro
    assert abs(_offset(RECT, edges, (0.0, 0.0))) < 1e-9           # esquina
    assert abs(_offset(RECT, edges, (2.0, 5.0)) - 0.10) < 1e-9    # manda el oeste

    segments = ridge_segments(edges, RECT)
    assert len(segments) == 5, segments
    assert _has_segment(segments, (5.0, 5.0), (15.0, 5.0))    # cumbrera
    assert _has_segment(segments, (0.0, 0.0), (5.0, 5.0))     # limatesas
    assert _has_segment(segments, (0.0, 10.0), (5.0, 5.0))
    assert _has_segment(segments, (20.0, 0.0), (15.0, 5.0))
    assert _has_segment(segments, (20.0, 10.0), (15.0, 5.0))


# --------------------------------------------------------------------------
# Conflictos
# --------------------------------------------------------------------------

def test_paralelas_con_signo_opuesto_son_irresolubles():
    # El caso de la especificacion: +5% y -5% en dos paralelas.
    edges = _edges((SUR, 5.0), (NORTE, -5.0))
    problems = find_conflicts(edges)
    assert len(problems) == 1
    c = problems[0]
    assert c.kind == "violation"
    assert {c.edge, c.other} == {0, 1}
    assert abs(abs(c.gap_ft) - 0.5) < 1e-9        # 5% * 10 pies
    texto = describe_conflict(c, edges)
    assert "se contradicen" in texto
    assert "0.152 m" in texto                     # 0.5 pies
    assert "una sola arista" in texto


def test_signos_opuestos_tambien_se_detectan_en_perpendiculares():
    edges = _edges((SUR, 5.0), (ESTE, -3.0))
    problems = find_conflicts(edges)
    assert [c.kind for c in problems] == ["violation"]


def test_con_tres_aristas_la_culpa_es_del_par_que_se_contradice():
    # El sur sube y las otras dos bajan: el conflicto es sur-contra-cada-una.
    # Norte y este comparten signo, no se contradicen entre si y no pueden
    # aparecer culpandose mutuamente.
    edges = _edges((SUR, 5.0), (NORTE, -1.0), (ESTE, -2.0))
    problems = find_conflicts(edges)
    assert len(problems) == 2
    for c in problems:
        assert c.kind == "violation"
        assert 0 in (c.edge, c.other)
        assert edges[c.edge].ratio * edges[c.other].ratio < 0
        assert "se contradicen" in describe_conflict(c, edges)


def test_dos_aristas_que_bajan_no_pelean_por_culpa_de_una_tercera():
    # Las mismas dos aristas, ahora sin el sur: siguen siendo compatibles.
    assert find_conflicts(_edges((NORTE, -1.0), (ESTE, -2.0))) == []


def test_pendiente_cero_aplana_todo():
    edges = _edges((SUR, 5.0), (NORTE, 0.0))
    kinds = [c.kind for c in find_conflicts(edges)]
    assert "flat" in kinds
    assert "0%" in describe_conflict(find_conflicts(edges)[0], edges)


def test_colineales_con_distinta_pendiente_se_avisan():
    otra_vez_el_sur = ((5.0, 0.0), (12.0, 0.0))
    edges = _edges((SUR, 5.0), (otra_vez_el_sur, 2.0))
    kinds = [c.kind for c in find_conflicts(edges)]
    assert kinds == ["collinear"]


def test_colineales_con_la_misma_pendiente_se_dejan_pasar():
    otra_vez_el_sur = ((5.0, 0.0), (12.0, 0.0))
    assert find_conflicts(_edges((SUR, 5.0), (otra_vez_el_sur, 5.0))) == []


def test_mismo_signo_nunca_entra_en_conflicto():
    # Invariante del metodo: con todas las pendientes del mismo signo la
    # envolvente vale 0 sobre cada arista, siempre.
    edges = _edges((SUR, 7.5), (NORTE, 1.0), (ESTE, 12.0), (OESTE, 0.5))
    assert find_conflicts(edges) == []
    for e in edges:
        for p in e.sample_points():
            assert abs(_offset(RECT, edges, p)) < 1e-9


# --------------------------------------------------------------------------
# Recorte al contorno
# --------------------------------------------------------------------------

def test_la_cumbrera_se_corta_en_el_rincon_de_una_planta_en_l():
    # Planta en L; las dos aristas largas del rincon exterior al 5%. La
    # limatesa nace en la esquina y muere en el vertice interior de la L.
    ele = [(0.0, 0.0), (20.0, 0.0), (20.0, 10.0),
           (10.0, 10.0), (10.0, 20.0), (0.0, 20.0)]
    edges = _edges((((0.0, 0.0), (20.0, 0.0)), 5.0),
                   (((0.0, 0.0), (0.0, 20.0)), 5.0))
    segments = ridge_segments(edges, ele)
    assert len(segments) == 1
    assert _has_segment(segments, (0.0, 0.0), (10.0, 10.0))


def test_una_sola_arista_no_genera_cumbreras():
    assert ridge_segments(_edges((SUR, 5.0)), RECT) == []


# --------------------------------------------------------------------------
# Entrante: cada arista manda solo frente a su tramo
# --------------------------------------------------------------------------

# Losa de 40 x 30 con un entrante de 15 x 8 mordido en el borde norte.
ENTRANTE = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (25.0, 30.0),
            (25.0, 22.0), (10.0, 22.0), (10.0, 30.0), (0.0, 30.0)]
FONDO = ((10.0, 22.0), (25.0, 22.0))      # fondo del entrante
LADO_IZQ = ((10.0, 22.0), (10.0, 30.0))   # jamba izquierda
LADO_DER = ((25.0, 22.0), (25.0, 30.0))   # jamba derecha


def _entrante_3pct():
    return _edges((FONDO, 3.0), (LADO_IZQ, 3.0), (LADO_DER, 3.0))


def test_una_jamba_corta_no_gobierna_la_losa_entera():
    # El bug reportado: a 12 pies al sur del entrante mandaba una jamba de 8
    # pies que vive al norte, porque se medía a su recta infinita.
    edges = _entrante_3pct()
    assert find_conflicts(edges) == []
    # frente al fondo del entrante manda el fondo: 3% x 12 pies
    assert abs(_offset(ENTRANTE, edges, (17.0, 10.0)) - 0.36) < 1e-9
    # con la recta infinita la jamba izquierda daba 3% x 7 = 0.21 aqui
    assert _offset(ENTRANTE, edges, (17.0, 10.0)) > 0.21


def test_pasado_el_extremo_el_plano_del_vecino_sigue():
    # (2, 10) queda fuera de la franja del fondo y del lado no material de la
    # jamba. En una esquina entrante gana el plano mas alto: el del fondo,
    # extendido, o sea los mismos 3% x 12 pies que justo al sur del entrante.
    edges = _entrante_3pct()
    assert abs(_offset(ENTRANTE, edges, (2.0, 10.0)) - 0.36) < 1e-9
    # la recta infinita de la jamba habria dado 3% x 8 = 0.24
    assert _offset(ENTRANTE, edges, (2.0, 10.0)) > 0.24
    # al noroeste del entrante si manda la jamba: ahi el fondo ya no alcanza
    assert abs(_offset(ENTRANTE, edges, (2.0, 25.0)) - 0.24) < 1e-9


def test_cada_esquina_entrante_da_una_sola_limatesa():
    # Nada de abanicos: los dos planos vecinos se cortan en una recta y ya.
    edges = _entrante_3pct()
    segments = ridge_segments(edges, ENTRANTE)
    assert len(segments) == 2, segments
    # bisectriz a 45 grados desde cada esquina, hasta el borde de la losa
    assert _has_segment(segments, (10.0, 22.0), (0.0, 12.0))
    assert _has_segment(segments, (25.0, 22.0), (40.0, 7.0))


def test_una_arista_no_manda_detras_de_si_misma():
    # Las dos jambas del entrante, enfrentadas, ambas al 3%: no comparten
    # esquina, asi que cada plano se extiende solo. Al este de la jamba
    # derecha el plano de la izquierda vale -0.6 pies; si se le deja competir,
    # gana ese negativo y la losa se hunde donde tendria que subir.
    edges = _edges((LADO_IZQ, 3.0), (LADO_DER, 3.0))
    assert abs(_offset(ENTRANTE, edges, (30.0, 25.0)) - 0.15) < 1e-9
    assert abs(_offset(ENTRANTE, edges, (5.0, 25.0)) - 0.15) < 1e-9

    # y en toda la losa la superficie sube, nunca baja
    surface = Surface(edges, ENTRANTE)
    for x in range(1, 40, 2):
        for y in range(1, 30, 2):
            if point_in_polygon((x, y), ENTRANTE):
                assert surface.offset_at((x, y)) >= -1e-9, (x, y)


def test_entre_dos_aristas_que_se_dan_la_espalda_queda_plano():
    # Las dos jambas, cada una con su pendiente hacia afuera. Entre ambas no
    # llega ninguna: esa franja se queda a la cota de las aristas, plana, y el
    # quiebre es la propia recta de cada arista prolongada.
    edges = _edges((LADO_IZQ, 3.0), (LADO_DER, 3.0))
    surface = Surface(edges, ENTRANTE)
    for y in (2.0, 10.0, 20.0):
        assert abs(surface.offset_at((17.0, y))) < 1e-9        # entre ambas
    assert abs(surface.offset_at((5.0, 10.0)) - 0.15) < 1e-9   # afuera, al oeste
    assert abs(surface.offset_at((30.0, 10.0)) - 0.15) < 1e-9  # afuera, al este

    segments = surface.ridge_segments()
    assert len(segments) == 2, segments
    assert _has_segment(segments, (10.0, 22.0), (10.0, 0.0))
    assert _has_segment(segments, (25.0, 22.0), (25.0, 0.0))


def test_aristas_colineales_sin_solape_no_son_conflicto():
    # Dos tramos seguidos de un mismo borde, cada uno con su pendiente: ahora
    # que cada uno manda frente a si mismo, es una configuracion valida.
    primero = ((0.0, 0.0), (10.0, 0.0))
    segundo = ((12.0, 0.0), (20.0, 0.0))
    assert find_conflicts(_edges((primero, 5.0), (segundo, 2.0))) == []


def test_point_in_polygon_en_planta_en_u():
    u = [(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (20.0, 20.0),
         (20.0, 10.0), (10.0, 10.0), (10.0, 20.0), (0.0, 20.0)]
    assert point_in_polygon((5.0, 15.0), u)
    assert not point_in_polygon((15.0, 15.0), u)      # el hueco de la U
    assert point_in_polygon((15.0, 5.0), u)


def test_la_recta_se_recorta_en_los_dos_brazos_de_la_u():
    u = [(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (20.0, 20.0),
         (20.0, 10.0), (10.0, 10.0), (10.0, 20.0), (0.0, 20.0)]
    origin, direction, spans = clip_line_to_polygon(0.0, 1.0, 15.0, u)
    assert len(spans) == 2
    anchos = sorted(round(t1 - t0, 6) for (t0, t1) in spans)
    assert anchos == [10.0, 10.0]


if __name__ == "__main__":
    pruebas = [v for k, v in sorted(globals().items())
               if k.startswith("test_") and callable(v)]
    for prueba in pruebas:
        prueba()
        print("ok   " + prueba.__name__)
    print("\n{} pruebas ok".format(len(pruebas)))
