# -*- coding: utf-8 -*-
"""Paleta de marca ARCHIPLAN y utilidades de WPF.

Estos brushes son para los elementos que se dibujan por codigo (swatches,
bordes, hover); los XAML estaticos repiten los mismos valores en hexadecimal.
Si cambia la marca, hay que tocar ambos lados.

  White Smoke     #F5F5F4
  Graphite        #333333
  Terracotta Clay #B26E63
  Porcelain       #FFFFF9
  Silver          #A1A1A0
  Pale Slate      #C7C5CD
"""

from pyrevit import framework

BRUSH_GRAPHITE         = framework.Media.SolidColorBrush(framework.Media.Color.FromRgb(0x33, 0x33, 0x33))
BRUSH_SILVER           = framework.Media.SolidColorBrush(framework.Media.Color.FromRgb(0xA1, 0xA1, 0xA0))
BRUSH_PORCELAIN        = framework.Media.SolidColorBrush(framework.Media.Color.FromRgb(0xFF, 0xFF, 0xF9))
BRUSH_PALE_SLATE       = framework.Media.SolidColorBrush(framework.Media.Color.FromRgb(0xC7, 0xC5, 0xCD))
BRUSH_TERRACOTTA       = framework.Media.SolidColorBrush(framework.Media.Color.FromRgb(0xB2, 0x6E, 0x63))
BRUSH_TERRACOTTA_HOVER = framework.Media.SolidColorBrush(framework.Media.Color.FromArgb(35, 0xB2, 0x6E, 0x63))
BRUSH_TRANSPARENT      = framework.Media.Brushes.Transparent


def brush_from_rgb(rgb):
    return framework.Media.SolidColorBrush(
        framework.Media.Color.FromRgb(rgb[0], rgb[1], rgb[2])
    )


def make_column(width_kind, value=None):
    """ColumnDefinition por tipo: "auto", "star" o "fixed" con ancho."""
    col = framework.Controls.ColumnDefinition()
    if width_kind == "auto":
        col.Width = framework.Windows.GridLength.Auto
    elif width_kind == "star":
        col.Width = framework.Windows.GridLength(1, framework.Windows.GridUnitType.Star)
    else:
        col.Width = framework.Windows.GridLength(value)
    return col
