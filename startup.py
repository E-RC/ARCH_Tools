# -*- coding: UTF-8 -*-
"""ScheduleBoard - punto de entrada de la extension.

pyRevit ejecuta este archivo al cargar la extension. Sus dos
responsabilidades son crear la API de Routes y registrar el dockable pane
donde vive el board.

Base de la API:  http://127.0.0.1:48884/schedule_board/

El servidor de Routes debe estar activo:
    Revit -> pestana pyRevit -> Settings -> Routes -> Routes Server

Compatibilidad: este archivo y todo `lib/schedule_board/` deben correr
tanto en IronPython 2.7 como en CPython 3.

POR QUE ESTE ARCHIVO ESCRIBE UN LOG PROPIO
------------------------------------------
Porque un fallo aqui es invisible. En esta maquina no hay ningun archivo
de log de pyRevit que se pueda leer despues -- se buscaron y no existen --
y un `except` silencioso en el arranque es la receta para perder una
tarde. La primera version del registro del pane se perdio exactamente
asi: `__revit__` llegaba en `None` y el error se lo comio el `except`.

El archivo queda en `%LOCALAPPDATA%\\ScheduleBoard\\startup.log` y se
reescribe en cada arranque.
"""

import logging
import os

logger = logging.getLogger(__name__)

API_NAME = "schedule_board"

_LOG_STARTED = []


# UNA SOLA COPIA VIVA DE SCHEDULEBOARD
# ------------------------------------
# Con las extensiones Archiplan (Archi_Tools) y Archiplan_Lab (Archi_LAB)
# cargadas a la vez, la API `schedule_board`, el pane (GUID fijo) y la URL
# fija solo admiten una copia; la segunda en arrancar terminaria usando el
# codigo de la primera. Por eso solo la copia "duena" registra algo. El
# marcador `%LOCALAPPDATA%\ScheduleBoard\owner` dice cual: `lab` = Lab;
# ausente o cualquier otra cosa = Tools (tools/scheduleboard_owner.ps1).
#
# Este bloque usa SOLO la libreria estandar y va ANTES de importar nada de
# `schedule_board` ni de `archi_common`: el motor de arranque es persistente
# y comparte modulos entre extensiones. Duplica a proposito el criterio de
# `archi_common.identity` (is_lab_path / is_owner); un test comprueba que
# coinciden.

def _is_lab_startup(path):
    """True si la carpeta que contiene este archivo termina en `_Lab.extension`."""
    folder = os.path.basename(os.path.dirname(os.path.abspath(path)))
    return folder.lower().endswith("_lab.extension")


def _is_owner(is_lab, owner_value):
    return bool(is_lab) == ((owner_value or "").strip().lower() == "lab")


def _data_dir():
    return os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ScheduleBoard")


def _read_owner():
    try:
        handle = open(os.path.join(_data_dir(), "owner"), "r")
        try:
            return handle.read()
        finally:
            handle.close()
    except (IOError, OSError):
        return None


def _log_not_owner(is_lab):
    """Anota en startup.log (al final, sin importar schedule_board) que esta copia cede. Nunca lanza."""
    message = ("esta copia ({0}) NO es la duena de ScheduleBoard; no registra rutas, "
               "pane ni suscripciones. Duena segun el marcador: {1}".format(
                   "Archi_LAB" if is_lab else "Archi_Tools",
                   "Archi_Tools" if is_lab else "Archi_LAB"))
    logger.info("ScheduleBoard: %s", message)
    try:
        folder = _data_dir()
        if not os.path.isdir(folder):
            os.makedirs(folder)
        handle = open(os.path.join(folder, "startup.log"), "a")
        try:
            handle.write(message + "\n")
        finally:
            handle.close()
    except Exception:
        pass


def _log_path():
    # Aca adentro y no arriba: `_log` lo llama bajo su `try`, asi que ni un
    # fallo al importar puede tumbar el arranque que el log viene a explicar.
    from schedule_board.paths import data_path
    return data_path("startup.log")


def _log(message):
    """Deja rastro del arranque en un archivo legible. Nunca lanza."""
    logger.info("ScheduleBoard: %s", message)
    try:
        path = _log_path()
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        # La primera linea de cada arranque reescribe el archivo; el resto
        # se agrega. Interesa el estado de ESTA sesion, no un historial
        # que crece sin techo.
        mode = "a" if _LOG_STARTED else "w"
        _LOG_STARTED.append(1)
        handle = open(path, mode)
        try:
            handle.write(message + "\n")
        finally:
            handle.close()
    except Exception:
        pass


def register():
    """Registra todas las rutas de ScheduleBoard."""
    try:
        from pyrevit import routes
        from schedule_board.api_routes import register_routes

        api = routes.API(API_NAME)
        register_routes(api)
        _log("rutas registradas en /{0}/".format(API_NAME))
    except Exception as ex:
        # Un fallo aqui deja la extension muda pero no debe tumbar pyRevit.
        _log("FALLO al registrar rutas: {0}: {1}".format(
            type(ex).__name__, ex))
        raise


def _ui_handle():
    """Algo que sepa `RegisterDockablePane`, o `(None, None)`.

    NO SE PUEDE CONFIAR EN `__revit__` A SECAS, y eso costo un reinicio:
    pyRevit corre los startup scripts con
    `CommandData.Application = HOST_APP.uiapp`, y esa propiedad devuelve
    `__revit__` **solo si es un `UIApplication`** (ver el `isinstance` en
    `pyrevit/__init__.py`). En el arranque es un `UIControlledApplication`,
    asi que devuelve `None` y el script recibe `None`.

    De ahi que se prueben varios origenes y se elija el primero que de
    verdad tenga el metodo, en vez de asumir cual es.
    """
    candidates = []

    try:
        candidates.append(("__revit__", __revit__))  # noqa: F821
    except NameError:
        candidates.append(("__revit__", None))

    # El nombre lo inyecta el ejecutor de C# en los builtins del motor,
    # asi que puede estar ahi aunque no llegue al scope del modulo.
    try:
        import __builtin__ as builtins_module
    except ImportError:  # CPython 3
        import builtins as builtins_module
    candidates.append(("builtins.__revit__",
                       getattr(builtins_module, "__revit__", None)))

    try:
        from pyrevit import HOST_APP

        candidates.append(("HOST_APP.uiapp", HOST_APP.uiapp))
    except Exception:
        pass

    for name, candidate in candidates:
        if candidate is not None and hasattr(candidate, "RegisterDockablePane"):
            return name, candidate

    _log("ningun handle sirve para RegisterDockablePane ({0})".format(
        ", ".join("{0}={1}".format(name, type(value).__name__)
                  for name, value in candidates)))
    return None, None


def register_pane():
    """Registra el dockable pane que muestra el board dentro de Revit.

    TIENE QUE SER AQUI Y NO EN EL BOTON: verificado que
    `RegisterDockablePane` desde un comando "funciona" pero deja el pane
    inservible en esa sesion -- `GetDockablePane` contesta "The requested
    dockable pane has not been created yet".

    NO PROPAGA EL ERROR, a diferencia de las rutas. La API es el producto;
    el pane es la forma de mirarlo. Si el pane falla, el board sigue
    accesible por HTTP desde un navegador, asi que tumbar la extension
    entera por esto seria desproporcionado. Pero ahora **queda escrito**
    en el log de arranque, que es lo que faltaba.
    """
    try:
        from schedule_board import pane

        source, handle = _ui_handle()
        if handle is None:
            _log("pane NO registrado: sin handle de UI")
            return
        if pane.register(handle) is None:
            # Segunda corrida de este archivo en la misma sesion de Revit:
            # o sea un Reload de pyRevit. NO es un problema desde que el
            # pane muestra un Grid compartido en el AppDomain en vez de
            # objetos de un motor concreto: el boton lo rellena con
            # contenido del motor vivo. Ver `pane.ensure_content`.
            _log("pane ya estaba registrado (hubo Reload de pyRevit); "
                 "el Grid compartido sigue sirviendo")
            return
        _log("dockable pane registrado (handle desde {0}: {1})".format(
            source, type(handle).__name__))
    except Exception as ex:
        _log("FALLO al registrar el pane: {0}: {1}".format(
            type(ex).__name__, ex))


def subscribe_changes():
    """Escucha los cambios del modelo para que el board sepa que quedo viejo.

    [Fase 23] El handler vive en `schedule_board.changes`, y la suscripcion
    en el AppDomain: si ya habia una de un arranque anterior (un Reload de
    pyRevit), `changes.subscribe` la quita antes de poner esta, asi que
    nunca quedan dos handlers ni uno de un motor muerto.

    NO PROPAGA EL ERROR, como el pane: sin esto el board funciona igual,
    solo que sin avisar cuando algo cambio en Revit.
    """
    try:
        from schedule_board import changes

        source, handle = _ui_handle()
        # En el arranque es un `UIControlledApplication`; tras un Reload
        # puede ser un `UIApplication`. Los dos exponen los eventos de
        # documento por una propiedad distinta.
        application = None
        for attr in ("ControlledApplication", "Application"):
            application = getattr(handle, attr, None) if handle else None
            if application is not None:
                break
        if application is None:
            _log("cambios del modelo NO suscritos: sin handle de aplicacion")
            return
        result = changes.subscribe(application)
        _log("cambios del modelo suscritos ({0}; {1})".format(
            ", ".join(result["events"]),
            "reemplazo la suscripcion anterior" if result["replaced"]
            else "primera suscripcion de la sesion"))
    except Exception as ex:
        _log("FALLO al suscribir los cambios del modelo: {0}: {1}".format(
            type(ex).__name__, ex))


def silence_console():
    """Manda `print` y los errores de ESTE motor a ningun lado.  [Fase 21]

    Los handlers sin `doc` (`/progress/`, `/audit/`, los preflight) corren
    en hilos del servidor, que no son STA, pero con los globales de este
    motor. Si uno imprimiera, pyRevit reconstruiria su consola de salida
    --una ventana WPF-- en ese hilo si el usuario la cerro, y eso mata
    Revit (`ScriptOutput.window`). Hoy
    ninguno imprime: esto es para el proximo `print` o `warnings.warn` que
    alguien agregue sin saberlo.

    Va al FINAL porque el arranque ya termino de hablar, y el diagnostico
    vive en `startup.log`, no en la consola.
    """
    import sys

    sink = open(os.devnull, "w")
    sys.stdout = sink
    sys.stderr = sink


_IS_LAB = _is_lab_startup(__file__)
if _is_owner(_IS_LAB, _read_owner()):
    register()
    register_pane()
    subscribe_changes()
    silence_console()
else:
    _log_not_owner(_IS_LAB)
