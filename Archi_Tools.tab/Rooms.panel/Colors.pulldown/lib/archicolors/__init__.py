# -*- coding: utf-8 -*-
"""Codigo compartido por AssignColors y ColorSchemeManager.

Vive en Colors.pulldown/lib/, que pyRevit agrega al search path de todos los
botones de este pulldown (ver genericcomps.py: cada componente puede tener su
lib/, y esa ruta se propaga a sus sub-componentes). Cada script ademas la
agrega a sys.path a mano, para no depender de ese registro.

Se comparte lo que es identico en ambas herramientas: la paleta de marca, la
lectura de Color Fill Schemes, y los dialogos propios. Lo especifico de cada
una (la tabla RAL, el selector de color, las listas y ventanas principales)
se queda en su propio script.
"""

from archicolors.brand import (BRUSH_GRAPHITE, BRUSH_PALE_SLATE, BRUSH_PORCELAIN,
                               BRUSH_SILVER, BRUSH_TERRACOTTA, BRUSH_TERRACOTTA_HOVER,
                               BRUSH_TRANSPARENT, brush_from_rgb, make_column)

from archicolors.schemes import (CATEGORY_OPTIONS, NO_PATTERN_LABEL, category_for_label,
                                 describe_param,
                                 entry_display_value, entry_raw_value, get_area_schemes,
                                 get_category_id, get_elements, get_param_by_id,
                                 get_schemes_for_category, label_for_bic,
                                 param_display_value, param_raw_value,
                                 fill_pattern_name, get_drafting_fill_patterns,
                                 raw_value_from_storage, scheme_label, view_scheme_id)

from archicolors.dialogs import (ConfirmWindow, ListPickerWindow, SegmentToggle,
                                 show_alert, show_confirm, show_list_picker)

__all__ = [
    # brand
    "BRUSH_GRAPHITE", "BRUSH_PALE_SLATE", "BRUSH_PORCELAIN", "BRUSH_SILVER",
    "BRUSH_TERRACOTTA", "BRUSH_TERRACOTTA_HOVER", "BRUSH_TRANSPARENT",
    "brush_from_rgb", "make_column",
    # schemes
    "CATEGORY_OPTIONS", "category_for_label", "describe_param", "entry_display_value",
    "entry_raw_value", "get_area_schemes", "get_category_id", "get_elements",
    "get_param_by_id", "get_schemes_for_category", "label_for_bic",
    "param_display_value", "param_raw_value", "raw_value_from_storage",
    "scheme_label", "view_scheme_id", "NO_PATTERN_LABEL", "fill_pattern_name",
    "get_drafting_fill_patterns",
    # dialogs
    "ConfirmWindow", "ListPickerWindow", "SegmentToggle", "show_alert", "show_confirm",
    "show_list_picker",
]
