# -*- coding: UTF-8 -*-
"""Abre el board: en su propia ventana, o con Shift+clic en el dock.

Este archivo es el gesto y nada mas. pyRevit 7 borra las globales del script
al terminar, y la ventana sigue viva despues, asi que todo lo que ella
necesita vive en `schedule_board.window` (ver su encabezado).

- Clic: ventana independiente (`window.py`). No exige un documento abierto y
  se puede llevar a otro monitor.
- Shift+clic: el dockable pane de siempre (`pane.py`). El boton NO lo
  registra: eso pasa en `startup.py`, porque `RegisterDockablePane` desde un
  comando deja el pane inservible en esa sesion (verificado). El WebView2 si
  lo crea este clic, via `pane.show()` -> `ensure_content()`.
"""

from pyrevit import forms

from schedule_board import pane, window

uiapp = __revit__  # noqa: F821 - lo inyecta pyRevit

try:
    use_dock = bool(__shiftclick__)  # noqa: F821 - lo inyecta pyRevit
except NameError:
    use_dock = False

if not use_dock:
    try:
        window.show(uiapp)
    except Exception as ex:
        forms.alert(
            "The ScheduleBoard window could not be opened.\n\n"
            "Details: {0}".format(ex),
            title="ScheduleBoard",
            warn_icon=True,
        )
elif getattr(uiapp, "ActiveUIDocument", None) is None:
    # Revit no CREA un dockable pane registrado hasta que hay documento:
    # verificado, con Revit abierto y vacio `GetDockablePane` contesta
    # "has not been created yet" aunque `PaneIsRegistered` diga True.
    forms.alert(
        "Open a model first.\n\n"
        "Revit only creates the ScheduleBoard pane once a document is "
        "open, so there is nothing to show yet.",
        title="ScheduleBoard",
        warn_icon=True,
    )
else:
    try:
        pane.show(uiapp)
    except Exception as ex:
        # El caso frecuente y con salida clara: el pane no quedo
        # registrado en el arranque, casi siempre porque `lib/` cambio y
        # Revit no se reinicio. El Reload de pyRevit 7 no alcanza: el pane
        # se registra una sola vez por sesion.
        forms.alert(
            "The ScheduleBoard pane is not available in this Revit "
            "session.\n\n"
            "It is registered while Revit starts up, so if the extension "
            "was just installed or updated, restart Revit.\n\n"
            "Details: {0}".format(ex),
            title="ScheduleBoard",
            warn_icon=True,
        )
