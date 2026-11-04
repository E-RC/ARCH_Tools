# -*- coding: utf-8 -*-
"""Parseo de paletas de color y de codigos hex escritos a mano.

A proposito no importa nada de Revit ni de pyRevit: es la unica logica de
la herramienta que se puede ejecutar fuera de Revit, y test_palette.py la
verifica con CPython normal.
"""

import re

HEX_RE       = re.compile(r'#([0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})\b')
BARE_HEX_RE  = re.compile(r'(?<![0-9A-Fa-f#])([0-9A-Fa-f]{6})(?![0-9A-Fa-f])')
RGB_FUNC_RE  = re.compile(r'rgb\s*\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)')
HEX_INPUT_RE = re.compile(r'^#?([0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})$')


def is_valid_rgb(rgb):
    """DB.Color revienta con componentes fuera de 0-255, y los archivos de
    paleta son entrada externa: se validan antes de llegar a Revit."""
    return all(0 <= c <= 255 for c in rgb)


def expand_hex(hexval):
    """'abc' o 'aabbcc' -> (r, g, b)."""
    if len(hexval) == 3:
        hexval = "".join(ch * 2 for ch in hexval)
    return (int(hexval[0:2], 16), int(hexval[2:4], 16), int(hexval[4:6], 16))


def format_hex(rgb):
    """(r, g, b) -> '#RRGGBB'.

    El int() es obligatorio, no cosmetico: los componentes que llegan de la
    API de Revit son System.Byte, y en IronPython el spec '{:02X}' no se
    aplica sobre un Byte -- devuelve el decimal mas el spec en crudo
    ('1362X' en vez de '88'), que despues parse_hex_input rechaza.
    """
    return "#{:02X}{:02X}{:02X}".format(int(rgb[0]), int(rgb[1]), int(rgb[2]))


def parse_hex_input(text):
    """Lo que el usuario escribe en el campo hex del picker.
    Acepta '#B26E63', 'b26e63', '#abc'. Devuelve None si no calza."""
    if not text:
        return None
    match = HEX_INPUT_RE.match(text.strip())
    return expand_hex(match.group(1)) if match else None


def parse_colors_from_text(text):
    """Colores en orden de aparicion y sin deduplicar: cubre SVG
    (fill="#abc", stop-color:#abc, rgb(...)) y listas de texto/CSV."""
    matches = [m.group(1) for m in HEX_RE.finditer(text)]
    if matches:
        return [expand_hex(h) for h in matches]

    rgb_matches = [(int(r), int(g), int(b)) for r, g, b in RGB_FUNC_RE.findall(text)]
    rgb_matches = [c for c in rgb_matches if is_valid_rgb(c)]
    if rgb_matches:
        return rgb_matches

    return [expand_hex(h) for h in BARE_HEX_RE.findall(text)]


def parse_gpl(text):
    """GIMP .gpl: lineas 'R G B  nombre', ignorando cabecera y comentarios."""
    colors = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.upper().startswith("GIMP") \
           or line.upper().startswith("NAME") or line.upper().startswith("COLUMNS"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            rgb = (int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            continue
        if is_valid_rgb(rgb):
            colors.append(rgb)
    return colors


def parse_palette_file(path):
    with open(path, "r") as f:
        text = f.read()
    if path.lower().endswith(".gpl"):
        return parse_gpl(text)
    return parse_colors_from_text(text)
