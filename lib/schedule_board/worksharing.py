# -*- coding: UTF-8 -*-
"""Propiedad de elementos en modelo compartido.  [Fase 8]

REGLA 2 (ver `__init__.py`): este modulo LEE. Jamas abre una transaccion.
El checkout, que si escribe, vive en `writer.checkout_elements`.

POR QUE ESTA FASE VA ANTES DE LA EDICION DE CELDA
Hasta la Fase 7 el proyecto entero se probo contra un modelo sin
archivo central, asi que NINGUN fallo de propiedad se disparo nunca y
`api_routes._write_failed` FINGIA la capa: ante cualquier excepcion
inesperada culpaba al worksharing, apuntara o no al lugar correcto.
Este modulo lo reemplaza por deteccion real.

LO QUE SE MIDIO (modelo de referencia hecho central, 368 schedules):
    GetCheckoutStatus         0,092 ms por elemento  (34 ms los 368)
    GetWorksharingTooltipInfo 0,617 ms por elemento  (6,7x mas caro)

De ahi el diseno de `ownership()`: el estado se pide para TODOS porque
es barato, y el nombre del dueno SOLO para los que estan tomados, que
es donde el dato existe -- en los demas `Owner` vuelve cadena vacia y
la llamada se pagaria para nada.
"""

from Autodesk.Revit import DB

from schedule_board import audit
from schedule_board import serializers as ser


# Valores de DB.CheckoutStatus, verificados en ejecucion. Se pasan tal
# cual al cliente en vez de traducirlos a slugs propios: son las
# palabras de Revit.
NOT_OWNED = "NotOwned"
OWNED_BY_CURRENT_USER = "OwnedByCurrentUser"
OWNED_BY_OTHER_USER = "OwnedByOtherUser"


def is_workshared(doc):
    """`True` si el documento tiene archivo central.

    Guarda de entrada de todo este modulo. En un modelo sin worksharing
    no hay propiedad que consultar y el camino tiene que costar CERO,
    porque es el caso de la mayoria de los modelos de prueba y de todo
    el desarrollo del proyecto hasta la Fase 7.
    """
    if doc is None:
        return False
    try:
        return bool(doc.IsWorkshared)
    except Exception:
        return False


def _as_element_id(element_id):
    """Acepta un entero o un ElementId y devuelve siempre un ElementId.

    Las rutas reciben enteros del JSON y el resto del backend pasa
    `view.Id`. Normalizar aca evita que `api_routes` tenga que importar
    `DB` solo para convertir, y evita repetir la conversion en cada
    funcion.
    """
    if isinstance(element_id, int):
        return ser.to_element_id(element_id)
    return element_id


def status_of(doc, element_id):
    """El `CheckoutStatus` de un elemento, como texto. `None` si no aplica.

    OJO, y esta documentado en 6.6 del planteamiento: el valor viene
    CACHEADO y puede estar viejo -- otro usuario pudo tomar el elemento
    hace un segundo. No sirve como unica verificacion previa: hay que
    manejar igual el fallo de la escritura. Se usa para INFORMAR, no
    para autorizar.
    """
    if not is_workshared(doc):
        return None
    try:
        return str(DB.WorksharingUtils.GetCheckoutStatus(
            doc, _as_element_id(element_id)))
    except Exception:
        return None


# [28-09-2026] Lo que dice `GetModelUpdatesStatus` cuando el local tiene una
# version VIEJA del elemento: Revit no lo presta hasta un Reload Latest.
STALE_UPDATES = ("UpdatedInCentral", "DeletedInCentral")


def model_updates_of(doc, element_id):
    """El `ModelUpdatesStatus` de un elemento, como texto. `None` si no aplica.

    Es lo que explica un checkout negado SIN dueno: medido en Revit 2024 sobre
    un local recien creado, `TO-Bajo NTN` daba `DeletedInCentral` antes del
    checkout, `CheckoutElements` devolvia vacio y nadie la tenia.
    """
    if not is_workshared(doc):
        return None
    try:
        return str(DB.WorksharingUtils.GetModelUpdatesStatus(
            doc, _as_element_id(element_id)))
    except Exception:
        return None


def not_granted_message(name, stale):
    """Revit no presto el elemento y NADIE lo tiene.  [28-09-2026]

    Hasta esta fecha el board decia "it is owned by another user" para
    cualquier checkout negado, y era falso: se vio en Revit 2024 con un local
    desactualizado respecto del central. No se afirma una causa que no se
    verifico: si el elemento esta desactualizado se
    dice, y si no, solo lo que se sabe.
    """
    if stale:
        return (
            "Revit did not let the board edit '{0}', and nobody else has it: it "
            "changed in the central model after this local was last updated. Use "
            "Reload Latest in Revit, then try again.".format(name)
        )
    return (
        "Revit did not let the board edit '{0}', and nobody else has it. Use "
        "Reload Latest in Revit, then try again.".format(name)
    )


def owner_of(doc, element_id):
    """El usuario que tiene tomado el elemento, o `None` si no lo tiene nadie.

    Revit devuelve cadena vacia cuando no hay dueno, no `None`.
    """
    if not is_workshared(doc):
        return None
    try:
        info = DB.WorksharingUtils.GetWorksharingTooltipInfo(
            doc, _as_element_id(element_id))
    except Exception:
        return None
    owner = getattr(info, "Owner", None)
    return owner or None


def ownership(doc, element_ids):
    """Propiedad de varios elementos: `{id_entero: payload}`.

    Devuelve `{}` si el modelo no es compartido, que es lo que hace que
    todo el resto del codigo pueda llamar esto sin preguntar antes.

    El payload por elemento:
        checkout : "NotOwned" | "OwnedByCurrentUser" | "OwnedByOtherUser"
        owner    : nombre del dueno, o None
        editable : False solo si lo tiene OTRO usuario

    `editable` es `True` para `NotOwned`: todavia no es nuestro, pero
    esta libre y el checkout lo va a conseguir. Lo unico que bloquea es
    que lo tenga otro.
    """
    result = {}
    if not is_workshared(doc):
        return result

    for element_id in element_ids or []:
        try:
            key = ser.element_id_value(_as_element_id(element_id))
        except Exception:
            continue
        checkout = status_of(doc, element_id)
        # El dueno solo se pide cuando lo hay: es 6,7x mas caro que el
        # estado y en `NotOwned` volveria vacio.
        owner = None
        if checkout in (OWNED_BY_OTHER_USER, OWNED_BY_CURRENT_USER):
            owner = owner_of(doc, element_id)
        result[key] = {
            "checkout": checkout,
            "owner": owner,
            "editable": checkout != OWNED_BY_OTHER_USER,
        }
    return result


def cannot_edit_message(name, owner):
    """El mensaje de "lo tiene otro usuario", con las palabras de Revit.

    Revit dice literalmente "Can't edit the element until '<user>'
    resigns from it or relinquishes it and you Reload Latest". Se copia
    esa forma a proposito: la convencion es usar la palabra
    exacta de Revit cuando el texto nombra algo que existe en Revit, y
    aca son tres -- Relinquish, Reload Latest y Synchronize with
    Central. Un usuario que ya vio ese cartel en Revit reconoce este.
    """
    who = owner or "another user"
    return (
        "Can't edit '{0}': it is owned by {1}. It stays locked until {1} "
        "relinquishes it and you Reload Latest.".format(name, who)
    )


# --- Fase 15: la posesion colgada -----------------------------------------

# Cuantos ids se sondean con `GetCheckoutStatus`. Medido en la Fase 8:
# 0,092 ms por elemento, o sea ~185 ms para este tope -- aceptable para
# una sola llamada al abrir el board, corriendo en el `ExternalEvent`.
# Pasado el tope el aviso se vuelve "al menos N", que es lo que igual
# dice por las dos razones del docstring de `pending_checkouts`.
MAX_PROBE = 2000

# Cuantas veces se pregunta el DUENO, que es la consulta cara: 0,617 ms
# por elemento contra 0,092 del estado (medido en la Fase 8). Sin este
# tope, un modelo donde un colega tenga tomado todo lo que el board toco
# costaria ~1,2 s de mas al abrir, y encima dentro del ExternalEvent.
# Pasado el tope el aviso pasa a decir "at least", igual que la cuenta
# principal.
MAX_OWNER_PROBE = 500

# Cuantos elementos se nombran en el aviso. Es una muestra para que el
# usuario reconozca de que esta hablando, no la lista completa.
SAMPLE = 5


def owned_worksets(doc):
    """Los worksets de usuario que tiene tomados el usuario actual.

    ES LA SENAL QUE DISTINGUE UN PRESTAMO DE VERDAD, y sin ella la Fase 15
    daba un falso positivo del peor tipo. Medido en el fixture de 2025:
    con los tres worksets de usuario a nombre de <usuario>, las **2.198**
    puertas del modelo leian `OwnedByCurrentUser` -- no porque el board
    hubiera pedido nada, sino porque quien posee el workset posee todo lo
    que hay dentro. Contarlas como posesion colgada seria acusar al board
    de algo que no hizo, y ademas ofrecer un boton que no puede
    arreglarlo: `RelinquishOptions.CheckedOutElements` suelta elementos
    prestados UNO A UNO y no toca los worksets (verificado: devolvio 0 de
    12; con los worksets sueltos, los mismos 12 volvieron todos).

    Barato: son unos pocos worksets por modelo, no uno por elemento.
    """
    result = set()
    if not is_workshared(doc):
        return result
    try:
        collector = DB.FilteredWorksetCollector(doc).OfKind(
            DB.WorksetKind.UserWorkset)
        for workset in collector.ToWorksets():
            # `Owner` vuelve cadena vacia cuando no lo tiene nadie, igual
            # que en `owner_of`.
            if workset.Owner:
                result.add(ser.element_id_value(workset.Id))
    except Exception:
        # Sin este dato el aviso sobre-reporta, que es preferible a
        # reventar: el usuario ve de mas, no de menos.
        pass
    return result


def _explained_by_workset(doc, element_id, worksets):
    """`True` si la posesion del elemento la explica su workset.

    Solo se pregunta cuando `worksets` NO esta vacio, que es lo que hace
    que el caso normal -- nadie posee worksets -- no pague ni una lectura
    de elemento.
    """
    try:
        element = doc.GetElement(_as_element_id(element_id))
        if element is None:
            return False
        return ser.element_id_value(element.WorksetId) in worksets
    except Exception:
        return False


def pending_checkouts(doc):
    """Que quedo prestado de sesiones anteriores del board.  [Fase 15]

    EL CRUCE DE LA FASE 15, y las dos mitades vienen de lados distintos:
    `audit.previous_session_ids` dice QUE TOCO el board en otra sesion
    -- leyendo un archivo, sin Revit -- y `GetCheckoutStatus` dice QUE
    SIGUE TOMADO. Ninguna de las dos sola contesta la pregunta: el
    rastro no sabe si ya se devolvio, y Revit no puede enumerar lo
    propio sin recorrer el modelo entero a 0,092 ms por elemento.

    EL NUMERO SE DICE "AT LEAST N", NO "N", y no es prudencia sino dos
    hechos medidos del proyecto:
      - `audit.capped_ids` corta la lista en 500 por linea, guardando
        siempre la cuenta real pero no todos los ids;
      - `writer._dragged_by` esta documentado como MINIMO: cubre los
        grupos, que es el arrastre de elementos ajenos y reconocibles,
        pero un piso se lleva ademas su `Sketch`, sus `ModelLine` y el
        `SketchPlane`, que no quedan listados.
    Es la misma palabra que ya usa la UI para el arrastre del checkout.

    En un modelo SIN worksharing corta en la primera linea y cuesta
    cero, que es el camino de la tarea 5 de la fase.
    """
    if not is_workshared(doc):
        return {"workshared": False, "count": 0, "element_ids": [],
                "sample": [], "capped": False, "checked": 0,
                "stale_count": 0, "worksets_owned": 0}

    known = audit.previous_session_ids(doc)
    capped = len(known) > MAX_PROBE
    probe = known[:MAX_PROBE]
    # `capped` puede volverse cierto tambien mas abajo, si se agotan las
    # consultas de dueno: las dos cosas significan lo mismo para el
    # cliente -- solo se miro una parte, asi que el numero es un minimo.

    me = None
    try:
        me = doc.Application.Username
    except Exception:
        pass

    # Si NADIE posee worksets -- el estado normal de un modelo compartido
    # en uso -- este set queda vacio y no se paga ni una lectura de
    # elemento mas abajo.
    worksets = owned_worksets(doc)

    still = []
    stale = []
    owner_probes = 0
    for value in probe:
        state = status_of(doc, value)
        if state == OWNED_BY_CURRENT_USER:
            # La posesion heredada del workset NO es un prestamo del
            # board: ver `owned_worksets`.
            if worksets and _explained_by_workset(doc, value, worksets):
                continue
            still.append(value)
        elif state == OWNED_BY_OTHER_USER and me and (
                owner_probes < MAX_OWNER_PROBE):
            owner_probes += 1
            # TOMADO POR MI, PERO DESDE OTRO LOCAL. No es un caso raro:
            # es exactamente lo que encontro la Fase 14 sin buscarlo --
            # una sesion anterior los tomo y se cerro sin sincronizar, y
            # ahora Revit los da por ajenos porque la posesion es del
            # LOCAL, no del nombre de usuario.
            #
            # Se separa de `still` porque `RelinquishOwnership` NO los
            # alcanza: *"Elements and worksets owned by other users are
            # ignored"*. Meterlos en la misma cuenta seria ofrecer un
            # boton que no puede cumplir.
            #
            # El nombre del dueno cuesta 6,7x mas que el estado, asi que
            # solo se pregunta aca, donde el dato existe -- misma
            # optimizacion que `ownership()`.
            if owner_of(doc, value) == me:
                stale.append(value)

    if owner_probes >= MAX_OWNER_PROBE:
        capped = True

    sample = []
    for value in still[:SAMPLE]:
        try:
            element = doc.GetElement(_as_element_id(value))
        except Exception:
            element = None
        if element is None:
            continue
        # `Category` directo y no `ser.category_name`, que toma un
        # `category_id`: aca se tiene el elemento, y una vista -- que es
        # lo que queda colgado tras editar la definicion de un schedule
        # -- no tiene categoria, asi que el `None` es normal.
        try:
            category = element.Category.Name if element.Category else None
        except Exception:
            category = None
        sample.append({
            "id": value,
            "name": ser.safe_name(element),
            "category": category,
        })

    return {
        "workshared": True,
        "count": len(still),
        "element_ids": still,
        "sample": sample,
        # `capped` y `checked` existen para que el cliente pueda ser
        # honesto sobre lo que se miro: sin ellos "at least" seria una
        # palabra sin respaldo en el payload.
        "capped": capped,
        "checked": len(probe),
        # Lo que esta tomado por mi mismo desde OTRO local. Se cuenta
        # aparte porque el boton no puede devolverlo.
        "stale_count": len(stale),
        # Cuantos worksets de usuario estan tomados. NO enciende el aviso
        # por si solo -- el board nunca pide worksets y Revit ya lo
        # muestra en su propio dialogo `Worksets` -- pero cuando ademas
        # hay elementos prestados, explica por que el boton puede no
        # alcanzar para desbloquear a un colega.
        "worksets_owned": len(worksets),
    }
