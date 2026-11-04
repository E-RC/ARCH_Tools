# -*- coding: utf-8 -*-
"""Dialogos y controles con la marca ARCHIPLAN.

Reemplazan alert/toast de pyRevit, que no se pueden re-pintar, para que las
herramientas del pulldown se sientan como una sola app.
"""

import os

from pyrevit import forms, framework

from archicolors.brand import (BRUSH_GRAPHITE, BRUSH_PALE_SLATE, BRUSH_PORCELAIN,
                               BRUSH_TERRACOTTA, BRUSH_TERRACOTTA_HOVER)

# Ruta ABSOLUTA a proposito: pyRevit resuelve un nombre de XAML relativo contra
# la carpeta del boton que se ejecuta (EXEC_PARAMS.command_path), no contra la
# del modulo, asi que un nombre pelado buscaria este XAML donde no esta. Con
# ruta absoluta lo toma tal cual.
_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIRM_XAML     = os.path.join(_HERE, "confirm_window.xaml")
LIST_PICKER_XAML = os.path.join(_HERE, "list_picker.xaml")


class ConfirmWindow(forms.WPFWindow):
    """Reemplaza forms.alert (OK) y forms.alert(yes=True, no=True)."""

    def __init__(self, title, message, yes_no):
        forms.WPFWindow.__init__(self, CONFIRM_XAML)
        self.result = False
        self.title_txt.Text = title
        self.message_txt.Text = message
        if yes_no:
            self.ok_btn.Content = "Si"
        else:
            self.no_btn.Visibility = framework.Windows.Visibility.Collapsed
        self.ok_btn.Click += self._on_ok
        self.no_btn.Click += self._on_no

    def _on_ok(self, sender, args):
        self.result = True
        self.Close()

    def _on_no(self, sender, args):
        self.result = False
        self.Close()


def show_alert(message, title="ARCHIPLAN"):
    ConfirmWindow(title, message, yes_no=False).ShowDialog()


def show_confirm(message, title="ARCHIPLAN"):
    win = ConfirmWindow(title, message, yes_no=True)
    win.ShowDialog()
    return win.result


class _PickerRow(object):
    """Fila de ListPickerWindow: clic elige de inmediato, sin boton Aceptar."""

    def __init__(self, label, value, on_pick):
        self.label = label
        self.value = value
        self.container = self._build(on_pick)

    def _build(self, on_pick):
        grid = framework.Controls.Grid()
        grid.Margin = framework.Windows.Thickness(0, 2, 0, 2)
        grid.Background = framework.Media.Brushes.Transparent
        try:
            grid.Cursor = framework.Input.Cursors.Hand
        except Exception:
            pass

        label_tb = framework.Controls.TextBlock()
        label_tb.Text = self.label
        label_tb.FontFamily = framework.Media.FontFamily("Lato")
        label_tb.Foreground = BRUSH_GRAPHITE
        label_tb.VerticalAlignment = framework.Windows.VerticalAlignment.Center
        label_tb.TextWrapping = framework.Windows.TextWrapping.Wrap
        grid.Children.Add(label_tb)

        def on_enter(sender, args):
            grid.Background = BRUSH_TERRACOTTA_HOVER

        def on_leave(sender, args):
            grid.Background = framework.Media.Brushes.Transparent

        def on_click(sender, args):
            on_pick(self.value)

        grid.MouseEnter += on_enter
        grid.MouseLeave += on_leave
        grid.MouseLeftButtonUp += on_click
        return grid

    def matches_filter(self, text):
        return (not text) or (text in self.label.lower())


class ListPickerWindow(forms.WPFWindow):
    """Lista de una sola eleccion con buscador. Reemplaza
    forms.SelectFromList, que no se puede re-pintar con la marca."""

    def __init__(self, title, subtitle, rows_data):
        forms.WPFWindow.__init__(self, LIST_PICKER_XAML)
        self.result = None
        self.picked = False

        self.title_txt.Text = title
        if subtitle:
            self.subtitle_txt.Text = subtitle
        else:
            self.subtitle_txt.Visibility = framework.Windows.Visibility.Collapsed

        self.rows = [_PickerRow(label, value, self._on_pick) for label, value in rows_data]
        for row in self.rows:
            self.rows_panel.Children.Add(row.container)

        self.search_tb.TextChanged += self._on_search_changed
        self.cancel_btn.Click      += self._on_cancel

    def _on_pick(self, value):
        # picked distingue "eligio la primera opcion" de "cancelo", que en
        # este picker pueden compartir valor (None = sin trama).
        self.result = value
        self.picked = True
        self.Close()

    def _on_search_changed(self, sender, args):
        text = (self.search_tb.Text or "").strip().lower()
        for row in self.rows:
            row.container.Visibility = (
                framework.Windows.Visibility.Visible if row.matches_filter(text)
                else framework.Windows.Visibility.Collapsed
            )

    def _on_cancel(self, sender, args):
        self.picked = False
        self.Close()


def show_list_picker(rows_data, title, subtitle=""):
    """rows_data: lista de (label, value).

    Devuelve (elegido, valor): elegido es False si se cancelo, para poder
    distinguirlo de haber elegido una opcion cuyo valor es None.
    """
    win = ListPickerWindow(title, subtitle, rows_data)
    win.ShowDialog()
    return win.picked, win.result


class SegmentToggle(object):
    """Boton tipo "segmented control".

    Lo usan la categoria (Rooms/Areas) y el alcance de la lista: decisiones
    que deben quedar a la vista y ser reversibles en cualquier momento, en
    vez de ser pasos previos que despues ya no se ven.
    """

    def __init__(self, label, on_click, compact=False):
        self.label     = label
        self.text      = None
        self.container = None
        self._build(on_click, compact)

    def _build(self, on_click, compact):
        border = framework.Controls.Border()
        border.BorderThickness = framework.Windows.Thickness(1)
        border.CornerRadius = framework.Windows.CornerRadius(2)
        if compact:
            border.Padding = framework.Windows.Thickness(10, 4, 10, 4)
        else:
            border.Padding = framework.Windows.Thickness(16, 5, 16, 5)
        border.Margin = framework.Windows.Thickness(0, 0, 6, 0)
        try:
            border.Cursor = framework.Input.Cursors.Hand
        except Exception:
            pass

        text = framework.Controls.TextBlock()
        text.Text = self.label
        text.FontFamily = framework.Media.FontFamily("Lato")
        text.FontSize = 11 if compact else 12
        self.text = text
        border.Child = text

        border.MouseLeftButtonUp += lambda sender, args: on_click(self.label)
        # self.container debe existir ANTES de llamar set_active, que lo usa.
        self.container = border
        self.set_active(False)

    def set_active(self, active):
        if active:
            self.container.Background  = BRUSH_TERRACOTTA
            self.container.BorderBrush = BRUSH_TERRACOTTA
            self.text.Foreground       = BRUSH_PORCELAIN
        else:
            self.container.Background  = BRUSH_PORCELAIN
            self.container.BorderBrush = BRUSH_PALE_SLATE
            self.text.Foreground       = BRUSH_GRAPHITE
