# -*- coding: utf-8 -*-
"""Rutas de datos de usuario, fuera del arbol de la extension (que es un clon de git)."""
import errno
import os

from archi_common.identity import data_dir_name


def user_data_path(*parts):
    """Devuelve %APPDATA%\<Archiplan o Archiplan_Lab>\<parts> y crea la carpeta padre; no crea el archivo."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, data_dir_name(), *parts)
    parent = os.path.dirname(path)
    try:
        os.makedirs(parent)
    except OSError as e:
        # IronPython 2.7 no tiene exist_ok
        if e.errno != errno.EEXIST or not os.path.isdir(parent):
            raise
    return path
