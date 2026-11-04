# -*- coding: utf-8 -*-
"""Centra las etiquetas de habitación (RoomTag) y de área (AreaTag) seleccionadas
en la ubicación geométrica de su elemento origen (Room o Area).
"""

from pyrevit import revit, DB, forms


# ---------------------------------------------------------------------------
# Estrategias de centrado por tipo de etiqueta (patrón polimórfico)
# ---------------------------------------------------------------------------

def _get_location_point_from_room(tag):
    """Devuelve el LocationPoint de la habitación (Room) vinculada al tag.

    Args:
        tag (DB.Architecture.RoomTag): etiqueta de habitación.

    Returns:
        DB.XYZ | None: punto de ubicación o None si no está disponible.
    """
    room = tag.Room
    if room and room.Location and isinstance(room.Location, DB.LocationPoint):
        return room.Location.Point
    return None


def _get_location_point_from_area(tag):  # tag: DB.AreaTag
    """Devuelve el punto central del área (Area) vinculada al tag.

    La API de Revit expone la propiedad ``Area`` en ``AreaTag``.
    El área hereda de ``SpatialElement`` y su ``Location`` es un
    ``LocationPoint`` que apunta al 'placement point' original.
    Como alternativa robusta se utiliza el centroide del bounding box
    cuando la ubicación no está disponible.

    Args:
        tag (DB.AreaTag): etiqueta de área.

    Returns:
        DB.XYZ | None: punto de ubicación o None si no está disponible.
    """
    area = tag.Area
    if area is None:
        return None

    # Primer intento: Location Point del área (idéntico al de Room)
    if area.Location and isinstance(area.Location, DB.LocationPoint):
        return area.Location.Point

    # Segundo intento: centroide del Bounding Box en la vista activa
    view = revit.active_view
    bbox = area.get_BoundingBox(view)
    if bbox:
        center = (bbox.Min + bbox.Max) * 0.5
        return DB.XYZ(center.X, center.Y, center.Z)

    return None


# Mapa de despacho: tipo → función extractora de punto
_LOCATION_STRATEGIES = {
    DB.Architecture.RoomTag: _get_location_point_from_room,
    DB.AreaTag:               _get_location_point_from_area,
}


# ---------------------------------------------------------------------------
# Lógica principal
# ---------------------------------------------------------------------------

def _move_tag_to_point(tag, target_pt):
    """Mueve la cabeza de una etiqueta al punto indicado.

    Activa temporalmente el líder para que Revit permita mover el tag
    a cualquier coordenada sin lanzar un ArgumentException, luego
    intenta desactivarlo una vez que el tag está centrado.

    Args:
        tag: instancia de RoomTag o AreaTag.
        target_pt (DB.XYZ): coordenada destino.
    """
    tag.HasLeader = True
    tag.TagHeadPosition = target_pt
    try:
        tag.HasLeader = False
    except Exception:
        # Si Revit detecta que el punto está fuera del contorno del elemento
        # anfitrión dejará el líder activo antes de lanzar excepción; lo
        # conservamos en ese caso para no revertir la transacción.
        pass


def center_ar_tags():
    """Punto de entrada principal: filtra la selección, despacha por tipo
    y ejecuta el centrado dentro de una única transacción de Revit.
    """
    doc = revit.doc
    selection = revit.get_selection()

    # Filtrar únicamente los tipos soportados
    supported_types = tuple(_LOCATION_STRATEGIES.keys())
    selected_tags = [
        el for el in selection.elements
        if isinstance(el, supported_types)
    ]

    if not selected_tags:
        forms.alert(
            "Por favor, seleccione al menos un Room Tag o Area Tag.",
            exitscript=True
        )

    with revit.Transaction("Centrar A/R Tags"):
        count   = 0
        skipped = 0

        for tag in selected_tags:
            # Despacho polimórfico: obtener la estrategia según el tipo exacto
            strategy = _LOCATION_STRATEGIES.get(type(tag))
            if strategy is None:
                skipped += 1
                continue

            target_pt = strategy(tag)
            if target_pt is None:
                skipped += 1
                continue

            _move_tag_to_point(tag, target_pt)
            count += 1

    # Resumen de resultados
    msg_parts = ["{} etiqueta(s) centrada(s) correctamente.".format(count)]
    if skipped:
        msg_parts.append("{} etiqueta(s) omitida(s) (sin elemento origen válido).".format(skipped))
    forms.alert(" ".join(msg_parts))


if __name__ == "__main__":
    center_ar_tags()
