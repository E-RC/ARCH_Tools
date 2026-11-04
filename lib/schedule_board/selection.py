# -*- coding: UTF-8 -*-
"""Seleccion y aislamiento en Revit.  [Fase 3]

Es lo que convierte el board en algo bidireccional: seleccionas filas y
los elementos quedan seleccionados y resaltados en la vista de Revit.

Solo es posible porque cada fila lleva su ElementId, gracias a la
estrategia de reconstruir la tabla (regla 1 de `schedule_board/__init__.py`).

API relevante:
    UIDocument.Selection.SetElementIds(ids)   sin transaccion
    UIDocument.ShowElements(ids)              sin transaccion.
                                              OJO: cambia el documento activo
    View.IsolateElementsTemporary(ids)        REQUIERE transaccion
    View.DisableTemporaryViewMode(...)        REQUIERE transaccion

`IsolateElementsTemporary` y `DisableTemporaryViewMode` son las unicas
escrituras de v1. Son modificaciones de estado de VISTA, reversibles y
sin efecto sobre los datos del modelo, pero igual entran en la pila de
deshacer de Revit (regla 3: una operacion de usuario = una transaccion).

Los handlers de estas rutas deben declarar `uidoc` en su firma para
recibir el UIDocument y correr en contexto de API.

`ShowElements` (navegar hasta los elementos) queda fuera a proposito:
cambia el documento activo, y el riesgo de producto (planteamiento 6.2)
pide confirmarlo en la UI en vez de dispararlo con cada clic. Si algun
dia se hace, es una ruta nueva; el stub se saco en la Fase 27.
"""

from Autodesk.Revit import DB
from System.Collections.Generic import List

from schedule_board import serializers as ser


class IsolateNotPossible(Exception):
    """La vista activa no puede mostrar un aislamiento de elementos.

    Se distingue de un fallo inesperado: es un "no, porque...". `code`
    es el identificador estable para el cliente.
    """

    def __init__(self, message, code):
        Exception.__init__(self, message)
        self.code = code


# Tipos de vista que NO muestran geometria del modelo, asi que aislar en
# ellas no puede tener ningun efecto visible.
#
# Se necesitan DOS guardas y no una, porque
# `CanUseTemporaryVisibilityModes()` no alcanza. Medido sobre los 13
# tipos de vista del modelo de referencia:
#
#   Schedule, ProjectBrowser, SystemBrowser  -> False
#   DrawingSheet, Legend, DraftingView       -> True (!)
#
# O sea que la API dice que una HOJA si admite modos de visibilidad
# temporal -- y es verdad para lo que la hoja dibuja--, pero una hoja no
# muestra geometria del modelo: la muestran las vistas colocadas en
# ella. Ese es exactamente el caso que quedo como limitacion conocida en
# la Fase 3, y el que esta lista cubre.
_VIEW_TYPES_WITHOUT_MODEL = (
    "DrawingSheet",
    "Legend",
    "DraftingView",
)


def _check_can_isolate(view):
    """Lanza IsolateNotPossible si aislar en esta vista no haria nada.

    Antes de la Fase 7 esto no se chequeaba: `IsolateElementsTemporary`
    NO lanza excepcion en una hoja ni en una tabla, la transaccion se
    confirma y `IsInTemporaryViewMode` pasa a True, pero no se ve nada.
    Silencioso y confuso.
    """
    name = ser.safe_name(view) or "unnamed"

    try:
        view_type = str(view.ViewType)
    except Exception:
        view_type = None

    try:
        can_use = bool(view.CanUseTemporaryVisibilityModes())
    except Exception:
        can_use = True

    if not can_use:
        raise IsolateNotPossible(
            "The active view in Revit is '{0}' ({1}), which does not "
            "support isolation. Open a model view -- a plan, a section or the "
            "3D -- and try again.".format(name, view_type or "?"),
            "view_cannot_isolate",
        )

    if view_type in _VIEW_TYPES_WITHOUT_MODEL:
        raise IsolateNotPossible(
            "The active view in Revit is '{0}' ({1}), which does not show "
            "model geometry: isolating there would not change anything you "
            "see. Open a plan, a section or the 3D.".format(
                name, view_type),
            "view_without_model",
        )


def _to_id_list(element_ids):
    """Convierte enteros de Python a un List[ElementId] de .NET.

    `SetElementIds` e `IsolateElementsTemporary` piden una coleccion
    tipada (`ICollection<ElementId>`), no un enumerable de Python
    cualquiera.

    [20-10-2026] Un valor que no convierte a entero (un texto, un `None`
    colado) se salta, no revienta: mismo patron tolerante que el resto del
    proyecto usa para IDs del cliente (`writer._set_parameter`, con
    `requested`). El `Count` de la lista resultante -- que es lo unico que
    el cliente ve (`selected_count`/`isolated_count`) -- ya refleja lo que
    de verdad se selecciono o aislo.
    """
    ids = List[DB.ElementId]()
    for value in element_ids:
        try:
            ids.Add(ser.to_element_id(value))
        except Exception:
            continue
    return ids


def select_elements(uidoc, element_ids):
    """Selecciona los elementos en Revit. Sin transaccion.  [Fase 3]

    Resalta los elementos en cualquier vista que el usuario ya tenga
    abierta (2D o 3D) sin cambiar el documento o la vista activa --
    esa es la diferencia deliberada con `ShowElements`.
    """
    id_list = _to_id_list(element_ids)
    uidoc.Selection.SetElementIds(id_list)
    return {"selected_count": id_list.Count}


def isolate_elements(uidoc, element_ids):
    """Aisla temporalmente los elementos en la vista activa.  [Fase 3]

    REQUIERE transaccion. Actua sobre `uidoc.ActiveView`, que es un
    estado de la VISTA, no de la tarjeta ni del schedule: aislar desde
    una tarjeta y despues desde otra debe reemplazar el conjunto
    aislado, no acumularlo.

    VERIFICADO CONTRA REVIT REAL (12-08-2026): llamar
    `IsolateElementsTemporary` una segunda vez mientras la vista YA
    esta en modo aislado NO reemplaza el conjunto de forma limpia --
    deja la vista en un estado roto donde no se ve ni el conjunto
    viejo ni el nuevo. Contradice la suposicion original (que la propia
    documentacion de la API bastaba). El fix: si la vista ya esta en
    `TemporaryHideIsolate`, apagarlo primero con
    `DisableTemporaryViewMode` antes de volver a isolar, en la MISMA
    transaccion -- asi el usuario ve un solo aislamiento en la pila de
    deshacer, no dos.

    Si la vista activa no es una vista grafica (por ejemplo, el
    usuario tiene una tabla o una hoja de planos como vista activa),
    `IsolateElementsTemporary` NO lanza excepcion: la transaccion se
    confirma y `IsInTemporaryViewMode` pasa a True, pero sin ningun
    efecto visual real. Eso quedo como limitacion conocida en la Fase 3
    ("no hay forma de detectarlo de antemano sin API interna") y en la
    Fase 7 se cerro: SI hay forma publica, y son dos chequeos. Ver
    `_check_can_isolate`.
    """
    view = uidoc.ActiveView
    _check_can_isolate(view)
    id_list = _to_id_list(element_ids)

    def isolate():
        if view.IsInTemporaryViewMode(DB.TemporaryViewMode.TemporaryHideIsolate):
            view.DisableTemporaryViewMode(DB.TemporaryViewMode.TemporaryHideIsolate)
        view.IsolateElementsTemporary(id_list)

    _view_transaction(uidoc.Document, "ScheduleBoard: isolate elements", isolate)

    return {
        "isolated_count": id_list.Count,
        "view_id": ser.element_id_value(view.Id),
        "view_name": ser.safe_name(view),
    }


def _view_transaction(doc, name, change):
    """La transaccion de un cambio de VISTA (aislar), no de datos.  [Fase 22]

    NO pasa por `writer._commit`, a proposito: aislar no escribe un dato
    del modelo, asi que no lleva la guarda de solo lectura -- el equipo
    aisla aunque su maquina no pueda escribir -- ni deja linea en el
    rastro de escritura. Lo que SI comparte con `_commit` son las dos
    defensas que le faltaban (bug B4): el `_FailureCatcher`, para que un
    error de Revit no abra un modal que cuelgue la API, y mirar lo que
    devuelve `Commit()`, que puede ser `RolledBack` sin lanzar nada.
    """
    from schedule_board import writer

    catcher = writer._FailureCatcher()
    transaction = DB.Transaction(doc, name)
    transaction.Start()
    try:
        options = transaction.GetFailureHandlingOptions()
        options.SetFailuresPreprocessor(catcher)
        options.SetClearAfterRollback(True)
        transaction.SetFailureHandlingOptions(options)
        change()
        status = transaction.Commit()
    except Exception:
        if transaction.HasStarted():
            transaction.RollBack()
        raise
    if catcher.errors or str(status) == "RolledBack":
        raise writer.RevitRefused(
            "Revit refused the change to the view.",
            detail=" | ".join(catcher.errors) or None)


def clear_isolation(uidoc):
    """Quita el aislamiento temporal de la vista activa.  [Fase 3]

    REQUIERE transaccion. No falla si la vista no estaba aislada --
    `IsInTemporaryViewMode` lo distingue antes de intentar apagarlo,
    para no ensuciar la pila de deshacer con una transaccion vacia
    cada vez que el usuario aprieta "Show all" de mas.
    """
    view = uidoc.ActiveView

    def clear():
        if view.IsInTemporaryViewMode(DB.TemporaryViewMode.TemporaryHideIsolate):
            view.DisableTemporaryViewMode(DB.TemporaryViewMode.TemporaryHideIsolate)

    _view_transaction(uidoc.Document, "ScheduleBoard: clear isolation", clear)

    return {
        "view_id": ser.element_id_value(view.Id),
        "view_name": ser.safe_name(view),
    }
