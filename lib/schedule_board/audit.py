# -*- coding: UTF-8 -*-
"""El rastro de lo que el board escribe.  [Fase 14]

POR QUE ESTE MODULO EXISTE
--------------------------
Desde la Fase 9 el board escribe datos del MODELO, y desde la Fase 10 lo
hace sobre cientos de elementos en una sola transaccion. Hasta aca no
quedaba ningun registro: ni de que se escribio, ni de sobre cuantos
elementos, ni -- lo mas caro -- de QUE VALOR HABIA ANTES. Si manana
aparece un valor que no deberia estar ahi, no hay forma de reconstruir
que paso sin abrir el modelo y comparar a ojo.

Lo levanto la revision cruzada del council del 21-08-2026, y es uno de
los tres huecos que no estaban en el programa original.

TRES DECISIONES QUE ORDENAN ESTE ARCHIVO
----------------------------------------
1. **Cuelga de `writer._commit`, no de cada funcion que escribe.** Es el
   UNICO lugar por donde pasan todas las transacciones del proyecto --
   el mismo razonamiento por el que `_FailureCatcher` vive ahi. Asi una
   escritura futura no puede quedar sin registrar porque alguien se
   olvido de agregar la llamada en un caller nuevo.

2. **Se registra TODA transaccion, no solo las que tocan datos del
   modelo.** Cuesta lo mismo (es el mismo gancho, sin ramas) y la Fase 15
   lo necesita: para avisar de elementos prestados hay que saber que se
   toco, y las escrituras de definicion tambien piden posesion.

3. **`ensure_ascii=True` a proposito.** El archivo queda ASCII puro. Con
   59 de los 240 schedules con tilde o `n` y el campo minado de unicode
   de IronPython 2.7, un archivo ASCII es el que
   no puede morder. El frontend decodifica `\\u00f3` sin enterarse.

LO QUE NO HACE, Y ES DELIBERADO
-------------------------------
**Un fallo del registro NUNCA puede voltear una escritura del usuario.**
Si el disco esta lleno o el archivo bloqueado, se traga la excepcion --
pero NO en silencio, que es justo el error que costo la Fase 12: queda
en `LAST_ERROR` y `GET /audit/` lo devuelve. Visible sin ser peligroso.
"""

import json
import os
import time

from schedule_board.paths import data_path

# `serializers` se importa DENTRO de `old_values()`, no aca: es lo unico
# de este modulo que necesita la API de Revit, y dejarlo arriba haria
# que el archivo entero -- rutas, rotacion, lectura -- solo se pudiera
# ejercitar dentro de Revit. Ver la comprobacion del final.


# Techo del archivo antes de rotar, y cuantas generaciones se guardan.
# ponytail: una sola generacion; si hace falta historia mas larga, esto
# es donde crece.
MAX_BYTES = 5 * 1024 * 1024
ROTATED_SUFFIX = ".1"

# Cuantos valores viejos DISTINTOS se guardan por transaccion. Una fila
# colapsada agrupa por valor mostrado, asi que lo normal es que haya uno
# solo; el tope existe para que un lote raro no infle la linea.
MAX_OLD_VALUES = 20

# Cuantos `element_ids` entran en una linea. Una fila de `TC-Puertas`
# puede tener 475 elementos y un lote de nueve filas varios miles: sin
# tope, una sola escritura dejaria una linea de decenas de KB. Con tope
# se guarda la cuenta completa igual, asi que no se pierde el "sobre
# cuantos" -- solo el "cuales" mas alla de los primeros.
MAX_ELEMENT_IDS = 500

# Lo que se esta por registrar. Mismo patron que `writer.PROGRESS`: un
# dict de modulo que el caller que SI conoce el dominio va llenando, y
# que `_commit` vacia al escribir la linea. `writer._commit` no sabe --
# ni tiene por que saber -- que es un `field_id`.
PENDING = {}

_STARTED = [0.0]

# El ultimo fallo del propio registro. Se expone en `GET /audit/` en vez
# de tragarselo: un rastro que falla en silencio es peor que no tenerlo,
# porque se confia en el.
LAST_ERROR = [None]

# QUE SESION ESCRIBIO CADA LINEA.  [Fase 15]
#
# El PID del proceso, que en el motor de las rutas es el de REVIT. Es lo
# que separa "lo que tome hace un minuto" de "lo que quedo colgado de la
# sesion que se cayo", y sin esa separacion el aviso de la Fase 15 se
# encenderia despues de cada escritura.
#
# Se eligio el PID y no un GUID por import a proposito: no hay nada que
# generar ni que guardar, y sobrevive un Reload de pyRevit -- mismo
# proceso, o sea correctamente "esta sesion" -- que es justo donde un
# GUID por motor mentiria. Que el sistema reuse un PID es posible y el
# costo es que un aviso sobre o no aparezca; no se pierde nada.
SESSION_PID = os.getpid()


def _root():
    """La carpeta del rastro. Fuera de carpetas sincronizadas, como el perfil de WebView2."""
    return data_path("writes")


# Lista blanca EXPLICITA y ASCII. No se usa `isalnum()` a proposito:
# bajo IronPython 2.7 devuelve `True` para los acentuados, asi que un
# proyecto llamado "Cotizacion Norte" con tilde dejaria el nombre del
# archivo dependiendo de la codificacion -- y despues `?document=` no
# volveria a encontrarlo.
_ALLOWED = ("abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789-_.")


def _safe_component(text):
    """Un nombre de documento convertido en nombre de archivo.

    Revit admite en el titulo cosas que el sistema de archivos no. Se
    pasa por una lista blanca en vez de intentar escapar: el nombre solo
    tiene que ser estable y legible, no reversible.
    """
    out = []
    for char in (text or u"document"):
        if char in _ALLOWED:
            out.append(char)
        elif char == u" ":
            out.append("_")
    name = "".join(out).strip("._") or "document"
    return name[:80]


def document_name(doc):
    """El titulo del documento, sin extension. Vacio si no hay documento."""
    if doc is None:
        return "document"
    try:
        title = doc.Title
    except Exception:
        title = None
    if not title:
        return "document"
    if title.lower().endswith(".rvt"):
        title = title[:-4]
    return title


def _path_for(doc):
    return os.path.join(_root(), _safe_component(document_name(doc)) + ".jsonl")


def _user_name(doc):
    """El usuario de Revit. Es read-only en la API pero se puede leer."""
    try:
        return doc.Application.Username
    except Exception:
        return None


def begin(operation, **detail):
    """Abre un registro. Lo llama quien conoce el dominio, no `_commit`.

    Es opcional: una transaccion sin `begin()` igual queda registrada por
    `record()`, solo que sin el detalle que este modulo no puede adivinar.
    """
    global PENDING
    _STARTED[0] = time.time()
    pending = {"operation": operation}
    pending.update(detail)
    PENDING = pending


def note(**detail):
    """Agrega detalle a lo que ya se venia armando.

    Existe por el valor VIEJO: no se conoce hasta que se resolvieron los
    parametros, o sea despues de `begin()` y antes del `Set()`.
    """
    global PENDING
    if not PENDING:
        return
    merged = dict(PENDING)
    merged.update(detail)
    PENDING = merged


def pending():
    """`True` si hay un registro abierto sin escribir todavia.

    Lo usa el camino del 409 para no duplicar la linea cuando el rechazo
    vino DESPUES del commit (un `RolledBack`, que `record()` ya anoto).
    """
    return bool(PENDING)


def record(doc, transaction, outcome, error=None, code=None):
    """Escribe UNA linea y cierra el registro. Nunca lanza.

    `outcome` es uno de:
      - `committed`   la transaccion se aplico de verdad;
      - `rolled_back` Revit la revirtio (el `Commit()` puede devolver
                      `RolledBack` sin lanzar nada);
      - `rejected`    una guarda nuestra dijo que no ANTES de abrir la
                      transaccion, o sea el 409 que ve el cliente.

    Los tres se guardan a proposito: un intento fallido es informacion, y
    es la mitad del valor de un rastro.
    """
    global PENDING
    detail = PENDING
    PENDING = {}

    started = _STARTED[0]
    _STARTED[0] = 0.0

    entry = {
        "at": _timestamp(),
        "user": _user_name(doc),
        "document": document_name(doc),
        "transaction": transaction,
        "outcome": outcome,
        "ms": int((time.time() - started) * 1000) if started else None,
        "pid": SESSION_PID,
    }
    entry.update(detail)
    if error is not None:
        entry["error"] = str(error)
    if code is not None:
        entry["code"] = code

    _append(_path_for(doc), entry)
    return entry


def ownership_line(doc, operation, element_ids, extra=None):
    """Una linea propia cada vez que el board MUEVE LA POSESION.  [Fase 15]

    `operation` es `"checkout"` (se tomo) o `"relinquish"` (se devolvio).
    Las dos comparten forma a proposito: son las dos mitades del mismo
    hecho, y el cruce de la fase resta una de la otra.

    POR QUE NO ALCANZABA CON LA LINEA DE LA ESCRITURA, que es lo que
    dejo la Fase 14. El rastro de una escritura guarda lo que se
    ESCRIBIO, y la posesion la toma el CHECKOUT: no son lo mismo, y se
    separan en tres casos verificados leyendo el archivo real y el
    camino de `writer`:

    1. En una columna de TIPO, `element_ids` guarda las INSTANCIAS y el
       checkout tomo los TIPOS (`param.Element.Id`). Sondear las
       instancias con `GetCheckoutStatus` diria "nada colgado" mientras
       los tipos siguen tomados -- falso negativo en el caso mas caro.
    2. Una linea `rejected` no trae ningun id, y la unica linea real de
       posesion colgada que dejo la Fase 14 es justamente de esa forma.
       Pero ese rechazo ocurrio DESPUES del checkout, que bien pudo
       tomar parte del lote antes de que otro usuario le negara el resto.
    3. Las escrituras de DEFINICION (`add_field`, `apply_batch`,
       `delete_schedule`) pasan por `_commit` con `PENDING` vacio, asi
       que su linea no tiene ids -- pero hicieron checkout de la
       `ViewSchedule`, y un schedule colgado bloquea a un colega para
       editar esa tabla.

    Por eso cuelga de `writer.checkout_elements`, que es el UNICO lugar
    por el que el board toma posesion: el mismo argumento de embudo por
    el que `_FailureCatcher` y el rastro de la Fase 14 viven en
    `_commit`. Una funcion futura que pida posesion queda registrada sin
    que nadie se acuerde de anotarla.

    NO toca `PENDING`: se escribe al instante y aparte. Mezclarla con el
    registro de la escritura habria obligado a que toda funcion que
    escribe abriera un `begin()`, que es exactamente el olvido que este
    diseno evita.
    """
    element_ids = list(element_ids or [])
    extra = list(extra or [])
    if not element_ids and not extra:
        return None

    capped, total = capped_ids(sorted(element_ids))
    extra_capped, extra_total = capped_ids(sorted(extra))
    entry = {
        "at": _timestamp(),
        "user": _user_name(doc),
        "document": document_name(doc),
        "operation": operation,
        "outcome": operation,
        "pid": SESSION_PID,
        "element_ids": capped,
        "element_count": total,
        # Lo que Revit se llevo SIN que se pidiera. Cuelga igual que lo
        # pedido y hasta ahora no lo sabia nadie mas que `_dragged_by`.
        "extra_element_ids": extra_capped,
        "extra_count": extra_total,
    }
    _append(_path_for(doc), entry)
    return entry


# Cuantas lineas del rastro se miran hacia atras al buscar posesion
# colgada. La rotacion ya topa el archivo en 5 MB; esto topa el trabajo
# de parsear JSON, que es lo unico que cuesta aca.
MAX_HISTORY_LINES = 5000


def previous_session_ids(doc):
    """Los ids que ESTA sesion no tomo, sacados del rastro.  [Fase 15]

    Devuelve una lista deduplicada, sin preguntarle nada a Revit: quien
    cruza esto con `GetCheckoutStatus` es `worksharing.pending_checkouts`,
    y esa division es la regla 2 -- este modulo lee un archivo, aquel lee
    el modelo.

    "Otra sesion" es `pid` distinto al de esta. Una linea SIN `pid` es de
    antes de la Fase 15 y se cuenta como anterior: es lo conservador y
    ademas es cierto, porque esas lineas quedaron de sesiones cerradas.

    DESCARTAR LAS LINEAS DE ESTA SESION NO ALCANZA, y por eso hay dos
    pasadas. [Fase 17] Un elemento que nombra una linea VIEJA y que ADEMAS
    tomo esta sesion sigue apareciendo -- la linea vieja lo nombra -- y
    `GetCheckoutStatus` lo ve tomado, por mi, recien. El aviso entonces
    decia "de una sesion anterior" sobre algo tomado hace un minuto.
    Encontrado en la Fase 16 escribiendo dos puertas que ya figuraban en
    el lote de 30 de la Fase 15. El boton siempre estuvo bien -- devolver
    esos elementos es correcto --; lo equivocado era la frase.

    Se resta lo que ESTA sesion tomo, que el rastro ya sabe. Si esta
    sesion lo tomo y despues lo devolvio, restarlo sigue siendo correcto:
    lo que no es, en ningun caso, es de una sesion anterior.
    """
    payload = read(limit=MAX_HISTORY_LINES, document=document_name(doc))
    entries = payload.get("entries") or []

    mine = set()
    for entry in entries:
        if entry.get("pid") != SESSION_PID:
            continue
        if entry.get("operation") != "checkout":
            continue
        for key in ("element_ids", "extra_element_ids"):
            for value in entry.get(key) or []:
                mine.add(value)

    seen = set()
    ids = []
    for entry in entries:
        if entry.get("pid") == SESSION_PID:
            continue
        # Una linea de `relinquish` nombra lo que se DEVOLVIO. Sumarla
        # seria sondear justo lo que ya no esta tomado -- inofensivo,
        # porque `GetCheckoutStatus` lo descartaria, pero es trabajo
        # pagado para nada sobre el tope de `MAX_PROBE`.
        if entry.get("operation") == "relinquish":
            continue
        for key in ("element_ids", "extra_element_ids"):
            for value in entry.get(key) or []:
                if value in seen or value in mine:
                    continue
                seen.add(value)
                ids.append(value)
    return ids


def _timestamp():
    """ISO 8601 local, al segundo. Sin `datetime.isoformat()` para no
    arrastrar microsegundos que no aportan nada a un rastro humano."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _append(path, entry):
    """Una linea JSON al final del archivo. Se traga cualquier fallo.

    APPEND-ONLY Y UNA LINEA POR TRANSACCION: si el archivo se corrompe se
    pierde una linea, no el historial. Por eso JSONL y no un JSON con un
    array, que habria que reescribir entero cada vez.
    """
    try:
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        _rotate_if_needed(path)
        line = _ascii_json(entry)
        handle = open(path, "a")
        try:
            handle.write(line + "\n")
        finally:
            handle.close()
        LAST_ERROR[0] = None
    except Exception as ex:
        # Ver el encabezado: esto NO puede voltear la escritura del
        # usuario, pero tampoco desaparece -- queda para `GET /audit/`.
        LAST_ERROR[0] = str(ex)


def _ascii_json(entry):
    """Una linea JSON en ASCII puro, que funcione en el motor de las rutas.

    [Fase 22, bug B2] NO ES `json.dumps(ensure_ascii=True)`, aunque el
    resultado es el mismo: esa llamada REVIENTA en el motor de arranque
    de ScheduleBoard con cualquier texto no ASCII ("'unknown' codec can't
    decode byte 0xed"), porque ahi `sys.getdefaultencoding()` es 'ascii'
    y en IronPython `str` y `unicode` son el mismo tipo. `_append` se
    tragaba el error, asi que TODA escritura sobre un schedule con tilde
    o `n` (59 de 240 nombres) desaparecia del rastro sin aviso. Medido
    en Revit 2025 con `TC-Griferias`. En el motor del Revit-Connector no
    falla, y por eso la comprobacion de abajo nunca lo vio.

    Vive en `serializers.ascii_json` (que tambien usa `http.ascii_json`);
    import perezoso, no a nivel de modulo -- este archivo tiene que poder
    importarse sin Revit, como dice el encabezado, y `serializers.py` si
    trae `Autodesk.Revit.DB`.
    """
    from schedule_board import serializers as ser
    return ser.ascii_json(entry, sort_keys=True)


def _rotate_if_needed(path):
    """Corta el archivo por tamano. Una sola generacion.

    Un lote de 7.285 filas no puede dejar decenas de MB en
    `%LOCALAPPDATA%`.
    """
    try:
        if not os.path.isfile(path):
            return
        if os.path.getsize(path) < MAX_BYTES:
            return
        rotated = path.replace(".jsonl", ROTATED_SUFFIX + ".jsonl")
        if os.path.isfile(rotated):
            os.remove(rotated)
        os.rename(path, rotated)
    except Exception as ex:
        LAST_ERROR[0] = str(ex)


def old_values(params):
    """Los valores DISTINTOS que hay ahora en esos parametros.

    HAY QUE LLAMARLA ANTES DE ESCRIBIR: despues del `Set()` el valor
    anterior no existe en ningun lado, y la API de Revit no ofrece un
    undo-log que se pueda consultar despues del hecho. Es lo que hace
    esta fase menos trivial de lo que parece.

    Distintos y no uno por elemento: una fila colapsada agrupa POR valor
    mostrado, asi que lo normal es que los 475 elementos de una fila de
    `TC-Puertas` compartan uno solo. Guardar 475 copias del mismo string
    seria pesar mas que la escritura, y guardar solo el primero seria
    mentir cuando de verdad hay varios.
    """
    from schedule_board import serializers as ser

    seen = []
    for param in params:
        try:
            display = ser.parameter_payload(param).get("display")
        except Exception:
            display = None
        if display not in seen:
            seen.append(display)
        if len(seen) >= MAX_OLD_VALUES:
            break
    return seen


def capped_ids(element_ids):
    """`(ids recortados, cuenta real)`, para no inflar la linea.

    Se devuelve SIEMPRE la cuenta real: perder el "cuales" mas alla de
    los primeros 500 es asumible, perder el "sobre cuantos" no -- es la
    mitad de lo que el entregable de la fase promete poder contestar.
    """
    ids = list(element_ids or [])
    return ids[:MAX_ELEMENT_IDS], len(ids)


def read(limit=50, document=None):
    """Las ultimas `limit` lineas, de la mas reciente a la mas vieja.

    QUE ARCHIVO LEE, Y POR QUE NO LO PREGUNTA: `GET /audit/` no declara
    `doc` -- ese es todo su punto, igual que `/progress/` -- asi que no
    puede saber cual es el documento activo. Lee el `.jsonl` MAS
    RECIENTEMENTE MODIFICADO de la carpeta, que con un documento abierto
    es siempre el correcto.
    # ponytail: con dos documentos abiertos muestra el del ultimo que
    # escribio; para el otro hay que pasar `document`.
    """
    path = None
    if document:
        candidate = os.path.join(_root(), _safe_component(document) + ".jsonl")
        path = candidate if os.path.isfile(candidate) else None
    else:
        path = _most_recent()

    if path is None:
        return {"document": document, "entries": [], "error": LAST_ERROR[0]}

    entries = []
    try:
        handle = open(path, "r")
        try:
            # ponytail: se lee entero y se corta al final. Con el techo de
            # rotacion en 5 MB son milisegundos; si algun dia sube, esto
            # es lo que hay que hacer al reves.
            lines = handle.readlines()
        finally:
            handle.close()
        for line in lines[-int(limit or 50):]:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except Exception:
                # Una linea rota no se lleva las demas: es exactamente
                # para esto que el formato es JSONL.
                continue
    except Exception as ex:
        return {"document": document, "entries": [], "error": str(ex)}

    entries.reverse()
    return {
        "document": os.path.basename(path).replace(".jsonl", ""),
        "entries": entries,
        "error": LAST_ERROR[0],
    }


def _most_recent():
    """El `.jsonl` modificado mas recientemente, o `None` si no hay ninguno."""
    folder = _root()
    try:
        if not os.path.isdir(folder):
            return None
        best = None
        best_time = -1
        for name in os.listdir(folder):
            if not name.endswith(".jsonl"):
                continue
            full = os.path.join(folder, name)
            try:
                stamp = os.path.getmtime(full)
            except Exception:
                continue
            if stamp > best_time:
                best = full
                best_time = stamp
        return best
    except Exception:
        return None


def _self_check():
    """Ejercita el ida y vuelta por el archivo, que es el entregable.

    Corre con CPython fuera de Revit -- desde `lib/`, con
    `PYTHONPATH=. python schedule_board/audit.py` (o `PYTHONPATH=..` desde
    esta misma carpeta) -- porque nada de lo que prueba toca la API: rutas,
    nombres de archivo, la linea JSON, la rotacion y la lectura del final.
    Lo unico que si la toca es `old_values()`, y por eso su import esta
    adentro de la funcion.

    [20-10-2026, revision de la auditoria] El `PYTHONPATH` hace falta
    porque este archivo mismo hace `from schedule_board.paths import
    data_path` arriba: un `python audit.py` a secas, desde esta carpeta,
    pone `lib/schedule_board` en `sys.path[0]` (el directorio del script),
    no `lib/`, asi que `schedule_board` no se encuentra. Costo un
    `ModuleNotFoundError` verificarlo.

    No es un framework de tests ni pretende serlo: es la comprobacion
    minima que falla si el rastro deja de escribirse o de leerse.
    """
    import shutil
    import tempfile

    global _root
    original_root = _root
    folder = tempfile.mkdtemp(prefix="sb_audit_")

    class _FakeApp(object):
        Username = "UsuarioPrueba"

    class _FakeDoc(object):
        Title = "Modelo-Central_UsuarioPrueba.rvt"
        Application = _FakeApp()

    try:
        _root = lambda: folder

        # Nombres de archivo: lo que Revit admite en un titulo no siempre
        # lo admite el sistema de archivos.
        assert _safe_component("TC-Puertas") == "TC-Puertas"
        assert _safe_component(u"Ciudad Empresarial") == "Ciudad_Empresarial"
        assert _safe_component("a/b\\c:d") == "abcd"
        assert _safe_component("") == "document"
        # Un acentuado se CAE del nombre en vez de colarse: ver `_ALLOWED`.
        assert _safe_component(u"Cotización") == "Cotizacin"

        doc = _FakeDoc()
        assert document_name(doc) == "Modelo-Central_UsuarioPrueba"

        # Una escritura con detalle de dominio, como la de una celda.
        begin("cell", schedule="TC-Puertas", field_id=15, new_value="Madera")
        note(old_values=["Aluminio"])
        assert pending() is True
        record(doc, "ScheduleBoard: edit 9 rows", "committed")
        assert pending() is False, "record() tiene que cerrar el registro"

        # Un rechazo, sin `begin()` previo: tiene que quedar igual.
        record(doc, "ScheduleBoard: edit cell", "rejected",
               error="'Marca' is read-only in Revit.",
               code="parameter_read_only")

        back = read(limit=10)
        entries = back["entries"]
        assert len(entries) == 2, entries
        # Mas reciente primero.
        assert entries[0]["outcome"] == "rejected"
        assert entries[0]["code"] == "parameter_read_only"
        assert entries[1]["outcome"] == "committed"
        assert entries[1]["old_values"] == ["Aluminio"]
        assert entries[1]["new_value"] == "Madera"
        assert entries[1]["schedule"] == "TC-Puertas"
        assert entries[1]["user"] == "UsuarioPrueba"
        assert entries[1]["document"] == "Modelo-Central_UsuarioPrueba"

        # El detalle de una transaccion NO se filtra a la siguiente: es lo
        # que pasaria si `record()` no vaciara `PENDING`.
        assert "old_values" not in entries[0], entries[0]

        # --- Fase 15: la linea del checkout y el cruce por sesion -----
        #
        # Es lo que hace posible el aviso de posesion colgada, asi que se
        # ejercita el caso completo: lo tomado por OTRA sesion se ve, lo
        # tomado por ESTA no.
        assert ownership_line(doc, "checkout", [], []) is None, (
            "sin nada tomado, sin linea")

        ownership_line(doc, "checkout", [101, 102], [900])
        line = read(limit=1)["entries"][0]
        assert line["operation"] == "checkout"
        assert line["element_ids"] == [101, 102]
        assert line["extra_element_ids"] == [900], "el arrastre cuelga igual"
        assert line["pid"] == SESSION_PID

        # Lo de ESTA sesion no es posesion colgada: si contara, el aviso
        # se encenderia despues de cada escritura.
        assert previous_session_ids(doc) == []

        # Una linea de otra sesion, y una vieja SIN `pid` -- las que dejo
        # la Fase 14, que son de sesiones ya cerradas.
        _append(_path_for(doc), {"at": _timestamp(), "operation": "checkout",
                                 "pid": SESSION_PID + 1,
                                 "element_ids": [201, 202],
                                 "extra_element_ids": [202, 203]})
        _append(_path_for(doc), {"at": _timestamp(), "operation": "cell",
                                 "element_ids": [301]})
        # Lo DEVUELTO por otra sesion no se sondea: ya no esta tomado.
        _append(_path_for(doc), {"at": _timestamp(), "operation": "relinquish",
                                 "pid": SESSION_PID + 1,
                                 "element_ids": [777]})
        found = previous_session_ids(doc)
        assert 777 not in found, found
        assert sorted(found) == [201, 202, 203, 301], found
        assert len(found) == len(set(found)), "sin repetidos: se sondea uno a uno"

        # [Fase 17] EL CASO QUE COSTO UN AVISO EQUIVOCADO: un elemento que
        # nombra una linea VIEJA y que ADEMAS tomo ESTA sesion. Antes salia
        # como "de una sesion anterior" estando tomado hace un minuto.
        _append(_path_for(doc), {"at": _timestamp(), "operation": "checkout",
                                 "pid": SESSION_PID + 1,
                                 "element_ids": [101, 555]})
        found = previous_session_ids(doc)
        assert 101 not in found, ("101 lo tomo esta sesion: no es de una "
                                  "anterior")
        assert 555 in found, found

        # ASCII puro: es la decision que evita el campo minado de unicode
        # de IronPython 2.7 con los 59 nombres que llevan tilde.
        begin("cell", schedule=u"TC-Ventanas cota mínima")
        record(doc, "ScheduleBoard: edit cell", "committed")
        path = _path_for(doc)
        handle = open(path, "rb")
        try:
            raw = handle.read()
        finally:
            handle.close()
        assert b"\\u00ed" in raw, "el archivo tiene que quedar ASCII puro"
        assert read(limit=1)["entries"][0]["schedule"] == (
            u"TC-Ventanas cota m\xednima")

        # Rotacion: una sola generacion, y la vieja se conserva aparte.
        global MAX_BYTES
        original_max = MAX_BYTES
        try:
            MAX_BYTES = 1
            record(doc, "ScheduleBoard: edit cell", "committed")
        finally:
            MAX_BYTES = original_max
        rotated = path.replace(".jsonl", ROTATED_SUFFIX + ".jsonl")
        assert os.path.isfile(rotated), "la generacion vieja tiene que quedar"
        assert len(read(limit=99)["entries"]) == 1, "el activo arranca limpio"

        print("audit self-check OK")
    finally:
        _root = original_root
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    # [20-10-2026] `_ascii_json` ahora delega en `serializers.ascii_json`
    # (import perezoso, ver su docstring), y `serializers.py` SI trae
    # `Autodesk.Revit.DB` a nivel de modulo -- a diferencia de este archivo,
    # que sigue sin tocarlo para poder correr con CPython fuera de Revit.
    # Solo hace falta para el self-check: en produccion, dentro de Revit, el
    # import real ya existe y esto nunca se ejecuta (vive bajo __main__).
    import sys
    import types

    def _fake(name, **attrs):
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        sys.modules[name] = module
        return module

    _fake("Autodesk")
    sys.modules["Autodesk"].Revit = _fake("Autodesk.Revit", DB=_fake("Autodesk.Revit.DB"))

    _self_check()
