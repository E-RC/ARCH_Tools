# -*- coding: utf-8 -*-
"""Self-check de palette.py.  Correr con CPython:  python test_palette.py

Sin framework a proposito: el resto de la herramienta solo existe dentro de
Revit, asi que esto es lo unico verificable en seco.
"""

from palette import (expand_hex, format_hex, parse_hex_input,
                     parse_colors_from_text, parse_gpl)


def test_expand_hex_largo_y_corto():
    assert expand_hex("B26E63") == (0xB2, 0x6E, 0x63)
    assert expand_hex("abc") == (0xAA, 0xBB, 0xCC)


def test_format_hex_siempre_mayusculas_de_6():
    assert format_hex((178, 110, 99)) == "#B26E63"
    assert format_hex((0, 0, 0)) == "#000000"


def test_format_hex_convierte_componentes_no_int():
    # Revit entrega System.Byte, que en IronPython ignora el spec '{:02X}'.
    # Aca se simula con float: lo que se fija es que format_hex coacciona.
    assert format_hex((136.0, 113.0, 66.0)) == "#887142"


def test_parse_hex_input_acepta_lo_razonable():
    assert parse_hex_input("#B26E63") == (178, 110, 99)
    assert parse_hex_input("b26e63") == (178, 110, 99)
    assert parse_hex_input("  #abc  ") == (170, 187, 204)


def test_parse_hex_input_rechaza_lo_demas():
    assert parse_hex_input("") is None
    assert parse_hex_input(None) is None
    assert parse_hex_input("#B26E6") is None
    assert parse_hex_input("#B26E633") is None
    assert parse_hex_input("terracota") is None


def test_parse_svg_respeta_orden_y_repetidos():
    svg = '<rect fill="#B26E63"/><rect fill="#333333"/><rect fill="#B26E63"/>'
    assert parse_colors_from_text(svg) == [(178, 110, 99), (51, 51, 51), (178, 110, 99)]


def test_parse_rgb_func_solo_si_no_hay_hex():
    assert parse_colors_from_text("rgb(1, 2, 3) rgb(4,5,6)") == [(1, 2, 3), (4, 5, 6)]
    assert parse_colors_from_text("#000000 rgb(1,2,3)") == [(0, 0, 0)]


def test_parse_rgb_func_descarta_fuera_de_rango():
    assert parse_colors_from_text("rgb(300,0,0) rgb(1,2,3)") == [(1, 2, 3)]


def test_parse_lista_de_hex_pelados():
    assert parse_colors_from_text("B26E63\n333333\n") == [(178, 110, 99), (51, 51, 51)]


def test_parse_gpl_ignora_cabecera_y_lineas_malas():
    gpl = "\n".join([
        "GIMP Palette",
        "Name: demo",
        "Columns: 4",
        "# comentario",
        "178 110  99\tTerracotta",
        "51 51 51 Graphite",
        "300 0 0 fuera de rango",
        "no numeros",
        "",
    ])
    assert parse_gpl(gpl) == [(178, 110, 99), (51, 51, 51)]


if __name__ == "__main__":
    pruebas = [v for k, v in sorted(globals().items())
               if k.startswith("test_") and callable(v)]
    for prueba in pruebas:
        prueba()
        print("ok   " + prueba.__name__)
    print("\n{} pruebas ok".format(len(pruebas)))
