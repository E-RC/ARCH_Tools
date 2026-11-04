# -*- coding: utf-8 -*-
"""Formato de las notas de cambios que muestra el boton Actualizar."""
from __future__ import unicode_literals


def format_notes(messages, limit=10):
    """Una linea '- mensaje' por cambio; si sobran, cierra con '... y N cambios mas'."""
    lines = ["- " + m for m in messages[:limit]]
    if len(messages) > limit:
        lines.append("\u2026 y %d cambios m\u00e1s" % (len(messages) - limit))
    return "\n".join(lines)
