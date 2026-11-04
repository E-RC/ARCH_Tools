# -*- coding: UTF-8 -*-
"""Que cambio en el modelo desde la ultima vez que el board miro.  [Fase 23]

`Application.DocumentChanged` se dispara en cada commit, Undo y Redo de
cualquier transaccion -- las del usuario en Revit, las de otro add-in y
las del propio board -- y trae la operacion, los nombres de las
transacciones y los ids agregados, modificados y borrados. Este modulo lo
escucha y guarda un DIARIO acotado; `GET /changes/?since=N` se lo entrega
al cliente, que decide que tarjeta quedo vieja.

LA IDEA NO ES TIEMPO REAL. Releer solo, con la API serial y tablas de
110 s en frio, saturaria la cola y congelaria Revit; una tanda de Ctrl+Z
serian N relecturas. Lo que dana de verdad es actuar sobre datos viejos,
y eso se resuelve AVISANDO.

Medido en la etapa 0 (Revit 2025, local de prueba, en el motor de las
rutas):

- El handler cuesta 0,4-0,8 ms, con la categoria de lo agregado incluida;
  15 ms la primera vez (el JIT del motor). Con el TOPE -- 1.000 agregados
  y 1.000 modificados, medido sobre `_classify` en solo lectura --, 46-51
  ms en tibio y 896 ms la primera vez. Una transaccion de ese tamano ya
  cuesta segundos en Revit; recortar la muestra ahorraria poco y podria
  no ver una categoria, que es un falso negativo. Corre en el HILO PRINCIPAL de
  Revit (ManagedThreadId 1), dentro del `Commit()` -- por eso tiene que
  ser barato y no puede lanzar nunca.
- Un Ctrl+Z llega como `TransactionUndone` con el NOMBRE de la
  transaccion deshecha y los ids invertidos: lo que el Undo trae de vuelta
  figura como agregado. El Redo, como `TransactionRedone`.
- Un `RollBack` TAMBIEN dispara el evento (`TransactionRolledBack`, cero
  ids). No cambio nada, asi que se descarta.

SOLO LECTURA. Como `reader.py`: jamas abre una transaccion (regla 2).

POR QUE LA SUSCRIPCION VIVE EN EL APPDOMAIN
-------------------------------------------
Un evento de Revit guarda una referencia al delegado, y el delegado a la
funcion Python de UN motor. Tras un Reload de pyRevit (o un
`/dev/reload/`) ese motor queda muerto, pero su handler seguiria colgado
del evento y corriendo en cada transaccion. `subscribe()` deja el
delegado y la fuente en el `AppDomain`, que sobrevive a los motores, y
antes de suscribir el nuevo quita el anterior. El hook
`hooks/doc-changed.py` de pyRevit quedo descartado: recompila y ejecuta
un script en CADA transaccion (`EventHooks.cs:65-94`).

LOS CAMBIOS DE OTROS USUARIOS NO PASAN POR ACA
----------------------------------------------
`DocumentChanged` ve lo que pasa en ESTE proceso. Lo que otro modelador
sincroniza llega al hacer *Synchronize with Central* o *Reload Latest*, y
eso se anota como una entrada gruesa ("se sincronizo"), sin ids.
"""

import threading
import time

import System
from Autodesk.Revit import DB

from schedule_board import audit
from schedule_board import serializers as ser


# La clave del AppDomain donde queda la suscripcion viva. Una sola por
# proceso de Revit, la ponga el motor que la ponga.
APPDOMAIN_KEY = "ScheduleBoard.Changes.Subscription"

# Cuantas entradas guarda el diario. El cliente sondea cada 5 s, asi que
# para perder algo tendrian que pasar 200 transacciones en 5 s. Si pasa,
# el cliente lo detecta (`oldest_seq`) y marca todo como posiblemente
# viejo en vez de adivinar.
JOURNAL_SIZE = 200

# Tope de ids POR LISTA y por entrada. Una transaccion puede tocar miles
# de elementos (mover un nivel), y el diario no puede pesar mas que la
# tabla que protege. Pasado el tope la entrada dice `truncated` y el
# cliente la trata como "puede afectar a cualquier tarjeta".
ID_CAP = 1000

# Las escrituras propias se reconocen por el nombre de su transaccion:
# todas empiezan asi (`writer.py`, `selection.py`).
OWN_PREFIX = "ScheduleBoard: "

# Operacion de Revit -> la palabra que viaja al cliente. Lo que no esta
# aca (los rollbacks) no cambio nada y no se anota.
_OPERATIONS = {
    "TransactionCommitted": "committed",
    "TransactionUndone": "undone",
    "TransactionRedone": "redone",
}

# Eventos de "llego trabajo de otros". El segundo existe desde hace
# varias versiones, pero se suscribe solo si esta: si no, se sigue sin el.
_SYNC_EVENTS = (
    ("DocumentSynchronizedWithCentral",
     DB.Events.DocumentSynchronizedWithCentralEventArgs),
    ("DocumentReloadedLatest", None),
)

# El MISMO lock sobrevive a un `/dev/reload/`: `reload()` re-ejecuta este
# archivo, y un `threading.Lock()` nuevo dejaria a un `/changes/` en vuelo
# (hilo del servidor, sin GIL en IronPython) con el lock viejo mientras el
# diario se vacia. El diario si empieza de cero, con `EPOCH` nueva.
try:
    _LOCK  # noqa: F821 - existe si esto es una recarga
except NameError:
    _LOCK = threading.Lock()

# El estado del diario. `reload()` lo vuelve a cero, y `EPOCH` cambia con
# el: el cliente compara la epoca y, si cambio, sabe que el contador
# empezo de nuevo y que pudo perderse algo.
EPOCH = int(time.time() * 1000)
SEQ = [0]
JOURNAL = []
STATS = {"events": 0, "recorded": 0, "max_handler_ms": 0.0,
         "last_error": None}


def _document_of(doc):
    try:
        return {"title": doc.Title, "path": doc.PathName or None}
    except Exception:
        return {"title": None, "path": None}


def _ids(collection):
    """Hasta `ID_CAP` ids como enteros, y si hubo que cortar."""
    values = []
    truncated = False
    for element_id in collection:
        if len(values) >= ID_CAP:
            truncated = True
            break
        values.append(ser.element_id_value(element_id))
    return values, truncated


def _classify(doc, added, modified):
    """Categorias de lo agregado y de los TIPOS modificados, y schedules.

    Por que hace falta: una fila del board lleva los ids de sus
    INSTANCIAS. Un elemento nuevo no esta en ninguna fila todavia, y un
    tipo modificado tampoco (cambia lo que muestran sus instancias). Los
    dos se ubican por categoria. Y un schedule agregado cambia la LISTA,
    que ninguna tarjeta ve.
    """
    added_categories = set()
    type_categories = set()
    added_schedules = []
    modified_schedules = []
    for element_id in added:
        element = doc.GetElement(element_id)
        if element is None:
            continue
        if isinstance(element, DB.ViewSchedule):
            added_schedules.append(ser.element_id_value(element_id))
        category = element.Category
        if category is not None:
            added_categories.add(ser.element_id_value(category.Id))
    for element_id in modified:
        element = doc.GetElement(element_id)
        if element is None:
            continue
        if isinstance(element, DB.ViewSchedule):
            modified_schedules.append(ser.element_id_value(element_id))
        elif isinstance(element, DB.ElementType):
            category = element.Category
            if category is not None:
                type_categories.add(ser.element_id_value(category.Id))
    return {
        "added_categories": sorted(added_categories),
        "type_categories": sorted(type_categories),
        "added_schedules": added_schedules,
        "modified_schedules": modified_schedules,
    }


def _own_schedule_id():
    """El schedule que el board esta escribiendo AHORA, o `None`.

    El evento corre sincronico dentro del `Commit()` de `writer._commit`,
    y en ese momento `audit.PENDING` todavia tiene el `begin()` de la
    escritura en curso (`record()` lo limpia recien despues del commit).
    Asi el cliente sabe que tarjeta hizo la escritura -- esa ya se repinto
    sola -- sin que este modulo dependa de `writer`.
    """
    try:
        value = audit.PENDING.get("schedule_id")
        return int(value) if value is not None else None
    except Exception:
        return None


def _append(entry):
    with _LOCK:
        SEQ[0] += 1
        entry["seq"] = SEQ[0]
        JOURNAL.append(entry)
        if len(JOURNAL) > JOURNAL_SIZE:
            del JOURNAL[:len(JOURNAL) - JOURNAL_SIZE]
        STATS["recorded"] += 1


def on_document_changed(sender, args):
    """El handler de `DocumentChanged`. NUNCA lanza ni imprime.

    Corre en el hilo principal, dentro del `Commit()` de quien sea -- un
    error aca viajaria a la transaccion de otro add-in, y un `print`
    construiria la consola de pyRevit (ver `startup.silence_console`).
    """
    # `time.clock()` y no `time.time()` a proposito: en Windows es el reloj
    # de alta resolucion, y aca se miden fracciones de milisegundo.
    started = time.clock()
    try:
        STATS["events"] += 1
        operation = _OPERATIONS.get(str(args.Operation))
        if operation is None:
            return
        doc = args.GetDocument()
        if doc is None or doc.IsFamilyDocument:
            return
        names = [str(name) for name in args.GetTransactionNames()]
        added_ids = list(args.GetAddedElementIds())
        modified_ids = list(args.GetModifiedElementIds())
        added, cut_a = _ids(added_ids)
        modified, cut_m = _ids(modified_ids)
        deleted, cut_d = _ids(args.GetDeletedElementIds())
        own = (operation == "committed" and bool(names)
               and all(name.startswith(OWN_PREFIX) for name in names))
        entry = {
            "time": time.time(),
            "document": _document_of(doc),
            "op": operation,
            "names": names,
            "own": own,
            "own_schedule_id": _own_schedule_id() if own else None,
            "added": added,
            "modified": modified,
            "deleted": deleted,
            "truncated": cut_a or cut_m or cut_d,
        }
        entry.update(_classify(doc, added_ids[:ID_CAP],
                               modified_ids[:ID_CAP]))
        # Lo que costo ESTE evento, sin el lock ni el append. Queda en la
        # entrada porque el handler corre dentro del `Commit()` de otros, y
        # su costo es lo primero que hay que poder mirar si algo se pone
        # lento.
        entry["handler_ms"] = round((time.clock() - started) * 1000.0, 3)
        _append(entry)
    except Exception as ex:
        STATS["last_error"] = str(ex)
    finally:
        elapsed = (time.clock() - started) * 1000.0
        if elapsed > STATS["max_handler_ms"]:
            STATS["max_handler_ms"] = round(elapsed, 3)


def on_synchronized(sender, args):
    """Llego trabajo de otros usuarios: una entrada gruesa, sin ids."""
    try:
        doc = getattr(args, "Document", None)
        if doc is None or doc.IsFamilyDocument:
            return
        status = str(getattr(args, "Status", "Succeeded"))
        if status not in ("Succeeded", "None"):
            return
        _append({
            "time": time.time(),
            "document": _document_of(doc),
            "op": "synchronized",
            "names": [],
            "own": False,
            "own_schedule_id": None,
            "added": [], "modified": [], "deleted": [],
            "truncated": True,
            "added_categories": [], "type_categories": [],
            "added_schedules": [], "modified_schedules": [],
        })
    except Exception as ex:
        STATS["last_error"] = str(ex)


def _event_args_type(event_name, declared):
    """El tipo de argumentos de un evento, leido de la API si no se sabe."""
    if declared is not None:
        return declared
    return getattr(DB.Events, event_name + "EventArgs", None)


def unsubscribe():
    """Quita la suscripcion que haya en el AppDomain. Devuelve si habia."""
    domain = System.AppDomain.CurrentDomain
    stored = domain.GetData(APPDOMAIN_KEY)
    if stored is None:
        return False
    source = stored[0]
    for event_name, handler in stored[1]:
        try:
            event = getattr(source, event_name)
            event -= handler
        except Exception:
            pass
    domain.SetData(APPDOMAIN_KEY, None)
    return True


def subscribe(application):
    """Suscribe los handlers de ESTE motor, quitando antes los de otro.

    `application` es cualquier objeto que exponga los eventos de
    documento: el `ControlledApplication` en el arranque, el
    `Application` de un documento desde una ruta. Se guarda junto con los
    delegados porque el `-=` tiene que ir contra el mismo objeto.

    Devuelve los nombres de los eventos suscritos, para el log de arranque.
    """
    replaced = unsubscribe()
    handlers = []
    changed = System.EventHandler[DB.Events.DocumentChangedEventArgs](
        on_document_changed)
    application.DocumentChanged += changed
    handlers.append(("DocumentChanged", changed))

    for event_name, declared in _SYNC_EVENTS:
        args_type = _event_args_type(event_name, declared)
        if args_type is None or not hasattr(application, event_name):
            continue
        try:
            handler = System.EventHandler[args_type](on_synchronized)
            event = getattr(application, event_name)
            event += handler
            handlers.append((event_name, handler))
        except Exception:
            pass

    System.AppDomain.CurrentDomain.SetData(
        APPDOMAIN_KEY, [application, handlers])
    return {"events": [name for name, _ in handlers], "replaced": replaced}


def snapshot(since=0, document=None):
    """Las entradas con `seq > since`, y lo que el cliente necesita.

    Corre en un hilo del SERVIDOR (la ruta no declara `doc`), mientras el
    handler puede estar escribiendo desde el hilo principal: por eso el
    lock. `document` filtra por ruta o titulo, la misma clave con la que
    el cliente guarda el layout.
    """
    with _LOCK:
        entries = [dict(entry) for entry in JOURNAL if entry["seq"] > since]
        seq = SEQ[0]
        oldest = JOURNAL[0]["seq"] if JOURNAL else seq + 1
        stats = dict(STATS)
    if document:
        entries = [entry for entry in entries
                   if document in (entry["document"]["path"],
                                   entry["document"]["title"])]
    subscribed = System.AppDomain.CurrentDomain.GetData(APPDOMAIN_KEY)
    return {
        "epoch": EPOCH,
        "seq": seq,
        "oldest_seq": oldest,
        "entries": entries,
        "subscribed": subscribed is not None,
        "stats": stats,
    }
