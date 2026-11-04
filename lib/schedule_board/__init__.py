# -*- coding: UTF-8 -*-
"""ScheduleBoard - capa Revit.

Modulos:
    paths        la carpeta %LOCALAPPDATA%\ScheduleBoard   [Fase 27]
    http         respuestas JSON con cabeceras CORS
    serializers  conversion segura a JSON (ElementId, parametros, unidades)
    reader       lectura del modelo. NUNCA abre transacciones
    profiler     estadistica de parametros            [Fase 2]
    selection    seleccion e isolate en Revit         [Fase 3]
    worksharing  posesion. Sin transacciones          [Fase 8]
    audit        el rastro de escritura y posesion    [Fases 14-15]
    changes      que cambio en Revit (DocumentChanged) [Fase 23]
    progress     progreso de una escritura larga      [Fase 11]
    writer       escritura. SIEMPRE dentro de transaccion
    pane         el board dentro de Revit (WebView2)
    api_routes   registro de rutas HTTP

Reglas invariantes:
    1. No leer la tabla renderizada de Revit. Reconstruirla desde los elementos.
    2. Lectura sin transaccion / escritura siempre en transaccion, separadas.
    3. Una operacion de usuario = una transaccion.
    4. Codigo compatible con IronPython 2.7 y CPython 3.
"""

__version__ = "0.1.0"
