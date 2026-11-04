# -*- coding: utf-8 -*-
"""Warnings Analyzer: dashboard interactivo de warnings sobre WebView2.

Todo el codigo vive en `warnings_window.py` (ventana y puente) y
`warnings_core.py` (Revit). Este script solo lo arranca: pyRevit borra las
globales del script al terminar, y la ventana sigue viva despues.
"""
import os
import sys

_BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BUNDLE_DIR not in sys.path:
    sys.path.insert(0, _BUNDLE_DIR)

import warnings_window  # noqa: E402

warnings_window.main(__revit__)  # noqa: F821 (lo inyecta pyRevit)
