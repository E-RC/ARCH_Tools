# -*- coding: UTF-8 -*-
"""El board dentro de Revit, en un dockable pane con WebView2.

POR QUE ESTE MODULO EXISTE
--------------------------
Hasta aqui la extension era headless: `startup.py` mas `lib/`, sin ningun
boton. El board se lanzaba a mano con `npm run dev` y un navegador, que
es lo que sirvio para verificar las 12 fases pero no para trabajar.

TRES COSAS SE VERIFICARON ANTES DE ESCRIBIR ESTO, y las tres cambian el
diseno:

1. `RegisterDockablePane` SI se puede llamar desde un comando, pero el
   pane queda inservible en esa sesion: `GetDockablePane` responde "The
   requested dockable pane has not been created yet". Por eso el registro
   vive en `startup.py` y el boton solo MUESTRA.

2. pyRevit Routes NO PUEDE servir el frontend. `parse_response` de
   `handler.py` pasa toda respuesta por `_safe_json_dumps` y ademas
   sobrescribe el `Content-Type` a `application/json`, asi que un
   `<html>` saldria convertido en un string JSON entre comillas. De ahi
   que los archivos se sirvan con el mapeo de host virtual de WebView2 y
   no por la API.

3. Se navega por **http** y no por https a proposito. La API es
   `http://127.0.0.1:<puerto>`, y una pagina servida por https no puede
   hacerle fetch (mixed content). Con `http://scheduleboard.local` el
   esquema coincide y las cabeceras CORS que ya pone `http.py` alcanzan.
   Verificado: 240 tarjetas y el punto de conexion en verde desde dentro
   de Revit.

LA TRAMPA QUE COSTO ENCONTRAR
-----------------------------
Revit trae `Microsoft.Web.WebView2.Core.dll` y `...Wpf.dll` **con el
mismo numero de version (1.0.1343.22) y contenidos que no se
corresponden**: el Wpf llama a un constructor de
`CoreWebView2EnvironmentOptions` de CINCO argumentos (el que lleva
`List<CoreWebView2CustomSchemeRegistration>`) y el Core que se envia solo
tiene el de CUATRO. Resultado: cualquier camino que pase por
`CoreWebView2CreationProperties` muere con
`MissingMethodException`, y muere DENTRO del evento de inicializacion, o
sea en silencio salvo que se lea `args.InitializationException`.

La salida es crear el environment a mano con
`CoreWebView2Environment.CreateAsync(None, carpeta, None)` -- con
`options=None` ese constructor no se toca nunca -- y pasarselo a
`EnsureCoreWebView2Async(env)`. Ver `_start_webview`.
"""

import os

import clr
import System

from Autodesk.Revit import DB
from Autodesk.Revit.UI import (DockablePaneId, DockablePaneState,
                               DockPosition, IDockablePaneProvider)

clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

# OJO con los namespaces: `TextWrapping` es un enum de `System.Windows`,
# NO de `System.Windows.Controls`, aunque la propiedad viva en TextBlock.
# Importarlo del lado equivocado revienta al CARGAR el modulo, o sea en
# el arranque de Revit.
from System.Windows import TextWrapping, Thickness
from System.Windows.Controls import Grid, RowDefinition, TextBlock
from System.Windows.Media import Brushes


# El GUID identifica el pane entre sesiones: Revit recuerda donde lo dejo
# el usuario (acoplado, flotante, en que monitor) atado a este valor. NO
# cambiarlo, o cada cambio le reinicia la posicion a todo el mundo.
PANE_GUID = System.Guid("6f2a1c74-3e58-4b9d-9a21-5c7d84e0b3f1")
PANE_ID = DockablePaneId(PANE_GUID)

# Titulo de la pestana del pane. Texto que ve el usuario.
PANE_TITLE = "ScheduleBoard"

# El host virtual que WebView2 mapea a la carpeta del frontend compilado.
# `.local` no resuelve en DNS, asi que no puede colisionar con un sitio
# real ni salir a la red.
VIRTUAL_HOST = "scheduleboard.local"
START_PAGE = "http://{0}/index.html".format(VIRTUAL_HOST)


def _extension_dir():
    """La carpeta `Archiplan.extension`, desde este archivo."""
    here = os.path.dirname(os.path.abspath(__file__))          # .../lib/schedule_board
    return os.path.dirname(os.path.dirname(here))              # .../Archiplan.extension


def dist_dir():
    """`web/dist` dentro de la extension, el frontend compilado."""
    return os.path.join(_extension_dir(), "web", "dist")


def _revit_dir():
    """La carpeta de instalacion del Revit que esta corriendo.

    Sale de donde este cargado RevitAPI.dll, y no de una ruta fija: en
    esta maquina Revit 2024 NO esta en `C:\\Program Files` sino en
    `C:\\Archivos de proyecto\\Autodesk\\Revit 2024`, una carpeta de
    Program Files localizada.
    """
    return os.path.dirname(clr.GetClrType(DB.Document).Assembly.Location)


def _load_webview2():
    """Carga los ensamblados de WebView2 que trae Revit. `True` si estan.

    Verificado presente en Revit 2024 **y** 2025, junto con el runtime
    Evergreen. `WebView2Loader.dll` no hace falta aparte: desde la
    version 1.0.1245 va embebido, y aca es la 1.0.1343.22.

    LA TRAMPA QUE COSTO ENCONTRAR EN 2025: si otra extension del ribbon ya
    cargo estos mismos DLL por el camino estandar (`clr.AddReference` por
    nombre, o una referencia .NET normal), `AddReferenceToFileAndPath` de
    abajo usa `Assembly.LoadFile`, que vive en un contexto de carga
    AISLADO -- crea una SEGUNDA copia con el mismo nombre y version pero
    identidad de tipo distinta. Medido en vivo: `EnsureCoreWebView2Async`
    fallaba con "expected CoreWebView2Environment, got
    CoreWebView2Environment" -- el objeto vino de una copia y el metodo
    esperaba el tipo de la otra. `clr.AddReference` por nombre simple
    busca primero en lo que ya este cargado por el camino estandar, asi
    que reusa esa copia en vez de duplicarla. Solo se cae al `LoadFile`
    por ruta si nada la tiene cargada todavia.
    """
    try:
        clr.AddReference("Microsoft.Web.WebView2.Core")
        clr.AddReference("Microsoft.Web.WebView2.Wpf")
        return True
    except Exception:
        pass
    folder = _revit_dir()
    try:
        for name in ("Microsoft.Web.WebView2.Core", "Microsoft.Web.WebView2.Wpf"):
            path = os.path.join(folder, name + ".dll")
            if not os.path.isfile(path):
                return False
            clr.AddReferenceToFileAndPath(path)
        return True
    except Exception:
        return False


def _user_data_folder():
    """Donde WebView2 guarda su perfil. Fuera de carpetas sincronizadas a proposito."""
    from schedule_board.paths import data_path
    return data_path("WebView2")


def _message(text, warn=False):
    block = TextBlock()
    block.Text = text
    block.TextWrapping = TextWrapping.Wrap
    block.Margin = Thickness(12)
    if warn:
        block.Foreground = Brushes.Firebrick
    return block


# --- Lo compartido entre motores -----------------------------------------
#
# POR QUE ESTO NO PUEDE VIVIR EN EL MODULO
# ----------------------------------------
# El pane lo ven VARIOS motores de pyRevit a lo largo de una sesion, no uno:
#   - con `rocketmode` apagado (preferencia de cada usuario), cada clic en
#     el boton corre en un motor limpio nuevo;
#   - un Reload de pyRevit llama `ScriptEngineManager.ClearEngines`, que
#     descarta el motor donde corrio `startup.py`.
# Revit, en cambio, conserva el provider y lo que se le entrego. Entonces el
# Grid del pane vive en el `AppDomain` (unico lugar compartido entre
# motores) y no tiene ni un handler Python colgado.
#
# `_ENGINE_ID` es un GUID nuevo por cada import del modulo, o sea uno por
# motor: es un string .NET, asi que compararlo nunca invoca codigo de otro
# motor.
#
# FASE 21 -- LA HISTORIA CORREGIDA. Hasta la Fase 20 este bloque decia que
# el cierre de Revit del 21-08-2026 lo causaron `Loaded`/`IsVisibleChanged`
# apuntando a un motor limpiado. Era falso: la revision del 25-09-2026 mostro que los cinco crashes tras un
# Reload tienen una sola causa, el servidor de Routes que pyRevit recrea
# en un motor huerfano, y que esa excepcion solo puede salir de un hilo que
# no es de UI. El pane nunca fue la causa. La regla de no colgar Python de
# lo que Revit conserva se mantiene porque es barata y sana, no porque
# haya matado a nadie.
ROOT_KEY = "ScheduleBoard.PaneRoot"
FILLED_BY_KEY = "ScheduleBoard.PaneFilledBy"

_ENGINE_ID = str(System.Guid.NewGuid())


def _shared_root():
    """El `Grid` que Revit muestra. Uno por sesion, no por motor."""
    root = System.AppDomain.CurrentDomain.GetData(ROOT_KEY)
    if root is None:
        root = Grid()
        root.RowDefinitions.Add(RowDefinition())
        System.AppDomain.CurrentDomain.SetData(ROOT_KEY, root)
    return root


def _filled_by_this_engine():
    return (System.AppDomain.CurrentDomain.GetData(FILLED_BY_KEY)
            == _ENGINE_ID)


class BoardPane(IDockablePaneProvider):
    """El provider: SIN estado y sin handlers, a proposito.

    Lo unico que hace es entregarle a Revit el Grid compartido. No guarda
    el WebView2 ni se suscribe a nada: despues de un Reload este objeto
    pertenece a un motor descartado, y lo sano es que no haya nada ahi
    que Revit o un evento puedan llamar.

    El contenido lo pone `ensure_content()`, que la llama el boton desde
    el motor que este vivo.
    """

    def SetupDockablePane(self, data):
        data.FrameworkElement = _shared_root()
        state = DockablePaneState()
        # Junto al Project Browser, que es su vecino natural. El usuario
        # puede soltarlo o flotarlo y Revit recuerda la eleccion, atada
        # al GUID de arriba.
        state.DockPosition = DockPosition.Tabbed
        data.InitialState = state
        data.VisibleByDefault = False


class _Content(object):
    """El contenido del pane, propiedad del motor que lo creo.

    Se guarda en el modulo y NO en el AppDomain: otro motor no lo ve, y
    por eso `ensure_content()` decide mirando el Grid, no este objeto.
    """

    def __init__(self, root=None):
        # `root` lo pasa `window.py`, que no usa el Grid del dock.
        self.root = root if root is not None else _shared_root()
        self.webview = None
        self.placeholder = None

    def ensure_loaded(self):
        """Crea y arranca el WebView2 la primera vez. Idempotente."""
        if self.webview is not None:
            return
        if self.placeholder is not None:
            self.root.Children.Remove(self.placeholder)
            self.placeholder = None

        index = os.path.join(dist_dir(), "index.html")
        if not os.path.isfile(index):
            self._show_placeholder(
                # Esto solo puede pasar con una instalacion
                # incompleta: `web/dist` esta versionado justo
                # para que el board siempre este ahi.
                "The board files are missing, so there is "
                "nothing to show. The extension looks "
                "incompletely installed.\n\n"
                "Expected them at: {0}".format(dist_dir()))
            return

        if not _load_webview2():
            self._show_placeholder(
                # Mismo criterio que el aviso que se saco de __init__: no
                # se le pide al usuario una tarea de programador. Esto es
                # un problema de la instalacion, no suyo.
                "WebView2 is not available in this Revit installation, "
                "so the board cannot be shown inside Revit. Ask for help "
                "with the ScheduleBoard install.")
            return

        try:
            self._start_webview()
        except Exception as ex:
            self._show_placeholder(
                "Could not start WebView2: {0}".format(ex))

    def _show_placeholder(self, text):
        self.placeholder = _message(text, warn=True)
        Grid.SetRow(self.placeholder, 0)
        self.root.Children.Add(self.placeholder)

    def _start_webview(self):
        """Crea el control y arranca WebView2 SIN pasar por
        `CoreWebView2CreationProperties`.

        Ese atajo es justo el que muere con `MissingMethodException` por
        el desajuste de ensamblados que documenta el encabezado de este
        modulo. Aca el environment se crea con `options=None`, que no
        toca el constructor ausente.
        """
        from Microsoft.Web.WebView2.Wpf import WebView2
        from Microsoft.Web.WebView2.Core import (
            CoreWebView2Environment, CoreWebView2HostResourceAccessKind)

        webview = WebView2()
        Grid.SetRow(webview, 0)
        self.root.Children.Add(webview)
        self.webview = webview

        folder = dist_dir()

        def on_ready(sender, args):
            if not args.IsSuccess:
                # El fallo llega POR ACA y no como excepcion: sin leer
                # `InitializationException` esto seria un panel en blanco
                # sin ninguna explicacion.
                self._show_placeholder(
                    "WebView2 failed to start: {0}".format(
                        args.InitializationException))
                return
            try:
                sender.CoreWebView2.SetVirtualHostNameToFolderMapping(
                    VIRTUAL_HOST, folder,
                    CoreWebView2HostResourceAccessKind.Allow)
                sender.Source = System.Uri(START_PAGE)
            except Exception as ex:
                self._show_placeholder(
                    "Could not map the board files: {0}".format(ex))

        webview.CoreWebView2InitializationCompleted += on_ready

        task = CoreWebView2Environment.CreateAsync(None, _user_data_folder(),
                                                  None)

        def when_env_ready(finished):
            if finished.IsFaulted:
                return
            env = finished.Result
            # `EnsureCoreWebView2Async` EXIGE el hilo de UI, y la
            # continuacion de un Task corre en el pool.
            def on_ui():
                try:
                    webview.EnsureCoreWebView2Async(env)
                except Exception as ex:
                    self._show_placeholder(
                        "Could not initialize WebView2 on the UI thread: "
                        "{0}".format(ex))
            webview.Dispatcher.BeginInvoke(System.Action(on_ui))

        task.ContinueWith(
            System.Action[System.Threading.Tasks.Task[CoreWebView2Environment]](
                when_env_ready))


# El contenido de ESTE motor. `None` hasta que alguien abre el board.
CONTENT = None


def _live_board(root):
    """`True` si el Grid ya tiene un WebView2, lo haya creado quien sea.

    Pregunta solo por el tipo .NET del hijo: no toca nada del motor que lo
    creo.
    """
    for child in root.Children:
        if child.GetType().Name == "WebView2":
            return True
    return False


def ensure_content():
    """Deja el pane con un board vivo. Idempotente.

    FASE 21: SI EL BOARD YA ESTA, SE REUSA, lo haya creado este motor u
    otro. Hasta la Fase 20, si lo habia llenado otro motor, se borraba y se
    pedia reiniciar Revit, porque se creia que la unica forma de llegar ahi
    era un Reload. No: con `rocketmode` apagado pasaba en el SEGUNDO CLIC
    del boton, sin ningun Reload, y destruia un board sano.

    Reusar es lo seguro: el WebView2 es .NET, y lo unico Python que tiene
    colgado es el aviso de inicializacion, que ya se disparo. Y es tambien
    lo que hace falta para que el board sobreviva un Reload de pyRevit una
    vez que pyRevit traiga el arreglo del servidor de Routes (PR #3558).

    Tres casos:
      - lo lleno este motor: se asegura que este cargado;
      - hay un WebView2 de otro motor: se deja como esta;
      - no hay board (primer clic de la sesion, o solo quedo un aviso de
        error de otro motor): se limpia y se llena con este motor.
    """
    global CONTENT
    if _filled_by_this_engine() and CONTENT is not None:
        CONTENT.ensure_loaded()
        return CONTENT

    root = _shared_root()
    if _live_board(root):
        return None

    if root.Children.Count:
        root.Children.Clear()
    CONTENT = _Content()
    System.AppDomain.CurrentDomain.SetData(FILLED_BY_KEY, _ENGINE_ID)
    CONTENT.ensure_loaded()
    return CONTENT


def register(uicontrolled_app):
    """Registra el pane. Llamar SOLO desde `startup.py`.

    Registrar desde un comando "funciona" pero deja el pane inservible en
    esa sesion -- verificado.

    Tras un Reload esto vuelve a correr y `RegisterDockablePane` rechaza
    el GUID repetido. **Eso es normal y no es un problema**: el pane que
    Revit ya tiene sigue sirviendo, porque lo que muestra es el Grid
    compartido y no este objeto. Se deja anotado en el log y se sigue.
    """
    provider = BoardPane()
    try:
        uicontrolled_app.RegisterDockablePane(PANE_ID, PANE_TITLE, provider)
        return provider
    except Exception:
        # Ya estaba registrado: segunda corrida de `startup.py`, o sea
        # Reload. El Grid compartido sobrevive y el boton lo rellena.
        return None


def show(uiapp):
    """Muestra el pane, con contenido del motor que este vivo.

    `uiapp` tiene que ser la `UIApplication` de un comando:
    `GetDockablePane` no existe en la `UIControlledApplication` del
    arranque.

    El orden importa: primero se llena y despues se muestra, para que el
    primer pintado no sea un panel vacio.
    """
    pane = uiapp.GetDockablePane(PANE_ID)
    ensure_content()
    pane.Show()
    return pane
