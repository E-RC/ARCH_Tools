# -*- coding: UTF-8 -*-
"""Escritura al modelo. TODA funcion de aqui abre y confirma su transaccion.

REGLA 3 (ver `__init__.py`): una operacion de usuario = una transaccion.
Un lote de 30 escrituras debe producir UNA entrada de deshacer en Revit,
no 30.

Estado por fase:
    add_field              implementado   [Fase 5]
    remove_field           implementado   [Fase 5]
    update_fields          implementado   [Fase 5]  orden + visibilidad
    apply_batch            implementado   [Fase 6]  N schedules a la vez
    create_schedule        implementado   [Fase 7]
    duplicate_schedule     implementado   [Fase 7]
    delete_schedule        implementado   [Fase 7]
    checkout_elements      implementado   [Fase 8]  worksharing
    set_parameter          implementado   [Fases 9 y 10]  celda y lote
    set_definition         implementado   [Fase 25]  filtros, orden, casillas

Desde la Fase 14, `_commit` deja un rastro de TODA transaccion en
`audit.py`. Cuelga ahi y no de cada funcion por la misma razon que
`_FailureCatcher`: es el unico lugar por donde pasan todas, asi que una
escritura futura no puede quedar sin registrar por olvido.

La Fase 10 -- edicion por lote de filas -- NO agrego una funcion nueva:
es la misma `set_parameter` recibiendo la union de los ids de las filas
seleccionadas. Editar una fila colapsada y aplicar un valor a 9 filas
son el mismo problema, y el checkout unico que pide 6.6 ya lo hacia
`_ensure_owned`. El stub `set_parameter_batch` que reservaba el hueco se
borro: su firma era anterior a la Fase 9 y no resolvia la columna contra
la definicion real, que es de donde sale `is_type_field`.

La Fase 8 agrego una guarda de propiedad a TODA funcion que modifique
un schedule existente: `_require_owned` en las de uno solo y
`_ensure_owned` en el lote. Crear y duplicar no la llevan porque
producen una vista nueva, que nace nuestra y no hay a quien pedirsela.
"""

import os

import System

from System.Collections.Generic import List

from Autodesk.Revit import DB

from schedule_board import audit
from schedule_board.paths import data_path
from schedule_board import progress
from schedule_board import reader
from schedule_board import serializers as ser
from schedule_board import worksharing as ws


# El andamio de fases (`PhaseNotReached` / `_not_yet`) se retiro al cerrar
# la Fase 10: ya no queda ninguna funcion pendiente en este modulo. En
# `selection.py` sigue vivo, pero por otra razon -- ahi marca algo que
# quedo deliberadamente fuera de alcance, no algo por hacer.


# --- El interruptor de escritura  [Fase 17] ------------------------------
#
# POR QUE EXISTE
# --------------
# No es por miedo a un crash: un crash no pierde nada, porque Revit no
# habia guardado. Es por la POSESION COLGADA que encontro la Fase 15 --
# Ctrl+Z no devuelve la posesion y un crash nunca hace *Relinquish*, asi
# que una escritura interrumpida deja elementos prestados y bloquea a un
# colega hasta que esa persona abra su local.
#
# DECISION DE PUBLICACION: la escritura VIENE ACTIVA. La Fase 17 la tenia
# apagada por defecto; al publicar la extension se invirtio, y ahora el
# archivo `writes-disabled` es el que deja una maquina en solo lectura
# (leer, perfilar, seleccionar, aislar y VER EL PREVIEW siguen enteros).
#
# POR QUE UN ARCHIVO Y NO UNA VARIABLE DE ENTORNO
# -----------------------------------------------
# Porque habilitarlo o deshabilitarlo es "crear o borrar un archivo vacio",
# sin tocar codigo ni variables de sistema. La carpeta ya existe: es la
# misma donde viven `startup.log`, el rastro y el perfil de WebView2.
#
# SE LEE FRESCO EN CADA ESCRITURA, no al arrancar. Cuesta un `os.path`
# contra escrituras de 1 a 12 segundos, y a cambio cambiar el modo no
# exige reiniciar Revit -- que en este proyecto son minutos.
WRITES_MARKER = "writes-disabled"


def _marker_path():
    return data_path(WRITES_MARKER)


def writes_enabled():
    """`True` si esta maquina puede escribir en el modelo.  [Fase 17]

    Lo reporta tambien `GET /status/`, para que el board pueda decirlo
    ANTES de que el usuario apriete un boton y no despues. Pero la
    autoridad es esta funcion, no el cliente: las dos guardas de abajo
    son las que hacen que el modo sea una garantia y no una cortesia.
    """
    try:
        return not os.path.isfile(_marker_path())
    except Exception:
        # Si no se puede ni preguntar, lo seguro es no escribir.
        return False


def _require_writes():
    """Corta cualquier escritura si la maquina esta en solo lectura.

    CUELGA DE LOS DOS EMBUDOS DE ESCRITURA DE ESTE MODULO, que son los
    mismos dos que ya estaban documentados como tales:

      - `_commit`, por donde pasan TODAS las transacciones (el mismo
        argumento por el que ahi viven `_FailureCatcher` y el rastro);
      - `checkout_elements`, la excepcion deliberada a la regla 2, que
        escribe en el central sin transaccion.

    `relinquish` -- la otra excepcion -- NO lleva la guarda, y es una
    decision, no un olvido: DEVUELVE posesion. Es la cura de lo que este
    modo previene, asi que apagarla dejaria a alguien sin la salida de la
    Fase 15 justo en la maquina donde mas falta hace.
    """
    if not writes_enabled():
        raise WriteRejected(
            "This machine is set to view only, so the board did not write "
            "anything. Delete the writes-disabled file in the ScheduleBoard "
            "folder under LOCALAPPDATA to enable it.",
            code="writes_disabled",
        )


# --- Fase 5: gestion de columnas ------------------------------------------
#
# NINGUNA de estas funciones construye un id a partir de un entero.
# `ScheduleFieldId(int)` y `ElementId(int)` compiten con las sobrecargas
# de enum en la resolucion de IronPython, y ademas
# `ScheduleFieldId` tiene `.IntegerValue` pero NO `.Value`, al reves que
# `ElementId`. Los ids se BUSCAN entre los que ya devuelve la API,
# comparando su valor entero. Es una busqueda lineal sobre un punado de
# campos: no vale la pena arriesgar nada por ahorrarla.


class WriteRejected(Exception):
    """Escritura rechazada por una causa conocida y explicable.

    El mensaje es para el usuario y `code` es el identificador estable
    con el que el cliente distingue el caso sin parsear texto. Se
    distingue de un fallo inesperado: esto es un "no, porque...", no un
    "algo se rompio".
    """

    def __init__(self, message, code):
        Exception.__init__(self, message)
        self.code = code


def _find_field_id(definition, field_id_value):
    """El ScheduleFieldId vivo cuyo valor entero coincide, o None."""
    try:
        field_ids = list(definition.GetFieldOrder())
    except Exception:
        return None

    for field_id in field_ids:
        try:
            if int(field_id.IntegerValue) == int(field_id_value):
                return field_id
        except Exception:
            continue
    return None


def _find_schedulable_field(definition, parameter_id, field_type):
    """El SchedulableField que coincide en (ParameterId, FieldType).

    `field_type` puede venir None: ahi alcanza con el parametro. Se
    compara como texto porque es lo que el payload de perfilado ya
    expone ("Instance", "ElementType", ...).
    """
    try:
        candidates = list(definition.GetSchedulableFields())
    except Exception:
        return None

    for field in candidates:
        try:
            if ser.element_id_value(field.ParameterId) != int(parameter_id):
                continue
        except Exception:
            continue
        if field_type is not None:
            try:
                if str(field.FieldType) != str(field_type):
                    continue
            except Exception:
                continue
        return field
    return None


def _existing_field(definition, parameter_id, field_type):
    """La columna ya presente que coincide en (ParameterId, FieldType).

    Sirve para no agregar dos veces el mismo campo. Se compara por
    parametro y tipo, no con `GetSchedulableField() == sf` como el
    ejemplo de la documentacion: la igualdad de esos objetos bajo
    IronPython no es algo en lo que convenga apoyarse.
    """
    try:
        field_ids = list(definition.GetFieldOrder())
    except Exception:
        return None

    for field_id in field_ids:
        try:
            field = definition.GetField(field_id)
            if ser.element_id_value(field.ParameterId) != int(parameter_id):
                continue
            if field_type is not None and str(field.FieldType) != str(field_type):
                continue
            return field
        except Exception:
            continue
    return None


class RevitRefused(Exception):
    """Revit rechazo la operacion con un error propio, y lo dice el.

    Se distingue de `WriteRejected` en el origen: eso es un "no" NUESTRO,
    decidido por una guarda; esto es un "no" de Revit, con su texto, que
    llega por el preprocesador de fallos.
    """

    def __init__(self, message, code="revit_refused", detail=None):
        Exception.__init__(self, message)
        self.code = code
        self.detail = detail


class _FailureCatcher(DB.IFailuresPreprocessor):
    """Atrapa los fallos de la transaccion en vez de dejar que Revit
    abra un dialogo modal.

    POR QUE ES OBLIGATORIO, y lo descubrio la Fase 9 de la peor forma:
    escribir un parametro de un elemento que esta DENTRO DE UN GRUPO
    hace que Revit levante un error "cannot be ignored" que solo ofrece
    *Ungroup* o *Cancel*. Ese modal **cuelga la API para siempre** --
    bloquea el ExternalEvent, quema un nucleo en el busy-wait de pyRevit
    y solo se sale con un clic humano. Medido en vivo: la peticion nunca
    volvio.

    Las escrituras de las Fases 5 a 7 nunca lo disparaban porque tocaban
    la DEFINICION de una vista, donde Revit no tiene nada que objetar.
    Escribir datos del modelo es otra cosa, y por eso esto vive en
    `_commit`, que es el unico lugar por donde pasan TODAS las
    transacciones del proyecto: asi ninguna futura puede colgarse.

    Politica:
      - Los avisos se borran. Son informativos y Revit los mostraria en
        un dialogo que nadie va a cerrar.
      - Los errores REVIERTEN la transaccion y se guardan sus textos,
        para devolverlos como `detail` en vez de un modal.
    """

    def __init__(self):
        self.errors = []

    def PreprocessFailures(self, accessor):
        try:
            accessor.DeleteAllWarnings()
        except Exception:
            pass

        try:
            failures = list(accessor.GetFailureMessages())
        except Exception:
            failures = []

        for failure in failures:
            try:
                severity = str(failure.GetSeverity())
            except Exception:
                severity = "Error"
            if severity == "Warning":
                continue
            try:
                self.errors.append(failure.GetDescriptionText())
            except Exception:
                self.errors.append("Revit reported an error without text.")

        if self.errors:
            # NO se resuelve por nuestra cuenta: el unico camino que
            # ofrecia Revit en el caso de los grupos era *Ungroup*, que
            # es destructivo. Se revierte y se informa.
            return DB.FailureProcessingResult.ProceedWithRollBack
        return DB.FailureProcessingResult.Continue


def _commit(doc, name, apply_changes):
    """Corre `apply_changes()` dentro de UNA transaccion.

    Regla 3 (ver `__init__.py`): una operacion de usuario = una transaccion =
    una entrada en la pila de deshacer de Revit. Todo lo que tenga que
    pasar junto va adentro de un solo `apply_changes`.

    Y desde la Fase 9, con `_FailureCatcher`: sin el, un error de Revit
    abre un modal que cuelga la API indefinidamente. Ver su docstring.

    Y desde la Fase 14 deja el rastro en `audit.py`, por el mismo motivo
    por el que `_FailureCatcher` vive aca: es el unico lugar por donde
    pasan TODAS las transacciones, asi que ninguna futura puede quedar
    sin registrar porque alguien se olvido de anotarla en su caller.

    Y desde la Fase 17 es tambien donde muere una escritura cuando la
    maquina esta en solo lectura. Ver `_require_writes`.
    """
    _require_writes()
    catcher = _FailureCatcher()

    transaction = DB.Transaction(doc, name)
    transaction.Start()
    try:
        options = transaction.GetFailureHandlingOptions()
        options.SetFailuresPreprocessor(catcher)
        options.SetClearAfterRollback(True)
        transaction.SetFailureHandlingOptions(options)

        result = apply_changes()
        # La fase que se lleva el 66-93% del reloj (ver PROGRESS): Revit
        # aplicando el cambio. No admite division -- es una sola llamada
        # -- asi que se nombra en vez de fingir un porcentaje. Va aca
        # porque `_commit` es el unico lugar por donde pasan TODAS las
        # transacciones del proyecto.
        progress.phase("applying")
        status = transaction.Commit()
    except Exception as ex:
        if transaction.HasStarted():
            transaction.RollBack()
        audit.record(doc, name, "rolled_back", error=ex, code="exception")
        raise

    # El commit puede devolver RolledBack sin lanzar nada, cuando el
    # preprocesador decidio revertir. Darlo por bueno seria reportar un
    # exito que no ocurrio -- el mismo error que se evita mirando el
    # retorno de `Parameter.Set()`.
    if catcher.errors or str(status) == "RolledBack":
        detail = " | ".join(catcher.errors) or None
        # Se registra el intento fallido igual que el exitoso: un
        # `RolledBack` es informacion, y es la mitad del valor del rastro.
        audit.record(doc, name, "rolled_back", error=detail,
                     code="revit_refused")
        raise RevitRefused(
            "Revit refused the change and nothing was written.",
            detail=detail,
        )

    audit.record(doc, name, "committed")
    return result


def add_field(doc, view, parameter_id, field_type=None):
    """Agrega una columna al schedule.  [Fase 5]

    `parameter_id` y `field_type` son los que expone el perfilado de la
    Fase 2, o sea que el usuario elige el campo viendo su % de llenado.
    """
    definition = view.Definition

    if _existing_field(definition, parameter_id, field_type) is not None:
        raise WriteRejected(
            "That field is already in this schedule.",
            "field_already_present",
        )

    schedulable = _find_schedulable_field(definition, parameter_id, field_type)
    if schedulable is None:
        raise WriteRejected(
            "Revit does not offer that field for this schedule. The profile "
            "you are looking at may belong to a different schedule, or the "
            "parameter may no longer exist in the project.",
            "field_not_schedulable",
        )

    # Posesion recien aca: las validaciones de arriba son locales y
    # gratis, y no vale ir al central a pedir un schedule para despues
    # rechazar la peticion por otra causa.
    _require_owned(doc, view)

    _commit(doc, "ScheduleBoard: add field",
            lambda: definition.AddField(schedulable))
    return reader.all_fields(definition)


def remove_field(doc, view, field_id_value):
    """Quita una columna del schedule.  [Fase 5]"""
    definition = view.Definition

    field_id = _find_field_id(definition, field_id_value)
    if field_id is None:
        raise WriteRejected(
            "That field is not in this schedule.",
            "field_not_found",
        )

    _require_owned(doc, view)

    _commit(doc, "ScheduleBoard: remove field",
            lambda: definition.RemoveField(field_id))
    return reader.all_fields(definition)


def update_fields(doc, view, order=None, hidden=None):
    """Reordena columnas y cambia su visibilidad, en UNA transaccion.  [Fase 5]

    Las dos cosas van juntas a proposito: son la misma operacion de
    usuario ("dejar las columnas como quiero"), y separarlas daria dos
    entradas de deshacer para un solo gesto (regla 3).

    `order`   lista de field_id con el orden deseado. Debe contener
              EXACTAMENTE los campos actuales: `SetFieldOrder` con un
              subconjunto no es "reordenar estos", y aceptar un
              subconjunto seria borrar columnas por omision, que es
              justo lo que no queremos que pase sin pedirlo.
    `hidden`  dict {field_id: bool}. Puede tocar solo algunos campos.
    """
    definition = view.Definition

    ordered_ids = None
    if order:
        current = list(definition.GetFieldOrder())
        if len(order) != len(current):
            raise WriteRejected(
                "The order received has {0} fields and the schedule has "
                "{1}. All of them must be sent.".format(len(order), len(current)),
                "order_incomplete",
            )
        ordered_ids = []
        for value in order:
            field_id = _find_field_id(definition, value)
            if field_id is None:
                raise WriteRejected(
                    "The order received includes a field that is not in "
                    "this schedule.",
                    "field_not_found",
                )
            ordered_ids.append(field_id)

    visibility = []
    for raw_id, want_hidden in (hidden or {}).items():
        field_id = _find_field_id(definition, raw_id)
        if field_id is None:
            raise WriteRejected(
                "Cannot hide a field that is not in this schedule.",
                "field_not_found",
            )
        visibility.append((field_id, bool(want_hidden)))

    if ordered_ids is None and not visibility:
        raise WriteRejected(
            "No change received: `order` or `hidden` is missing.",
            "nothing_to_do",
        )

    def apply_changes():
        if ordered_ids is not None:
            # SetFieldOrder pide una List<ScheduleFieldId> de .NET; una
            # lista de Python no sirve.
            dotnet_ids = List[DB.ScheduleFieldId]()
            for field_id in ordered_ids:
                dotnet_ids.Add(field_id)
            definition.SetFieldOrder(dotnet_ids)
        for field_id, want_hidden in visibility:
            definition.GetField(field_id).IsHidden = want_hidden

    _require_owned(doc, view)

    _commit(doc, "ScheduleBoard: reorder fields", apply_changes)
    return reader.all_fields(definition)


# --- Fase 25: Schedule Properties -----------------------------------------
#
# Lo que hace el OK del dialogo nativo en las pestanas Filter y
# Sorting/Grouping, mas las casillas de Fields que no son columnas. UNA
# transaccion por OK (regla 3), como en Revit.
#
# Los filtros y el orden referencian campos DE ESTE schedule por su
# `field_id`, igual que el dialogo, que solo ofrece los campos que ya
# estan (incluidos los ocultos). Por eso aca no hace falta el
# `_ensure_field(hidden=True)` del lote: eso existe para COPIAR a otro
# schedule que puede no tener el campo.

MAX_FILTERS = 8           # el dialogo de Revit ofrece ocho
MAX_SORT_LEVELS = 4       # y cuatro niveles de orden


def _definition_field(definition, raw_id, what):
    field_id = _find_field_id(definition, raw_id)
    if field_id is None:
        raise WriteRejected(
            "The {0} uses a field that is not in this schedule.".format(what),
            "field_not_found")
    return field_id


def _plan_filters(doc, definition, items):
    """Valida los filtros y los deja listos para `_build_filter`."""
    if len(items) > MAX_FILTERS:
        raise WriteRejected(
            "Revit allows up to {0} filters.".format(MAX_FILTERS),
            "too_many_filters")
    plan = []
    for item in items:
        field_id = _definition_field(definition, item.get("field_id"), "filter")
        field = definition.GetField(field_id)
        options = reader.field_options(doc, definition, field)
        filter_type = item.get("filter_type")
        if filter_type not in options["filter_operators"]:
            raise WriteRejected(
                "'{0}' cannot be filtered with '{1}'.".format(
                    field.GetName(), filter_type),
                "filter_not_allowed")
        kind = reader.filter_value_kind(filter_type, options["storage"])
        value = item.get("value")
        try:
            if kind == "none":
                value = None
            elif kind == "double":
                value = reader.to_internal_number(doc, field, value)
            elif kind in ("integer", "element_id"):
                value = int(value)
            else:
                value = u"" if value is None else u"{0}".format(value)
        except (TypeError, ValueError):
            raise WriteRejected(
                "The value for '{0}' is not valid.".format(field.GetName()),
                "filter_value_invalid")
        plan.append((field_id, filter_type, kind, value))
    return plan


def _plan_sort(definition, items):
    if len(items) > MAX_SORT_LEVELS:
        raise WriteRejected(
            "Revit allows up to {0} sorting levels.".format(MAX_SORT_LEVELS),
            "too_many_sort_levels")
    plan = []
    for item in items:
        field_id = _definition_field(definition, item.get("field_id"),
                                     "sorting")
        if not definition.CanSortByField(field_id):
            raise WriteRejected(
                "Revit cannot sort by '{0}'.".format(
                    definition.GetField(field_id).GetName()),
                "sort_not_allowed")
        plan.append((field_id, dict(item)))
    return plan


# --- Formatting  [Fase 26] ------------------------------------------------
#
# Por campo, y solo lo que viaja: cada clave presente de un item se
# aplica, una ausente no se toca (a diferencia de filtros y orden, que se
# reconstruyen enteros, aca no hay lista que reconstruir).

_FORMAT_ENUMS = (
    # clave del item, propiedad del ScheduleField, enum de Revit
    ("heading_orientation", "HeadingOrientation", "ScheduleHeadingOrientation"),
    ("horizontal_alignment", "HorizontalAlignment",
     "ScheduleHorizontalAlignment"),
    ("vertical_alignment", "VerticalAlignment", "ScheduleVerticalAlignment"),
    ("display_type", "DisplayType", "ScheduleFieldDisplayType"),
    ("multiple_values", "MultipleValuesDisplayType",
     "ScheduleFieldMultipleValuesDisplayType"),
)


def _enum_value(enum_name, raw, field_name, key):
    enum_type = getattr(DB, enum_name, None)
    if enum_type is None:
        raise WriteRejected(
            "This version of Revit does not have '{0}'.".format(key),
            "format_not_supported")
    if u"{0}".format(raw) not in list(System.Enum.GetNames(enum_type)):
        raise WriteRejected(
            "'{0}' is not a valid value for '{1}' of '{2}'.".format(
                raw, key, field_name), "format_value_invalid")
    return System.Enum.Parse(enum_type, u"{0}".format(raw))


def _plan_format_options(field, raw):
    """El *Field Format* pedido, como un `FormatOptions` ya validado."""
    name = field.GetName()
    if raw.get("use_default"):
        return DB.FormatOptions()     # UseDefault = True
    spec = field.GetSpecTypeId()
    valid_units = dict((unit.TypeId, unit)
                       for unit in DB.UnitUtils.GetValidUnits(spec))
    unit = valid_units.get(raw.get("unit"))
    if unit is None:
        raise WriteRejected(
            "'{0}' cannot use that unit.".format(name), "format_value_invalid")
    options = DB.FormatOptions(unit)
    symbol = raw.get("symbol") or ""
    symbols = dict((item.TypeId or "", item)
                   for item in DB.FormatOptions.GetValidSymbols(unit))
    if symbol not in symbols:
        raise WriteRejected(
            "'{0}' cannot use that unit symbol.".format(name),
            "format_value_invalid")
    if symbol:
        options.SetSymbolTypeId(symbols[symbol])
    if raw.get("accuracy") is not None:
        try:
            accuracy = float(raw["accuracy"])
        except (TypeError, ValueError):
            accuracy = None
        if accuracy is None or not options.IsValidAccuracy(accuracy):
            raise WriteRejected(
                "That rounding is not valid for '{0}'.".format(name),
                "format_value_invalid")
        options.Accuracy = accuracy
    if raw.get("suppress_trailing_zeros") is not None and \
            options.CanSuppressTrailingZeros():
        options.SuppressTrailingZeros = bool(raw["suppress_trailing_zeros"])
    if raw.get("digit_grouping") is not None:
        options.UseDigitGrouping = bool(raw["digit_grouping"])
    return options


def _plan_fields(doc, definition, items):
    """Valida el formato de cada campo; devuelve (field_id, cambios)."""
    plan = []
    for item in items:
        field_id = _definition_field(definition, item.get("field_id"),
                                     "formatting")
        field = definition.GetField(field_id)
        name = field.GetName()
        changes = []
        if item.get("heading") is not None:
            changes.append(("ColumnHeading", u"{0}".format(item["heading"])))
        for key, attr, enum_name in _FORMAT_ENUMS:
            if item.get(key) is None:
                continue
            if not hasattr(field, attr):
                raise WriteRejected(
                    "This version of Revit does not have '{0}'.".format(key),
                    "format_not_supported")
            changes.append((attr, _enum_value(enum_name, item[key], name, key)))
        mode = item.get("display_type")
        # Lo mismo que ofrece el dialogo: Totals solo si `CanTotal`, y
        # Min/Max solo si `CanDisplayMinMax`. Revit lanzaria adentro de
        # la transaccion; aca se dice antes, con el nombre del campo.
        if mode == "Totals" and not field.CanTotal():
            raise WriteRejected("'{0}' cannot calculate totals.".format(name),
                                "calculation_not_allowed")
        if mode in ("MinMax", "Min", "Max") and not field.CanDisplayMinMax():
            raise WriteRejected(
                "'{0}' cannot show minimum and maximum.".format(name),
                "calculation_not_allowed")
        if item.get("multiple_values_custom_text") is not None:
            changes.append(("MultipleValuesCustomText",
                            u"{0}".format(item["multiple_values_custom_text"])))
        if item.get("format") is not None:
            if reader.field_format(doc, field) is None and \
                    not item["format"].get("use_default"):
                raise WriteRejected(
                    "'{0}' has no number format.".format(name),
                    "format_not_supported")
            changes.append(("format", _plan_format_options(field,
                                                           item["format"])))
        if changes:
            plan.append((field_id, changes))
    return plan


def _apply_fields(definition, plan):
    for field_id, changes in plan:
        field = definition.GetField(field_id)
        for attr, value in changes:
            if attr == "format":
                field.SetFormatOptions(value)
            else:
                setattr(field, attr, value)


def _plan_definition(doc, definition, recipe):
    """Valida TODA la receta antes de abrir la transaccion.

    Cada clave presente reemplaza su seccion entera; una ausente no se
    toca. Devuelve un dict con solo lo que hay que aplicar.
    """
    plan = {}
    if recipe.get("fields") is not None:
        fields_plan = _plan_fields(doc, definition, recipe["fields"])
        if fields_plan:
            plan["fields"] = fields_plan
    if recipe.get("filters") is not None:
        plan["filters"] = _plan_filters(doc, definition, recipe["filters"])
    if recipe.get("sort") is not None:
        plan["sort"] = _plan_sort(definition, recipe["sort"])
    for key in ("itemized", "filter_by_sheet", "include_links",
                "grand_total", "grand_total_title_shown", "grand_total_count"):
        if recipe.get(key) is not None:
            plan[key] = bool(recipe[key])
    if plan.get("filter_by_sheet") and \
            not definition.IsValidCategoryForFilterBySheet():
        raise WriteRejected(
            "This category cannot be filtered by sheet.",
            "filter_by_sheet_not_allowed")
    if plan.get("include_links") and not definition.CanIncludeLinkedFiles():
        raise WriteRejected(
            "This schedule cannot include elements in links.",
            "links_not_allowed")
    # Medido en la Fase 25 sobre `TO-Bajo NTN`: encender IncludeLinkedFiles
    # BORRA EN SILENCIO los filtros que comparan contra un elemento del
    # modelo (su `Level < NPT_1` desaparecio; los de texto quedaron). Se
    # rechaza antes, diciendo cual, en vez de perder un filtro sin aviso.
    links_on = plan.get("include_links",
                        bool(definition.IncludeLinkedFiles))
    if links_on:
        if "filters" in plan:
            kinds = [(definition.GetField(fid).GetName(), kind)
                     for fid, _t, kind, _v in plan["filters"]]
        else:
            kinds = [(item["field"]["name"], item["value_kind"])
                     for item in reader.schedule_filters(definition)]
        lost = [name for name, kind in kinds if kind == "element_id"]
        if lost:
            raise WriteRejected(
                "With 'Include elements in links' on, Revit drops the filters "
                "that point to an element of this model ({0}). Remove them "
                "first, or leave links off.".format(", ".join(lost)),
                "links_drop_filters")
    title = recipe.get("grand_total_title")
    if title is not None:
        title = u"{0}".format(title).strip()
        if not title:
            raise WriteRejected("The grand total title is empty.",
                                "grand_total_title_empty")
        plan["grand_total_title"] = title
    return plan


def _apply_definition(definition, plan):
    """Aplica un plan ya validado. Sin transaccion propia: la abre el caller."""
    if "fields" in plan:
        _apply_fields(definition, plan["fields"])
    if "filters" in plan:
        definition.ClearFilters()
        for field_id, filter_type, kind, value in plan["filters"]:
            definition.AddFilter(_build_filter(field_id, filter_type, kind, value))
    if "filter_by_sheet" in plan:
        definition.IsFilteredBySheet = plan["filter_by_sheet"]
    if "sort" in plan:
        # Limpiar y reconstruir, como el lote (ver `_apply_to_one`).
        definition.ClearSortGroupFields()
        for field_id, item in plan["sort"]:
            sort_field = DB.ScheduleSortGroupField(
                field_id,
                DB.ScheduleSortOrder.Descending
                if item.get("ascending") is False
                else DB.ScheduleSortOrder.Ascending)
            sort_field.ShowHeader = bool(item.get("show_header"))
            sort_field.ShowFooter = bool(item.get("show_footer"))
            # Titulo y conteo del pie solo con pie, por la misma razon
            # que el gran total de abajo.
            if sort_field.ShowFooter:
                sort_field.ShowFooterTitle = bool(item.get("show_footer_title"))
                sort_field.ShowFooterCount = bool(item.get("show_footer_count"))
            sort_field.ShowBlankLine = bool(item.get("show_blank_line"))
            definition.AddSortGroupField(sort_field)
    for key, attr in (("itemized", "IsItemized"),
                      ("include_links", "IncludeLinkedFiles"),
                      ("grand_total", "ShowGrandTotal")):
        if key in plan:
            setattr(definition, attr, plan[key])
    # Revit LANZA si se toca el titulo o el conteo con el gran total
    # apagado ("Display of a grand total row is not enabled", medido en la
    # Fase 25). Por eso van despues del interruptor, y solo si quedo
    # encendido.
    if definition.ShowGrandTotal:
        for key, attr in (("grand_total_title_shown", "ShowGrandTotalTitle"),
                          ("grand_total_count", "ShowGrandTotalCount"),
                          ("grand_total_title", "GrandTotalTitle")):
            if key in plan:
                setattr(definition, attr, plan[key])


def set_definition(doc, view, recipe):
    """El OK de Schedule Properties: filtros, orden y casillas.  [Fase 25]

    [Fase 26] Y el formato de cada campo (`fields`), en la misma
    transaccion.

    Todo lo que llega va en UNA transaccion, un solo Ctrl+Z. Se valida
    entero antes -- es local y gratis -- y la posesion se pide recien
    despues, igual que `add_field`. Devuelve la receta fresca de Revit:
    si Revit hizo algo distinto de lo pedido, lo que se ve es lo que
    quedo de verdad.
    """
    definition = view.Definition
    plan = _plan_definition(doc, definition, recipe or {})
    if not plan:
        raise WriteRejected("No change received.", "nothing_to_do")

    _require_owned(doc, view)

    # El rastro con el schedule: sin `schedule_id` en `PENDING`, el
    # diario de la Fase 23 marcaria como "Changed in Revit" a la propia
    # tarjeta que acaba de aplicar.
    audit.begin("definition", schedule_id=ser.element_id_value(view.Id),
                schedule=ser.safe_name(view), parts=sorted(plan.keys()))
    try:
        _commit(doc, "ScheduleBoard: schedule properties",
                lambda: _apply_definition(definition, plan))
    except Exception as ex:
        if audit.pending():
            audit.record(doc, "ScheduleBoard: schedule properties",
                         "rejected", error=ex, code=getattr(ex, "code", None))
        raise
    return reader.definition_payload(doc, view)


# --- Fase 6: batch entre schedules ----------------------------------------

def _field_index(definition):
    """{(parameter_id, field_type): ScheduleFieldId} de las columnas actuales."""
    index = {}
    try:
        field_ids = list(definition.GetFieldOrder())
    except Exception:
        return index
    for field_id in field_ids:
        try:
            field = definition.GetField(field_id)
            key = (ser.element_id_value(field.ParameterId), str(field.FieldType))
        except Exception:
            continue
        index[key] = field_id
    return index


def _ensure_field(definition, parameter_id, field_type, hidden=False):
    """Devuelve el ScheduleFieldId del campo, agregandolo si falta.

    Con `hidden=True` el campo agregado queda oculto: es el patron que
    documenta la propia API de Revit para filtrar u ordenar por algo que
    no se quiere mostrar. Sin esto, copiar
    un filtro a un schedule que no tiene ese campo seria imposible.

    Devuelve (field_id, "presente" | "agregado" | None).
    """
    existing = _field_index(definition).get((parameter_id, field_type))
    if existing is not None:
        return existing, "presente"

    schedulable = _find_schedulable_field(definition, parameter_id, field_type)
    if schedulable is None:
        return None, None

    field = definition.AddField(schedulable)
    if hidden:
        field.IsHidden = True
    return field.FieldId, "agregado"


_FILTER_VALUE_SETTERS = ("string", "double", "integer", "element_id")


def _build_filter(field_id, filter_type_name, value_kind, value):
    """Reconstruye un ScheduleFilter en OTRA definicion.

    El `ScheduleFieldId` de origen no sirve: hay que pasar el del campo
    equivalente ya resuelto en el destino.
    """
    filter_type = getattr(DB.ScheduleFilterType, filter_type_name)
    if value_kind == "none" or value is None:
        return DB.ScheduleFilter(field_id, filter_type)
    if value_kind == "element_id":
        return DB.ScheduleFilter(field_id, filter_type, ser.to_element_id(value))
    if value_kind == "double":
        return DB.ScheduleFilter(field_id, filter_type, float(value))
    if value_kind == "integer":
        return DB.ScheduleFilter(field_id, filter_type, int(value))
    return DB.ScheduleFilter(field_id, filter_type, str(value))


def _apply_to_one(doc, view, add_fields, remove_fields, recipe, copy_parts):
    """Aplica el lote a UN schedule. Sin transaccion propia: la abre el lote."""
    definition = view.Definition
    report = {
        "schedule_id": ser.element_id_value(view.Id),
        "name": ser.safe_name(view),
        "added": [],
        "removed": [],
        "copied": [],
        "skipped": [],
        "error": None,
    }

    for spec in add_fields:
        # [20-10-2026] `spec["parameter_id"]` sin `.get()` tiraba un
        # KeyError crudo (500) ante un item malformado del cliente, distinto
        # del resto de esta funcion, que reporta cada item malo en
        # `skipped` y sigue con los demas (el lote parcial es el caso
        # normal, no la excepcion).
        if spec.get("parameter_id") is None:
            report["skipped"].append("a field to add is missing 'parameter_id'")
            continue
        key = (spec["parameter_id"], spec.get("field_type"))
        if _field_index(definition).get(key) is not None:
            report["skipped"].append("'{0}': already in the schedule".format(spec.get("name") or key[0]))
            continue
        schedulable = _find_schedulable_field(definition, key[0], key[1])
        if schedulable is None:
            report["skipped"].append(
                "'{0}': Revit does not offer it for this category".format(spec.get("name") or key[0]))
            continue
        definition.AddField(schedulable)
        report["added"].append(spec.get("name") or key[0])

    for spec in remove_fields:
        if spec.get("parameter_id") is None:
            report["skipped"].append("a field to remove is missing 'parameter_id'")
            continue
        key = (spec["parameter_id"], spec.get("field_type"))
        field_id = _field_index(definition).get(key)
        if field_id is None:
            report["skipped"].append("'{0}': was not in the schedule".format(spec.get("name") or key[0]))
            continue
        definition.RemoveField(field_id)
        report["removed"].append(spec.get("name") or key[0])

    if recipe is None or not copy_parts:
        return report

    # --- Copiar filtros ---------------------------------------------------
    if "filters" in copy_parts:
        definition.ClearFilters()
        aplicados = 0
        for item in recipe["filters"]:
            source_field = item["field"]
            field_id, how = _ensure_field(
                definition, source_field["parameter_id"], source_field["field_type"],
                hidden=True)
            if field_id is None:
                report["skipped"].append(
                    "filter by '{0}': the field does not exist in this category".format(
                        source_field.get("name")))
                continue
            if how == "agregado":
                report["added"].append(
                    "{0} (hidden, so it can filter)".format(source_field.get("name")))
            definition.AddFilter(_build_filter(
                field_id, item["filter_type"], item["value_kind"], item["value"]))
            aplicados += 1
        report["copied"].append("{0} filter(s)".format(aplicados))

    # --- Copiar orden y agrupacion ---------------------------------------
    if "sort" in copy_parts:
        # Limpiar y reconstruir, nunca agregar sobre lo que hay: leer
        # `GetSortGroupFields()[0]` despues de un AddSortGroupField NO
        # devuelve lo recien agregado si ya habia orden previo.
        definition.ClearSortGroupFields()
        aplicados = 0
        for item in recipe["sort"]:
            source_field = item["field"]
            field_id, how = _ensure_field(
                definition, source_field["parameter_id"], source_field["field_type"],
                hidden=True)
            if field_id is None:
                report["skipped"].append(
                    "sorting by '{0}': the field does not exist in this category".format(
                        source_field.get("name")))
                continue
            if how == "agregado":
                report["added"].append(
                    "{0} (hidden, so it can sort)".format(source_field.get("name")))
            sort_field = DB.ScheduleSortGroupField(
                field_id,
                DB.ScheduleSortOrder.Ascending if item["ascending"]
                else DB.ScheduleSortOrder.Descending)
            sort_field.ShowHeader = bool(item["show_header"])
            sort_field.ShowFooter = bool(item["show_footer"])
            sort_field.ShowFooterTitle = bool(item.get("show_footer_title"))
            sort_field.ShowFooterCount = bool(item.get("show_footer_count"))
            sort_field.ShowBlankLine = bool(item["show_blank_line"])
            definition.AddSortGroupField(sort_field)
            aplicados += 1
        report["copied"].append("{0} sorting level(s)".format(aplicados))

    # --- Copiar el orden de las columnas ----------------------------------
    if "order" in copy_parts:
        # El destino puede tener columnas que el origen no tiene: esas
        # conservan su orden relativo y van al final. SetFieldOrder exige
        # TODAS las columnas actuales, no un subconjunto.
        rank = {}
        for position, item in enumerate(recipe["order"]):
            rank[(item["parameter_id"], item["field_type"])] = position

        current = list(definition.GetFieldOrder())
        decorated = []
        for position, field_id in enumerate(current):
            try:
                field = definition.GetField(field_id)
                key = (ser.element_id_value(field.ParameterId), str(field.FieldType))
            except Exception:
                key = None
            decorated.append((rank.get(key, len(rank) + position), position, field_id))
        decorated.sort()

        ordered = List[DB.ScheduleFieldId]()
        for _rank, _position, field_id in decorated:
            ordered.Add(field_id)
        definition.SetFieldOrder(ordered)
        report["copied"].append("field order")

    return report


def apply_batch(doc, views, add_fields=None, remove_fields=None,
                source_view=None, copy_parts=None):
    """Aplica los mismos cambios a N schedules, en UNA transaccion.  [Fase 6]

    Envoltorio de progreso [Fase 11]: marca el estado terminal SIEMPRE,
    tambien si el cuerpo lanza antes de llegar al commit -- que es justo
    lo que pasa cuando el checkout falla. Sin esto el progreso quedaria
    clavado en su ultima fase y una lectura posterior de `/progress/`
    diria que Revit sigue trabajando cuando ya no.

    Se hace envolviendo en vez de indentar el cuerpo entero a proposito:
    un diff de 30 lineas de espacios en blanco sobre codigo verificado
    esconde cualquier cambio real que venga con el.
    """
    progress.begin("batch")
    # Este lote NO toca datos del modelo -- solo la definicion de las
    # vistas -- pero se registra igual: la Fase 15 necesita saber que se
    # toco, y estas cinco funciones tambien piden posesion.  [Fase 14]
    audit.begin(
        "batch",
        schedules=len(views or []),
        add_fields=len(add_fields or []),
        remove_fields=len(remove_fields or []),
        copy_parts=list(copy_parts or []),
    )
    try:
        result = _apply_batch(doc, views, add_fields, remove_fields,
                              source_view, copy_parts)
    except Exception as ex:
        progress.end("failed")
        if audit.pending():
            audit.record(doc, "ScheduleBoard: batch", "rejected", error=ex,
                         code=getattr(ex, "code", None))
        raise
    progress.end("done")
    return result


def _apply_batch(doc, views, add_fields, remove_fields, source_view,
                 copy_parts):
    """El cuerpo de `apply_batch`. Ver alli.

    RESTRICCION ESTRUCTURAL, ver 6.5 del planteamiento:
    `GetSchedulableFields()` devuelve un conjunto distinto segun la
    categoria, asi que un campo puede no aplicar a toda la seleccion. No
    se falla ni se aborta por eso: se aplica donde se puede y se reporta
    schedule por schedule que se hizo y que no. El aviso "aplica a N de
    M" lo da el cliente ANTES, con `reader.compatible_fields`.

    Los filtros y el orden se COPIAN DE UN SCHEDULE DE ORIGEN, porque un
    `ScheduleFilter` referencia un `ScheduleFieldId` que solo existe
    dentro de su propia definicion: no se copian, se reconstruyen campo
    por campo en cada destino.

    UNA transaccion para todo el lote (regla 3): un solo Ctrl+Z lo
    revierte entero. Un fallo en un schedule NO aborta los demas -- en
    modelo compartido el lote parcialmente exitoso es el caso normal
    (6.6), no la excepcion.
    """
    add_fields = add_fields or []
    remove_fields = remove_fields or []
    copy_parts = copy_parts or []

    recipe = None
    if source_view is not None and copy_parts:
        source_definition = source_view.Definition
        recipe = {
            "filters": reader.schedule_filters(source_definition),
            "sort": reader.schedule_sort_fields(source_definition),
            "order": [
                {"parameter_id": payload["parameter_id"],
                 "field_type": payload["field_type"]}
                for payload in reader.all_fields(source_definition)
            ],
        }

    # UN SOLO checkout grande para todo el lote, antes de abrir la
    # transaccion (6.6, y porque el checkout no puede ir dentro de una).
    # Los que niegue NO abortan el lote: se aplican los demas y cada
    # negado sale con su propio error. El lote parcialmente exitoso es
    # el caso NORMAL en modelo compartido, no la excepcion.
    progress.phase("checkout", 0, len(views))
    denied, checkout = _ensure_owned(doc, views)

    reports = []

    def apply_changes():
        # Aca el N de M SI es real: cada vuelta reconstruye los campos,
        # filtros y orden de un schedule entero. Es lo contrario del
        # bucle de `set_parameter`, donde cada vuelta es un `Set()` de
        # 0,34 ms y el tiempo esta en el commit.
        progress.phase("writing", 0, len(views))
        for index, view in enumerate(views):
            progress.step(index)
            key = ser.element_id_value(view.Id)
            if key in denied:
                info = denied[key]
                reports.append({
                    "schedule_id": key,
                    "name": info["name"],
                    "added": [], "removed": [], "copied": [], "skipped": [],
                    "error": _denied_reason(info)[0],
                    "owner": info["owner"],
                })
                continue
            try:
                reports.append(_apply_to_one(
                    doc, view, add_fields, remove_fields, recipe, copy_parts))
            except Exception as ex:
                reports.append({
                    "schedule_id": key,
                    "name": ser.safe_name(view),
                    "added": [], "removed": [], "copied": [], "skipped": [],
                    "error": str(ex),
                })
        progress.step(len(views))

    _commit(doc, "ScheduleBoard: batch of fields", apply_changes)

    result = {
        "applied_to": len([r for r in reports if r["error"] is None]),
        "failed": len([r for r in reports if r["error"] is not None]),
        "results": reports,
    }
    if checkout is not None:
        # `extra` son los elementos que Revit tomo sin que se pidieran.
        # 6.6 pide que el usuario se entere.
        result["checkout"] = {
            "denied": checkout["denied"],
            "extra": checkout["extra"],
        }
    return result


# --- Fase 7: crear y borrar -----------------------------------------------

def _validate_name(doc, name):
    """Valida un nombre de schedule ANTES de escribirlo. Devuelve el limpio.

    Las dos guardas salen de sondear Revit, no de suponer:

    1. `view.Name = ""` lo ACEPTA sin chistar, y deja una vista sin
       nombre que en el board se ve como "Schedule 50735425". Revit no
       nos protege de esto: hay que rechazarlo aca.

    2. Un nombre repetido lanza `Exception: Name must be unique.`, y bajo
       IronPython el tipo es `Exception` pelado -- no una
       `ArgumentException` que se pueda distinguir con `isinstance`. Sin
       este chequeo previo, la unica salida seria mostrarle al usuario el
       texto crudo en ingles de Revit.

    El chequeo previo no reemplaza al manejo del fallo: entre validar y
    escribir, la unica cosa capaz de crear una vista es el propio Revit
    en este mismo hilo, asi que la carrera no existe -- pero si Revit
    rechaza por una razon que no previmos, `_write_failed` lo traduce.
    """
    if name is None:
        return None

    cleaned = name.strip()
    if not cleaned:
        raise WriteRejected(
            "The name cannot be empty. Revit accepts an empty name without "
            "warning and leaves the view unnamed, so the board does not allow "
            "it.",
            "name_empty",
        )

    for view in reader.iter_schedules(doc):
        if ser.safe_name(view) == cleaned:
            raise WriteRejected(
                "A schedule named '{0}' already exists. Revit requires the "
                "name to be unique.".format(cleaned),
                "name_taken",
            )
    return cleaned


def _find_valid_category(category_id):
    """El ElementId vivo de la categoria pedida, buscado entre las validas.

    Mismo criterio que el resto del modulo: los ids se BUSCAN entre los
    que devuelve la API, no se construyen con `ElementId(int)`, que
    compite con las sobrecargas de enum en la resolucion de IronPython.
    Aca ademas evita un problema propio: los ids de categoria son
    NEGATIVOS (Walls es -2000011), o sea exactamente los valores que
    tambien son `BuiltInCategory` validos.

    De paso valida: si el id no esta en `GetValidCategoriesForSchedule()`
    no hay nada que crear. `IsValidCategoryForSchedule` se consulta
    igual, que es la guarda que documenta la API, aunque llegado aca ya
    deberia ser redundante.
    """
    try:
        candidates = list(DB.ViewSchedule.GetValidCategoriesForSchedule())
    except Exception:
        candidates = []

    for candidate in candidates:
        if ser.element_id_value(candidate) != int(category_id):
            continue
        if not DB.ViewSchedule.IsValidCategoryForSchedule(candidate):
            break
        return candidate

    raise WriteRejected(
        "That category does not support schedules in this document.",
        "category_not_valid",
    )


def _find_area_scheme(doc, area_scheme_id):
    """El ElementId vivo del AreaScheme pedido. Buscado, no construido."""
    try:
        collector = DB.FilteredElementCollector(doc).OfClass(DB.AreaScheme)
    except Exception:
        collector = []

    for scheme in collector:
        if ser.element_id_value(scheme.Id) == int(area_scheme_id):
            return scheme.Id

    raise WriteRejected(
        "That area scheme does not exist in this document.",
        "area_scheme_not_found",
    )


def create_schedule(doc, category_id, name=None, area_scheme_id=None):
    """Crea un schedule nuevo para una categoria.  [Fase 7]

    Nace VACIO: `CreateSchedule` no agrega ni una columna (verificado).
    Las columnas se agregan despues con el panel de la Fase 5, que es
    donde el usuario ve el % de llenado de cada campo antes de elegirlo
    -- justo el punto del producto.

    `name=None` deja el nombre que pone Revit ("Wall Schedule"), que ya
    viene libre de colisiones. Se crea y se renombra en UNA transaccion
    (regla 3): un solo Ctrl+Z deshace el schedule entero, no deja una
    vista con el nombre a medio poner.

    AREAS ES EL CASO ESPECIAL, y el unico: de las 187 categorias
    validas, 186 se crean con dos argumentos y Areas exige un tercero,
    el AreaScheme. Sin el, Revit lanza "The Areas category was specified
    but an area scheme ID was not provided". Lo encontro Eduardo
    intentando crear un schedule de Areas en la primera prueba de la
    Fase 7, cuando el codigo solo llamaba la version de 2 argumentos.
    """
    cleaned = _validate_name(doc, name)
    category_element_id = _find_valid_category(category_id)

    scheme_element_id = None
    if reader.needs_area_scheme(category_id):
        if area_scheme_id is None:
            raise WriteRejected(
                "An area schedule needs an area scheme: Revit has no "
                "default one.",
                "area_scheme_required",
            )
        scheme_element_id = _find_area_scheme(doc, area_scheme_id)

    created = {}

    def apply_changes():
        if scheme_element_id is not None:
            # Con el scheme, Revit nombra la vista solo:
            # "Area Schedule (VENTAS)".
            view = DB.ViewSchedule.CreateSchedule(
                doc, category_element_id, scheme_element_id)
        else:
            view = DB.ViewSchedule.CreateSchedule(doc, category_element_id)
        if cleaned:
            view.Name = cleaned
        created["view"] = view

    _commit(doc, "ScheduleBoard: create schedule", apply_changes)
    return reader.schedule_summary(doc, created["view"])


def duplicate_schedule(doc, view, name=None):
    """Duplica un schedule con todas sus columnas, filtros y orden.  [Fase 7]

    A diferencia del lote de la Fase 6 -- que RECONSTRUYE filtros y orden
    campo por campo porque un `ScheduleFilter` referencia un
    `ScheduleFieldId` que solo existe dentro de su propia definicion --
    aca no hay nada que reconstruir: `Duplicate` es de Revit y copia la
    definicion completa. Verificado conservando las columnas del origen.

    `name=None` deja el nombre que pone Revit ("<origen> Copy 1").
    Duplicar y renombrar van en UNA transaccion, por lo mismo que crear.
    """
    cleaned = _validate_name(doc, name)

    option = DB.ViewDuplicateOption.Duplicate
    try:
        can_duplicate = bool(view.CanViewBeDuplicated(option))
    except Exception:
        can_duplicate = True
    if not can_duplicate:
        raise WriteRejected(
            "Revit does not allow duplicating this schedule.",
            "cannot_duplicate",
        )

    created = {}

    def apply_changes():
        new_id = view.Duplicate(option)
        new_view = doc.GetElement(new_id)
        if cleaned:
            new_view.Name = cleaned
        created["view"] = new_view

    _commit(doc, "ScheduleBoard: duplicate schedule", apply_changes)
    return reader.schedule_summary(doc, created["view"])


def delete_schedule(doc, view):
    """Borra un schedule.  [Fase 7]

    Lee nombre y presencia en laminas ANTES de borrar: despues del
    commit el `view` ya no referencia nada y el reporte saldria vacio.

    `doc.Delete` arrastra dependientes y devuelve todos los ids
    borrados: medido, 7 elementos para un schedule vacio y 16 para uno
    con columnas. Se reporta ese numero porque es la unica senal de que
    se fue mas de lo que se pidio.
    """
    schedule_id = ser.element_id_value(view.Id)
    name = ser.safe_name(view)
    on_sheets = reader.schedule_instance_count(view)

    _require_owned(doc, view)

    def apply_changes():
        return len(list(doc.Delete(view.Id)))

    deleted_count = _commit(doc, "ScheduleBoard: delete schedule", apply_changes)

    return {
        "schedule_id": schedule_id,
        "name": name,
        "on_sheets": on_sheets,
        "deleted_elements": deleted_count,
    }


# --- Fase 8: worksharing --------------------------------------------------

def checkout_elements(doc, element_ids):
    """Toma posesion de los elementos. UN solo checkout grande.  [Fase 8]

    EXCEPCION DELIBERADA A LA REGLA 2 de este modulo: es la unica
    funcion de `writer.py` que NO abre transaccion, porque Revit no
    permite pedir posesion dentro de una. Escribe igual -- registra la
    posesion en el archivo central -- y por eso vive aca y no en
    `worksharing.py`, que es de solo lectura.

    UN SOLO CHECKOUT GRANDE, nunca uno por elemento (6.6 del
    planteamiento, recomendacion de Autodesk). Medido: 178 ms para 3
    schedules y 216 ms para 1 elemento suelto, o sea que el costo es
    casi todo la ida y vuelta al central y partirlo en N llamadas se
    paga N veces.

    Devuelve, y las tres cosas importan:
        checked_out  lo que quedo en nuestro poder
        extra        lo que Revit ARRASTRO sin que se pidiera
        denied       lo que no se pudo tomar, casi siempre porque lo
                     tiene otro usuario

    DOS COSAS QUE `CheckoutElements` NO HACE, las dos medidas contra el
    modelo de referencia hecho central, y las dos costaron una version
    equivocada de esta funcion:

    1. NO LISTA LO QUE ARRASTRA. Pedir UN muro miembro del grupo
       `ED1-PISO TIPO` (271 miembros) dejo tomados los 271 miembros MAS
       el grupo -- 272 elementos -- y la llamada devolvio exactamente
       UNO. O sea que calcular el arrastre por diferencia contra lo
       pedido no ve nada. Por eso `extra` sale de `_dragged_by`, que
       mira los grupos de los elementos pedidos.

    2. NO SE PUEDE DEDUCIR LO NEGADO de lo que devuelve, porque con
       todo exitoso no hay forma de distinguir "devuelve lo pedido" de
       "devuelve lo logrado". Asi que `denied` se decide por el ESTADO
       despues del checkout, que la propia llamada acaba de refrescar:
       lo que no quedo `OwnedByCurrentUser` no se consiguio. Cuesta
       0,092 ms por elemento pedido.
    """
    # [Fase 17] Pedir posesion ESCRIBE en el central, asi que es una
    # escritura aunque no abra transaccion. Va antes del camino de costo
    # cero de abajo a proposito: en un modelo sin worksharing tampoco hay
    # nada que tomar, pero el "no" tiene que ser el mismo en los dos
    # casos.
    _require_writes()

    # Acepta enteros o ElementId indistintamente: las rutas reciben
    # enteros del JSON y `_ensure_owned` pasa `view.Id`. Convertir aca
    # evita que `api_routes` tenga que importar `DB` solo para esto.
    normalized = []
    for element_id in element_ids or []:
        if isinstance(element_id, int):
            normalized.append(ser.to_element_id(element_id))
        else:
            normalized.append(element_id)

    requested = []
    for element_id in normalized:
        try:
            requested.append(ser.element_id_value(element_id))
        except Exception:
            continue

    if not ws.is_workshared(doc):
        # Modelo sin archivo central: no hay posesion que pedir y todo
        # es editable. Camino de costo cero, que es el de todo el
        # desarrollo del proyecto hasta esta fase.
        return {
            "workshared": False,
            "requested": requested,
            "checked_out": requested,
            "extra": [],
            "denied": [],
        }

    # El arrastre se calcula ANTES: despues del checkout ya esta hecho y
    # no se puede distinguir de lo que se pidio.
    dragged = _dragged_by(doc, normalized, requested)

    # [28-09-2026] ANTES del checkout, porque la llamada lo refresca: despues
    # dice `CurrentWithCentral` aunque haya negado por estar desactualizado.
    updates_before = dict(
        (ser.element_id_value(element_id), ws.model_updates_of(doc, element_id))
        for element_id in normalized)

    ids = List[DB.ElementId]()
    for element_id in normalized:
        ids.Add(element_id)

    # El valor de retorno se ignora a proposito: ver el punto 2 del
    # docstring. Lo que manda es el estado despues.
    DB.WorksharingUtils.CheckoutElements(doc, ids)

    checked_out = []
    denied = []
    # [28-09-2026] Por que se nego cada uno. "Lo tiene otro" SOLO si Revit
    # lo dice (`OwnedByOtherUser`); si no, es que no lo presto, y casi
    # siempre porque el local esta desactualizado.
    denied_detail = {}
    for element_id in normalized:
        key = ser.element_id_value(element_id)
        status = ws.status_of(doc, element_id)
        if status == ws.OWNED_BY_CURRENT_USER:
            checked_out.append(key)
        else:
            denied.append(key)
            denied_detail[key] = {
                "owned_by_other": status == ws.OWNED_BY_OTHER_USER,
                "stale": updates_before.get(key) in ws.STALE_UPDATES,
            }

    # EL RASTRO DE LA POSESION, no el de la escritura.  [Fase 15]
    #
    # Va aca y no en la linea de la escritura porque esta es la UNICA
    # funcion por la que el board toma posesion -- mismo argumento de
    # embudo que `_FailureCatcher` y el rastro de la Fase 14 en
    # `_commit`. Ver `audit.checkout_line` para los tres casos que la
    # linea de la escritura no cubria.
    #
    # Se anota lo que quedo TOMADO, no lo pedido: lo negado no cuelga.
    audit.ownership_line(doc, "checkout", checked_out, dragged)

    return {
        "workshared": True,
        "requested": requested,
        "checked_out": sorted(checked_out),
        "extra": sorted(dragged),
        "denied": sorted(denied),
        "denied_detail": denied_detail,
    }


def relinquish(doc):
    """Devuelve la posesion de los elementos tomados.  [Fase 15]

    SEGUNDA EXCEPCION DELIBERADA A LA REGLA 2 de este modulo, con la
    misma forma que `checkout_elements`: escribe -- suelta la posesion
    en el archivo central -- pero NO abre transaccion, porque Revit lo
    prohibe expresamente (*"Operation is not permitted when there is any
    open sub-transaction, transaction, or transaction group"*). Por lo
    mismo NO pasa por `_commit`, asi que el rastro se escribe aca a mano:
    el gancho de la Fase 14 no la ve.

    NO EXISTE UN RELINQUISH POR ELEMENTO. Lo unico que hay es
    `RelinquishOwnership`, que es de DOCUMENTO ENTERO -- verificado en
    `RevitAPI.xml` de la instalacion, no hay sobrecarga con una lista.
    Asi que "devolver lo que el board tomo" no es expresable; lo que si
    lo es, y es lo que se hace, es `RelinquishOptions(False)` con solo
    `CheckedOutElements = True`: elementos y nada mas, sin tocar la
    posesion de WORKSETS, que el board nunca pide y el usuario si puede
    estar sosteniendo a proposito.

    NO PUEDE DESTRUIR TRABAJO SIN SINCRONIZAR, y esto cambia la razon
    por la que el boton es manual. Textual de la API: *"Only unmodified
    elements already in central will be relinquished. Newly added and
    modified elements cannot be relinquished until they have been
    synchronized with central."* O sea que lo modificado se rechaza solo
    y queda intacto. El boton sigue siendo manual igual, pero por el
    motivo honesto: ademas concede las solicitudes de posesion
    pendientes de OTROS usuarios, es una operacion real contra el
    central, y puede fallar por red. Eso lo decide el usuario, no el
    arranque del board.

    Devuelve cuantos elementos volvieron de verdad -- de
    `RelinquishedItems`, no de lo que se pidio.
    """
    if not ws.is_workshared(doc):
        # Sin archivo central no hay posesion que devolver. Camino de
        # costo cero, igual que en `checkout_elements`.
        return {"workshared": False, "relinquished": 0, "element_ids": []}

    options = DB.RelinquishOptions(False)
    options.CheckedOutElements = True

    # `None` en las opciones de central significa "sin personalizar", que
    # es lo que se quiere: el board no cambia como se accede al central.
    items = DB.WorksharingUtils.RelinquishOwnership(doc, options, None)

    # `GetRelinquishedElements()`, no `GetElementIds()`: verificado en
    # `RevitAPI.xml`. `RelinquishedItems` tiene ademas
    # `GetRelinquishedWorksets()`, que aca siempre viene vacio porque las
    # opciones no piden worksets.
    element_ids = []
    try:
        for element_id in items.GetRelinquishedElements():
            element_ids.append(ser.element_id_value(element_id))
    except Exception:
        # La cuenta importa mas que la lista; si la enumeracion falla,
        # el resto de la respuesta sigue siendo cierto.
        pass

    audit.ownership_line(doc, "relinquish", element_ids)
    return {
        "workshared": True,
        "relinquished": len(element_ids),
        "element_ids": sorted(element_ids),
    }


def _dragged_by(doc, normalized_ids, requested):
    """Lo que el checkout se va a llevar ADEMAS de lo pedido.

    Solo cubre el caso de los GRUPOS, que es el que documenta 6.6 y el
    unico medido: pedir un miembro se lleva el grupo entero. Revit puede
    arrastrar por otras dependencias que esto no ve, asi que el numero
    es un MINIMO, no un total -- el texto de la UI tiene que decir "al
    menos", no "exactamente".

    Barato: una lectura de `GroupId` por elemento pedido, y los miembros
    solo de los grupos involucrados. Para vistas (`ViewSchedule`)
    `GroupId` es invalido, asi que no cuesta nada y no hace falta
    preguntar antes si lo que se pide es una vista.
    """
    asked = set(requested)
    dragged = set()
    seen_groups = set()

    for element_id in normalized_ids:
        try:
            element = doc.GetElement(element_id)
        except Exception:
            continue
        if element is None:
            continue
        try:
            group_id = element.GroupId
        except Exception:
            continue
        if not ser.is_valid_id(group_id):
            continue

        group_key = ser.element_id_value(group_id)
        if group_key in seen_groups:
            continue
        seen_groups.add(group_key)

        if group_key not in asked:
            dragged.add(group_key)
        try:
            group = doc.GetElement(group_id)
            for member_id in group.GetMemberIds():
                member_key = ser.element_id_value(member_id)
                if member_key not in asked:
                    dragged.add(member_key)
        except Exception:
            continue

    return dragged


def _ensure_owned(doc, views):
    """Pide posesion de las vistas antes de escribirlas.

    Devuelve `{id: {"name": ..., "owner": ...}}` con las NEGADAS, para
    que el lote pueda reportarlas schedule por schedule en vez de
    abortar: en modelo compartido el lote parcialmente exitoso es el
    caso normal (6.6), no la excepcion.
    """
    views = list(views or [])
    if not views or not ws.is_workshared(doc):
        return {}, None

    report = checkout_elements(doc, [v.Id for v in views])
    denied = set(report["denied"])

    result = {}
    for view in views:
        key = ser.element_id_value(view.Id)
        if key in denied:
            detail = report.get("denied_detail", {}).get(key, {})
            result[key] = {
                "name": ser.safe_name(view),
                "owner": ws.owner_of(doc, view.Id),
                "owned_by_other": detail.get("owned_by_other", True),
                "stale": detail.get("stale", False),
            }
    return result, report


def _denied_reason(info, name=None):
    """El mensaje y el `code` de un checkout negado.  [28-09-2026]

    El UNICO lugar que decide que se dice: lo usan la celda, las propiedades
    del schedule y el lote. "Lo tiene otro" solo si Revit lo dijo.
    """
    label = info.get("name") or name
    if info.get("owned_by_other", True):
        return ws.cannot_edit_message(label, info.get("owner")), "owned_by_other_user"
    return ws.not_granted_message(label, info.get("stale")), "checkout_not_granted"


def _require_owned(doc, view):
    """Igual que `_ensure_owned` pero para un solo schedule: rechaza.

    Se llama ANTES de abrir la transaccion, porque el checkout no puede
    ir dentro de una y porque asi el rechazo no deja una transaccion
    abierta a medias.

    NO sustituye al manejo del fallo de la escritura: entre este
    checkout y el commit otro usuario puede tomar el elemento. Esto
    convierte el caso FRECUENTE en un mensaje bueno; el raro sigue
    cayendo en `_write_failed`.
    """
    denied, _report = _ensure_owned(doc, [view])
    if not denied:
        return
    info = list(denied.values())[0]
    message, code = _denied_reason(info)
    raise WriteRejected(message, code)


def _preview_denied(doc, owners):
    """Los duenos que el checkout NEGARIA, sin pedir posesion.  [Fase 16]

    ES EL SUSTITUTO DE `_ensure_owned` EN UN DRY-RUN, y devuelve la misma
    forma -- `{id: {"name": ..., "owner": ...}}` -- justamente para que
    `_set_parameter` no tenga que ramificar mas alla de elegir cual llama.

    POR QUE SUSTITUIR Y NO SALTAR. El checkout es la unica de las cuatro
    guardas que ademas ESCRIBE (toma posesion en el central), asi que un
    preview no puede correrla. Pero saltarla a secas le sacaria al preview
    el motivo `owned_by_other_user`, que es uno de los cuatro que existe
    para mostrar. `ws.ownership()` da el mismo predicado sin viajar: su
    `editable` es `False` SOLO cuando lo tiene otro usuario, que es
    exactamente la condicion con la que `checkout_elements` decide
    `denied`.

    LA TRAMPA DEL WORKSET DE LA FASE 15 NO APLICA ACA, y conviene decir
    por que para que nadie "arregle" esto agregando `ws.owned_worksets()`:
    alli la pregunta era "que quedo prestado", y un `OwnedByCurrentUser`
    heredado del workset era un falso positivo. Aca la pregunta es la
    inversa -- "que me va a negar el checkout" -- y el predicado es
    `OwnedByOtherUser`. Si el workset es propio, lo de adentro lee
    `OwnedByCurrentUser` y el preview dice "se escribe", que es CIERTO:
    quien posee el workset puede editarlo. Y el caso espejo tambien:
    workset de otro -> todo adentro `OwnedByOtherUser` -> el preview dice
    denegado, y tambien es cierto. Predice bien en las dos direcciones.

    LA DIFERENCIA REAL, y es la unica: `GetCheckoutStatus` devuelve un
    valor CACHEADO, y `CheckoutElements` lo REFRESCA -- por eso
    `checkout_elements` decide lo negado por el estado POSTERIOR a su
    propia llamada. O sea que el preview mira una foto mas vieja que la
    escritura. No tiene arreglo sin ir al central, que es justo el costo
    que esta fase evita, y encaja con lo que ya dice el docstring de
    `ws.status_of`: se usa para INFORMAR, no para autorizar. Un preview
    informa.
    """
    owners = list(owners or [])
    if not owners or not ws.is_workshared(doc):
        # Mismo camino de costo cero que `_ensure_owned`: sin archivo
        # central no hay posesion que negar.
        return {}

    status = ws.ownership(doc, [owner.Id for owner in owners])

    result = {}
    for owner in owners:
        key = ser.element_id_value(owner.Id)
        info = status.get(key)
        if info is not None and not info["editable"]:
            result[key] = {
                "name": ser.safe_name(owner),
                "owner": info["owner"],
                "owned_by_other": True,
            }
    return result


# --- Fases 9 y 10: escritura de datos -------------------------------------

def _has_units(param):
    """`True` si el parametro tiene unidades (largo, area, angulo...).

    `IsMeasurableSpec` distingue `length`/`area` de `number`. Si no se
    puede preguntar, se asume que SI: lo seguro es rechazar un valor, no
    escribirlo en pies.
    """
    try:
        return bool(DB.UnitUtils.IsMeasurableSpec(param.Definition.GetDataType()))
    except Exception:
        return True


def _coerce_and_set(param, storage, value):
    """Escribe `value` con el `Set()` que corresponde al StorageType.

    Devuelve (ok, como), donde `como` dice por que via se escribio para
    que el reporte sea explicable.

    UNIDADES: para un `Double` se intenta PRIMERO `SetValueString`, que
    interpreta el texto en las unidades del proyecto. Es la unica via
    correcta desde una UI: el usuario ve "220.0" (centimetros) y escribe
    "95", mientras que `Set()` espera PIES internos (medido: 220 cm son
    7,21784776903 pies). Con `Set(float)` el numero entrado se tomaria
    como pies y el elemento quedaria treinta veces mas grande, en
    silencio y sin error.

    NUNCA `UnitType.UT_*` ni `DisplayUnitType`: esa API murio en Revit
    2021 y el codigo de ejemplo que circula por la web todavia la usa.
    """
    if storage == "String":
        return bool(param.Set(value if value is not None else "")), "Set(String)"

    if storage == "ElementId":
        return bool(param.Set(ser.to_element_id(value))), "Set(ElementId)"

    if storage == "Double":
        try:
            if bool(param.SetValueString(str(value))):
                return True, "SetValueString"
        except Exception:
            pass
        # Un Double sin unidades (una relacion, un factor) no admite
        # SetValueString y hay que escribirlo crudo.
        # [Fase 22, bug B3] PERO SOLO ESE. Si el parametro TIENE unidades y
        # `SetValueString` no acepto el texto, caer a `Set(float)` escribia
        # el numero como pies internos: el error x30 silencioso de arriba,
        # justo el que esta funcion existe para evitar. Ahi se rechaza.
        if _has_units(param):
            return False, "SetValueString"
        return bool(param.Set(float(value))), "Set(Double)"

    if storage == "Integer":
        try:
            return bool(param.Set(int(value))), "Set(Integer)"
        except (TypeError, ValueError):
            # Un entero con unidades, o un Yes/No entrado como texto.
            return bool(param.SetValueString(str(value))), "SetValueString"

    raise WriteRejected(
        "This cell has storage type '{0}', which the board cannot "
        "write.".format(storage),
        "storage_not_writable",
    )


def _grouped_elements(doc, element_ids):
    """Los que estan DENTRO de un grupo, que no admiten escritura.

    Revit no deja cambiar un parametro de INSTANCIA de un elemento
    agrupado fuera del modo *Edit Group*, y si se intenta levanta un
    error "cannot be ignored" cuyo unico camino hacia adelante es
    *Ungroup* -- destructivo. `_FailureCatcher` ya impide que eso cuelgue
    la API, pero saberlo ANTES de escribir da un mensaje propio y no
    gasta una transaccion.

    Solo aplica a columnas de INSTANCIA: el parametro de una columna de
    tipo vive en el tipo, que no esta dentro de ningun grupo.

    No es un caso borde: el modelo de referencia tiene 1.062 grupos de
    modelo, y es el mismo `GroupId` con el que la Fase 8 detecta el
    arrastre del checkout.
    """
    grouped = []
    for value_id in element_ids:
        element = doc.GetElement(ser.to_element_id(value_id))
        if element is None:
            continue
        try:
            if ser.is_valid_id(element.GroupId):
                grouped.append(int(value_id))
        except Exception:
            continue
    return grouped


def _group_message(field_name, blocked, total, rows=1):
    """El texto del rechazo por grupo.

    Tres formas y no un ternario dentro del format: un mensaje que dice
    "1 ... are" se lee como un bug, y uno que habla de "this row" en un
    lote de nueve filas, tambien.

    Las dos primeras son textuales de la Fase 9, donde se verificaron
    por HTTP; la tercera es la que agrega el lote.
    """
    if rows > 1:
        return (
            "{0} of the selected elements are inside a group. Revit only "
            "allows changing them with the Edit Group command, so the board "
            "cannot write '{1}' to them.".format(blocked, field_name)
        )
    if blocked == 1 and total == 1:
        quien = "This element is"
    else:
        quien = "{0} of the elements behind this row are".format(blocked)
    return (
        "{0} inside a group. Revit only allows changing them with the "
        "Edit Group command, so the board cannot write '{1}' "
        "here.".format(quien, field_name)
    )


def _transaction_name(rows):
    """El nombre que va a ver el usuario en el menu *Undo* de Revit.

    Es texto de la aplicacion, no una etiqueta interna: sale en la UI
    nativa. Un lote de 9 filas rotulado "edit cell" seria mentira.
    """
    if rows and rows > 1:
        return "ScheduleBoard: edit {0} rows".format(rows)
    return "ScheduleBoard: edit cell"


def set_parameter(doc, view, field_id, element_ids, value, rows=1,
                  dry_run=False):
    """Escribe un valor sobre N elementos en UNA transaccion.  [Fases 9 y 10]

    Envoltorio de progreso [Fase 11]. El cuerpo esta en `_set_parameter`
    y no se toco: rechaza por seis caminos distintos ANTES del commit
    -- campo computado, campo inexistente, todo agrupado, read-only,
    dueno ajeno, parametro ausente -- y cada uno tiene que dejar el
    progreso en un estado terminal. Envolver es la unica forma de
    cubrirlos todos sin reindentar 240 lineas de la funcion mas delicada
    del proyecto.

    `dry_run` [Fase 16]: calcula el alcance y devuelve la MISMA forma,
    sin tomar posesion y sin abrir transaccion. Ver `_set_parameter`.
    """
    if dry_run:
        # BIFURCACION C DEL PREVIEW: ni progreso ni rastro. Las dos
        # omisiones son deliberadas y ninguna es cosmetica.
        #
        # EL RASTRO: un preview que dejara lineas en el JSONL envenenaria
        # el cruce de `pending_checkouts` de la Fase 15 -- que lee del
        # rastro QUE se toco para sondear si sigue tomado -- y llenaria el
        # panel `History` de escrituras que nunca ocurrieron. Alcanza con
        # NO llamar `audit.begin()`: los tres `audit.note()` de adentro
        # retornan temprano con `PENDING` vacio, asi que el cuerpo no
        # necesita ni una rama mas por esto. Y `PENDING` esta vacio fuera
        # de una escritura en vuelo porque `record()` siempre lo limpia
        # -- en el commit, en el rollback y en el 409 del envoltorio.
        #
        # EL PROGRESO: sin checkout y sin `Commit()` no queda nada que
        # reportar; el bucle de `Set()` ni se corre. Tocar `PROGRESS`
        # aca solo serviria para que un sondeo confundiera un preview con
        # la escritura que viene despues.
        return _set_parameter(doc, view, field_id, element_ids, value, rows,
                              dry_run=True)

    progress.begin("cell")
    # El detalle de dominio que `_commit` no puede adivinar. El valor
    # VIEJO todavia no se conoce aca: se agrega con `audit.note()` en
    # `_set_parameter`, justo antes de escribirlo.  [Fase 14]
    audit.begin(
        "cell",
        schedule_id=ser.element_id_value(view.Id),
        schedule=ser.safe_name(view),
        field_id=field_id,
        new_value=value,
        rows=rows,
        requested=len(element_ids or []),
    )
    try:
        result = _set_parameter(doc, view, field_id, element_ids, value, rows)
    except Exception as ex:
        progress.end("failed")
        # Un rechazo NUESTRO (el 409 de una guarda) nunca llega a
        # `_commit`, asi que la linea se escribe aca. Si en cambio el
        # rechazo vino DESPUES del commit -- un `RolledBack` -- `_commit`
        # ya la escribio y vacio el contexto, y `pending()` evita la
        # linea duplicada.
        if audit.pending():
            audit.record(doc, _transaction_name(rows), "rejected", error=ex,
                         code=getattr(ex, "code", None))
        raise
    progress.end("done")
    return result


def _set_parameter(doc, view, field_id, element_ids, value, rows=1,
                   dry_run=False):
    """El cuerpo de `set_parameter`. Ver alli.

    [Fase 22, bug B1] EMPIEZA VACIANDO LOS CACHES DE PARAMETROS Y DE TIPO.
    Viven a nivel de modulo y solo los vaciaban las LECTURAS, asi que una
    escritura podia usar el tipo resuelto por una peticion anterior:
    cambiar el `Type` de una fila y despues escribir un type parameter en
    esa misma fila escribia sobre el tipo VIEJO -- todas sus instancias --
    y el preview decia lo mismo.

    EL PREVIEW DE LA FASE 16 ENTRA POR ACA MISMO, con `dry_run=True`, y
    esa es toda la fase: no hay una segunda funcion que resuelva la
    columna, aplique las guardas y calcule el alcance. Dos caminos se
    desincronizan, y el que se desincronizaria es el que le PROMETE algo
    al usuario.

    Son DOS bifurcaciones y no una, porque este camino tiene dos efectos
    de escritura, no uno -- el plan hablaba de "lo mas cerca posible del
    `Commit()`" y eso solo cubre el segundo:

      A. el CHECKOUT (mas abajo, donde se resuelve la posesion), que
         escribe en el archivo central. Se SUSTITUYE por `_preview_denied`,
         que da el mismo predicado leyendo;
      B. el `_commit`, que se SALTA entero, y con el la clausura
         `apply_changes`: el valor viejo, el bucle de `Set()` y el
         recuento de lo escrito.

    Todo lo demas -- resolver la columna contra la definicion real, las
    cuatro guardas de 6.1, el dedup por dueno, los 409 cuando no queda
    nada escribible y el armado de `results` con los motivos agrupados
    por `code` -- es LITERALMENTE el mismo codigo. Los 409 tambien: la
    escritura devuelve 409 ahi, asi que el preview tiene que devolver el
    mismo, con el mismo `code`. Predecir el rechazo es la mitad de para
    que existe.

    LO QUE EL PREVIEW NO PUEDE PROMETER, y hay que decirlo donde se lee:
    si Revit va a aceptar el VALOR. Eso es el retorno de
    `_coerce_and_set`, que vive dentro de `apply_changes`, o sea del lado
    que no se corre. El preview es exacto sobre el ALCANCE -- que
    elementos, cuales se saltan y por que -- y mudo sobre `value_refused`.

    N Y NO 1 A PROPOSITO desde la Fase 9: una fila no itemizada
    representa varios elementos -- son el 42% de los schedules del
    proyecto real, y `TC-Puertas` tiene filas de 475 -- asi que "editar
    una celda" YA era una escritura multiple.

    LA FASE 10 NO AGREGA UNA FUNCION NUEVA, y esa es la conclusion de la
    fase: editar una fila colapsada y aplicar un valor a 9 filas
    seleccionadas son el MISMO problema -- un conjunto de elementos, un
    campo, un valor. El cliente manda la union de los ids de las filas
    seleccionadas y todo lo demas ya estaba: una transaccion (regla 3),
    dedup por dueno, y UN SOLO CHECKOUT GRANDE via `_ensure_owned`, que
    es lo que 6.6 recomienda. Medido en la Fase 9: la MISMA escritura de
    10 elementos costo 2.710 ms la primera vez y 724 ms la segunda, y la
    diferencia es la ida y vuelta al central del checkout. En un lote de
    N filas ese viaje se paga UNA vez.

    `rows` solo sirve para el nombre de la transaccion, o sea para el
    texto que el usuario ve en el menu *Undo*.

    LA COLUMNA NO SE TOMA DE LO QUE DIGA EL CLIENTE: se resuelve contra
    la definicion real con `reader.find_visible_field`. De ahi sale
    `is_type_field`, que decide si se escribe en la instancia o en el
    TIPO, y 6.1 llama a eso el riesgo de producto mas serio de la
    herramienta. No puede depender de un campo del body.

    UN LOTE PARCIALMENTE EXITOSO ES LO NORMAL, no la excepcion (6.6).
    Lo bloqueado -- elemento en grupo, parametro read-only, dueno ajeno
    -- se SALTA y se reporta en `results`, en vez de abortar lo demas:
    con 1.062 grupos en el modelo, que una fila agrupada tumbe las otras
    ocho seria inservible. Pero si NO queda nada escribible, se lanza el
    `WriteRejected` de ese motivo, con su `code` -- asi el rechazo de una
    celda sola sigue siendo el 409 exacto que verifico la Fase 9.
    """
    ser.clear_param_cache()
    payload, field = reader.find_visible_field(view.Definition, field_id)
    if payload is None:
        raise WriteRejected(
            "That field is not in this schedule.", "field_not_found")

    prefer_type = payload["is_type_field"]

    # El detalle de la COLUMNA se anota apenas se resuelve, ANTES de la
    # primera guarda que puede rechazar: los rechazos por campo computado,
    # por posesion o por grupo ocurren todos mas abajo, y un registro que
    # no dice sobre que campo se intentaba escribir sirve la mitad.
    # [Fase 14]
    #
    # `heading` es la palabra que el usuario ve en Revit -- se usa
    # esa -- y `parameter` el nombre interno, para
    # poder rastrearlo despues leyendo el archivo.
    audit.note(
        field=payload["heading"] or payload["name"],
        parameter=payload["name"],
        is_type_field=prefer_type,
    )

    if payload["revit_computed"]:
        raise WriteRejected(
            "'{0}' is computed by Revit, so there is no parameter to write "
            "to. Formulas, percentages, combined fields and Count are "
            "read-only in the board.".format(payload["name"]),
            "field_computed",
        )

    requested = []
    for value_id in element_ids or []:
        try:
            requested.append(int(value_id))
        except Exception:
            continue

    grouped = [] if prefer_type else _grouped_elements(doc, requested)
    grouped_set = set(grouped)

    # Resolver elemento -> parametro. En una columna de tipo el
    # parametro vive en el TIPO, asi que varios elementos comparten uno
    # solo: se deduplica por el elemento DUENO, que ademas es el que hay
    # que pedir en el checkout.
    targets = {}
    order = []
    missing = []
    for value_id in requested:
        if value_id in grouped_set:
            continue
        element = doc.GetElement(ser.to_element_id(value_id))
        if element is None:
            missing.append(value_id)
            continue
        param = ser.find_parameter(element, field.ParameterId,
                                   prefer_type=prefer_type)
        if param is None:
            missing.append(value_id)
            continue
        owner_key = ser.element_id_value(param.Element.Id)
        if owner_key not in targets:
            targets[owner_key] = {"param": param, "owner": param.Element,
                                  "elements": []}
            order.append(owner_key)
        targets[owner_key]["elements"].append(value_id)

    # Motivos por los que un dueno queda fuera. Se acumulan en vez de
    # lanzar, y solo se lanza si al final no queda nada que escribir.
    blocked = {}

    # Solo lectura: se comprueba ANTES de escribir, porque
    # `Parameter.Set()` LANZA EXCEPCION en vez de devolver False cuando
    # el parametro no admite escritura.
    for key in order:
        if bool(targets[key]["param"].IsReadOnly):
            blocked[key] = (
                "'{0}' is read-only in Revit, so the board cannot change "
                "it.".format(payload["name"]),
                "parameter_read_only",
            )

    # Posesion, en UN SOLO checkout grande (Fase 8), sobre los DUENOS --
    # que en una columna de tipo son los tipos, no las instancias. Se
    # piden juntos justamente para pagar un solo viaje al central.
    pending = [k for k in order if k not in blocked]
    if pending:
        owners = [targets[k]["owner"] for k in pending]
        if dry_run:
            # BIFURCACION A DEL PREVIEW. Es la unica guarda que ademas
            # ESCRIBE, asi que un dry-run no puede correrla -- pero
            # tampoco puede saltarla sin perder el motivo
            # `owned_by_other_user`. Ver `_preview_denied`, que devuelve
            # la misma forma que `_ensure_owned` leyendo el estado.
            denied = _preview_denied(doc, owners)
        else:
            # Un solo viaje al central para todo el lote. Medido: 290-340 ms
            # tanto para 40 elementos como para 369, o sea que el costo es
            # del viaje y no de la cantidad -- que es la razon de ser de la
            # Fase 10. Y ES JUSTO LO QUE EL PREVIEW NO PAGA.
            progress.phase("checkout", 0, len(pending))
            denied = _ensure_owned(doc, owners)[0]
        for key in pending:
            if key in denied:
                blocked[key] = _denied_reason(denied[key], payload["name"])

    writable = [k for k in order if k not in blocked]

    if not writable:
        # Nada que escribir: el lote entero tiene una sola causa y se
        # dice como en la Fase 9, con un 409 y su `code`. El grupo va
        # primero porque es el que se comprueba antes.
        if grouped:
            raise WriteRejected(
                _group_message(payload["name"], len(grouped), len(requested),
                               rows),
                "element_in_group",
            )
        for key in order:
            if key in blocked:
                raise WriteRejected(blocked[key][0], blocked[key][1])
        raise WriteRejected(
            "None of the elements behind this row has the parameter "
            "'{0}'.".format(payload["name"]),
            "parameter_not_found",
        )

    results = []

    def apply_changes():
        # OJO: este bucle es el 2-6% del reloj (ver PROGRESS). Se reporta
        # porque es lo unico divisible que hay, no porque sea la espera:
        # la espera es el `Commit()` que viene despues, y esa fase la
        # marca `_commit`.
        progress.phase("writing", 0, len(writable))

        # EL VALOR VIEJO SE LEE ACA, DENTRO DE LA TRANSACCION Y ANTES DEL
        # PRIMER `Set()`. Despues no existe en ningun lado: la API de
        # Revit no ofrece un undo-log que se pueda consultar despues del
        # hecho, asi que un rastro que no lo capture aca no lo captura
        # nunca.  [Fase 14]
        audit.note(old_values=audit.old_values(
            [targets[key]["param"] for key in writable]))
        for index, key in enumerate(writable):
            progress.step(index)
            entry = targets[key]
            param = entry["param"]
            item = {
                "owner_id": key,
                "element_ids": entry["elements"],
                "written": False,
                "how": None,
                "error": None,
                "code": None,
            }
            try:
                # EL VALOR DE RETORNO IMPORTA: `Set()` puede devolver
                # False sin lanzar nada, y darlo por bueno seria
                # reportar un exito que no ocurrio.
                ok, how = _coerce_and_set(param, str(param.StorageType), value)
                item["written"] = ok
                item["how"] = how
                if not ok:
                    item["error"] = "Revit refused the value for '{0}'.".format(
                        payload["name"])
                    item["code"] = "value_refused"
            except WriteRejected:
                raise
            except Exception as ex:
                item["error"] = str(ex)
                item["code"] = "write_failed"
            results.append(item)
        progress.step(len(writable))

        # QUE elementos quedaron escritos, no solo cuantos. Se anota aca
        # -- todavia dentro de `apply_changes` -- porque `_commit` escribe
        # la linea del rastro apenas esto retorna.  [Fase 14]
        written_ids = [eid for item in results if item["written"]
                       for eid in item["element_ids"]]
        capped, total = audit.capped_ids(sorted(written_ids))
        audit.note(element_ids=capped, element_count=total)

    # El alcance REAL, no el pedido: `_commit` escribe la linea del
    # rastro y para entonces esto ya tiene que estar puesto. Los motivos
    # van agrupados por `code` -- los mismos que ve el cliente -- porque
    # una fila de `TC-Puertas` puede tener 475 elementos y el rastro no
    # puede pesar mas que la escritura.  [Fase 14]
    skipped_codes = sorted(set([blocked[key][1] for key in blocked]))
    if grouped:
        skipped_codes.append("element_in_group")
    skipped_ids = list(grouped)
    for key in blocked:
        skipped_ids.extend(targets[key]["elements"])
    skipped_capped, skipped_total = audit.capped_ids(sorted(skipped_ids))
    audit.note(
        owners_writable=len(writable),
        owners_blocked=len(blocked),
        elements_in_group=len(grouped),
        skipped_codes=skipped_codes,
        skipped_element_ids=skipped_capped,
        skipped_count=skipped_total,
    )

    # BIFURCACION B DEL PREVIEW, y es la que el plan pedia "lo mas cerca
    # posible del `Commit()`": literalmente la linea de al lado. Todo lo
    # de arriba se corrio igual, y todo lo de abajo se corre igual.
    #
    # Con esto se salta la clausura `apply_changes` entera: la lectura
    # del valor viejo, el bucle de `_coerce_and_set` y el recuento de lo
    # escrito. `results` queda vacio, que es exactamente lo que hace que
    # el bloque de abajo lo llene SOLO con lo saltado -- y eso es la
    # mitad del entregable de la Fase 16, sin una linea nueva.
    #
    # El `audit.note()` de arriba ya fue inerte (`PENDING` vacio, ver el
    # envoltorio), asi que no hace falta ramificarlo.
    if not dry_run:
        _commit(doc, _transaction_name(rows), apply_changes)

    # Lo saltado viaja en `results` igual que lo escrito, para que el
    # cliente pueda decir "applied to 7 of 9 rows" y NO repintar las
    # filas que no cambiaron.
    for key in order:
        if key in blocked:
            results.append({
                "owner_id": key,
                "element_ids": targets[key]["elements"],
                "written": False,
                "how": None,
                "error": blocked[key][0],
                "code": blocked[key][1],
            })
    if grouped:
        # Un item para TODOS los agrupados y no uno por elemento: una
        # fila colapsada de `TC-Puertas` puede tener 475, y el reporte
        # no puede pesar mas que la escritura.
        results.append({
            "owner_id": None,
            "element_ids": grouped,
            "written": False,
            "how": None,
            "error": _group_message(payload["name"], len(grouped),
                                    len(requested), rows),
            "code": "element_in_group",
        })

    # A QUE OTRAS FILAS les cambio el valor sin que el usuario las tocara.
    #
    # Solo en una columna de TIPO: el parametro vive en el tipo, asi que
    # escribirlo alcanza a TODAS sus instancias, no solo a las de las
    # filas editadas. El board las repinta con esta lista en vez de
    # adivinar cuales son -- compararlas por valor mostrado seria una
    # aproximacion, y ya hay bastante de eso en `_collapsed_rows`.
    #
    # Es el MISMO recorrido que `cell_options` usa para el aviso
    # "affects: N instances" (medido: 475 en 302 ms), y se paga solo
    # cuando de verdad se escribio un type parameter.
    written_keys = [item["owner_id"] for item in results if item["written"]]

    # EN UN PREVIEW no hay nada escrito todavia, asi que el alcance sale
    # de lo ESCRIBIBLE. Es lo que convierte el aviso "changes all 39
    # instances" de prediccion en hecho -- y es el UNICO lugar donde un
    # dry-run puede costar de verdad: `instances_of_type` se midio en
    # 302 ms para 475 instancias, y aca se paga una vez por TIPO
    # distinto, no una sola como en `GET /cell/`. Solo ocurre en una
    # columna de tipo; en una de instancia el bucle no se entra.
    reach_keys = writable if dry_run else written_keys

    affected = []
    if prefer_type:
        for key in reach_keys:
            try:
                affected.extend(reader.instances_of_type(
                    doc, targets[key]["owner"].Id, view.Definition.CategoryId))
            except Exception:
                continue

    # El valor QUE QUEDO, releido del parametro y no el tecleado: si
    # Revit redondeo o convirtio unidades, el board tiene que mostrar el
    # suyo. Sale de un dueno que se haya escrito de verdad -- tomar el
    # primero a secas mostraria el valor viejo cuando justo ese fallo.
    #
    # EN UN PREVIEW cae siempre a `writable[0]` y devuelve el valor
    # ACTUAL, sin ningun cambio: es justo lo que el cliente necesita para
    # decir "de X a Y" antes de escribir.
    sample_key = written_keys[0] if written_keys else writable[0]

    return {
        "schedule_id": ser.element_id_value(view.Id),
        "field_id": field_id,
        "name": payload["name"],
        "is_type_field": prefer_type,
        "rows": rows,
        # Que esto es un preview y NADA se escribio. Va aunque sea False:
        # un cliente que lo lea no tiene que saber si la ruta lo soporta.
        "dry_run": bool(dry_run),
        "affected_element_ids": affected,
        # En un preview, cuantos duenos SE ESCRIBIRIAN. Es el mismo
        # significado -- los que se llevan el valor -- solo que predicho,
        # y `dry_run` de arriba dice cual de los dos es. Ademas es EL
        # numero que le faltaba al cliente para estimar el tiempo de una
        # columna de tipo: son TIPOS distintos, no elementos, y esa
        # cuenta el cliente no la puede hacer solo (ver `estimate.ts`).
        "written_to": len(writable) if dry_run else len(written_keys),
        "failed": len([r for r in results if r["error"] is not None]),
        "skipped_element_ids": sorted(
            [eid for item in results if item["error"] is not None
             for eid in item["element_ids"]]),
        "missing_ids": missing,
        "results": results,
        # [Fase 19] Con el formato del campo, igual que la tabla: este es
        # el valor ACTUAL que el preview muestra en "de X a Y".
        "cell": ser.parameter_payload(targets[sample_key]["param"],
                                      reader._format_options(field)),
    }
