# -*- coding: UTF-8 -*-
"""Perfilado de parametros. NUNCA abre una transaccion.  [Fase 2]

Es la funcionalidad mas diferenciadora del producto y la de menor
riesgo: solo lectura. Responde, ANTES de agregar una columna, si vale
la pena agregarla.

Salida objetivo por parametro, replicando lo que muestra la referencia:

    Area                 100% filled - 39 values - 6 m2, 16 m2
    Structural Material   82% filled -  6 values - Metal Stud Layer, ...
    Assembly Code         36% filled -  7 values - C1010145, B2020300

Algoritmo, en UNA sola pasada sobre una MUESTRA de elementos (ver Cuidados):
    1. definition.GetSchedulableFields()  -> universo de campos de la
       categoria, mas amplio que las columnas ya visibles.
    2. reader.schedule_elements()         -> los elementos del schedule,
       recortados a PROFILE_SAMPLE_SIZE si el schedule tiene mas.
    3. Por cada elemento de la muestra y cada campo perfilable, leer
       el parametro.
    4. Agregar: % con valor, cardinalidad, hasta N valores de muestra.

Cuidados:
    - Es O(campos x elementos) y corre en el hilo principal de Revit.
      Por eso NUNCA se dispara junto con expandir la tarjeta ni en la
      carga inicial del board: un boton explicito ("Perfilar
      columnas"), aparte de "Expandir", en la tarjeta.
    - El universo de campos (~2.500, ver PROFILE_SAMPLE_SIZE) no se
      capa -- es una decision de producto ya cerrada perfilar TODOS
      los campos que ofrece Revit. Lo que se capa son los ELEMENTOS
      leidos, para que el costo total quede acotado sin importar el
      tamano del schedule.
    - `column_quality` NO vuelve a recorrer los elementos: se deriva
      del resultado ya calculado por `profile_schedulable_fields`,
      filtrando los campos que ya son columna. Perfilar y medir calidad
      de columnas son la MISMA pasada, no dos.
    - Distinguir "el parametro no existe en el elemento" de "existe
      pero esta vacio". `serializers.is_empty_payload` ya aplica el
      criterio correcto para ambos casos.
    - GetSchedulableFields devuelve MAS campos que la UI de Revit
      (shared y project parameters). Se distinguen por ParameterId > 0
      y se marcan con `is_shared_or_project` para que la
      interfaz los distinga.
    - Los campos de tipo (`is_type_field`) se leen del tipo, no de la
      instancia -- mismo mecanismo que ya usa `serializers.find_parameter`
      para las columnas ya visibles (`prefer_type`).
    - No todo lo que devuelve GetSchedulableFields tiene un Parameter
      real detras (algunos son pseudo-campos de Revit). Esos se marcan
      `profileable=False` en vez de reportar un enganoso "0% filled".
"""

from schedule_board import reader
from schedule_board import serializers as ser


MAX_SAMPLE_VALUES = 2

# Techo de elementos a leer por pasada. VERIFICADO CONTRA REVIT REAL
# (12-08-2026): perfilar TC-Ventanas (432 elementos, ~2.500 campos
# schedulable) colgo Revit mas de 40 minutos sin terminar -- muy por
# encima de lo asumido al disenar esto.
#
# ACTUALIZACION (Fase 7): buena parte de ese costo era un bug, no la
# naturaleza del problema. El cache de parametros de `serializers` NUNCA
# acertaba porque su clave usaba `id(element.Document)`, que bajo
# IronPython cambia en cada acceso; cada lectura de campo reconstruia el
# mapa de parametros del elemento. Con la clave arreglada, perfilar
# TC-Ventanas con esta muestra bajo de 103-135 s a ~7 s, unas 15 veces.
#
# El cap SE MANTIENE igual: el algoritmo sigue siendo O(campos x
# elementos) y sin el, una categoria con miles de elementos vuelve a
# ser un riesgo real de colgar Revit. Ahora es un techo comodo en vez de
# una necesidad urgente. El universo de campos
# (~2.500, medido en Areas y Sheets) es constante independiente de la
# categoria, asi que O(elementos x campos) crece sin limite con el
# tamano del schedule. Sin este cap, cualquier categoria con mas de un
# punado de elementos es un riesgo real de colgar Revit, no teorico.
# El % de llenado, la cardinalidad y las muestras se calculan sobre
# esta muestra, no sobre el total -- se marca `sampled` en la
# respuesta para que la interfaz lo distinga de un perfilado completo.
PROFILE_SAMPLE_SIZE = 25


def _schedulable_field_payload(doc, field, visible_by_parameter):
    """Representacion JSON de un SchedulableField, antes de leer valores.

    `in_schedule` cruza contra las columnas YA visibles del schedule
    (por `parameter_id`), para que la interfaz distinga "ya es columna"
    de "candidato a agregar".
    """
    payload = {
        "name": None,
        "parameter_id": None,
        # Junto con `parameter_id`, es lo que identifica al campo sin
        # ambiguedad para agregarlo como columna (Fase 5): un mismo
        # parametro puede ofrecerse como Instance y como ElementType.
        "field_type": None,
        "is_shared_or_project": False,
        "is_type_field": False,
        "in_schedule": False,
        "profileable": True,
        "filled_pct": None,
        "value_count": None,
        "samples": [],
    }

    try:
        payload["name"] = field.GetName(doc)
    except Exception:
        pass

    parameter_id = None
    try:
        parameter_id = field.ParameterId
        payload["parameter_id"] = ser.element_id_value(parameter_id)
    except Exception:
        parameter_id = None

    if payload["parameter_id"] is not None and payload["parameter_id"] > 0:
        payload["is_shared_or_project"] = True

    try:
        payload["field_type"] = str(field.FieldType)
        payload["is_type_field"] = payload["field_type"] == "ElementType"
    except Exception:
        pass

    if payload["parameter_id"] is not None and payload["parameter_id"] in visible_by_parameter:
        payload["in_schedule"] = True

    if parameter_id is None or not ser.is_valid_id(parameter_id):
        # Sin un Parameter real detras (pseudo-campo de Revit): no hay
        # nada que leer por elemento, y reportar 0% seria enganoso.
        payload["profileable"] = False

    return payload


def profile_schedulable_fields(doc, view, max_samples=MAX_SAMPLE_VALUES):
    """Estadistica de todos los campos agendables de la categoria.  [Fase 2]

    Una sola pasada sobre `reader.schedule_elements(doc, view)`: por
    cada elemento se leen todos los campos perfilables a la vez, no un
    recorrido de elementos por campo.

    Devuelve:
        {
            "element_count": 2083,
            "fields": [
                {
                    "name": "Structural Material",
                    "parameter_id": 12345,
                    "is_shared_or_project": True,
                    "is_type_field": False,
                    "in_schedule": False,
                    "profileable": True,
                    "filled_pct": 82,
                    "value_count": 6,
                    "samples": ["Metal Stud Layer", "Structure - Timber"],
                },
                ...
            ],
        }
    """
    ser.clear_param_cache()

    definition = view.Definition

    visible_by_parameter = {}
    for visible_payload, _visible_field in reader.visible_fields(definition):
        parameter_id = visible_payload.get("parameter_id")
        if parameter_id is not None:
            visible_by_parameter[parameter_id] = True

    try:
        schedulable_fields = list(definition.GetSchedulableFields())
    except Exception:
        schedulable_fields = []

    entries = []
    for field in schedulable_fields:
        payload = _schedulable_field_payload(doc, field, visible_by_parameter)
        entry = {"payload": payload, "field": field, "filled": 0, "seen": set(), "samples": []}
        entries.append(entry)

    profileable = [entry for entry in entries if entry["payload"]["profileable"]]

    all_elements = reader.schedule_elements(doc, view)
    total_elements = len(all_elements)
    sampled = total_elements > PROFILE_SAMPLE_SIZE
    elements = all_elements[:PROFILE_SAMPLE_SIZE] if sampled else all_elements
    sample_size = len(elements)

    for element in elements:
        for entry in profileable:
            payload = entry["payload"]
            param = ser.find_parameter(
                element,
                entry["field"].ParameterId,
                prefer_type=payload["is_type_field"],
            )
            cell = ser.parameter_payload(param)
            if ser.is_empty_payload(cell):
                continue
            entry["filled"] += 1
            display = cell.get("display")
            if display in entry["seen"]:
                continue
            entry["seen"].add(display)
            if len(entry["samples"]) < max_samples:
                entry["samples"].append(display)

    ser.clear_param_cache()

    for entry in profileable:
        payload = entry["payload"]
        payload["value_count"] = len(entry["seen"])
        payload["filled_pct"] = (
            int(round(100.0 * entry["filled"] / sample_size)) if sample_size else 0
        )
        payload["samples"] = entry["samples"]

    return {
        "element_count": total_elements,
        "sampled": sampled,
        "sample_size": sample_size,
        "fields": [entry["payload"] for entry in entries],
    }


def column_quality(profile):
    """Metricas de calidad de las columnas ya presentes.  [Fase 2]

    NO vuelve a leer el modelo: `profile` es la salida de
    `profile_schedulable_fields`, ya calculada en una sola pasada.
    Alimenta el resumen de la tarjeta:
        "56 elements - 64 rows x 7 columns - 1 empty column"

    Una columna cuenta como vacia si su % de llenado es 0. Los campos
    ya visibles que no son `profileable` (formulas, Count, combinados:
    no aparecen en GetSchedulableFields) no entran en este calculo --
    su calidad no se puede medir aca, y no es un vacio real.
    """
    visible = [f for f in profile["fields"] if f["in_schedule"]]
    measurable = [f for f in visible if f["profileable"]]
    empty = [f for f in measurable if f["filled_pct"] == 0]

    return {
        "element_count": profile["element_count"],
        "column_count": len(visible),
        "empty_column_count": len(empty),
        "empty_columns": [f["name"] for f in empty],
    }
