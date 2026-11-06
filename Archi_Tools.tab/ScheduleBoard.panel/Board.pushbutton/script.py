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

from pyrevit import forms, script

from archi_common import identity

# Archi_Tools y Archi_LAB conviven, pero ScheduleBoard solo puede estar vivo
# en una de las dos (ver startup.py). Antes de importar schedule_board: esta
# copia podria ser la que no registro nada.
_IS_LAB = identity.is_lab_path(__file__)
if not identity.is_owner(_IS_LAB, identity.read_owner_marker()):
    forms.alert(
        "ScheduleBoard is running from the other tab "
        "({0}).\n\n"
        "Only one copy can be alive per Revit session. To use the one in "
        "this tab, run\n"
        "tools/scheduleboard_owner.ps1 -Owner {1}\n"
        "from the ARCH_Tools repository and restart Revit.".format(
            "Archi_Tools" if _IS_LAB else "Archi_LAB",
            "lab" if _IS_LAB else "tools"),
        title="ScheduleBoard",
        warn_icon=True,
    )
    script.exit()

from schedule_board import pane, window  # noqa: E402

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
