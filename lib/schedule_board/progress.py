# -*- coding: UTF-8 -*-
"""Progreso de una operacion larga de escritura.  [Fase 11]

Vivia en `writer.py`; se mudo en la Fase 27. Solo estado de modulo y
funciones puras: no toca Revit, asi que `GET /progress/` -- que NO
declara `doc` -- lo puede leer desde el hilo del servidor mientras la
escritura tiene tomado el hilo principal.
"""

import time


# POR QUE ESTO PUEDE FUNCIONAR
# ----------------------------
# `wants_api_context` de pyRevit mira la firma del handler: el que NO
# declara `doc` corre en el HILO DEL SERVIDOR, no en el ExternalEvent.
# Asi que mientras una escritura tiene tomado el hilo principal de Revit,
# una ruta sin `doc` sigue respondiendo -- y ve este diccionario, porque
# los handlers comparten los modulos vivos de la extension.
#
# Medido en la Fase 11, con un handler con `doc` bloqueando 10 s el hilo
# principal: 8 lecturas seguidas de la ruta sin `doc` en 4-6 ms cada una,
# viendo el contador avanzar. Es el mismo mecanismo por el que el latido
# de conexion usa el preflight OPTIONS y no `/status/`.
#
# POR QUE ESTO NO ES UNA BARRA DE PROGRESO
# ----------------------------------------
# Medido en la Fase 11, instrumentando las fases de `set_parameter` con
# todos los elementos `NotOwned` (checkout virgen):
#
#            fase          40 elementos   369 elementos   2 tipos
#   group_scan                    1 ms            6 ms        --
#   checkout                    340 ms          290 ms     223 ms
#   bucle de Set()               78 ms  5,6%    126 ms  4,7%    270 ms  2,2%
#   Commit() solo               923 ms   66%  1.979 ms   74%  11.492 ms  93%
#   TOTAL                     1.387 ms        2.676 ms      12.402 ms
#
# El bucle por el que un contador puede avanzar es el 2-6% del reloj.
# Entre dos tercios y nueve decimos se los lleva UN `Commit()` opaco, del
# que la API no ofrece ninguna granularidad. Una barra por elemento
# llegaria al 100% en el primer decimo de la espera y se quedaria ahi
# clavada, que es exactamente como se ve algo colgado.
#
# Por eso lo que se reporta es la FASE, no un porcentaje: `checkout`,
# `writing` (ahi si con N de M, que es real) y `applying`, que es la
# grande y no admite division. Los TEXTOS los pone el cliente: aca solo
# viajan datos, para que no haya que mantener strings visibles por el
# usuario en dos lugares.
#
# El costo de mantenerlo es despreciable -- rearmar un dict chico contra
# los 0,34 ms por elemento que cuesta el bucle -- y se rearma en vez de
# mutarse a proposito: asi el lector de otro hilo nunca ve un dict a
# medio actualizar, solo el anterior o el nuevo.

PROGRESS = {"phase": "idle", "operation": None, "done": 0, "total": 0,
            "seq": 0}

_PROGRESS_STARTED = [0.0]


def snapshot():
    """Lo que devuelve `GET /progress/`. Sin transaccion y sin tocar Revit.

    `seq` sube una vez por operacion, y con eso el cliente descarta una
    lectura vieja: sin el, el progreso de la escritura anterior se
    confundiria con el de la que acaba de empezar.
    """
    snapshot = dict(PROGRESS)
    started = _PROGRESS_STARTED[0]
    snapshot["elapsed_ms"] = (
        int((time.time() - started) * 1000) if started else 0)
    return snapshot


def begin(operation):
    global PROGRESS
    _PROGRESS_STARTED[0] = time.time()
    PROGRESS = {"phase": "resolving", "operation": operation, "done": 0,
                "total": 0, "seq": PROGRESS["seq"] + 1}


def phase(name, done=None, total=None):
    """Cambia de fase. Sin `total`, CONSERVA el de la fase anterior.

    Conservarlo importa: `applying` no tiene cuantos -- es una sola
    llamada a `Commit()` -- pero el cliente igual quiere poder decir
    sobre cuantos elementos esta aplicando el cambio. Con un default de
    0 la cuenta se perdia justo en la fase mas larga.
    """
    global PROGRESS
    PROGRESS = {
        "phase": name,
        "operation": PROGRESS["operation"],
        "done": PROGRESS["done"] if done is None else done,
        "total": PROGRESS["total"] if total is None else total,
        "seq": PROGRESS["seq"],
    }


def step(done):
    global PROGRESS
    PROGRESS = {"phase": PROGRESS["phase"], "operation": PROGRESS["operation"],
                "done": done, "total": PROGRESS["total"],
                "seq": PROGRESS["seq"]}


def end(final_phase):
    """Estado terminal. Va SIEMPRE en un `finally`.

    Sin esto una escritura que lanza dejaria el progreso clavado en su
    ultima fase, y el cliente creeria para siempre que Revit sigue
    trabajando.
    """
    global PROGRESS
    PROGRESS = {"phase": final_phase, "operation": PROGRESS["operation"],
                "done": PROGRESS["done"], "total": PROGRESS["total"],
                "seq": PROGRESS["seq"]}
