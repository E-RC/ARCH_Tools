# -*- coding: UTF-8 -*-
"""La carpeta de datos de ScheduleBoard.  [Fase 27]

`%LOCALAPPDATA%\ScheduleBoard\...`, fuera de carpetas sincronizadas a proposito (el
rastro, los interruptores `writes-disabled` y `dev-mode`). El unico lugar
de todo el proyecto que arma esa ruta: `startup.py` (el log del arranque) y
`pane.py` (el perfil de WebView2) lo usan desde el 27-09-2026.

Es un modulo y no una funcion en `__init__.py` porque `/dev/reload/` no
recarga el paquete: un nombre nuevo ahi no llegaria al motor vivo.
"""

import os


def data_path(*parts):
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "ScheduleBoard", *parts)
