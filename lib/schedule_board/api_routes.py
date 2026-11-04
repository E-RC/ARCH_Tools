# -*- coding: UTF-8 -*-
"""Registro de rutas HTTP de ScheduleBoard.

Base:  http://127.0.0.1:48884/schedule_board/   (nunca `localhost`)

DOS COSAS QUE NO SE PUEDEN OLVIDAR AL AGREGAR UNA RUTA:

1. Declarar `doc` (o `uidoc` / `uiapp`) en la firma del handler. Eso es
   lo que hace que pyRevit lo ejecute dentro de un ExternalEvent, en
   contexto de API. Sin eso, cualquier llamada a la API de Revit falla.

2. Llamar `http.register_preflight(api, "<mismo patron>")`. Sin eso el
   preflight OPTIONS del navegador recibe **500**
   (`RouteHandlerNotDefinedException`), no 404, y el frontend no puede
   usar la ruta aunque `curl` funcione perfectamente.
"""

import sys
import time

from pyrevit import routes

from schedule_board import audit
from schedule_board import changes
from schedule_board import http
from schedule_board import profiler
# Con alias: el handler de `/progress/` se llama `progress` y lo taparia.
from schedule_board import progress as progress_state
from schedule_board import reader
from schedule_board import selection
from schedule_board import serializers as ser
from schedule_board import worksharing as ws
from schedule_board import writer


# Patrones expuestos. La lista existe para que el registro del preflight
# no dependa de recordar hacerlo ruta por ruta.
PATTERNS = [
    "/status/",
    "/schedules/",
    "/schedules/<int:schedule_id>/",
    "/schedules/<int:schedule_id>/profile/",
    "/schedules/<int:schedule_id>/fields/",
    "/schedules/<int:schedule_id>/fields/<int:field_id>/",
    "/schedules/<int:schedule_id>/definition/",
    "/schedules/<int:schedule_id>/duplicate/",
    "/categories/",
    "/batch/fields/",
    "/batch/apply/",
    "/selection/",
    "/selection/isolate/",
    "/schedules/<int:schedule_id>/cell/",
    "/progress/",
    "/audit/",
    "/worksharing/pending/",
    "/worksharing/relinquish/",
    "/dev/reload/",
    "/changes/",
]


# --- Recarga en caliente del backend  [Fase 21] ----------------------------
#
# Solo con el archivo `dev-mode` (`http.dev_mode_enabled`, leido fresco en
# cada llamada). Sin el archivo la ruta existe pero se niega, asi que en
# una maquina del reparto no hace nada.

# El mismo nombre que registra `startup.py`.
API_NAME = "schedule_board"

# Orden de dependencias, sin ciclos: cada modulo va despues de los que
# importa. `pane` NO esta, a proposito: un `_ENGINE_ID` nuevo haria que el
# pane crea que lo lleno otro motor. Y `api_routes` va ultimo porque es el
# que vuelve a registrar las rutas.
RELOAD_ORDER = (
    "paths", "serializers", "progress", "audit", "changes", "worksharing", "reader",
    "profiler", "selection", "http", "writer", "api_routes",
)

# Lo que un `reload()` vuelve a su valor inicial. Va en la respuesta para
# que nadie se sorprenda de un `seq` que volvio a 0.
RELOAD_RESETS = (
    "progress.PROGRESS (seq back to 0)",
    "audit.PENDING and audit.LAST_ERROR",
    "serializers parameter and type caches",
    "changes journal (new epoch; the Revit event subscription is renewed)",
)


def reload_backend(application=None):
    """Recarga `lib/` y vuelve a registrar las rutas. Devuelve la lista.

    `reload()` re-ejecuta cada modulo EN EL MISMO objeto modulo, asi que
    los `from schedule_board import X` de los demas ven la version nueva
    sin tocarlos. Si un modulo falla a mitad de camino (un error de
    sintaxis), la excepcion sube y las rutas NO se re-registran: siguen
    las viejas, que es lo menos malo hasta corregir y volver a llamar.
    """
    reloaded = []
    for name in RELOAD_ORDER:
        full_name = "schedule_board." + name
        if full_name in sys.modules:
            reload(sys.modules[full_name])  # noqa: F821 - builtin de IronPython 2.7
        else:
            # Un modulo nuevo que el arranque todavia no importo: la primera
            # recarga despues de agregarlo lo trae por primera vez.
            __import__(full_name)
        reloaded.append(name)

    # [Fase 23] El handler de `DocumentChanged` es una funcion del modulo
    # `changes` VIEJO: recargarlo no lo cambia, y el diario nuevo quedaria
    # vacio para siempre. Se vuelve a suscribir, y `subscribe()` quita el
    # anterior del AppDomain.
    if application is not None:
        changes.subscribe(application)

    # `add_route` sobrescribe por (patron, metodo), asi que registrar de
    # nuevo es seguro. NO se vacia el mapa antes: entre el vaciado y el
    # registro, el latido del board recibiria un 500. Lo que se barre
    # despues es lo que no volvio a registrarse, o sea una ruta borrada.
    table = routes.get_routes(API_NAME)
    before = dict(table)
    register_routes(routes.API(API_NAME))
    for route, handler in before.items():
        if table.get(route) is handler:
            table.pop(route, None)
    return reloaded


def _element_ids_from_body(request):
    """Extrae `element_ids` del body JSON de una peticion.

    `request.data` ya viene decodificado por pyRevit cuando el cliente
    manda `Content-Type: application/json` (ver `server.py:_prepare_request`
    en pyRevit 6.5.3+). Sin Content-Type o sin body, `data` es `None`.
    """
    body = request.data or {}
    try:
        return list(body.get("element_ids") or [])
    except AttributeError:
        return []


def _python_implementation():
    """Nombre del motor de Python, sin depender de `platform`.

    IronPython trae `platform`, pero preferimos no arriesgar una
    excepcion en la ruta de salud, que es la primera que se prueba.
    """
    try:
        import platform
        return platform.python_implementation()
    except Exception:
        version = str(sys.version)
        if "IronPython" in version:
            return "IronPython"
        return "CPython"


# --- Helpers de las rutas  [a nivel de modulo desde la Fase 27] -------------
#
# Vivian anidados en `register_routes` sin capturar nada de ahi.

def _editable_schedule(doc, schedule_id):
    """(view, None) si se puede gestionar; (None, respuesta) si no."""
    try:
        view = reader.find_schedule(doc, schedule_id)
    except Exception as ex:
        return None, http.fail("Failed to look up the schedule.",
                               status=500, detail=str(ex))
    if view is None:
        return None, http.fail(
            "There is no schedule with id {0}.".format(schedule_id),
            status=404,
        )

    kind = reader.schedule_kind(view)
    if not reader.is_editable_kind(kind):
        # Restriccion deliberada, no una limitacion de la API: Revit
        # deja gestionar columnas en estos tipos, pero el board no
        # sabe pintar su tabla ("una fila = un elemento" no aplica),
        # asi que el usuario no podria ver desde aca lo que acaba de
        # hacer. Si algun dia se levanta, es un cambio de una linea.
        return None, http.fail(
            "This schedule is of kind '{0}' and the board treats it as "
            "read-only.".format(kind),
            status=409,
            code="schedule_not_editable",
        )
    return view, None


def _fields_response(view, fields):
    return http.ok({
        "schedule_id": ser.element_id_value(view.Id),
        "fields": fields,
    })


def _write_failed(ex, action):
    """Traduce una excepcion de escritura a respuesta HTTP.

    HASTA LA FASE 7 ESTA FUNCION FINGIA LA CAPA DE WORKSHARING: ante
    cualquier excepcion inesperada respondia "si el modelo es
    compartido, puede ser que la vista pertenezca a otro usuario",
    apuntara o no al lugar correcto. Era un parche deliberado de la
    Fase 5, y costo caro: el bug de Areas de la Fase 7 se veia como
    un problema de propiedad y llevo un ciclo entero de reinicio
    diagnosticarlo.

    Ahora la propiedad se comprueba DE VERDAD antes de escribir
    (`writer._require_owned`), asi que ese caso llega como
    `WriteRejected` con `code = "owned_by_other_user"` por la rama de
    arriba. Lo que caiga aca ya no se atribuye al worksharing: se
    dice que fallo y se manda la causa real en `detail`.
    """
    if isinstance(ex, writer.WriteRejected):
        return http.fail(str(ex), status=409, code=ex.code)
    # [Fase 9] Revit rechazo la operacion y lo dijo el. Es 409 y no
    # 500 por la misma razon que WriteRejected: hay una causa
    # conocida, y viaja en `detail` con el texto exacto de Revit.
    if isinstance(ex, writer.RevitRefused):
        return http.fail(str(ex), status=409, code=ex.code,
                         detail=ex.detail)
    return http.fail(
        "Could not {0}.".format(action),
        status=500,
        detail=str(ex),
    )


def _schedules_from_ids(doc, ids):
    """(views, faltantes). Nunca falla por un id inexistente."""
    views = []
    missing = []
    for schedule_id in ids:
        view = reader.find_schedule(doc, schedule_id)
        if view is None:
            missing.append(schedule_id)
        else:
            views.append(view)
    return views, missing


def _ids_from_body(body, key="schedule_ids"):
    try:
        return [int(value) for value in (body.get(key) or [])]
    except (AttributeError, TypeError, ValueError):
        return []


def _cell_context(doc, schedule_id, request):
    """(view, payload, field, element, param, respuesta_de_error)."""
    # [Fase 22, B1] Los caches de parametros y de tipo sobreviven entre
    # peticiones; sin esto, el tipo resuelto podia ser el de antes de
    # cambiar el `Type` de la fila. Ver `writer._set_parameter`.
    ser.clear_param_cache()
    view, failure = _editable_schedule(doc, schedule_id)
    if failure is not None:
        return None, None, None, None, None, failure

    try:
        field_id = http.query_int(request, "field_id", default=None)
        element_id = http.query_int(request, "element_id", default=None)
    except http.QueryParamsUnsupported:
        return None, None, None, None, None, http.query_params_unsupported()

    if field_id is None or element_id is None:
        return None, None, None, None, None, http.fail(
            "`field_id` and `element_id` are required.", status=400)

    payload, field = reader.find_visible_field(view.Definition, field_id)
    if payload is None:
        return None, None, None, None, None, http.fail(
            "That field is not in this schedule.", status=404,
            code="field_not_found")

    element = doc.GetElement(ser.to_element_id(element_id))
    if element is None:
        return None, None, None, None, None, http.fail(
            "There is no element with id {0}.".format(element_id),
            status=404)

    param = None
    if not payload["revit_computed"]:
        param = ser.find_parameter(element, field.ParameterId,
                                   prefer_type=payload["is_type_field"])
    return view, payload, field, element, param, None


def register_routes(api):
    """Registra todas las rutas de ScheduleBoard en la API dada."""

    # Preflight CORS para todos los patrones. Estos handlers no declaran
    # `doc`, asi que corren en el hilo del servidor y no encolan un
    # ExternalEvent detras del comando activo de Revit.
    for pattern in PATTERNS:
        http.register_preflight(api, pattern)

    # --- Salud ------------------------------------------------------------

    @api.route("/status/", methods=["GET"])
    def status(doc, request):
        """Salud del servidor y del documento activo.

        Alimenta el punto de conexion del board. Devuelve tambien
        `runtime`, que responde una de las verificaciones pendientes de
        la Fase 0: bajo que motor corre realmente el handler.

        OJO: esta ruta NO usa `http.no_document()`. Es la ruta de salud,
        y "no hay documento abierto" es una respuesta valida y exitosa
        para ella: si devolviera error, el frontend dibujaria "servidor
        caido" con el servidor perfecto. Las demas rutas si fallan.
        """
        # Sin documento abierto, pyRevit inyecta `doc = None`. Eso NO es
        # un error: es un estado normal de Revit y esta ruta alimenta el
        # punto de conexion del board. Si devolvieramos 500, el frontend
        # dibujaria "servidor caido" cuando el servidor esta perfecto.
        document = None
        if doc is not None:
            try:
                document = {
                    "title": doc.Title,
                    "path": doc.PathName or None,
                    "is_workshared": bool(doc.IsWorkshared),
                    "is_family": bool(doc.IsFamilyDocument),
                }
            except Exception as ex:
                document = {"error": str(ex)}

        # Que Revit es este. Con varias versiones abiertas cada una toma
        # un puerto distinto (48884, 48885...), y el cliente necesita
        # saber cual es cual: las rutas propias de pyRevit que lo dirian
        # (`/routes/sisters`) NO llevan cabeceras CORS, asi que desde el
        # navegador son inusables. Tiene que salir de aca.
        # Campo por campo a proposito: si un atributo no existe en alguna
        # version, se pierde ese campo y no los otros dos.
        host = {}
        try:
            from pyrevit import HOST_APP
        except Exception as ex:
            HOST_APP = None
            host["error"] = str(ex)
        for key, attr in (("version", "version"),
                          ("build", "build"),
                          ("name", "pretty_name")):
            if HOST_APP is None:
                break
            try:
                host[key] = str(getattr(HOST_APP, attr))
            except Exception:
                host[key] = None

        return http.ok({
            "service": "ScheduleBoard",
            "revit": host,
            "document": document,
            "runtime": {
                "python_version": str(sys.version),
                "implementation": _python_implementation(),
            },
            # Capacidades del servidor de Routes que nos condicionan.
            # `query_params` es False en pyRevit anterior a 6.5.3, y sin
            # el no hay paginacion posible: el frontend deberia negarse a
            # arrancar en vez de descubrirlo con una tabla de 11 MB.
            "capabilities": {
                "query_params": http.supports_query_params(request),
                # [Fase 17] Si esta maquina puede escribir en el modelo.
                # El board lo usa para decirlo ANTES de que el usuario
                # apriete un boton; quien lo hace cumplir es
                # `writer._require_writes`, no el cliente.
                "writes_enabled": writer.writes_enabled(),
                # [27-09-2026] "installed" o "skipped: <motivo>". Ver
                # `http.install_fast_serializer`.
                "fast_serializer": http.SERIALIZER_STATE,
            },
        })

    # --- Lectura de schedules --------------------------------------------

    @api.route("/progress/", methods=["GET"])
    def progress():
        """En que anda la operacion larga que hay en vuelo.  [Fase 11]

        NO DECLARA `doc`, Y ESO ES TODO EL PUNTO DE ESTA RUTA.
        `wants_api_context` de pyRevit mira la firma del handler: sin
        `doc`, `uidoc` ni `uiapp`, esto corre en el HILO DEL SERVIDOR y
        responde aunque el ExternalEvent este ocupado con la escritura
        cuyo progreso se esta preguntando. Es el mismo mecanismo por el
        que el latido de conexion usa un preflight OPTIONS y no
        `/status/`.

        Medido en la Fase 11, con un handler con `doc` reteniendo el
        hilo principal 10 s: 8 lecturas seguidas en 4-6 ms cada una,
        viendo el contador avanzar. Verificado tambien que los dos
        handlers comparten el modulo `writer` vivo.

        SI ALGUIEN LE AGREGA `doc` A ESTA FIRMA, la ruta deja de servir
        para lo unico que existe: quedaria encolada detras de la misma
        escritura que intenta observar.

        Devuelve datos, no frases: los textos que ve el usuario los
        arma el cliente, para no mantener strings visibles en dos
        lugares. `seq` sube una vez por operacion y con eso el cliente
        descarta la lectura de una escritura anterior.
        """
        return http.ok(progress_state.snapshot())

    @api.route("/audit/", methods=["GET"])
    def audit_log(request):
        """El rastro de lo que el board escribio.  [Fase 14]

        TAMPOCO DECLARA `doc`, y por la misma razon que `/progress/`: el
        panel `History` tiene que poder abrirse mientras una escritura
        larga tiene tomado el ExternalEvent. Es la segunda ruta del
        proyecto que no lo declara, y la lista es corta a proposito.

        La contra de no declarar `doc` es que no puede saber cual es el
        documento activo, asi que `audit.read()` lee el `.jsonl` mas
        recientemente modificado. Con un documento abierto siempre es el
        correcto; con dos, se pasa `?document=`.

        Devuelve tambien `error`: si el propio registro fallo al escribir
        -- disco lleno, archivo bloqueado -- eso NO puede voltear la
        escritura del usuario, pero tiene que verse en algun lado.
        """
        try:
            limit = http.query_int(request, "limit", 50)
            document = http.query_str(request, "document", None)
        except http.QueryParamsUnsupported:
            return http.query_params_unsupported()
        return http.ok(audit.read(limit=limit, document=document))

    @api.route("/changes/", methods=["GET"])
    def document_changes(request):
        """Que cambio en el modelo desde `since`.  [Fase 23]

        LA TERCERA RUTA QUE NO DECLARA `doc`, y por la misma razon que
        `/progress/` y `/audit/`: el board la sondea junto al latido, y
        tiene que contestar con Revit ocupado. Solo lee el diario de
        `changes.py`, que llena el handler de `DocumentChanged`; no toca
        la API de Revit.

        Query `since` (el `seq` de la ultima respuesta; 0 la primera vez) y
        `document` (ruta o titulo, la clave del layout del cliente).
        Devuelve `epoch`: si cambia, el diario empezo de cero (un Reload de
        pyRevit, un `/dev/reload/`) y el cliente no puede confiar en su
        `since`. Y `oldest_seq`: si `since` quedo por debajo, se perdieron
        entradas.
        """
        try:
            since = http.query_int(request, "since", 0)
            document = http.query_str(request, "document", None)
        except http.QueryParamsUnsupported:
            return http.query_params_unsupported()
        return http.ok(changes.snapshot(since=since, document=document))

    @api.route("/schedules/", methods=["GET"])
    def schedules(doc, request):
        """Lista los schedules con su resumen.

        [Fase 22] Ya no acepta `?counts=1`. Contaba los elementos de cada
        vista, que congela Revit 5-10 minutos con los 240 schedules, y era
        un GET sin preflight: cualquier pagina abierta en la maquina podia
        dispararlo. Ningun cliente lo usaba.
        """
        if doc is None:
            return http.no_document()

        started = time.time()
        try:
            items = reader.list_schedules(doc)
        except Exception as ex:
            return http.fail("Failed to list the schedules.",
                             status=500, detail=str(ex))

        return http.ok({
            "count": len(items),
            "workshared": ws.is_workshared(doc),
            "elapsed_ms": int((time.time() - started) * 1000),
            "schedules": items,
        })

    @api.route("/schedules/<int:schedule_id>/", methods=["GET"])
    def schedule_detail(doc, request, schedule_id):
        """Detalle de un schedule: columnas y filas paginadas.

        Cada fila trae `element_ids` (lista, no un id suelto): una fila
        colapsada por `IsItemized = False` representa varios elementos.

        Query params:
            offset=N   arranca en la fila logica N. Default 0.
            limit=N    cuantas filas devolver. Sin limite, todas.

        OJO con schedules no itemizados: `limit` acota lo que se
        DEVUELVE, pero no reduce lo que se LEE, porque colapsar exige
        conocer los valores de todos los elementos primero. La primera
        pagina de un schedule grande y no itemizado paga el costo
        completo igual. Ver `reader._collapsed_rows`.
        """
        if doc is None:
            return http.no_document()

        try:
            offset = http.query_int(request, "offset", default=0)
            limit = http.query_int(request, "limit", default=None)
        except http.QueryParamsUnsupported:
            return http.query_params_unsupported()

        try:
            view = reader.find_schedule(doc, schedule_id)
        except Exception as ex:
            return http.fail("Failed to look up the schedule.",
                             status=500, detail=str(ex))

        if view is None:
            return http.fail(
                "There is no schedule with id {0}.".format(schedule_id),
                status=404,
            )

        started = time.time()
        try:
            detail = reader.schedule_detail(doc, view, offset=offset, limit=limit)
        except Exception as ex:
            return http.fail("Failed to rebuild the table.",
                             status=500, detail=str(ex))

        detail["elapsed_ms"] = int((time.time() - started) * 1000)
        return http.ok(detail)

    # --- Perfilado (Fase 2) ------------------------------------------------

    @api.route("/schedules/<int:schedule_id>/profile/", methods=["GET"])
    def schedule_profile(doc, request, schedule_id):
        """Perfilado de todos los campos schedulable de la categoria.

        Es UNA pasada cara sobre una MUESTRA acotada de elementos del
        schedule (ver `profiler.PROFILE_SAMPLE_SIZE`), sin paginar:
        pensada para dispararse por click explicito en el panel de
        columnas ("Perfilar columnas"), nunca junto con expandir la
        tarjeta ni en la carga inicial del board. Ver `profiler.py`
        para el porque de la pasada unica y del cap de elementos.
        """
        if doc is None:
            return http.no_document()

        try:
            view = reader.find_schedule(doc, schedule_id)
        except Exception as ex:
            return http.fail("Failed to look up the schedule.",
                             status=500, detail=str(ex))

        if view is None:
            return http.fail(
                "There is no schedule with id {0}.".format(schedule_id),
                status=404,
            )

        started = time.time()
        try:
            profile = profiler.profile_schedulable_fields(doc, view)
            profile["quality"] = profiler.column_quality(profile)
        except Exception as ex:
            return http.fail("Failed to profile the fields.",
                             status=500, detail=str(ex))

        profile["elapsed_ms"] = int((time.time() - started) * 1000)
        return http.ok(profile)

    # --- Gestion de columnas (Fase 5) -------------------------------------
    #
    # Primera escritura de DEFINICION del proyecto: toca ViewSchedule y
    # ScheduleDefinition, jamas un dato del modelo. Las tres rutas de
    # escritura devuelven la lista fresca de columnas para que el cliente
    # se re-sincronice sin pagar otra vuelta por la cola serial.

    @api.route("/schedules/<int:schedule_id>/fields/", methods=["GET"])
    def schedule_fields(doc, schedule_id):
        """Columnas del schedule, en orden e INCLUIDAS las ocultas.

        Distinta de las `columns` que devuelve `/schedules/<id>/`: esa
        omite las ocultas porque no se pintan. Para gestionarlas hay que
        verlas. Es barata -- no recorre elementos, solo la definicion.
        """
        if doc is None:
            return http.no_document()

        view, error = _editable_schedule(doc, schedule_id)
        if error is not None:
            return error

        try:
            fields = reader.all_fields(view.Definition)
        except Exception as ex:
            return http.fail("Failed to read the fields.",
                             status=500, detail=str(ex))
        return _fields_response(view, fields)

    @api.route("/schedules/<int:schedule_id>/fields/", methods=["POST"])
    def schedule_fields_add(doc, request, schedule_id):
        """Agrega una columna. Body: {"parameter_id": N, "field_type": "..."}

        `parameter_id` y `field_type` son los que expone el perfilado de
        la Fase 2: el usuario elige el campo con su % de llenado a la
        vista, que es el punto del producto.
        """
        if doc is None:
            return http.no_document()

        view, error = _editable_schedule(doc, schedule_id)
        if error is not None:
            return error

        body = request.data or {}
        parameter_id = body.get("parameter_id")
        if parameter_id is None:
            return http.fail("`parameter_id` is missing from the body.", status=400)

        try:
            fields = writer.add_field(doc, view, parameter_id,
                                      body.get("field_type"))
        except Exception as ex:
            return _write_failed(ex, "add the field")
        return _fields_response(view, fields)

    @api.route("/schedules/<int:schedule_id>/fields/", methods=["PUT"])
    def schedule_fields_update(doc, request, schedule_id):
        """Reordena y/o cambia visibilidad. UNA transaccion, un Ctrl+Z.

        Body: {"order": [id, ...], "hidden": {"id": true}}. Los dos
        campos son opcionales por separado, pero al menos uno tiene que
        venir. `order` debe traer TODAS las columnas actuales.
        """
        if doc is None:
            return http.no_document()

        view, error = _editable_schedule(doc, schedule_id)
        if error is not None:
            return error

        body = request.data or {}
        try:
            fields = writer.update_fields(doc, view,
                                          order=body.get("order"),
                                          hidden=body.get("hidden"))
        except Exception as ex:
            return _write_failed(ex, "update the fields")
        return _fields_response(view, fields)

    @api.route("/schedules/<int:schedule_id>/fields/<int:field_id>/",
               methods=["DELETE"])
    def schedule_fields_remove(doc, schedule_id, field_id):
        """Quita una columna del schedule.

        Ojo: en Revit un campo puede estar siendo usado por un filtro o
        por el orden. Si Revit se niega, el mensaje sale por
        `_write_failed` con el detalle real.
        """
        if doc is None:
            return http.no_document()

        view, error = _editable_schedule(doc, schedule_id)
        if error is not None:
            return error

        try:
            fields = writer.remove_field(doc, view, field_id)
        except Exception as ex:
            return _write_failed(ex, "remove the field")
        return _fields_response(view, fields)

    # --- Lote entre schedules (Fase 6) ------------------------------------
    #
    # La restriccion que manda acá es 6.5 del planteamiento:
    # `GetSchedulableFields()` devuelve un conjunto distinto por
    # categoria, asi que un campo puede no aplicar a toda la seleccion.
    # `GET /batch/fields/` existe para poder AVISARLO ANTES de ejecutar.

    @api.route("/schedules/<int:schedule_id>/definition/", methods=["GET"])
    def schedule_definition(doc, schedule_id):
        """Receta del schedule: columnas, filtros y orden.

        Es lo que el board muestra antes de copiar a un lote, para que
        el usuario vea QUE va a copiar. Barata: solo la definicion.
        """
        if doc is None:
            return http.no_document()

        view, error = _editable_schedule(doc, schedule_id)
        if error is not None:
            return error

        try:
            # [Fase 25] Con lo que hace falta para editarla: operadores y
            # tipo de valor por campo, filtros numericos en la unidad que
            # se ve, y las casillas del dialogo. Sigue siendo barata.
            return http.ok(reader.definition_payload(doc, view))
        except Exception as ex:
            return http.fail("Failed to read the definition.",
                             status=500, detail=str(ex))

    @api.route("/schedules/<int:schedule_id>/definition/", methods=["PUT"])
    def schedule_definition_update(doc, request, schedule_id):
        """El OK de Schedule Properties.  [Fase 25]

        Body, todas las claves opcionales -- la que viene reemplaza su
        seccion entera, la que no viene no se toca:
            filters   [{field_id, filter_type, value}]   hasta 8, AND
            sort      [{field_id, ascending, show_header, show_footer,
                        show_footer_title, show_footer_count,
                        show_blank_line}]                 hasta 4
            itemized, filter_by_sheet, include_links, grand_total,
            grand_total_title_shown, grand_total_count    booleanos
            grand_total_title                            texto
            fields    [{field_id, heading, heading_orientation,
                        horizontal_alignment, vertical_alignment,
                        display_type, multiple_values,
                        multiple_values_custom_text, format}]
                      [Fase 26] el formato de cada campo; por item, solo
                      las claves presentes. `format` es el Field Format
                      entero: {use_default} o {unit, symbol, accuracy,
                      suppress_trailing_zeros, digit_grouping}
        Un filtro numerico viaja en la unidad que VE el usuario; el
        servidor lo pasa a unidades internas. UNA transaccion.
        """
        if doc is None:
            return http.no_document()

        view, error = _editable_schedule(doc, schedule_id)
        if error is not None:
            return error

        try:
            payload = writer.set_definition(doc, view, request.data or {})
        except Exception as ex:
            return _write_failed(ex, "apply the schedule properties")
        return http.ok(payload)

    @api.route("/schedules/<int:schedule_id>/fields/<int:field_id>/",
               methods=["GET"])
    def schedule_field_values(doc, schedule_id, field_id):
        """Los valores posibles de un campo `ElementId`.  [Fase 25]

        Para el desplegable de un filtro por `Level` o `Family`. Recorre
        la categoria del schedule, asi que cuesta segundos en una
        categoria grande: el cliente la pide solo al elegir el campo.
        """
        if doc is None:
            return http.no_document()

        view, error = _editable_schedule(doc, schedule_id)
        if error is not None:
            return error

        definition = view.Definition
        found = writer._find_field_id(definition, field_id)
        if found is None:
            return http.fail("That field is not in this schedule.",
                             status=404, code="field_not_found")
        field = definition.GetField(found)
        if reader.field_storage(doc, field) != "ElementId":
            # Un texto o un numero se escribe; solo un `ElementId` necesita
            # elegirse de una lista.
            return http.fail("Only a field that points to an element has a "
                             "list of values.", status=409,
                             code="not_element_id")
        try:
            values, truncated = reader.field_values(doc, view, field)
        except Exception as ex:
            return http.fail("Failed to read the values of the field.",
                             status=500, detail=str(ex))
        ser.clear_param_cache()
        return http.ok({"field_id": field_id, "values": values,
                        "truncated": truncated})

    @api.route("/batch/fields/", methods=["GET"])
    def batch_fields(doc, request):
        """Campos aplicables a una seleccion, con a cuantos aplica cada uno.

        Query params:
            ids=1,2,3   los schedules seleccionados.

        Devuelve la union con `applies_to`, no la interseccion: hace
        falta saber de los que NO aplican a todos para poder decir
        "este campo aplica a 4 de los 7 seleccionados" antes de
        ejecutar, que es lo que pide 6.5.
        """
        if doc is None:
            return http.no_document()

        try:
            raw = http.query_str(request, "ids", default="")
        except http.QueryParamsUnsupported:
            return http.query_params_unsupported()

        ids = []
        for chunk in (raw or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                ids.append(int(chunk))
            except ValueError:
                return http.fail(
                    "The `ids` parameter must be a comma-separated list of "
                    "integers.", status=400)
        if not ids:
            return http.fail("The `ids` parameter is missing.", status=400)

        views, missing = _schedules_from_ids(doc, ids)
        if not views:
            return http.fail("None of the ids matches a schedule.",
                             status=404)

        started = time.time()
        try:
            fields = reader.compatible_fields(doc, views)
            # Las columnas ACTUALES viajan en la misma respuesta. Son la
            # lectura que faltaba para poder ofrecer "quitar del lote"
            # sin hacerlo a ciegas: `fields` son los ~2.500 que Revit
            # OFRECE por categoria, no los que el schedule usa. Es
            # barata (solo definiciones) y evita otra vuelta por la cola
            # serial, que con 15 schedules serian 15 peticiones.
            columns = reader.columns_across(views)
        except Exception as ex:
            return http.fail("Failed to work out the compatible fields.",
                             status=500, detail=str(ex))

        return http.ok({
            "schedule_count": len(views),
            "missing_ids": missing,
            "field_count": len(fields),
            "fields": fields,
            "column_count": len(columns),
            "columns": columns,
            "elapsed_ms": int((time.time() - started) * 1000),
        })

    @api.route("/batch/apply/", methods=["POST"])
    def batch_apply(doc, request):
        """Aplica los mismos cambios a N schedules, en UNA transaccion.

        Body:
            {
              "schedule_ids": [1, 2, 3],
              "add_fields":    [{"parameter_id": N, "field_type": "...", "name": "..."}],
              "remove_fields": [{"parameter_id": N, "field_type": "..."}],
              "copy_from": 123,
              "copy": ["order", "filters", "sort"]
            }

        Un fallo en un schedule no aborta los demas: se reporta schedule
        por schedule. En modelo compartido el lote parcial es el caso
        normal (6.6), no la excepcion.
        """
        if doc is None:
            return http.no_document()

        body = request.data or {}
        ids = _ids_from_body(body)
        if not ids:
            return http.fail("`schedule_ids` is missing from the body.", status=400)

        views, missing = _schedules_from_ids(doc, ids)
        if not views:
            return http.fail("None of the ids matches a schedule.",
                             status=404)

        # Los tipos que el board trata como solo lectura no entran al
        # lote, por la misma razon que en la Fase 5: no sabemos pintar su
        # tabla, asi que el usuario no podria ver lo que hizo.
        editable = []
        rejected = []
        for view in views:
            kind = reader.schedule_kind(view)
            if reader.is_editable_kind(kind):
                editable.append(view)
            else:
                rejected.append({"schedule_id": ser.element_id_value(view.Id),
                                 "name": ser.safe_name(view),
                                 "reason": "kind '{0}', read-only".format(kind)})
        if not editable:
            return http.fail(
                "None of the selected schedules is editable.",
                status=409, code="schedule_not_editable")

        source_view = None
        copy_parts = body.get("copy") or []
        if copy_parts:
            copy_from = body.get("copy_from")
            if copy_from is None:
                return http.fail(
                    "Copying {0} was requested but `copy_from` is missing.".format(
                        ", ".join(copy_parts)), status=400)
            source_view = reader.find_schedule(doc, copy_from)
            if source_view is None:
                return http.fail(
                    "The source schedule {0} does not exist.".format(copy_from),
                    status=404)

        started = time.time()
        try:
            result = writer.apply_batch(
                doc, editable,
                add_fields=body.get("add_fields") or [],
                remove_fields=body.get("remove_fields") or [],
                source_view=source_view,
                copy_parts=copy_parts,
            )
        except Exception as ex:
            return _write_failed(ex, "apply the batch")

        result["missing_ids"] = missing
        result["rejected"] = rejected
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return http.ok(result)

    # --- Crear y borrar schedules (Fase 7) --------------------------------
    #
    # Los patrones de estas dos rutas (`/schedules/` y
    # `/schedules/<id>/`) YA estaban registrados por las Fases 0/1, asi
    # que su preflight ya existe: pyRevit indexa por (patron, metodo) y
    # el OPTIONS de un patron sirve para todos sus metodos. `duplicate/`
    # y `categories/` si son patrones nuevos y estan en PATTERNS.
    #
    # ASIMETRIA DELIBERADA entre duplicar y borrar: duplicar exige un
    # schedule editable, porque duplicar un takeoff daria una tarjeta que
    # el board no sabe pintar. Borrar se permite en CUALQUIER tipo -- el
    # argumento de la Fase 5 ("no podrias ver lo que hiciste") no aplica
    # cuando el resultado visible es que la tarjeta desaparece, y son
    # justamente los tipos de solo lectura los que uno querria limpiar.

    @api.route("/categories/", methods=["GET"])
    def categories(doc, request):
        """Categorias que admiten schedule, para el selector de crear.

        Query params:
            default_names=1   agrega el nombre sugerido por Revit.

        Apagado por defecto porque los costos no se parecen: los nombres
        de las 188 categorias son 11 ms y los nombres por defecto son
        1.643 ms (~9 ms cada `GetDefaultNameForSchedule`). El cliente los
        pide UNA vez al abrir el panel de crear y los cachea.
        """
        if doc is None:
            return http.no_document()

        try:
            with_defaults = http.query_flag(request, "default_names",
                                            default=False)
        except http.QueryParamsUnsupported:
            return http.query_params_unsupported()

        started = time.time()
        try:
            items = reader.valid_categories(doc, with_default_names=with_defaults)
        except Exception as ex:
            return http.fail("Failed to list the categories.",
                             status=500, detail=str(ex))

        # Los area schemes viajan con las categorias y no en una ruta
        # propia: los necesita el mismo panel, son 4 en el modelo de
        # referencia, y asi no se paga otra vuelta por la cola serial ni
        # hace falta otro patron con su preflight.
        try:
            schemes = reader.area_schemes(doc)
        except Exception:
            schemes = []

        return http.ok({
            "count": len(items),
            "with_default_names": with_defaults,
            "elapsed_ms": int((time.time() - started) * 1000),
            "categories": items,
            "area_schemes": schemes,
        })

    @api.route("/schedules/", methods=["POST"])
    def schedules_create(doc, request):
        """Crea un schedule vacio para una categoria.

        Body: {"category_id": -2000011, "name": "opcional",
               "area_scheme_id": N}

        Sin `name`, queda el nombre que pone Revit
        (`GetDefaultNameForSchedule`), que ya viene libre de colisiones.
        Nace sin columnas: se agregan con el panel de la Fase 5.

        `area_scheme_id` es OBLIGATORIO y solo aplica cuando la
        categoria es Areas -- la unica de las 187 que lo exige. El
        cliente sabe cuales por la bandera `needs_area_scheme` de
        `/categories/`, y los schemes disponibles vienen en la misma
        respuesta.
        """
        if doc is None:
            return http.no_document()

        body = request.data or {}
        category_id = body.get("category_id")
        if category_id is None:
            return http.fail("`category_id` is missing from the body.", status=400)
        try:
            category_id = int(category_id)
        except (TypeError, ValueError):
            return http.fail("`category_id` must be a number.", status=400)

        try:
            summary = writer.create_schedule(doc, category_id, body.get("name"),
                                             body.get("area_scheme_id"))
        except Exception as ex:
            return _write_failed(ex, "create the schedule")
        return http.ok({"schedule": summary})

    @api.route("/schedules/<int:schedule_id>/duplicate/", methods=["POST"])
    def schedules_duplicate(doc, request, schedule_id):
        """Duplica un schedule con sus columnas, filtros y orden.

        Body: {"name": "opcional"}. Sin `name`, Revit lo llama
        "<origen> Copy 1".
        """
        if doc is None:
            return http.no_document()

        view, error = _editable_schedule(doc, schedule_id)
        if error is not None:
            return error

        body = request.data or {}
        try:
            summary = writer.duplicate_schedule(doc, view, body.get("name"))
        except Exception as ex:
            return _write_failed(ex, "duplicate the schedule")
        return http.ok({"schedule": summary})

    @api.route("/schedules/<int:schedule_id>/", methods=["DELETE"])
    def schedules_delete(doc, schedule_id):
        """Borra un schedule. La confirmacion es responsabilidad del cliente.

        A diferencia del resto de las rutas de escritura, NO exige que el
        schedule sea editable: ver el comentario de la asimetria arriba.

        `doc.Delete` arrastra dependientes; la respuesta trae cuantos
        elementos se fueron y en cuantas laminas estaba, que es lo que el
        cliente debio advertir ANTES de llamar aca.
        """
        if doc is None:
            return http.no_document()

        try:
            view = reader.find_schedule(doc, schedule_id)
        except Exception as ex:
            return http.fail("Failed to look up the schedule.",
                             status=500, detail=str(ex))
        if view is None:
            return http.fail(
                "There is no schedule with id {0}.".format(schedule_id),
                status=404,
            )

        try:
            result = writer.delete_schedule(doc, view)
        except Exception as ex:
            return _write_failed(ex, "delete the schedule")
        return http.ok(result)

    # --- Seleccion y aislamiento (Fase 3) ---------------------------------

    @api.route("/selection/", methods=["POST"])
    def selection_select(uidoc, request):
        """Selecciona elementos en Revit. Sin transaccion.

        Body: {"element_ids": [123, 456]}

        Pensada para dispararse con un debounce corto del lado del
        cliente ante selecciones de fila multiples (ctrl/shift+click):
        una sola llamada con el conjunto final, no una por click.
        """
        if uidoc is None or uidoc.Document is None:
            return http.no_document()

        element_ids = _element_ids_from_body(request)
        if not element_ids:
            return http.fail("No element_ids were received.", status=400)

        try:
            result = selection.select_elements(uidoc, element_ids)
        except Exception as ex:
            return http.fail("Failed to select the elements.",
                             status=500, detail=str(ex))
        return http.ok(result)

    @api.route("/selection/isolate/", methods=["POST"])
    def selection_isolate(uidoc, request):
        """Aisla temporalmente los elementos en la vista activa.

        REQUIERE transaccion. Body: {"element_ids": [123, 456]}
        """
        if uidoc is None or uidoc.Document is None:
            return http.no_document()

        element_ids = _element_ids_from_body(request)
        if not element_ids:
            return http.fail("No element_ids were received.", status=400)

        try:
            result = selection.isolate_elements(uidoc, element_ids)
        except selection.IsolateNotPossible as ex:
            # No es un fallo: es un "no, porque la vista activa no puede
            # mostrarlo". Antes de la Fase 7 esto no se detectaba y la
            # operacion fingia haber funcionado.
            return http.fail(str(ex), status=409, code=ex.code)
        except writer.RevitRefused as ex:
            # [Fase 22, B4] El texto de Revit que junto el `_FailureCatcher`
            # viaja en `detail`, igual que en las demas escrituras.
            return http.fail(str(ex), status=409, code=ex.code,
                             detail=ex.detail)
        except Exception as ex:
            return http.fail("Failed to isolate the elements.",
                             status=500, detail=str(ex))
        return http.ok(result)

    @api.route("/selection/isolate/", methods=["DELETE"])
    def selection_clear_isolation(uidoc):
        """Quita el aislamiento temporal de la vista activa. REQUIERE transaccion."""
        if uidoc is None or uidoc.Document is None:
            return http.no_document()

        try:
            result = selection.clear_isolation(uidoc)
        except writer.RevitRefused as ex:
            return http.fail(str(ex), status=409, code=ex.code,
                             detail=ex.detail)
        except Exception as ex:
            return http.fail("Failed to clear the isolation.",
                             status=500, detail=str(ex))
        return http.ok(result)

    # --- Edicion de celda (Fase 9) ---------------------------------------
    #
    # PRIMERA ESCRITURA DE UN DATO DEL MODELO del proyecto. Todo lo
    # anterior tocaba ViewSchedule / ScheduleDefinition, o sea la
    # definicion de una vista; esto toca el elemento.
    #
    # Un solo patron con dos metodos: GET devuelve con que se puede
    # llenar la celda (opciones y a cuantas instancias afecta) y PUT
    # escribe. pyRevit indexa por (patron, metodo), asi que comparten el
    # preflight.

    @api.route("/schedules/<int:schedule_id>/cell/", methods=["GET"])
    def cell_options(doc, request, schedule_id):
        """Con que se puede llenar esta celda, antes de tocarla.

        Query: field_id, element_id (un elemento representativo de la fila).

        Devuelve:
            editable   si el board puede escribirla, y `reason` si no
            options    valores posibles cuando el parametro es ElementId.
                       Salen de `element.GetValidTypes()` cuando el valor
                       es el tipo del elemento -- la respuesta de Revit a
                       "que puede ser esto" -- y si no, de los elementos
                       de la misma clase que el valor actual
            affects    a cuantas instancias del modelo afecta escribir,
                       que para una columna de tipo NO es una: es el
                       aviso que pide el entregable de la fase
        """
        if doc is None:
            return http.no_document()

        view, payload, field, element, param, failure = _cell_context(
            doc, schedule_id, request)
        if failure is not None:
            return failure

        result = {
            "field_id": payload["field_id"],
            "name": payload["name"],
            "is_type_field": payload["is_type_field"],
            "editable": True,
            "reason": None,
            "options": [],
            "affects": 1,
            # [Fase 19] Mismo formato que la tabla: si esta celda dijera
            # `220.0` y la de al lado `220.00`, la diferencia se veria
            # como un bug del board.
            "cell": ser.parameter_payload(param, reader._format_options(field)),
        }

        if payload["revit_computed"]:
            result["editable"] = False
            result["reason"] = (
                "Computed by Revit. Formulas, percentages, combined fields "
                "and Count have no parameter to write to.")
            return http.ok(result)

        if param is None:
            result["editable"] = False
            result["reason"] = "This element does not have that parameter."
            return http.ok(result)

        if bool(param.IsReadOnly):
            result["editable"] = False
            result["reason"] = "Read-only in Revit."
            return http.ok(result)

        started = time.time()
        try:
            if str(param.StorageType) == "ElementId":
                result["options"] = reader.parameter_options(doc, element, param)
        except Exception as ex:
            result["options_error"] = str(ex)

        # A cuantas instancias afecta. Solo se cuenta para las columnas
        # de tipo: en una de instancia la respuesta es siempre 1 y el
        # recorrido no valdria la pena.
        if payload["is_type_field"]:
            try:
                result["affects"] = len(reader.instances_of_type(
                    doc, param.Element.Id, view.Definition.CategoryId))
            except Exception:
                result["affects"] = None

        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return http.ok(result)

    @api.route("/schedules/<int:schedule_id>/cell/", methods=["PUT"])
    def cell_write(doc, request, schedule_id):
        """Escribe un valor sobre los elementos de una fila o de un lote.

        Body: {"field_id": N, "element_ids": [...], "value": ..., "rows": N}

        `element_ids` es una LISTA porque una fila no itemizada
        representa varios elementos. Todo entra en UNA transaccion.

        [Fase 10] La edicion por lote de filas usa ESTA MISMA ruta: el
        cliente manda la union de los ids de las filas seleccionadas.
        No hace falta una ruta aparte porque el problema es el mismo --
        un conjunto de elementos, un campo, un valor -- y porque asi el
        lote hereda tal cual las guardas y el checkout unico de la
        celda. `rows` es opcional y solo nombra la transaccion, o sea el
        texto que el usuario ve en el menu *Undo* de Revit.

        [Fase 16] `?dry_run=1` devuelve el ALCANCE sin escribir nada: sin
        tomar posesion y sin abrir transaccion. Misma forma de respuesta
        (`affected_element_ids`, `skipped_element_ids`, `results` con los
        motivos agrupados por `code`), mas `dry_run: true`, y el MISMO 409
        que devolveria la escritura cuando no queda nada escribible.

        Va como query param y no como campo del body a proposito: el body
        queda identico entre el preview y la escritura, asi que el cliente
        manda el mismo objeto a las dos y no puede desincronizarlos.

        METODO NUEVO SOBRE UN PATRON EXISTENTE: no hay que tocar
        `PATTERNS` -- el preflight de `/schedules/<id>/cell/` ya esta.
        """
        if doc is None:
            return http.no_document()

        view, failure = _editable_schedule(doc, schedule_id)
        if failure is not None:
            return failure

        body = request.data or {}
        field_id = body.get("field_id")
        if field_id is None:
            return http.fail("`field_id` is missing from the body.", status=400)
        try:
            field_id = int(field_id)
        except (TypeError, ValueError):
            return http.fail("`field_id` must be a number.", status=400)

        element_ids = _element_ids_from_body(request)
        if not element_ids:
            return http.fail("`element_ids` is missing from the body.",
                             status=400)

        if "value" not in body:
            return http.fail("`value` is missing from the body.", status=400)

        try:
            rows = int(body.get("rows") or 1)
        except (TypeError, ValueError):
            rows = 1

        # Sin query params no hay preview posible, y contestar la escritura
        # seria lo peor que se puede hacer: el cliente pidio VER y se
        # escribiria. `query_flag` lanza en vez de devolver el default
        # justamente para que este caso no pase inadvertido.
        try:
            dry_run = http.query_flag(request, "dry_run", default=False)
        except http.QueryParamsUnsupported:
            return http.query_params_unsupported()

        started = time.time()
        try:
            result = writer.set_parameter(
                doc, view, field_id, element_ids, body.get("value"),
                rows=rows, dry_run=dry_run)
        except Exception as ex:
            # El preview usa el MISMO camino de error que la escritura, y
            # eso es lo que hace que un 409 previsto tenga el `code` exacto
            # del 409 real. El verbo cambia porque el texto lo lee un
            # usuario: "could not write the cell" sobre algo que no
            # intentaba escribir seria mentira.
            return _write_failed(
                ex, "preview the change" if dry_run else "write the cell")

        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return http.ok(result)

    # --- Worksharing (Fases 15) ---------------------------------------------
    #
    # [Fase 22] Aca estaba `POST /worksharing/checkout/`, la sonda de la
    # Fase 8. No tenia cliente y escribia en el central: se saco. El
    # checkout sigue vivo dentro de la escritura (`writer.checkout_elements`).

    @api.route("/worksharing/pending/", methods=["GET"])
    def worksharing_pending(doc):
        """Que quedo prestado de sesiones ANTERIORES del board.  [Fase 15]

        DECLARA `doc` A PROPOSITO, y es la diferencia con `/audit/` y
        `/progress/`: `GetCheckoutStatus` necesita el documento, asi que
        esta si pasa por el `ExternalEvent`. Es aceptable porque es UNA
        llamada al abrir el board, no un latido.

        Y tener `doc` compra algo de paso: lee el `.jsonl` EXACTO de este
        documento en vez de la adivinanza del mas reciente que tiene que
        hacer `/audit/`, que no sabe cual es el documento activo.

        El camino sin worksharing corta en la primera linea y cuesta
        cero -- era la verificacion por HTTP que quedo pendiente de la
        Fase 11.
        """
        if doc is None:
            return http.no_document()

        started = time.time()
        try:
            result = ws.pending_checkouts(doc)
        except Exception as ex:
            return http.fail("Could not check what is still checked out.",
                             status=500, detail=str(ex))
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return http.ok(result)

    @api.route("/worksharing/relinquish/", methods=["POST"])
    def worksharing_relinquish(doc):
        """Devuelve la posesion de los elementos tomados.  [Fase 15]

        NUNCA SE LLAMA SOLA. El plan lo pide explicito y la razon real
        esta en `writer.relinquish`: no es que pueda destruir trabajo sin
        sincronizar -- la API lo impide sola --, es que ademas concede
        las solicitudes de posesion pendientes de OTROS usuarios y es una
        operacion contra el central. Eso lo decide el usuario.

        Los fallos de central y de red llegan como excepcion y se
        convierten en un 502 con el texto de Revit en `detail`: sin eso
        el usuario ve "no se pudo" sin saber si fue la red, un permiso o
        que otro cliente tiene el central bloqueado.
        """
        if doc is None:
            return http.no_document()

        started = time.time()
        try:
            result = writer.relinquish(doc)
        except Exception as ex:
            return http.fail(
                "Could not relinquish the elements. The central model may be "
                "unreachable or locked by another client.",
                status=502, detail=str(ex), code="relinquish_failed")
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return http.ok(result)

    # --- Desarrollo  [Fase 21] ---------------------------------------------

    @api.route("/dev/reload/", methods=["POST"])
    def dev_reload(doc, uiapp):
        """Recarga `lib/` sin reiniciar Revit. Solo con el archivo `dev-mode`.

        DECLARA `doc` SIN USARLO, y es a proposito: asi pasa por el
        `ExternalEvent`, queda en la fila serial con todo lo demas y nunca
        corre en medio de una transaccion ni de una lectura. Por lo mismo
        no pide documento: recargar con Revit vacio es valido.

        `startup.py` y `pane.py` siguen exigiendo reiniciar Revit.
        """
        if not http.dev_mode_enabled():
            return http.fail(
                "Reloading the board backend is only for development, and "
                "it is not enabled on this machine.",
                status=403, code="dev_mode_disabled")

        started = time.time()
        try:
            reloaded = reload_backend(uiapp.Application)
        except Exception:
            import traceback
            return http.fail(
                "The backend could not be reloaded. The previous routes are "
                "still in place.",
                status=500, detail=traceback.format_exc(),
                code="reload_failed")
        return http.ok({
            "reloaded": reloaded,
            "reset": list(RELOAD_RESETS),
            "routes": len(routes.get_routes(API_NAME)),
            "elapsed_ms": int((time.time() - started) * 1000),
        })

    # --- Rutas de fases posteriores --------------------------------------
    #
    # Al agregar una ruta, agregar tambien su patron a PATTERNS.
    #
    # Las Fases 9 y 10 no dejaron ninguna pendiente: la edicion de celda
    # y la edicion por lote de filas comparten `PUT /schedules/<id>/cell/`
    # -- la segunda manda la union de los ids de las filas seleccionadas
    # y `rows` para el nombre de la transaccion. Lo unico que falta del
    # plan es la Fase 11, que es endurecimiento y no rutas nuevas.

    return api
