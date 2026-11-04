# -*- coding: utf-8 -*-
"""Warnings Analyzer: la ventana WPF + WebView2 y el puente con Revit.

Vive en un MODULO y no en script.py a proposito: al terminar cada script,
pyRevit 7 borra sus globales (`for __deref in dir(): del globals()[...]`,
verificado en pyRevitLabs.PyRevit.Runtime). Las devoluciones asincronas de
WebView2 y los handlers de la ventana corren DESPUES y usaban `System`,
`json`, etc. ya borrados: la ventana quedaba en blanco sin ningun error.
Las globales de un modulo importado viven en sys.modules y no se tocan.

COMO FUNCIONA
-------------
Una ventana WPF no modal aloja un WebView2 que carga `web/index.html` desde
un host virtual (sin servidor, sin red). La pagina habla con Revit por
mensajes:

    JS  chrome.webview.postMessage({id, action, payload})
    ->  WebMessageReceived (hilo de UI)  ->  cola  ->  ExternalEvent.Raise()
    ->  Execute(uiapp) en contexto valido de la API  ->  warnings_core.ACTIONS
    ->  PostWebMessageAsJson({id, ok, data | error})  ->  JS resuelve la promesa

Nunca se toca la API de Revit fuera del ExternalEvent.

WEBVIEW2 Y PYREVIT 7
--------------------
pyRevit 7 carga su propio par Core/Wpf de WebView2 (1.0.4129, emparejados)
para su ventana de salida. `clr.AddReference` por nombre reusa esa copia en
vez de crear otra con identidad de tipo distinta (lo que en ScheduleBoard daba
"expected CoreWebView2Environment, got CoreWebView2Environment"). El
environment se crea a mano con `options=None`, el camino que ScheduleBoard
verifico que funciona tambien con el par desemparejado que trae Revit.
"""
import json
import os

import clr
import System

clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System.Windows import (Thickness, TextWrapping, Window,
                            WindowStartupLocation, WindowState)
from System.Windows.Controls import Grid, TextBlock
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Media import BrushConverter

from Autodesk.Revit.UI import ExternalEvent, IExternalEventHandler

import warnings_core

BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))

WEB_DIR = os.path.join(BUNDLE_DIR, "web")
VIRTUAL_HOST = "archiplan-warnings.local"
START_PAGE = "https://{0}/index.html".format(VIRTUAL_HOST)
WINDOW_KEY = "Archiplan.WarningsAnalyzer.Window"
USER_DATA = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ARCHIPLAN",
                         "WarningsAnalyzer", "WebView2")  # fuera de carpetas sincronizadas


class Bridge(IExternalEventHandler):
    """Ejecuta en Revit los pedidos que llegan de la pagina, en orden."""

    def __init__(self):
        self.queue = []
        self.post = lambda reply: None

    def Execute(self, uiapp):
        while self.queue:
            msg = self.queue.pop(0)
            action = msg.get("action")
            reply = {"id": msg.get("id"), "action": action}
            try:
                reply["data"] = warnings_core.dispatch(
                    uiapp, action, msg.get("payload") or {})
                reply["ok"] = True
            except Exception as ex:
                reply["ok"] = False
                reply["error"] = u"{0}".format(ex)
            try:
                self.post(reply)
            except Exception as ex:
                # Que la pagina no quede esperando para siempre.
                self.post({"id": reply["id"], "ok": False,
                           "error": u"No se pudo enviar la respuesta: {0}"
                                    .format(ex)})

    def GetName(self):
        return "Archiplan Warnings Analyzer"


def _load_webview2():
    clr.AddReference("Microsoft.Web.WebView2.Core")
    clr.AddReference("Microsoft.Web.WebView2.Wpf")


class AnalyzerWindow(Window):

    def __init__(self, uiapp):
        self.Title = u"Warnings Analyzer · ARCHIPLAN"
        self.closed = False
        self.Width, self.Height = 1440, 900
        self.MinWidth, self.MinHeight = 980, 640
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        self.Background = BrushConverter().ConvertFromString("#F5F5F4")
        WindowInteropHelper(self).Owner = uiapp.MainWindowHandle

        self.root = Grid()
        self.Content = self.root
        self.webview = None

        self.bridge = Bridge()
        self.bridge.post = self._post
        self.event = ExternalEvent.Create(self.bridge)
        self.Closed += self._on_closed

        try:
            _load_webview2()
            self._start_webview()
        except Exception as ex:
            self._show_error(u"No se pudo iniciar WebView2: {0}".format(ex))

    # --- WebView2 -------------------------------------------------------------

    def _start_webview(self):
        from Microsoft.Web.WebView2.Wpf import WebView2
        from Microsoft.Web.WebView2.Core import CoreWebView2Environment

        self.webview = WebView2()
        self.root.Children.Add(self.webview)
        self.webview.CoreWebView2InitializationCompleted += self._on_ready

        if not os.path.isdir(USER_DATA):
            os.makedirs(USER_DATA)
        task = CoreWebView2Environment.CreateAsync(None, USER_DATA, None)
        webview = self.webview

        def when_env_ready(finished):
            if finished.IsFaulted:
                error = finished.Exception
                webview.Dispatcher.BeginInvoke(System.Action(
                    lambda: self.closed or self._show_error(
                        u"WebView2 Runtime no disponible: {0}".format(error))))
                return
            env = finished.Result

            def on_ui():
                # El usuario pudo cerrar la ventana mientras se creaba el
                # environment: no tocar un control ya liberado.
                if not self.closed:
                    webview.EnsureCoreWebView2Async(env)
            # EnsureCoreWebView2Async exige el hilo de UI.
            webview.Dispatcher.BeginInvoke(System.Action(on_ui))

        task.ContinueWith(System.Action[
            System.Threading.Tasks.Task[CoreWebView2Environment]](
                when_env_ready))

    def _on_ready(self, sender, args):
        from Microsoft.Web.WebView2.Core import (
            CoreWebView2HostResourceAccessKind)
        if not args.IsSuccess:
            # El fallo llega por aqui, no como excepcion.
            self._show_error(u"WebView2 no arrancó: {0}".format(
                args.InitializationException))
            return
        core = sender.CoreWebView2
        core.Settings.IsStatusBarEnabled = False
        core.SetVirtualHostNameToFolderMapping(
            VIRTUAL_HOST, WEB_DIR, CoreWebView2HostResourceAccessKind.Allow)
        core.WebMessageReceived += self._on_message
        sender.Source = System.Uri(START_PAGE)

    def _on_message(self, sender, args):
        try:
            msg = json.loads(args.WebMessageAsJson)
        except Exception:
            return
        self.bridge.queue.append(msg)
        self.event.Raise()

    def _post(self, reply):
        core = self.webview.CoreWebView2 if self.webview else None
        if core is not None:
            # ensure_ascii=False: el json de IronPython revienta con tildes
            # (nombres de niveles, categorias) si intenta escaparlas.
            core.PostWebMessageAsJson(json.dumps(reply, ensure_ascii=False))

    # --- Ciclo de vida --------------------------------------------------------

    def _show_error(self, text):
        block = TextBlock()
        block.Text = text
        block.Margin = Thickness(24)
        block.TextWrapping = TextWrapping.Wrap
        self.root.Children.Clear()
        self.root.Children.Add(block)

    def _on_closed(self, sender, args):
        self.closed = True
        self.bridge.post = lambda reply: None
        self.bridge.queue = []
        try:
            if self.webview is not None:
                self.webview.Dispose()
            self.event.Dispose()
        finally:
            # Siempre: si no, el proximo clic intentaria activar una ventana
            # muerta.
            System.AppDomain.CurrentDomain.SetData(WINDOW_KEY, None)


def main(uiapp):
    existing = System.AppDomain.CurrentDomain.GetData(WINDOW_KEY)
    # `closed` falta o es True en una ventana que ya se cerro (o que quedo
    # registrada por una version anterior): en ese caso se crea otra.
    if existing is not None and getattr(existing, "closed", True) is False:
        # Una sola ventana por sesion: si ya esta abierta, se trae al frente.
        if existing.WindowState == WindowState.Minimized:
            existing.WindowState = WindowState.Normal
        existing.Activate()
        return
    window = AnalyzerWindow(uiapp)
    System.AppDomain.CurrentDomain.SetData(WINDOW_KEY, window)
    window.Show()
