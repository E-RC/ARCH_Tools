# -*- coding: utf-8 -*-
__title__ = "Color Scheme\nManager"
__doc__ = "Limpia valores fantasma y controla la visibilidad de un Color Scheme de Rooms o Areas desde una sola ventana."

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")

import os
import sys

from pyrevit import revit, DB, forms, script, framework

# El paquete archicolors vive en Colors.pulldown/lib/. pyRevit agrega esa
# carpeta al search path por su cuenta, pero se asegura aqui para no depender
# de ese detalle: si ese registro fallara, fallarian las dos herramientas del
# pulldown a la vez.
_LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if _LIB not in sys.path:
    sys.path.append(_LIB)

from archicolors import (BRUSH_GRAPHITE, BRUSH_SILVER, BRUSH_TERRACOTTA, BRUSH_TRANSPARENT,
                         CATEGORY_OPTIONS, SegmentToggle, brush_from_rgb,
                         category_for_label, entry_display_value, entry_raw_value,
                         get_area_schemes, get_elements, get_param_by_id,
                         get_schemes_for_category, label_for_bic, make_column,
                         param_display_value, param_raw_value, scheme_label,
                         show_alert, show_confirm)

doc    = revit.doc
logger = script.get_logger()

PROGRESS_BAR_THRESHOLD = 25
ALERT_LIST_PREVIEW     = 30
PARAM_NAME_SAMPLE_SIZE = 60

# Sobre esta cantidad de entradas se pregunta antes de dibujar la lista: cada
# fila es un Grid de WPF sin virtualizar, y un scheme sobre un parametro
# continuo (una superficie) puede traer miles.
BIG_SCHEME_WARNING = 400


# ═══════════════════════════════════════════════════════════════════════════════
# LECTURA PROPIA DE ESTA HERRAMIENTA
# (lo compartido con AssignColors vive en Colors.pulldown/lib/archicolors)
# ═══════════════════════════════════════════════════════════════════════════════

def get_filterable_param_names(elements):
    """Nombres de parametros con valor, tomados de una muestra de elementos
    (recorrer todos los parametros de miles de Rooms/Areas es innecesario
    solo para descubrir los nombres disponibles). La muestra toma elementos
    distribuidos a lo largo de la lista, no solo los primeros, para no
    perder parametros que solo existan en ciertos niveles o fases."""
    stride = max(1, len(elements) // PARAM_NAME_SAMPLE_SIZE)
    sample = elements[::stride][:PARAM_NAME_SAMPLE_SIZE]
    names  = set()
    for el in sample:
        for p in el.Parameters:
            if p.Definition is not None and p.HasValue:
                names.add(p.Definition.Name)
    return sorted(names)


def format_list_preview(labels):
    if len(labels) <= ALERT_LIST_PREVIEW:
        return "\n".join("- " + l for l in labels)
    shown = labels[:ALERT_LIST_PREVIEW]
    return "\n".join("- " + l for l in shown) + "\n... y {} mas".format(len(labels) - ALERT_LIST_PREVIEW)


class ScanCancelled(Exception):
    """El usuario cancelo un barrido largo desde la barra de progreso."""


def scan_removable(scheme, entries):
    """Valores crudos de las entradas que Revit acepta eliminar.

    Usa CanRemoveEntry, la misma regla que habilita el signo '-' rojo de la
    UI nativa: es la definicion autoritativa de "fantasma".

    Se llama SOLO cuando el usuario pide limpiar, nunca al cargar un scheme.
    Revit parece recorrer los elementos de la categoria en CADA llamada, asi
    que el costo crece como entradas x elementos. En un scheme sobre un
    parametro continuo (ROOM_AREA, por ejemplo) hay una entrada por cada
    valor distinto de superficie: cientos o miles. Correrlo al abrir cada
    scheme dejaba Revit colgado sin salida.

    Devuelve None si el usuario cancela.
    """
    removable = set()
    total = len(entries)
    with forms.ProgressBar(title="Buscando valores fantasma ({value} de {max_value})...",
                           cancellable=True) as pb:
        for i, entry in enumerate(entries):
            if pb.cancelled:
                return None
            if scheme.CanRemoveEntry(entry):
                removable.add(entry_raw_value(entry))
            pb.update_progress(i + 1, total)
    return removable


class ElementIndex(object):
    """Indice de Rooms/Areas del proyecto, solo para la preseleccion.

    Se construye bajo demanda: ver y limpiar entradas no necesita recorrer
    miles de elementos, y hacerlo en cada cambio de combo congelaria la
    ventana en proyectos grandes.
    """

    PROGRESS_STEP = 50

    def __init__(self, bic, area_scheme_id, scheme):
        self.elements = get_elements(bic, area_scheme_id)
        color_param_id = scheme.ParameterDefinition
        self.scheme_value_by_element = {}
        total = len(self.elements)

        # Con barra de progreso sobre el umbral: esto se dispara al abrir un
        # desplegable, y un congelamiento sin explicacion ahi se lee como que
        # la herramienta se colgo.
        if total > PROGRESS_BAR_THRESHOLD:
            with forms.ProgressBar(title="Leyendo elementos ({value} de {max_value})...",
                                   cancellable=True) as pb:
                for i, el in enumerate(self.elements):
                    if pb.cancelled:
                        raise ScanCancelled()
                    self._read(el, color_param_id)
                    if (i + 1) % self.PROGRESS_STEP == 0 or (i + 1) == total:
                        pb.update_progress(i + 1, total)
        else:
            for el in self.elements:
                self._read(el, color_param_id)

        self.param_names = get_filterable_param_names(self.elements)

    def _read(self, element, color_param_id):
        self.scheme_value_by_element[element.Id.IntegerValue] = param_raw_value(
            get_param_by_id(element, color_param_id))


class ChecklistRow(object):
    """Una fila de ChecklistWindow: checkbox + etiqueta."""

    def __init__(self, label, value, checked):
        self.label     = label
        self.value     = value
        self.checked   = checked
        self.checkbox  = None
        self.container = None

    def build(self):
        grid = framework.Controls.Grid()
        grid.Margin = framework.Windows.Thickness(0, 3, 0, 3)
        grid.ColumnDefinitions.Add(make_column("auto"))
        grid.ColumnDefinitions.Add(make_column("star"))

        self.checkbox = framework.Controls.CheckBox()
        self.checkbox.IsChecked = self.checked
        self.checkbox.Margin = framework.Windows.Thickness(0, 0, 8, 0)
        framework.Controls.Grid.SetColumn(self.checkbox, 0)
        grid.Children.Add(self.checkbox)

        label_tb = framework.Controls.TextBlock()
        label_tb.Text = self.label
        label_tb.FontFamily = framework.Media.FontFamily("Lato")
        label_tb.Foreground = BRUSH_GRAPHITE
        label_tb.VerticalAlignment = framework.Windows.VerticalAlignment.Center
        label_tb.TextWrapping = framework.Windows.TextWrapping.Wrap
        framework.Controls.Grid.SetColumn(label_tb, 1)
        grid.Children.Add(label_tb)

        self.container = grid
        return grid

    def matches_filter(self, text):
        return (not text) or (text in self.label.lower())


class ChecklistWindow(forms.WPFWindow):
    """Checklist con buscador y marcar/desmarcar. Unico uso que le queda a
    list_window.xaml: el paso de preseleccion por parametro."""

    def __init__(self, title, subtitle, rows):
        forms.WPFWindow.__init__(self, "list_window.xaml")
        self.rows   = rows
        self.result = None

        self.title_txt.Text = title
        if subtitle:
            self.subtitle_txt.Text = subtitle
        else:
            self.subtitle_txt.Visibility = framework.Windows.Visibility.Collapsed

        for row in self.rows:
            self.rows_panel.Children.Add(row.build())

        self.check_all_btn.Click   += self._on_check_all
        self.uncheck_all_btn.Click += self._on_uncheck_all
        self.apply_btn.Click       += self._on_apply
        self.search_tb.TextChanged += self._on_search_changed
        self.cancel_btn.Click      += self._on_cancel

    def _on_search_changed(self, sender, args):
        text = (self.search_tb.Text or "").strip().lower()
        for row in self.rows:
            row.container.Visibility = (
                framework.Windows.Visibility.Visible if row.matches_filter(text)
                else framework.Windows.Visibility.Collapsed
            )

    def _on_check_all(self, sender, args):
        for row in self.rows:
            if row.container.Visibility == framework.Windows.Visibility.Visible:
                row.checkbox.IsChecked = True

    def _on_uncheck_all(self, sender, args):
        # Ignora el filtro a proposito: desmarcar siempre devuelve a cero, asi
        # no quedan filas marcadas fuera de la vista decidiendo por ti.
        for row in self.rows:
            row.checkbox.IsChecked = False

    def _on_apply(self, sender, args):
        self.result = [(row.value, bool(row.checkbox.IsChecked)) for row in self.rows]
        self.Close()

    def _on_cancel(self, sender, args):
        self.result = None
        self.Close()


def show_checklist(rows_data, title, subtitle=""):
    """rows_data: lista de (label, value, checked). Devuelve lista de
    (value, checked) tras Aplicar, o None si se cancela."""
    rows = sorted(
        [ChecklistRow(label, value, checked) for label, value, checked in rows_data],
        key=lambda r: r.label
    )
    win = ChecklistWindow(title, subtitle, rows)
    win.ShowDialog()
    return win.result


# ═══════════════════════════════════════════════════════════════════════════════
# FILAS DE LA LISTA PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════════

class EntryRow(object):
    """Una entrada del Color Scheme. El checkbox ES su visibilidad: marcado
    significa visible. Nada se escribe hasta pulsar "Aplicar visibilidad"."""

    def __init__(self, entry, label, on_check_changed):
        self.entry            = entry
        self.label            = label
        # Desconocido hasta que el usuario pida limpiar: averiguarlo es caro
        # (ver scan_removable) y no puede pagarse al cargar la lista.
        self.removable        = False
        self.original_visible = bool(entry.IsVisible)
        self.on_check_changed = on_check_changed
        self.conflicted       = False
        self.checkbox         = None
        self.tag_text         = None
        self.container        = None
        self._build()

    def _build(self):
        grid = framework.Controls.Grid()
        grid.Margin = framework.Windows.Thickness(0, 2, 0, 2)
        grid.Background = BRUSH_TRANSPARENT
        grid.ColumnDefinitions.Add(make_column("auto"))
        grid.ColumnDefinitions.Add(make_column("fixed", 26))
        grid.ColumnDefinitions.Add(make_column("star"))
        grid.ColumnDefinitions.Add(make_column("auto"))

        self.checkbox = framework.Controls.CheckBox()
        self.checkbox.IsChecked = self.original_visible
        self.checkbox.Margin = framework.Windows.Thickness(0, 0, 8, 0)
        self.checkbox.ToolTip = "Marcado = visible en el Color Scheme"
        framework.Controls.Grid.SetColumn(self.checkbox, 0)

        swatch = framework.Controls.Border()
        swatch.Width  = 18
        swatch.Height = 18
        swatch.CornerRadius = framework.Windows.CornerRadius(3)
        swatch.BorderBrush = BRUSH_GRAPHITE
        swatch.BorderThickness = framework.Windows.Thickness(1)
        color = self.entry.Color
        if color and color.IsValid:
            swatch.Background = brush_from_rgb((int(color.Red), int(color.Green), int(color.Blue)))
        else:
            swatch.Background = BRUSH_TRANSPARENT
        framework.Controls.Grid.SetColumn(swatch, 1)

        label_tb = framework.Controls.TextBlock()
        label_tb.Text = self.label
        label_tb.FontFamily = framework.Media.FontFamily("Lato")
        label_tb.Foreground = BRUSH_GRAPHITE
        label_tb.VerticalAlignment = framework.Windows.VerticalAlignment.Center
        label_tb.TextTrimming = framework.Windows.TextTrimming.CharacterEllipsis
        framework.Controls.Grid.SetColumn(label_tb, 2)

        self.tag_text = framework.Controls.TextBlock()
        self.tag_text.FontFamily = framework.Media.FontFamily("Lato")
        self.tag_text.FontSize = 10
        self.tag_text.TextAlignment = framework.Windows.TextAlignment.Right
        self.tag_text.VerticalAlignment = framework.Windows.VerticalAlignment.Center
        self.tag_text.Margin = framework.Windows.Thickness(8, 0, 0, 0)
        framework.Controls.Grid.SetColumn(self.tag_text, 3)

        grid.Children.Add(self.checkbox)
        grid.Children.Add(swatch)
        grid.Children.Add(label_tb)
        grid.Children.Add(self.tag_text)

        def on_check(sender, args):
            self.refresh_tag()
            self.on_check_changed()

        self.checkbox.Checked   += on_check
        self.checkbox.Unchecked += on_check

        self.container = grid
        self.refresh_tag()

    @property
    def target_visible(self):
        return bool(self.checkbox.IsChecked)

    def is_dirty(self):
        return self.target_visible != self.original_visible

    def set_target(self, visible, conflicted=False):
        self.conflicted = conflicted
        self.checkbox.IsChecked = visible
        self.refresh_tag()

    def mark_removable(self, removable):
        self.removable = removable
        self.refresh_tag()

    def refresh_tag(self):
        parts = []
        if self.conflicted:
            parts.append("conflicto")
        if self.removable:
            parts.append("fantasma")
        if self.is_dirty():
            parts.append("modificado")
        self.tag_text.Text = "  ·  ".join(parts)
        self.tag_text.Foreground = (BRUSH_TERRACOTTA if (self.conflicted or self.is_dirty())
                                    else BRUSH_SILVER)

    def matches_filter(self, text):
        return (not text) or (text in self.label.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# VENTANA PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════════

class ColorSchemeManagerWindow(forms.WPFWindow):
    """Una sola ventana: elegir el scheme, ver sus entradas, limpiar las
    fantasma y decidir cuales se ven. Las dos acciones que antes eran flujos
    encadenados son ahora dos botones sobre la misma lista, porque son dos
    preguntas sobre la misma tabla: "se usa?" y "se ve?"."""

    def __init__(self):
        forms.WPFWindow.__init__(self, "ui.xaml")
        # _loading tapa los eventos de los combos mientras se repueblan solos.
        self._loading = True
        self.rows = []
        self.current_bic = None
        self.current_nouns = ("elemento", "elementos")
        self.current_scheme = None
        self.current_area_scheme = None
        self._area_schemes_by_name = {}
        self._schemes_by_label = {}
        self._current_scheme_label = None
        self._current_area_scheme_name = None
        self._index = None

        self.category_toggles = {}
        for label, _b, _s, _p in CATEGORY_OPTIONS:
            toggle = SegmentToggle(label, self._on_category_click)
            self.category_toggles[label] = toggle
            self.category_row.Children.Add(toggle.container)

        self.area_scheme_cb.SelectionChanged += self._on_area_scheme_changed
        self.scheme_cb.SelectionChanged      += self._on_scheme_changed
        self.preselect_cb.DropDownOpened     += self._on_preselect_dropdown
        self.preselect_btn.Click             += self._on_preselect
        self.search_tb.TextChanged           += self._on_search_changed
        self.check_all_btn.Click             += self._on_check_all
        self.uncheck_all_btn.Click           += self._on_uncheck_all
        self.clean_btn.Click                 += self._on_clean
        self.apply_btn.Click                 += self._on_apply_visibility
        self.close_btn.Click                 += self._on_close

        self._select_category(CATEGORY_OPTIONS[0][0])
        self._loading = False

    # ── estado ────────────────────────────────────────────────────────────────

    def set_status(self, message):
        self.status_txt.Text = message

    def _area_scheme_id(self):
        return self.current_area_scheme.Id if self.current_area_scheme else None

    def _set_area_scheme_visible(self, visible):
        state = (framework.Windows.Visibility.Visible if visible
                 else framework.Windows.Visibility.Collapsed)
        self.area_scheme_label.Visibility = state
        self.area_scheme_cb.Visibility    = state

    def _dirty_rows(self):
        return [r for r in self.rows if r.is_dirty()]

    def _confirm_discard_pending(self):
        """Los cambios de visibilidad viven solo en las casillas hasta que se
        aplican, y cualquier recarga los borra. Mejor preguntar que perderlos
        en silencio, sobre todo despues de una preseleccion larga."""
        dirty = self._dirty_rows()
        if not dirty:
            return True
        return show_confirm(
            "Hay {} cambio(s) de visibilidad marcados y sin aplicar.\n\n"
            "Si continua se pierden. Continuar?".format(len(dirty)),
            title="Cambios sin aplicar"
        )

    def _invalidate_index(self):
        self._index = None
        was_loading = self._loading
        self._loading = True
        self.preselect_cb.ItemsSource = None
        self._loading = was_loading

    def _ensure_index(self):
        if self._index is None and self.current_scheme is not None:
            try:
                self._index = ElementIndex(self.current_bic, self._area_scheme_id(),
                                           self.current_scheme)
            except ScanCancelled:
                self.set_status("Lectura de elementos cancelada.")
                return None
        return self._index

    # ── cabecera ──────────────────────────────────────────────────────────────

    def _on_category_click(self, label):
        if label == label_for_bic(self.current_bic):
            return
        if not self._confirm_discard_pending():
            return
        self._select_category(label)

    def _select_category(self, label):
        bic, singular, plural = category_for_label(label)
        self.current_bic = bic
        self.current_nouns = (singular, plural)
        for lbl, toggle in self.category_toggles.items():
            toggle.set_active(lbl == label)

        was_loading = self._loading
        self._loading = True
        if bic == DB.BuiltInCategory.OST_Areas:
            area_schemes = get_area_schemes()
            self._area_schemes_by_name = dict((a.Name, a) for a in area_schemes)
            names = sorted(self._area_schemes_by_name.keys())
            self._set_area_scheme_visible(True)
            self.area_scheme_cb.ItemsSource = names
            if names:
                self.area_scheme_cb.SelectedIndex = 0
                self.current_area_scheme = self._area_schemes_by_name[names[0]]
                self._current_area_scheme_name = names[0]
            else:
                self.current_area_scheme = None
                self._current_area_scheme_name = None
        else:
            self._set_area_scheme_visible(False)
            self._area_schemes_by_name = {}
            self.area_scheme_cb.ItemsSource = None
            self.current_area_scheme = None
            self._current_area_scheme_name = None
        self._loading = was_loading

        self._reload_schemes()

    def _on_area_scheme_changed(self, sender, args):
        if self._loading:
            return
        name = self.area_scheme_cb.SelectedItem
        if not name or name == self._current_area_scheme_name:
            return
        if not self._confirm_discard_pending():
            self._loading = True
            self.area_scheme_cb.SelectedItem = self._current_area_scheme_name
            self._loading = False
            return
        self._current_area_scheme_name = name
        self.current_area_scheme = self._area_schemes_by_name.get(name)
        self._reload_schemes()

    def _reload_schemes(self):
        schemes = get_schemes_for_category(self.current_bic, self._area_scheme_id())
        schemes.sort(key=lambda s: s.Name)

        self._schemes_by_label = {}
        labels = []
        for s in schemes:
            label = scheme_label(s)
            if label in self._schemes_by_label:
                # dos schemes pueden compartir nombre y parametro; el id desempata
                label = "{}  [id {}]".format(label, s.Id.IntegerValue)
            self._schemes_by_label[label] = s
            labels.append(label)

        was_loading = self._loading
        self._loading = True
        self.scheme_cb.ItemsSource = labels
        if labels:
            self.scheme_cb.SelectedIndex = 0
            self.current_scheme = self._schemes_by_label[labels[0]]
            self._current_scheme_label = labels[0]
        else:
            self.current_scheme = None
            self._current_scheme_label = None
        self._loading = was_loading

        self.scheme_cb.ToolTip = self._current_scheme_label
        self._invalidate_index()
        self._reload_rows()

    def _on_scheme_changed(self, sender, args):
        if self._loading:
            return
        label = self.scheme_cb.SelectedItem
        if not label or label == self._current_scheme_label:
            return
        if not self._confirm_discard_pending():
            self._loading = True
            self.scheme_cb.SelectedItem = self._current_scheme_label
            self._loading = False
            return
        self._current_scheme_label = label
        self.current_scheme = self._schemes_by_label.get(label)
        self.scheme_cb.ToolTip = label
        self._invalidate_index()
        self._reload_rows()

    # ── lista ─────────────────────────────────────────────────────────────────

    def _show_empty(self, message):
        empty_tb = framework.Controls.TextBlock()
        empty_tb.Text = message
        empty_tb.Foreground = BRUSH_SILVER
        empty_tb.FontStyle = framework.Windows.FontStyles.Italic
        empty_tb.TextWrapping = framework.Windows.TextWrapping.Wrap
        self.rows_panel.Children.Add(empty_tb)

    def _reload_rows(self):
        self.rows_panel.Children.Clear()
        self.rows = []

        if self.current_scheme is None:
            self._show_empty("No hay Color Fill Schemes para esta seleccion.")
            self.set_status("")
            self._refresh_actions()
            return

        entries = list(self.current_scheme.GetEntries())
        if not entries:
            self._show_empty("Este Color Scheme no tiene entradas todavia.")
            self.set_status("")
            self._refresh_actions()
            return

        # Un scheme sobre un parametro continuo (una superficie, por ejemplo)
        # tiene una entrada por valor distinto, y aca se dibuja una fila WPF
        # por entrada, sin virtualizar. Mejor advertir que congelar Revit.
        if len(entries) > BIG_SCHEME_WARNING and not show_confirm(
            "'{}' tiene {} entradas.\n\n"
            "Suele pasar cuando el scheme usa un parametro continuo, como una "
            "superficie: Revit crea una entrada por cada valor distinto.\n\n"
            "Dibujar la lista completa puede tardar. Continuar?"
            .format(self.current_scheme.Name, len(entries)),
            title="Scheme muy grande"
        ):
            self._show_empty(
                "Lista no cargada: '{}' tiene {} entradas.\n"
                "Elija otro Color Scheme, o vuelva a seleccionar este para cargarlo."
                .format(self.current_scheme.Name, len(entries))
            )
            self.set_status("")
            self._refresh_actions()
            return

        rows = [EntryRow(entry, entry_display_value(entry), self._refresh_actions)
                for entry in entries]
        rows.sort(key=lambda r: r.label)

        self.rows = rows
        for row in self.rows:
            self.rows_panel.Children.Add(row.container)

        self._apply_filter()
        self._refresh_actions()

        hidden = sum(1 for r in self.rows if not r.original_visible)
        self.set_status(
            "{} entrada(s) en el scheme, {} oculta(s). Use 'Limpiar valores "
            "fantasma' para detectar las que ya no usa nadie."
            .format(len(self.rows), hidden)
        )

    def _refresh_actions(self):
        # El conteo de fantasmas no se sabe hasta correr el barrido, asi que
        # el boton lo ofrece en vez de anunciarlo, y muestra el numero recien
        # cuando ya se busco.
        stale = sum(1 for r in self.rows if r.removable)
        self.clean_btn.IsEnabled = bool(self.rows)
        self.clean_btn.Content = ("Limpiar valores fantasma..." if stale == 0
                                  else "Limpiar {} fantasma...".format(stale))

        dirty = len(self._dirty_rows())
        self.apply_btn.IsEnabled = dirty > 0
        self.apply_btn.Content = ("Aplicar visibilidad" if dirty == 0
                                  else "Aplicar visibilidad ({})".format(dirty))

        has_rows = bool(self.rows)
        self.check_all_btn.IsEnabled   = has_rows
        self.uncheck_all_btn.IsEnabled = has_rows
        self.preselect_btn.IsEnabled   = has_rows

    def _apply_filter(self):
        text = (self.search_tb.Text or "").strip().lower()
        for row in self.rows:
            row.container.Visibility = (
                framework.Windows.Visibility.Visible if row.matches_filter(text)
                else framework.Windows.Visibility.Collapsed
            )

    def _on_search_changed(self, sender, args):
        self._apply_filter()

    def _on_check_all(self, sender, args):
        for row in self.rows:
            if row.container.Visibility == framework.Windows.Visibility.Visible:
                row.checkbox.IsChecked = True

    def _on_uncheck_all(self, sender, args):
        # Ignora el filtro a proposito, igual que en AssignColors: desmarcar
        # siempre devuelve a cero, para no dejar decisiones fuera de la vista.
        for row in self.rows:
            row.checkbox.IsChecked = False

    # ── preseleccion por parametro ────────────────────────────────────────────

    def _on_preselect_dropdown(self, sender, args):
        """El combo se llena la primera vez que se abre: armar el indice de
        elementos solo tiene sentido si el usuario va a usar esta funcion."""
        if self.preselect_cb.ItemsSource is not None:
            return
        index = self._ensure_index()
        if index is None:
            # Se cancelo la lectura: dejar ItemsSource en None para que el
            # proximo despliegue reintente. Ponerle una lista vacia dejaria
            # el combo muerto hasta cambiar de scheme, porque el guard de
            # arriba no volveria a entrar.
            return
        was_loading = self._loading
        self._loading = True
        self.preselect_cb.ItemsSource = index.param_names
        self._loading = was_loading

    def _on_preselect(self, sender, args):
        param_name = self.preselect_cb.SelectedItem
        if not param_name:
            show_alert("Elija primero un parametro en la lista de al lado.",
                       title="Sin parametro")
            return

        index = self._ensure_index()
        if index is None or not index.elements:
            show_alert("No hay {} en el proyecto para esta seleccion."
                       .format(self.current_nouns[1]), title="Sin elementos")
            return

        row_by_scheme_value = dict((entry_raw_value(r.entry), r) for r in self.rows)

        # Un valor del parametro de filtro (ej. "Nivel 1") puede tocar varias
        # entradas del scheme, y una entrada puede ser tocada por varios
        # valores: la relacion es N:M, y de ahi salen los conflictos de abajo.
        rows_by_value   = {}
        labels_by_value = {}
        counts_by_value = {}
        for el in index.elements:
            param = el.LookupParameter(param_name)
            label = param_display_value(param)
            if not label:
                continue
            raw = param_raw_value(param)
            labels_by_value.setdefault(raw, label)
            counts_by_value[raw] = counts_by_value.get(raw, 0) + 1
            row = row_by_scheme_value.get(index.scheme_value_by_element.get(el.Id.IntegerValue))
            if row is not None:
                rows_by_value.setdefault(raw, set()).add(row)

        if not labels_by_value:
            show_alert("El parametro '{}' no tiene valores en estos {}."
                       .format(param_name, self.current_nouns[1]), title="Sin valores")
            return

        singular, plural = self.current_nouns

        def currently_visible(raw):
            touched = rows_by_value.get(raw)
            return all(r.target_visible for r in touched) if touched else True

        rows_data = []
        for raw, label in labels_by_value.items():
            count = counts_by_value.get(raw, 0)
            rows_data.append((
                "{}   ({} {})".format(label, count, singular if count == 1 else plural),
                raw,
                currently_visible(raw),
            ))

        results = show_checklist(
            rows_data,
            title="Marque los valores que deben quedar visibles",
            subtitle="'{}'  -  {} valor(es). Esto solo mueve las casillas de la lista; "
                     "nada se escribe hasta pulsar Aplicar visibilidad."
                     .format(param_name, len(rows_data))
        )
        if results is None:
            return

        # Una misma entrada puede recibir instrucciones opuestas (el mismo
        # color existe en Nivel 1 y en Nivel 2, filtrando por Nivel). La
        # visibilidad es UNA bandera por entrada, asi que gana VISIBLE, que es
        # lo menos destructivo -- y la fila queda marcada como "conflicto"
        # para poder corregirla a mano antes de aplicar.
        target     = {}
        conflicted = set()
        for raw, want_visible in results:
            for row in rows_by_value.get(raw, ()):
                if row in target and target[row] != want_visible:
                    conflicted.add(row)
                    target[row] = True
                else:
                    target[row] = want_visible

        for row in self.rows:
            row.conflicted = False
            row.refresh_tag()
        for row, want_visible in target.items():
            row.set_target(want_visible, conflicted=(row in conflicted))

        self._refresh_actions()

        message = "Preseleccion por '{}': {} de {} entradas afectadas.".format(
            param_name, len(target), len(self.rows))
        if conflicted:
            message += (" {} quedaron VISIBLES por conflicto (el mismo color aparece en "
                        "grupos con instrucciones opuestas); revise las filas marcadas."
                        .format(len(conflicted)))
        message += " Nada se ha escrito todavia."
        self.set_status(message)

    # ── acciones que escriben ─────────────────────────────────────────────────

    def _on_apply_visibility(self, sender, args):
        dirty = self._dirty_rows()
        if not dirty:
            return
        count = len(dirty)
        with revit.Transaction("ARCH - Filtrar visibilidad Color Scheme"):
            for row in dirty:
                row.entry.IsVisible = row.target_visible
                self.current_scheme.UpdateEntry(row.entry)
        self._reload_rows()
        self.set_status("Visibilidad actualizada en {} entrada(s).".format(count))

    def _on_clean(self, sender, args):
        if not self.rows:
            return

        removable = scan_removable(self.current_scheme, [r.entry for r in self.rows])
        if removable is None:
            self.set_status("Busqueda de valores fantasma cancelada.")
            return

        for row in self.rows:
            row.mark_removable(entry_raw_value(row.entry) in removable)
        self._refresh_actions()

        stale = [r for r in self.rows if r.removable]
        if not stale:
            self.set_status("No se encontraron valores eliminables en '{}'."
                            .format(self.current_scheme.Name))
            return

        if not self._confirm_discard_pending():
            return

        if not show_confirm(
            "Se eliminaran {} valor(es) de '{}':\n\n{}".format(
                len(stale), self.current_scheme.Name,
                format_list_preview([r.label for r in stale])),
            title="Confirmar limpieza"
        ):
            return

        removed = 0
        blocked = []
        with revit.Transaction("ARCH - Limpiar Color Scheme"):
            for row in stale:
                try:
                    self.current_scheme.RemoveEntry(row.entry)
                    removed += 1
                except Exception as ex:
                    logger.debug("No se pudo eliminar entrada: {}".format(ex))
                    blocked.append(row.label)

        self._invalidate_index()
        self._reload_rows()

        if blocked:
            show_alert(
                "Eliminados {} valor(es). Estos no se pudieron quitar:\n\n{}"
                .format(removed, format_list_preview(blocked)),
                title="Limpieza parcial"
            )
        else:
            self.set_status("Eliminados {} valor(es) fantasma.".format(removed))

    def _on_close(self, sender, args):
        if not self._confirm_discard_pending():
            return
        self.Close()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    has_any_scheme = any(get_schemes_for_category(bic) for _l, bic, _s, _p in CATEGORY_OPTIONS)
    if not has_any_scheme:
        show_alert(
            "Este proyecto no tiene ningun Color Fill Scheme definido para Rooms ni "
            "para Areas. Cree uno primero desde el dialogo nativo de Revit.",
            title="Sin Color Schemes"
        )
        return

    ColorSchemeManagerWindow().ShowDialog()


main()
