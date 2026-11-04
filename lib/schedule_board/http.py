# -*- coding: UTF-8 -*-
"""Helpers de respuesta HTTP con cabeceras CORS.

POR QUE EXISTE ESTE MODULO
--------------------------
pyRevit Routes NO agrega cabeceras CORS por su cuenta. Verificado en
`pyrevitlib/pyrevit/routes/server/server.py`: el metodo `_write_response`
solo escribe `Content-Length` y las cabeceras que vengan en el objeto
Response devuelto por el handler.

Sin CORS, un frontend servido desde cualquier origen distinto de
127.0.0.1:48884 no puede llamar a la API: el navegador bloquea la
peticion en el preflight.

DOS COSAS SON NECESARIAS, NO UNA:
    1. Devolver las cabeceras Access-Control-Allow-* (helpers de aqui).
    2. Registrar una ruta OPTIONS para CADA patron, o el preflight
       recibe **500** (`RouteHandlerNotDefinedException`), no 404.
       Usar `register_preflight()`.

OJO CON LA VERSION DE pyREVIT
-----------------------------
`handler.parse_response` hace `json.dumps(data)` con `ensure_ascii=True`.
En pyRevit 5.2 eso revienta con cualquier texto no-ASCII bajo IronPython
2.7 y se pierde la respuesta entera, no solo el texto. Verificado: no hay
forma de evitarlo desde aqui. Desde 6.5.3 pyRevit trae `_safe_json_dumps`,
que escapa el no-ASCII a mano.

POR QUE EL PREFLIGHT VA EN UNA RUTA APARTE
------------------------------------------
pyRevit decide si ejecutar el handler dentro de un ExternalEvent
inspeccionando su firma: si declara `doc`, `uidoc` o `uiapp`, lo hace.

Si el mismo handler atendiera GET y OPTIONS, cada preflight del
navegador dispararia un ExternalEvent y quedaria encolado detras del
comando que el usuario tenga activo en Revit. El preflight se colgaria
sin necesidad, porque no toca el modelo.

Por eso `register_preflight()` registra un handler que NO declara `doc`:
pyRevit lo ejecuta en el hilo del servidor y responde al instante.
"""

import json
import os

from pyrevit import routes

from schedule_board.paths import data_path


# QUIEN PUEDE LLAMAR A LA API DESDE UN NAVEGADOR.  [Fase 22]
#
# Hasta la Fase 21 era '*', y "solo escucha en 127.0.0.1" no alcanzaba:
# cualquier pagina abierta en un navegador de ESTA maquina puede mandar
# pedidos a 127.0.0.1. Con la escritura habilitada, eso incluye escribir
# en el modelo. Ahora el unico origen es el del pane.
#
# Se deja abierto SOLO con el archivo `dev-mode`, porque ahi corre ademas
# el frontend de desarrollo (http://127.0.0.1:6300) y la cabecera admite
# UN origen o '*'. Elegir segun el `Origin` del pedido no se puede:
# pyRevit no se lo pasa al handler (`Request.headers` existe pero el
# servidor nunca lo llena, verificado en `base.py` y `server.py`).
PANE_ORIGIN = "http://scheduleboard.local"

# Mismo patron que `writer.WRITES_MARKER`: un archivo, leido fresco.
DEV_MARKER = "dev-mode"

CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Accept",
    "Access-Control-Max-Age": "600",
}


def dev_mode_enabled():
    """`True` si esta maquina es de desarrollo (archivo `dev-mode`)."""
    try:
        return os.path.isfile(data_path(DEV_MARKER))
    except Exception:
        return False


def allowed_origin():
    return "*" if dev_mode_enabled() else PANE_ORIGIN


def _headers(extra=None):
    # Toda respuesta pasa por aca (ok, fail, preflight): es el primer
    # momento en que el servidor de Routes seguro existe. Ver abajo.
    if SERIALIZER_STATE in _RETRY_STATES:
        install_fast_serializer()
    headers ={"Content-Type": "application/json",
               "Access-Control-Allow-Origin": allowed_origin()}
    headers.update(CORS_HEADERS)
    if extra:
        headers.update(extra)
    return headers


def ok(data, status=200):
    """Respuesta correcta con envoltorio uniforme.

    Formato: {"ok": true, "data": <payload>}
    """
    return routes.Response(
        status=status,
        data={"ok": True, "data": data},
        headers=_headers(),
    )


def fail(message, status=400, detail=None, code=None):
    """Respuesta de error con envoltorio uniforme.

    Formato: {"ok": false, "error": <mensaje>, "code": <slug>,
              "detail": <extra>}

    Devolvemos siempre este envoltorio en vez de dejar que la excepcion
    llegue al manejador generico de pyRevit, para que el frontend tenga
    un contrato unico de errores.

    `code` es un identificador estable para que el cliente distinga los
    casos sin parsear el mensaje. Los mensajes son para personas y
    pueden cambiar; los codigos no.
    """
    payload = {"ok": False, "error": message}
    if code is not None:
        payload["code"] = code
    if detail is not None:
        payload["detail"] = detail
    return routes.Response(status=status, data=payload, headers=_headers())


# --- Casos que el cliente tiene que poder distinguir ----------------------

def no_document():
    """No hay documento abierto en Revit.

    POR QUE MERECE UNA RESPUESTA PROPIA
    Sin documento, pyRevit inyecta `doc = None` y cualquier llamada a la
    API de Revit revienta. Medido en Fase 0: `FilteredElementCollector`
    lanza un error de C++ ("Parameter name: document") que terminaba en
    un 500 con la traza cruda, indistinguible para el frontend de un
    servidor caido. Y no es un caso raro: es el estado de Revit cada vez
    que el usuario cierra un modelo.

    409 y no 500: el servidor esta perfecto, es el estado del host el que
    no permite responder todavia.
    """
    return fail(
        "No document is open in Revit. Open a model and try again.",
        status=409,
        code="no_document",
    )


def register_preflight(api, pattern):
    """Registra el handler OPTIONS de un patron de ruta.

    Llamar una vez por cada patron que exponga la API. El handler no
    declara `doc`, asi que corre en el hilo del servidor sin tocar Revit.

    pyRevit indexa las rutas por (patron, metodo), asi que este registro
    convive sin conflicto con el GET o POST del mismo patron.
    """
    def _preflight(request):  # pylint: disable=unused-argument
        return routes.Response(status=200, data={"ok": True},
                               headers=_headers())

    # El nombre de la funcion debe ser unico por patron para que pyRevit
    # no reutilice el registro anterior.
    _preflight.__name__ = "preflight_{0}".format(
        pattern.strip("/").replace("/", "_").replace("<", "").replace(">", "")
        .replace(":", "_") or "root"
    )
    api.route(pattern, methods=["OPTIONS"])(_preflight)


# --- Query params ---------------------------------------------------------
#
# POR QUE ESTO NO PUEDE SER TOLERANTE
#
# Antes estas dos funciones envolvian la lectura en `try/except Exception:
# return default`. Parecia defensivo y era lo contrario: en pyRevit 5.2 el
# `Request` no tiene `query_params` —el servidor descarta el query string
# al parsear la ruta, con `urlparse(...).path`, y no lo entrega en ninguna
# forma—, asi que el `except` se comia la incompatibilidad y `max_rows` se
# ignoraba SIN AVISO.
#
# Verificado en Fase 0: `/schedules/41007715/?max_rows=5` devolvio las 132
# filas con `truncated: false`. Pedir 250 filas y recibir 7.285 es peor que
# un error, porque el cliente no se entera y se come 220 s y 11 MB.
#
# Y no se puede detectar desde aca que el cliente mando el parametro: el
# query string ya se perdio antes de construir el `Request`. Entonces la
# unica opcion honesta es fallar cuando la capacidad no esta.


QUERY_PARAMS_MESSAGE = (
    "This pyRevit server does not pass query params to the handler: it "
    "drops them while parsing the route. pyRevit 6.5.3 or newer is "
    "required. Without query params there is no pagination, and without "
    "pagination large schedules are not viable."
)


class QueryParamsUnsupported(Exception):
    """El pyRevit del host no expone `query_params` en el `Request`."""

    def __init__(self):
        Exception.__init__(self, QUERY_PARAMS_MESSAGE)


def supports_query_params(request):
    """True si este pyRevit entrega los query params al handler.

    Se expone en `/status/` para que el frontend pueda negarse a arrancar
    con un host incompatible, en vez de descubrirlo con una tabla que
    llega entera cuando pidio una pagina.
    """
    return isinstance(getattr(request, "query_params", None), dict)


def _query_params(request):
    if not supports_query_params(request):
        raise QueryParamsUnsupported()
    return request.query_params


def query_flag(request, name, default=False):
    """Lee un query param booleano.

    Lanza `QueryParamsUnsupported` si el host no los soporta. NO devuelve
    el default en ese caso: seria mentir.
    """
    raw = _query_params(request).get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def query_int(request, name, default=None):
    """Lee un query param entero, o `default` si falta.

    Lanza `QueryParamsUnsupported` si el host no los soporta. Un valor
    presente pero no numerico si cae al default: eso es un error del
    cliente en un solo parametro, no una incompatibilidad del entorno.
    """
    raw = _query_params(request).get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def query_str(request, name, default=None):
    """Lee un query param como texto, o `default` si falta.

    Lanza `QueryParamsUnsupported` si el host no los soporta, por la
    misma razon que las otras dos: devolver el default seria mentir.

    [Fase 26] Acepta el valor como literal JSON entre comillas
    (`"C:\\\\...\\u00c1rea.rvt"`), que es como lo manda el cliente con
    `jsonBody()`. pyRevit 6.5 revienta con un 500 ANTES de llegar al
    handler si el query string trae cualquier caracter no ASCII
    (`_decode_utf8_params` en `server.py`), asi que el texto tiene que
    viajar en ASCII puro -- igual que los cuerpos. Un valor sin comillas
    se devuelve tal cual: ninguna ruta ni titulo de Windows empieza con
    `"`, asi que no hay ambiguedad.
    """
    raw = _query_params(request).get(name)
    if raw is None:
        return default
    text = raw if isinstance(raw, basestring) else str(raw)
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            value = json.loads(text)
        except ValueError:
            return text
        if isinstance(value, basestring):
            return value
    return text


def query_params_unsupported():
    """Respuesta para cuando el host no soporta query params."""
    return fail(
        QUERY_PARAMS_MESSAGE,
        status=501,
        code="query_params_unsupported",
    )


# --- El serializador de respuestas de pyRevit  [27-09-2026] ------------------
#
# `handler.parse_response` prueba `json.dumps(ensure_ascii=True)`, que en
# IronPython falla con CUALQUIER tilde, `ñ` o `²`, y cae a `_safe_json_dumps`,
# que arma el JSON caracter por caracter. Medido: ~173 KB, 0,3 s en ASCII y
# 36-50 s con un `²`; una pagina de `TC-Ventanas`, 43-56 s con 2,5 s de
# handler.
#
# El servidor usa SU copia del modulo `handler` (la de su motor), no la que
# importamos: por eso se la busca a traves del objeto del servidor.
#
# SE INSTALA EN LA PRIMERA RESPUESTA, no al arrancar: pyRevit corre los
# `startup.py` de las extensiones ANTES de activar el servidor
# (`sessionmgr.load_session`: `_new_session()` y despues
# `_perform_onsessionloadcomplete_ops()`), asi que en `register_routes`
# todavia no existe. `_headers()` lo intenta mientras el servidor no este;
# con el servidor y otra forma de pyRevit, se rinde una vez y no insiste.
#
# LA GUARDA: si algo de esa cadena no esta (otra version de pyRevit, Routes
# apagado), no se toca nada y queda el serializador de pyRevit, que es lento
# pero correcto. Y ante cualquier objeto que `json.dumps` no acepte (o un NaN),
# el reemplazo le pasa el trabajo al original: mismo resultado que antes.
# El dia que pyRevit lo arregle, este bloque se saca.

_ORIGINAL_ATTR = "_schedule_board_original"

# Lo que dice `/status/`: "installed", "skipped: <motivo>", o uno de los
# dos de abajo, que son los unicos en que `_headers()` vuelve a intentar.
NOT_INSTALLED = "not installed"
WAITING = "waiting for the Routes server"
_RETRY_STATES = (NOT_INSTALLED, WAITING)
SERIALIZER_STATE = NOT_INSTALLED


def ascii_json(obj):
    """JSON en ASCII puro. Vive en `serializers.ascii_json` (que tambien
    usa `audit._ascii_json`); import perezoso para no arrastrar
    `Autodesk.Revit.DB` a este modulo, que `checks/serializer.check.py`
    importa sin Revit. `allow_nan=False`: un NaN tiene que fallar aca para
    que el reemplazo del serializador le pase el trabajo al original (ver
    el bloque de abajo), no colarse como un `NaN` no estandar.
    """
    from schedule_board import serializers as ser
    return ser.ascii_json(obj, allow_nan=False)


def _routes_server():
    from pyrevit.coreutils import envvars
    return envvars.get_pyrevit_env_var(envvars.ROUTES_SERVER)


def install_fast_serializer():
    """Reemplaza `_safe_json_dumps` en la copia del servidor. Idempotente.

    Lo llama `_headers()` hasta que queda instalado (o se rinde). Tras un
    `/dev/reload/` este modulo vuelve a `NOT_INSTALLED` y se reinstala en
    la respuesta siguiente. El original se guarda la primera vez y se
    reusa despues, para no envolver un reemplazo viejo.
    """
    global SERIALIZER_STATE
    try:
        server = _routes_server()
    except Exception as ex:
        SERIALIZER_STATE = "skipped: {0}".format(ex)
        return SERIALIZER_STATE
    if server is None:
        SERIALIZER_STATE = WAITING
        return SERIALIZER_STATE
    try:
        write = server.server.RequestHandlerClass.__dict__["_write_response"]
        module = write.__globals__["handler"]
        original = getattr(module, _ORIGINAL_ATTR, None) or module._safe_json_dumps
        if not callable(original):
            raise TypeError("_safe_json_dumps is not callable")
    except Exception as ex:
        SERIALIZER_STATE = "skipped: {0}".format(ex)
        return SERIALIZER_STATE

    def fast(obj):
        try:
            return ascii_json(obj)
        except Exception:
            return original(obj)

    setattr(module, _ORIGINAL_ATTR, original)
    module._safe_json_dumps = fast
    SERIALIZER_STATE = "installed"
    return SERIALIZER_STATE
