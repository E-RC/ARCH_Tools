# -*- coding: UTF-8 -*-
"""Quien soy: la copia de desarrollo (Archiplan_Lab) o la instalada (Archiplan).

Las dos extensiones conviven en Revit. La de desarrollo vive en una carpeta
cuyo nombre termina en `_Lab.extension`; la publicada, en cualquier otra.
Las funciones puras reciben la ruta, asi se prueban sin Revit; las que no
la reciben usan este mismo archivo.
"""

import os

LAB_SUFFIX = "_lab.extension"
DATA_DIR_LAB = "Archiplan_Lab"
DATA_DIR_RELEASE = "Archiplan"
# Valor del marcador de dueno de ScheduleBoard que significa "la copia Lab".
OWNER_LAB = "lab"


def find_extension_root(path):
    """Sube desde `path` hasta la carpeta que termina en `.extension`; None si no hay."""
    current = os.path.normpath(path)
    while True:
        if os.path.basename(current).lower().endswith(".extension"):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def is_lab_path(path):
    root = find_extension_root(path)
    return root is not None and os.path.basename(root).lower().endswith(LAB_SUFFIX)


def is_owner(is_lab_copy, owner_value):
    """True si esta copia es la que debe tener ScheduleBoard vivo.

    `owner_value` es el texto del marcador (`lab`), o None si no existe.
    Ausente o cualquier otra cosa = dueno Tools.
    """
    return bool(is_lab_copy) == ((owner_value or "").strip().lower() == OWNER_LAB)


def read_owner_marker():
    """Texto del marcador `owner` (en la carpeta de datos de ScheduleBoard), o None si falta."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    try:
        handle = open(os.path.join(base, "ScheduleBoard", "owner"), "r")
        try:
            return handle.read()
        finally:
            handle.close()
    except (IOError, OSError):
        return None


def extension_root():
    return find_extension_root(os.path.abspath(__file__))


def is_lab():
    return is_lab_path(os.path.abspath(__file__))


def data_dir_name():
    return DATA_DIR_LAB if is_lab() else DATA_DIR_RELEASE
