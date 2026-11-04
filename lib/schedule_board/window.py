# -*- coding: UTF-8 -*-
"""El board en una ventana independiente (WPF + WebView2).  [20-10-2026]

POR QUE EXISTE
--------------
El dockable pane obliga a que el board viva pegado a Revit: hay que tener un
documento abierto, el provider se registra una sola vez por sesion y un Reload
de pyRevit no lo reemplaza. Una ventana propia no tiene nada de eso, se puede
llevar a otro monitor (un lienzo infinito rinde mas en una pantalla entera) y
Revit sigue usable detras. El patron sale del *Warnings Analyzer*, que Eduardo
construyo con el WebView2 de pyRevit 7 y le acomodo mas que el dock.

QUE NO CAMBIA
-------------
Solo cambia donde se dibuja el board. El frontend es el MISMO `web/dist`, se
sirve desde el mismo host virtual (`http://scheduleboard.local`, asi el CORS
de `http.py` y el `localStorage` --pestanas, layout-- son los de siempre) y
habla con Revit por HTTP como el pane. Nada del backend se toca.

POR QUE ES UN MODULO Y NO EL script.py
--------------------------------------
pyRevit 7 borra las globales de un script al terminar
(`for __deref in dir(): del globals()[...]`). Los handlers de la ventana y las
devoluciones asincronas de WebView2 corren DESPUES y encontraban `System` o
`json` ya borrados: ventana en blanco, sin ningun error. Las globales de un
modulo importado viven en `sys.modules` y no se tocan. `script.py` solo llama a
`show()`.

UNA SOLA VENTANA
----------------
Se guarda en el `AppDomain` (el unico lugar compartido entre motores de
pyRevit) y se pregunta SOLO por miembros .NET (`IsVisible`, `WindowState`): un
atributo Python puesto por otro motor no se ve desde este, que es como se
termina abriendo una ventana nueva en cada clic.
"""

import json
import os
import time

import clr
import System

clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System.Windows import (SystemParameters, Window, WindowStartupLocation,
                            WindowState)
from System.Windows.Controls import Grid
from System.Windows.Interop import WindowInteropHelper

from schedule_board import pane
from schedule_board.paths import data_path


WINDOW_KEY = "ScheduleBoard.Window"
TITLE = "Schedule Board"
DEFAULT_SIZE = (1440, 900)
MIN_SIZE = (900, 600)

# Cuanto de la ventana tiene que quedar dentro de algun monitor para
# restaurarla donde estaba. Sin esto, desenchufar el segundo monitor deja la
# ventana fuera de la pantalla, y no hay como agarrarla.
VISIBLE_MARGIN = 120


def _log(text):
    """Una linea en `window.log`. Fallar aca nunca debe tumbar la ventana."""
    try:
        path = data_path("window.log")
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with open(path, "a") as handle:
            handle.write("{0}  {1}\n".format(
                time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


# --- Donde estaba la ventana ----------------------------------------------

def _geometry_path():
    return data_path("window.json")


def _is_visible_on_screen(left, top, width, height):
    """`True` si queda suficiente ventana dentro del escritorio virtual."""
    v_left = SystemParameters.VirtualScreenLeft
    v_top = SystemParameters.VirtualScreenTop
    v_right = v_left + SystemParameters.VirtualScreenWidth
    v_bottom = v_top + SystemParameters.VirtualScreenHeight
    return (left + width > v_left + VISIBLE_MARGIN
            and left < v_right - VISIBLE_MARGIN
            and top >= v_top - 10
            and top < v_bottom - VISIBLE_MARGIN)


def load_geometry():
    """La posicion y el tamano de la ultima vez, o `None` si no sirven."""
    try:
        with open(_geometry_path()) as handle:
            saved = json.load(handle)
        values = [float(saved[key]) for key in
                  ("left", "top", "width", "height")]
        left, top, width, height = values
        if width < MIN_SIZE[0] or height < MIN_SIZE[1]:
            return None
        if not _is_visible_on_screen(left, top, width, height):
            return None
        saved["left"], saved["top"] = left, top
        saved["width"], saved["height"] = width, height
        return saved
    except Exception:
        # Sin archivo, o uno ilegible: se abre centrada, como la primera vez.
        return None


def save_geometry(window):
    """Guarda donde dejo el usuario la ventana. `RestoreBounds` da el tamano
    NORMAL aunque este maximizada, que es el que hay que recordar."""
    try:
        bounds = window.RestoreBounds
        data = {
            "left": bounds.Left, "top": bounds.Top,
            "width": bounds.Width, "height": bounds.Height,
            "maximized": window.WindowState == WindowState.Maximized,
        }
        path = _geometry_path()
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with open(path, "w") as handle:
            json.dump(data, handle)
    except Exception as ex:
        _log("no se pudo guardar la posicion: %r" % (ex,))


# --- La ventana -------------------------------------------------------------

class BoardWindow(Window):

    def __init__(self, uiapp):
        self.Title = TITLE
        self.MinWidth, self.MinHeight = MIN_SIZE
        self.Width, self.Height = DEFAULT_SIZE
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen

        saved = load_geometry()
        if saved is not None:
            self.WindowStartupLocation = WindowStartupLocation.Manual
            self.Left, self.Top = saved["left"], saved["top"]
            self.Width, self.Height = saved["width"], saved["height"]
            if saved.get("maximized"):
                self.WindowState = WindowState.Maximized

        # Con dueno, la ventana queda siempre por encima de Revit y se
        # minimiza con el, pero se puede mover a otro monitor.
        WindowInteropHelper(self).Owner = uiapp.MainWindowHandle

        self.root = Grid()
        self.Content = self.root
        self.Closing += self._on_closing
        self.Closed += self._on_closed

        # El mismo arranque de WebView2 que usa el pane, con sus avisos
        # de error en pantalla: ver `pane._Content`.
        self.board = pane._Content(self.root)
        self.board.ensure_loaded()

    def _on_closing(self, sender, args):
        save_geometry(self)

    def _on_closed(self, sender, args):
        try:
            if self.board.webview is not None:
                # Sin esto el proceso del navegador sobrevive a la ventana.
                self.board.webview.Dispose()
        except Exception as ex:
            _log("no se pudo liberar el WebView2: %r" % (ex,))
        finally:
            # Siempre: si no, el proximo clic intentaria activar una
            # ventana muerta.
            System.AppDomain.CurrentDomain.SetData(WINDOW_KEY, None)


def show(uiapp):
    """Abre la ventana del board, o trae al frente la que ya esta abierta."""
    existing = System.AppDomain.CurrentDomain.GetData(WINDOW_KEY)
    if existing is not None and existing.IsVisible:
        if existing.WindowState == WindowState.Minimized:
            existing.WindowState = WindowState.Normal
        existing.Activate()
        return existing

    window = BoardWindow(uiapp)
    System.AppDomain.CurrentDomain.SetData(WINDOW_KEY, window)
    window.Show()
    return window
