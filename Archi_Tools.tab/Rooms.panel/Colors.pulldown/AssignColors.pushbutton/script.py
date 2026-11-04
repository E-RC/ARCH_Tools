# -*- coding: utf-8 -*-
__title__ = "Assign\nColors"
__doc__ = "Asigna colores a un Color Fill Scheme de Rooms o Areas, todo desde una sola ventana."

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")

import os
import sys

from pyrevit import revit, DB, forms, script, framework

# palette.py vive junto a este script; el paquete archicolors, en
# Colors.pulldown/lib/. pyRevit agrega ambas carpetas al search path por su
# cuenta, pero se aseguran aqui para no depender de ese detalle: si fallara,
# fallarian las dos herramientas del pulldown a la vez.
_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB  = os.path.join(os.path.dirname(_HERE), "lib")
for _path in (_HERE, _LIB):
    if _path not in sys.path:
        sys.path.append(_path)

from palette import expand_hex, format_hex, parse_hex_input, parse_palette_file

from archicolors import (BRUSH_GRAPHITE, BRUSH_PALE_SLATE, BRUSH_PORCELAIN, BRUSH_SILVER,
                         BRUSH_TERRACOTTA, BRUSH_TERRACOTTA_HOVER, BRUSH_TRANSPARENT,
                         CATEGORY_OPTIONS, NO_PATTERN_LABEL, SegmentToggle,
                         brush_from_rgb, category_for_label, entry_display_value,
                         entry_raw_value, fill_pattern_name, get_area_schemes,
                         get_category_id, get_drafting_fill_patterns, get_elements,
                         get_param_by_id, get_schemes_for_category, label_for_bic,
                         make_column, param_raw_value, scheme_label, show_alert,
                         show_confirm, show_list_picker, view_scheme_id)

doc    = revit.doc
logger = script.get_logger()

SCOPE_VIEW = "Visibles en la vista"
SCOPE_ALL  = "Todo el scheme"

# Sobre esta cantidad de valores se pregunta antes de dibujar la lista: cada
# fila es un Grid de WPF sin virtualizar, y un scheme sobre un parametro
# continuo (una superficie) puede traer miles.
BIG_SCHEME_WARNING = 400


# ═══════════════════════════════════════════════════════════════════════════════
# LECTURA PROPIA DE ESTA HERRAMIENTA
# (lo compartido con ColorSchemeManager vive en Colors.pulldown/lib/archicolors)
# ═══════════════════════════════════════════════════════════════════════════════

def detect_from_active_view():
    """(bic, area_scheme_id, scheme) segun lo que la vista activa ya aplica.

    Sirve para precargar la cabecera: abrir la herramienta sobre una planta
    ya coloreada deberia mostrar ese scheme, no hacerlo buscar de nuevo.
    """
    for _label, bic, _s, _p in CATEGORY_OPTIONS:
        sid = view_scheme_id(bic)
        if not sid or sid == DB.ElementId.InvalidElementId:
            continue
        scheme = doc.GetElement(sid)
        if not isinstance(scheme, DB.ColorFillScheme):
            continue
        area_scheme_id = None
        if bic == DB.BuiltInCategory.OST_Areas:
            asid = scheme.AreaSchemeId
            if asid and asid != DB.ElementId.InvalidElementId:
                area_scheme_id = asid
        return bic, area_scheme_id, scheme
    return None


def build_rows_data(scheme, bic, area_scheme_id, view_only):
    """Una fila por entrada del scheme, con cuantos elementos la usan.

    En "Visibles en la vista" quedan solo las entradas que algun elemento
    visible esta usando (el comportamiento historico de esta herramienta);
    en "Todo el scheme" salen todas, y un contador en 0 delata los valores
    fantasma que ya no usa nadie.
    """
    color_param_id = scheme.ParameterDefinition
    elements = get_elements(bic, area_scheme_id, view_only)

    counts = {}
    for el in elements:
        value = param_raw_value(get_param_by_id(el, color_param_id))
        if value is not None:
            counts[value] = counts.get(value, 0) + 1

    rows_data = []
    known = set()
    for entry in scheme.GetEntries():
        raw = entry_raw_value(entry)
        known.add(raw)
        count = counts.get(raw, 0)
        if view_only and count == 0:
            continue
        rows_data.append({"entry": entry, "label": entry_display_value(entry), "count": count})

    orphans = [v for v in counts if v not in known]
    if orphans:
        logger.debug("{} valor(es) en uso sin entrada en el scheme todavia.".format(len(orphans)))

    rows_data.sort(key=lambda d: d["label"])
    return rows_data

# ═══════════════════════════════════════════════════════════════════════════════
# PALETA RAL CLASSIC (valores de referencia PUBLICOS, no oficiales)
#
# Revit trae sus propios libros RAL/Pantone bajo licencia de Autodesk, pero los
# almacena en XML deliberadamente ofuscado (C:\ProgramData\Autodesk\RVT <ver>\
# Colorbooks\*.acb) para proteger esos datos comerciales; no se leen aqui.
#
# Esta tabla es la tabla RAL Classic publica de Wikipedia (CC BY-SA):
# https://en.wikipedia.org/wiki/List_of_RAL_colors
# Son aproximaciones sRGB de uso general, NO los valores oficiales calibrados
# de RAL. Suficiente para elegir un color de referencia rapido, no para
# especificaciones de pintura/fabricacion.
# ═══════════════════════════════════════════════════════════════════════════════

RAL_CLASSIC = [
    ("1000", "Green beige", "CDBA88"), ("1001", "Beige", "D0B084"),
    ("1002", "Sand yellow", "D2AA6D"), ("1003", "Signal yellow", "F9A800"),
    ("1004", "Golden yellow", "E49E00"), ("1005", "Honey yellow", "CB8E00"),
    ("1006", "Maize yellow", "E29000"), ("1007", "Daffodil yellow", "E88C00"),
    ("1011", "Brown beige", "AF804F"), ("1012", "Lemon yellow", "DDAF27"),
    ("1013", "Oyster white", "E3D9C6"), ("1014", "Ivory", "DDC49A"),
    ("1015", "Light ivory", "E6D2B5"), ("1016", "Sulfur yellow", "F1DD38"),
    ("1017", "Saffron yellow", "F6A950"), ("1018", "Zinc yellow", "FACA30"),
    ("1019", "Grey beige", "A48F7A"), ("1020", "Olive yellow", "A08F65"),
    ("1021", "Colza yellow", "F6B600"), ("1023", "Traffic yellow", "F7B500"),
    ("1024", "Ochre yellow", "BA8F4C"), ("1026", "Luminous yellow", "FFFF00"),
    ("1027", "Curry", "A77F0E"), ("1028", "Melon yellow", "FF9B00"),
    ("1032", "Broom yellow", "E2A300"), ("1033", "Dahlia yellow", "F99A1C"),
    ("1034", "Pastel yellow", "EB9C52"), ("1035", "Pearl beige", "908370"),
    ("1036", "Pearl gold", "80643F"), ("1037", "Sun yellow", "F09200"),
    ("2000", "Yellow orange", "DD7907"), ("2001", "Red orange", "BE4E20"),
    ("2002", "Vermilion", "C63927"), ("2003", "Pastel orange", "FA842B"),
    ("2004", "Pure orange", "E75B12"), ("2005", "Luminous orange", "FF2300"),
    ("2007", "Luminous bright orange", "FFA421"), ("2008", "Bright red orange", "F3752C"),
    ("2009", "Traffic orange", "E15501"), ("2010", "Signal orange", "D4652F"),
    ("2011", "Deep orange", "EC7C25"), ("2012", "Salmon orange", "DB6A50"),
    ("2013", "Pearl orange", "954527"), ("2017", "RAL orange", "FA4402"),
    ("3000", "Flame red", "AB2524"), ("3001", "Signal red", "A02128"),
    ("3002", "Carmine red", "A1232B"), ("3003", "Ruby red", "8D1D2C"),
    ("3004", "Purple red", "701F29"), ("3005", "Wine red", "5E2028"),
    ("3007", "Black red", "402225"), ("3009", "Oxide red", "703731"),
    ("3011", "Brown red", "7E292C"), ("3012", "Beige red", "CB8D73"),
    ("3013", "Tomato red", "9C322E"), ("3014", "Antique pink", "D47479"),
    ("3015", "Light pink", "E1A6AD"), ("3016", "Coral red", "AC4034"),
    ("3017", "Rose", "D3545F"), ("3018", "Strawberry red", "D14152"),
    ("3020", "Traffic red", "C1121C"), ("3022", "Salmon pink", "D56D56"),
    ("3024", "Luminous red", "F70000"), ("3026", "Luminous bright red", "FF0000"),
    ("3027", "Raspberry red", "B42041"), ("3028", "Pure red", "E72512"),
    ("3031", "Orient red", "AC323B"), ("3032", "Pearl ruby red", "711521"),
    ("3033", "Pearl pink", "B24C43"), ("4001", "Red lilac", "8A5A83"),
    ("4002", "Red violet", "933D50"), ("4003", "Heather violet", "D15B8F"),
    ("4004", "Claret violet", "691639"), ("4005", "Blue lilac", "83639D"),
    ("4006", "Traffic purple", "992572"), ("4007", "Purple violet", "4A203B"),
    ("4008", "Signal violet", "904684"), ("4009", "Pastel violet", "A38995"),
    ("4010", "Telemagenta", "C63678"), ("4011", "Pearl violet", "8773A1"),
    ("4012", "Pearl blackberry", "6B6880"), ("5000", "Violet blue", "384C70"),
    ("5001", "Green blue", "1F4764"), ("5002", "Ultramarine blue", "2B2C7C"),
    ("5003", "Sapphire blue", "2A3756"), ("5004", "Black blue", "1D1F2A"),
    ("5005", "Signal blue", "154889"), ("5007", "Brilliant blue", "41678D"),
    ("5008", "Grey blue", "313C48"), ("5009", "Azure blue", "2E5978"),
    ("5010", "Gentian blue", "13447C"), ("5011", "Steel blue", "232C3F"),
    ("5012", "Light blue", "3481B8"), ("5013", "Cobalt blue", "232D53"),
    ("5014", "Pigeon blue", "6C7C98"), ("5015", "Sky blue", "2874B2"),
    ("5017", "Traffic blue", "0E518D"), ("5018", "Turquoise blue", "21888F"),
    ("5019", "Capri blue", "1A5784"), ("5020", "Ocean blue", "0B4151"),
    ("5021", "Water blue", "07737A"), ("5022", "Night blue", "2F2A5A"),
    ("5023", "Distant blue", "4D668E"), ("5024", "Pastel blue", "6A93B0"),
    ("5025", "Pearl Gentian blue", "296478"), ("5026", "Pearl night blue", "102C54"),
    ("6000", "Patina green", "327662"), ("6001", "Emerald green", "28713E"),
    ("6002", "Leaf green", "276235"), ("6003", "Olive green", "4B573E"),
    ("6004", "Blue green", "0E4243"), ("6005", "Moss green", "0F4336"),
    ("6006", "Grey olive", "40433B"), ("6007", "Bottle green", "283424"),
    ("6008", "Brown green", "35382E"), ("6009", "Fir green", "26392F"),
    ("6010", "Grass green", "3E753B"), ("6011", "Reseda green", "68825B"),
    ("6012", "Black green", "31403D"), ("6013", "Reed green", "797C5A"),
    ("6014", "Yellow olive", "444337"), ("6015", "Black olive", "3D403A"),
    ("6016", "Turquoise green", "026A52"), ("6017", "May green", "468641"),
    ("6018", "Yellow green", "48A43F"), ("6019", "Pastel green", "B7D9B1"),
    ("6020", "Chrome green", "354733"), ("6021", "Pale green", "86A47C"),
    ("6022", "Olive-drab", "3E3C32"), ("6024", "Traffic green", "008754"),
    ("6025", "Fern green", "53753C"), ("6026", "Opal green", "005D52"),
    ("6027", "Light green", "81C0BB"), ("6028", "Pine green", "2D5546"),
    ("6029", "Mint green", "007243"), ("6031", "Bronze green", "1B542C"),
    ("6032", "Signal green", "0F8558"), ("6033", "Mint turquoise", "478A84"),
    ("6034", "Pastel turquoise", "7FB0B2"), ("6035", "Pearl green", "1B542C"),
    ("6036", "Pearl opal green", "005D4C"), ("6037", "Pure green", "25E712"),
    ("6038", "Luminous green", "00F700"), ("7000", "Squirrel grey", "7E8B92"),
    ("7001", "Silver grey", "8F999F"), ("7002", "Olive grey", "817F68"),
    ("7003", "Moss grey", "7A7B6D"), ("7004", "Signal grey", "9EA0A1"),
    ("7005", "Mouse grey", "6B716F"), ("7006", "Beige grey", "756F61"),
    ("7008", "Khaki grey", "746643"), ("7009", "Green grey", "5B6259"),
    ("7010", "Tarpaulin grey", "575D57"), ("7011", "Iron grey", "555D61"),
    ("7012", "Basalt grey", "596163"), ("7013", "Brown grey", "555548"),
    ("7015", "Slate grey", "51565C"), ("7016", "Anthracite grey", "373F43"),
    ("7021", "Black grey", "2E3234"), ("7022", "Umbra grey", "4B4D46"),
    ("7023", "Concrete grey", "818479"), ("7024", "Graphite grey", "474A50"),
    ("7026", "Granite grey", "374447"), ("7030", "Stone grey", "939388"),
    ("7031", "Blue grey", "5D6970"), ("7032", "Pebble grey", "B9B9A8"),
    ("7033", "Cement grey", "818979"), ("7034", "Yellow grey", "939176"),
    ("7035", "Light grey", "CBD0CC"), ("7036", "Platinum grey", "9A9697"),
    ("7037", "Dusty grey", "7C7F7E"), ("7038", "Agate grey", "B4B8B0"),
    ("7039", "Quartz grey", "6B695F"), ("7040", "Window grey", "9DA3A6"),
    ("7042", "Traffic grey A", "8F9695"), ("7043", "Traffic grey B", "4E5451"),
    ("7044", "Silk grey", "BDBDB2"), ("7045", "Telegrey 1", "91969A"),
    ("7046", "Telegrey 2", "82898E"), ("7047", "Telegrey 4", "CFD0CF"),
    ("7048", "Pearl mouse grey", "888175"), ("8000", "Green brown", "887142"),
    ("8001", "Ochre brown", "9C6B30"), ("8002", "Signal brown", "7B5141"),
    ("8003", "Clay brown", "80542F"), ("8004", "Copper brown", "8F4E35"),
    ("8007", "Fawn brown", "6F4A2F"), ("8008", "Olive brown", "6F4F28"),
    ("8011", "Nut brown", "5A3A29"), ("8012", "Red brown", "673831"),
    ("8014", "Sepia brown", "49392D"), ("8015", "Chestnut brown", "633A34"),
    ("8016", "Mahogany brown", "4C2F26"), ("8017", "Chocolate brown", "44322D"),
    ("8019", "Grey brown", "3F3A3A"), ("8022", "Black brown", "211F20"),
    ("8023", "Orange brown", "A65E2F"), ("8024", "Beige brown", "79553C"),
    ("8025", "Pale brown", "755C49"), ("8028", "Terra brown", "4E3B2B"),
    ("8029", "Pearl copper", "773C27"), ("9001", "Cream", "EFEBDC"),
    ("9002", "Grey white", "DDDED4"), ("9003", "Signal white", "F4F8F4"),
    ("9004", "Signal black", "2E3032"), ("9005", "Jet black", "0A0A0D"),
    ("9006", "White aluminium", "A5A8A6"), ("9007", "Grey aluminium", "8F8F8C"),
    ("9010", "Pure white", "F7F9EF"), ("9011", "Graphite black", "1C1C1C"),
    ("9012", "Clean room white", "FFFDE6"), ("9016", "Traffic white", "F7FBF5"),
    ("9017", "Traffic black", "2A2D2F"), ("9018", "Papyrus white", "CFD3CD"),
    ("9022", "Pearl light grey", "858583"), ("9023", "Pearl dark grey", "7E8182"),
]


class RalColorOption(object):
    def __init__(self, code, color_name, hexval):
        self.code = code
        self.color_name = color_name
        self.rgb = expand_hex(hexval)
        self.name = "RAL {}  -  {}   (#{})".format(code, color_name, hexval)


RAL_OPTIONS = [RalColorOption(code, name, hexval) for code, name, hexval in RAL_CLASSIC]

# RGB -> "RAL nnnn", para marcar en la lista principal cuando un color
# coincide exactamente con uno de la paleta. Un par de codigos RAL Classic
# comparten el mismo hex de referencia (dato de la tabla fuente); en ese
# caso se muestra el que aparece ultimo en RAL_CLASSIC.
RAL_CODE_BY_RGB = {opt.rgb: "RAL " + opt.code for opt in RAL_OPTIONS}


# ═══════════════════════════════════════════════════════════════════════════════
# SELECTOR DE COLOR UNIFICADO
# ═══════════════════════════════════════════════════════════════════════════════

class RalRow(object):
    """Una fila clickeable del listado RAL: swatch + nombre, elige al hacer clic."""

    def __init__(self, option, on_pick):
        self.option = option
        self.container = self._build(on_pick)

    def _build(self, on_pick):
        grid = framework.Controls.Grid()
        grid.Margin = framework.Windows.Thickness(0, 1, 0, 1)
        grid.Background = BRUSH_TRANSPARENT
        try:
            grid.Cursor = framework.Input.Cursors.Hand
        except Exception:
            pass
        grid.ColumnDefinitions.Add(make_column("fixed", 22))
        grid.ColumnDefinitions.Add(make_column("star"))

        swatch = framework.Controls.Border()
        swatch.Width  = 18
        swatch.Height = 18
        swatch.CornerRadius = framework.Windows.CornerRadius(3)
        swatch.BorderBrush = BRUSH_GRAPHITE
        swatch.BorderThickness = framework.Windows.Thickness(1)
        swatch.Margin = framework.Windows.Thickness(2, 0, 8, 0)
        swatch.Background = brush_from_rgb(self.option.rgb)
        framework.Controls.Grid.SetColumn(swatch, 0)

        label = framework.Controls.TextBlock()
        label.Text = self.option.name
        label.FontFamily = framework.Media.FontFamily("Lato")
        label.Foreground = BRUSH_GRAPHITE
        label.VerticalAlignment = framework.Windows.VerticalAlignment.Center
        framework.Controls.Grid.SetColumn(label, 1)

        grid.Children.Add(swatch)
        grid.Children.Add(label)

        def on_enter(sender, args):
            grid.Background = BRUSH_TERRACOTTA_HOVER

        def on_leave(sender, args):
            grid.Background = BRUSH_TRANSPARENT

        def on_click(sender, args):
            on_pick(self.option)

        grid.MouseEnter += on_enter
        grid.MouseLeave += on_leave
        grid.MouseLeftButtonUp += on_click

        return grid

    def matches_filter(self, text):
        return (not text) or (text in self.option.name.lower())


class ColorPickerWindow(forms.WPFWindow):
    """Hex, selector de Windows y paleta RAL en una sola ventana.

    Antes esto eran tres pasos encadenados (marcar, elegir "origen del
    color", y recien ahi el picker). Ahora elegir un color es: clic en la
    fila, clic en el color.
    """

    def __init__(self, subtitle, current_rgb):
        forms.WPFWindow.__init__(self, "color_picker.xaml")
        self.result = None
        self.title_txt.Text = subtitle

        self.rows = [RalRow(opt, self._on_pick_ral) for opt in RAL_OPTIONS]
        for row in self.rows:
            self.rows_panel.Children.Add(row.container)

        if current_rgb:
            self.hex_tb.Text = format_hex(current_rgb)
        self._refresh_preview()

        self.hex_tb.TextChanged    += self._on_hex_changed
        self.hex_tb.KeyDown        += self._on_hex_key
        self.use_hex_btn.Click     += self._on_use_hex
        self.windows_btn.Click     += self._on_windows_picker
        self.search_tb.TextChanged += self._on_search_changed
        self.cancel_btn.Click      += self._on_cancel

    def _refresh_preview(self):
        rgb = parse_hex_input(self.hex_tb.Text)
        if rgb:
            self.preview_swatch.Background = brush_from_rgb(rgb)
            self.use_hex_btn.IsEnabled = True
        else:
            self.preview_swatch.Background = BRUSH_TRANSPARENT
            self.use_hex_btn.IsEnabled = False

    def _on_hex_changed(self, sender, args):
        self._refresh_preview()

    def _on_hex_key(self, sender, args):
        if args.Key == framework.Input.Key.Enter:
            self._accept(parse_hex_input(self.hex_tb.Text))

    def _on_use_hex(self, sender, args):
        self._accept(parse_hex_input(self.hex_tb.Text))

    def _on_windows_picker(self, sender, args):
        dlg = framework.Forms.ColorDialog()
        dlg.FullOpen = True
        rgb = parse_hex_input(self.hex_tb.Text)
        if rgb:
            dlg.Color = framework.Drawing.Color.FromArgb(rgb[0], rgb[1], rgb[2])
        if dlg.ShowDialog() != framework.Forms.DialogResult.OK:
            return
        self._accept((dlg.Color.R, dlg.Color.G, dlg.Color.B))

    def _on_pick_ral(self, option):
        self._accept(option.rgb)

    def _accept(self, rgb):
        if rgb is None:
            return
        self.result = rgb
        self.Close()

    def _on_search_changed(self, sender, args):
        text = (self.search_tb.Text or "").strip().lower()
        for row in self.rows:
            row.container.Visibility = (
                framework.Windows.Visibility.Visible if row.matches_filter(text)
                else framework.Windows.Visibility.Collapsed
            )

    def _on_cancel(self, sender, args):
        self.result = None
        self.Close()


def show_color_picker(subtitle, current_rgb=None):
    win = ColorPickerWindow(subtitle, current_rgb)
    win.ShowDialog()
    return win.result


# ═══════════════════════════════════════════════════════════════════════════════
# FILAS DE LA LISTA (construidas de forma procedural, sin data-binding)
# ═══════════════════════════════════════════════════════════════════════════════

class ColorRow(object):
    """Un valor del scheme.

    Clic en la fila pinta ese valor y nada mas, que es el caso comun; el
    checkbox queda para el caso masivo. El CheckBox marca el clic como
    manejado, asi que marcarlo no dispara ademas el picker.
    """

    def __init__(self, entry, label, count, nouns, on_pick, on_pattern_pick, on_check_changed):
        self.entry = entry
        self.label = label
        self.count = count
        self.nouns = nouns
        self.on_pick = on_pick
        self.on_pattern_pick = on_pattern_pick
        self.on_check_changed = on_check_changed
        self.checkbox     = None
        self.swatch       = None
        self.hex_text     = None
        self.pattern_chip = None
        self.pattern_text = None
        self.container    = None
        self._build()

    def _usage_text(self):
        if self.count == 0:
            return "(sin uso)"
        singular, plural = self.nouns
        return "({} {})".format(self.count, singular if self.count == 1 else plural)

    def _build(self):
        grid = framework.Controls.Grid()
        grid.Margin = framework.Windows.Thickness(0, 2, 0, 2)
        grid.Background = BRUSH_TRANSPARENT
        grid.ColumnDefinitions.Add(make_column("auto"))
        grid.ColumnDefinitions.Add(make_column("fixed", 26))
        grid.ColumnDefinitions.Add(make_column("star"))
        grid.ColumnDefinitions.Add(make_column("auto"))
        grid.ColumnDefinitions.Add(make_column("auto"))
        try:
            grid.Cursor = framework.Input.Cursors.Hand
        except Exception:
            pass

        self.checkbox = framework.Controls.CheckBox()
        self.checkbox.Margin = framework.Windows.Thickness(0, 0, 8, 0)
        self.checkbox.ToolTip = "Marcar para pintar varios valores del mismo color"
        framework.Controls.Grid.SetColumn(self.checkbox, 0)

        self.swatch = framework.Controls.Border()
        self.swatch.Width  = 18
        self.swatch.Height = 18
        self.swatch.CornerRadius = framework.Windows.CornerRadius(3)
        self.swatch.BorderBrush = BRUSH_GRAPHITE
        self.swatch.BorderThickness = framework.Windows.Thickness(1)
        framework.Controls.Grid.SetColumn(self.swatch, 1)

        label_tb = framework.Controls.TextBlock()
        label_tb.Text = "{}   {}".format(self.label, self._usage_text())
        label_tb.FontFamily = framework.Media.FontFamily("Lato")
        label_tb.Foreground = BRUSH_GRAPHITE
        label_tb.VerticalAlignment = framework.Windows.VerticalAlignment.Center
        label_tb.TextTrimming = framework.Windows.TextTrimming.CharacterEllipsis
        framework.Controls.Grid.SetColumn(label_tb, 2)

        self.pattern_chip = self._build_pattern_chip()
        framework.Controls.Grid.SetColumn(self.pattern_chip, 3)

        self.hex_text = framework.Controls.TextBlock()
        self.hex_text.FontFamily = framework.Media.FontFamily("Consolas")
        self.hex_text.FontSize = 10
        self.hex_text.Foreground = BRUSH_SILVER
        self.hex_text.TextAlignment = framework.Windows.TextAlignment.Right
        self.hex_text.VerticalAlignment = framework.Windows.VerticalAlignment.Center
        self.hex_text.Margin = framework.Windows.Thickness(8, 0, 0, 0)
        framework.Controls.Grid.SetColumn(self.hex_text, 4)

        grid.Children.Add(self.checkbox)
        grid.Children.Add(self.swatch)
        grid.Children.Add(label_tb)
        grid.Children.Add(self.pattern_chip)
        grid.Children.Add(self.hex_text)

        def on_enter(sender, args):
            grid.Background = BRUSH_TERRACOTTA_HOVER

        def on_leave(sender, args):
            grid.Background = BRUSH_TRANSPARENT

        def on_click(sender, args):
            self.on_pick(self)

        def on_check(sender, args):
            self.on_check_changed()

        grid.MouseEnter += on_enter
        grid.MouseLeave += on_leave
        grid.MouseLeftButtonUp += on_click
        self.checkbox.Checked   += on_check
        self.checkbox.Unchecked += on_check

        self.container = grid
        self.refresh_swatch()
        self.refresh_pattern()

    def _build_pattern_chip(self):
        """Chip clickeable con el nombre de la trama.

        Marca el clic como manejado para que NO burbujee a la fila, que abre
        el selector de color: son dos destinos distintos en la misma fila.
        """
        chip = framework.Controls.Border()
        chip.BorderBrush = BRUSH_PALE_SLATE
        chip.BorderThickness = framework.Windows.Thickness(1)
        chip.CornerRadius = framework.Windows.CornerRadius(2)
        chip.Padding = framework.Windows.Thickness(6, 2, 6, 2)
        chip.Margin = framework.Windows.Thickness(8, 0, 0, 0)
        chip.Background = BRUSH_PORCELAIN
        chip.VerticalAlignment = framework.Windows.VerticalAlignment.Center
        chip.ToolTip = "Trama de relleno. Clic para cambiarla."
        try:
            chip.Cursor = framework.Input.Cursors.Hand
        except Exception:
            pass

        self.pattern_text = framework.Controls.TextBlock()
        self.pattern_text.FontFamily = framework.Media.FontFamily("Lato")
        self.pattern_text.FontSize = 10
        self.pattern_text.Foreground = BRUSH_GRAPHITE
        self.pattern_text.TextTrimming = framework.Windows.TextTrimming.CharacterEllipsis
        self.pattern_text.MaxWidth = 130
        chip.Child = self.pattern_text

        def on_enter(sender, args):
            chip.Background = BRUSH_TERRACOTTA_HOVER
            chip.BorderBrush = BRUSH_TERRACOTTA

        def on_leave(sender, args):
            chip.Background = BRUSH_PORCELAIN
            chip.BorderBrush = BRUSH_PALE_SLATE

        def on_click(sender, args):
            args.Handled = True
            self.on_pattern_pick(self)

        chip.MouseEnter += on_enter
        chip.MouseLeave += on_leave
        chip.MouseLeftButtonUp += on_click
        return chip

    def current_pattern_id(self):
        return self.entry.FillPatternId

    def is_pattern(self, pattern_id):
        return self.entry.FillPatternId == pattern_id

    def set_pattern(self, pattern_id):
        self.entry.FillPatternId = pattern_id
        self.refresh_pattern()

    def refresh_pattern(self):
        name = fill_pattern_name(self.entry.FillPatternId)
        self.pattern_text.Text = name
        # Sin trama no se dibuja nada aunque haya color: se marca en gris
        # para que no pase inadvertido.
        self.pattern_text.Foreground = (BRUSH_SILVER if name == NO_PATTERN_LABEL
                                        else BRUSH_GRAPHITE)

    def refresh_swatch(self):
        rgb = self.current_rgb()
        if rgb:
            self.swatch.Background = brush_from_rgb(rgb)
            text = format_hex(rgb)
            ral_code = RAL_CODE_BY_RGB.get(rgb)
            if ral_code:
                text += "\n" + ral_code
            self.hex_text.Text = text
        else:
            self.swatch.Background = BRUSH_TRANSPARENT
            self.hex_text.Text = "N/A"

    def current_rgb(self):
        """Tupla de int de Python, no de System.Byte: asi el hex se formatea
        bien y la clave calza con RAL_CODE_BY_RGB sin depender de como
        IronPython compara Byte contra int."""
        c = self.entry.Color
        if c and c.IsValid:
            return (int(c.Red), int(c.Green), int(c.Blue))
        return None

    def is_color(self, r, g, b):
        c = self.entry.Color
        return bool(c and c.IsValid and c.Red == r and c.Green == g and c.Blue == b)

    def set_color(self, r, g, b):
        self.entry.Color = DB.Color(r, g, b)
        self.refresh_swatch()

    def matches_filter(self, text):
        return (not text) or (text in self.label.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# VENTANA PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════════

class AssignColorsWindow(forms.WPFWindow):
    """Una sola ventana de principio a fin: categoria, Area Scheme, Color
    Scheme y alcance viven en la cabecera y recargan la lista en vivo."""

    def __init__(self, detected):
        forms.WPFWindow.__init__(self, "ui.xaml")
        # _loading tapa los eventos de los combos mientras se repueblan solos.
        self._loading = True
        self.rows = []
        self.current_bic = None
        self.current_nouns = ("elemento", "elementos")
        self.current_scheme = None
        self.current_area_scheme = None
        self.view_only = True
        self._area_schemes_by_name = {}
        self._schemes_by_label = {}

        self.category_toggles = {}
        for label, _b, _s, _p in CATEGORY_OPTIONS:
            toggle = SegmentToggle(label, self._on_category_click)
            self.category_toggles[label] = toggle
            self.category_row.Children.Add(toggle.container)

        self.scope_toggles = {}
        for label in (SCOPE_VIEW, SCOPE_ALL):
            toggle = SegmentToggle(label, self._on_scope_click, compact=True)
            self.scope_toggles[label] = toggle
            self.scope_row.Children.Add(toggle.container)
        self.scope_toggles[SCOPE_VIEW].set_active(True)

        self.area_scheme_cb.SelectionChanged += self._on_area_scheme_changed
        self.scheme_cb.SelectionChanged      += self._on_scheme_changed
        self.apply_view_cb.Checked           += self._on_apply_view_checked
        self.apply_view_cb.Unchecked         += self._on_apply_view_unchecked
        self.search_tb.TextChanged           += self._on_search_changed
        self.check_all_btn.Click             += self._on_check_all
        self.uncheck_all_btn.Click           += self._on_uncheck_all
        self.assign_btn.Click                += self._on_assign_to_checked
        self.palette_btn.Click               += self._on_load_palette
        self.close_btn.Click                 += self._on_close

        bic, area_scheme_id, scheme = detected or (CATEGORY_OPTIONS[0][1], None, None)
        self._select_category(label_for_bic(bic), area_scheme_id, scheme)
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

    # ── cabecera ──────────────────────────────────────────────────────────────

    def _on_category_click(self, label):
        if label != label_for_bic(self.current_bic):
            self._select_category(label)

    def _select_category(self, label, preset_area_scheme_id=None, preset_scheme=None):
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
            index = 0
            if preset_area_scheme_id is not None:
                for i, name in enumerate(names):
                    if self._area_schemes_by_name[name].Id == preset_area_scheme_id:
                        index = i
                        break
            if names:
                self.area_scheme_cb.SelectedIndex = index
                self.current_area_scheme = self._area_schemes_by_name[names[index]]
            else:
                self.current_area_scheme = None
        else:
            self._set_area_scheme_visible(False)
            self._area_schemes_by_name = {}
            self.area_scheme_cb.ItemsSource = None
            self.current_area_scheme = None
        self._loading = was_loading

        self._reload_schemes(preset_scheme)

    def _on_area_scheme_changed(self, sender, args):
        if self._loading:
            return
        name = self.area_scheme_cb.SelectedItem
        if name:
            self.current_area_scheme = self._area_schemes_by_name.get(name)
            self._reload_schemes()

    def _reload_schemes(self, preset_scheme=None):
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
            index = 0
            if preset_scheme is not None:
                for i, label in enumerate(labels):
                    if self._schemes_by_label[label].Id == preset_scheme.Id:
                        index = i
                        break
            self.scheme_cb.SelectedIndex = index
            self.current_scheme = self._schemes_by_label[labels[index]]
        else:
            self.current_scheme = None
        self._loading = was_loading

        self._refresh_scheme_tooltip()
        self._reload_rows()
        self._refresh_apply_checkbox()

    def _refresh_scheme_tooltip(self):
        """El combo es angosto y trunca el nombre del scheme mas el del
        parametro; el tooltip da el texto completo sin robar ancho."""
        self.scheme_cb.ToolTip = self.scheme_cb.SelectedItem

    def _on_scheme_changed(self, sender, args):
        if self._loading:
            return
        label = self.scheme_cb.SelectedItem
        if not label:
            return
        self.current_scheme = self._schemes_by_label.get(label)
        self._refresh_scheme_tooltip()
        self._reload_rows()
        self._refresh_apply_checkbox()

    # ── aplicar a la vista activa ─────────────────────────────────────────────

    def _refresh_apply_checkbox(self):
        applied_id = view_scheme_id(self.current_bic)
        was_loading = self._loading
        self._loading = True
        if applied_id is None or self.current_scheme is None:
            self.apply_view_cb.IsEnabled = False
            self.apply_view_cb.IsChecked = False
            self.apply_view_cb.ToolTip = (
                "La vista activa no admite un Color Scheme de esta categoria."
            )
        else:
            self.apply_view_cb.IsEnabled = True
            self.apply_view_cb.IsChecked = (applied_id == self.current_scheme.Id)
            self.apply_view_cb.ToolTip = (
                "Marcado: la vista activa muestra este Color Scheme. "
                "Desmarcar lo quita de la vista."
            )
        self._loading = was_loading

    def _on_apply_view_checked(self, sender, args):
        if self._loading or self.current_scheme is None:
            return
        try:
            with revit.Transaction("ARCH - Aplicar Color Scheme a la vista"):
                doc.ActiveView.SetColorFillSchemeId(
                    get_category_id(self.current_bic), self.current_scheme.Id)
        except Exception as ex:
            logger.debug("No se pudo aplicar el scheme a la vista: {}".format(ex))
            show_alert(
                "Revit no acepta este Color Scheme en la vista activa. Suele pasar "
                "cuando la vista no es del tipo correcto (una planta comun para un "
                "scheme de Areas) o cuando su Area Scheme no calza con el de la vista.",
                title="No se pudo aplicar"
            )
            self._refresh_apply_checkbox()
            return
        self._reload_rows()
        self.set_status("Color Scheme aplicado a la vista activa.")

    def _on_apply_view_unchecked(self, sender, args):
        if self._loading:
            return
        try:
            with revit.Transaction("ARCH - Quitar Color Scheme de la vista"):
                doc.ActiveView.SetColorFillSchemeId(
                    get_category_id(self.current_bic), DB.ElementId.InvalidElementId)
        except Exception as ex:
            logger.debug("No se pudo quitar el scheme de la vista: {}".format(ex))
            self._refresh_apply_checkbox()
            return
        self.set_status("Color Scheme quitado de la vista activa.")

    # ── alcance y lista ───────────────────────────────────────────────────────

    def _on_scope_click(self, label):
        view_only = (label == SCOPE_VIEW)
        if view_only == self.view_only:
            return
        self.view_only = view_only
        for lbl, toggle in self.scope_toggles.items():
            toggle.set_active(lbl == label)
        self._reload_rows()

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

        rows_data = build_rows_data(self.current_scheme, self.current_bic,
                                    self._area_scheme_id(), self.view_only)

        if not rows_data:
            if self.view_only:
                self._show_empty(
                    "Ningun {} visible en la vista activa usa un valor de este scheme.\n"
                    "Cambie el alcance a \"{}\" para ver todas sus entradas."
                    .format(self.current_nouns[0], SCOPE_ALL)
                )
            else:
                self._show_empty("Este Color Scheme no tiene entradas todavia.")
            self.set_status("")
            self._refresh_actions()
            return

        # Un scheme sobre un parametro continuo (una superficie, por ejemplo)
        # tiene una entrada por valor distinto, y aca se dibuja una fila WPF
        # por entrada, sin virtualizar. Mejor advertir que congelar Revit.
        if len(rows_data) > BIG_SCHEME_WARNING and not show_confirm(
            "Este Color Scheme deja {} valores en la lista.\n\n"
            "Suele pasar cuando usa un parametro continuo, como una superficie: "
            "Revit crea una entrada por cada valor distinto.\n\n"
            "Dibujar la lista completa puede tardar. Continuar?"
            .format(len(rows_data)),
            title="Lista muy grande"
        ):
            self._show_empty(
                "Lista no cargada: son {} valores.\n"
                "Pruebe con el alcance \"{}\", o filtre desde otro Color Scheme."
                .format(len(rows_data), SCOPE_VIEW)
            )
            self.set_status("")
            self._refresh_actions()
            return

        for data in rows_data:
            row = ColorRow(data["entry"], data["label"], data["count"],
                           self.current_nouns, self._on_row_pick,
                           self._on_row_pattern_pick, self._refresh_actions)
            self.rows.append(row)
            self.rows_panel.Children.Add(row.container)

        self._apply_filter()
        self._refresh_actions()
        self.set_status("{} valor(es) en la lista. Una paleta de {} color(es) calza exacto."
                        .format(len(self.rows), len(self.rows)))

    def _refresh_actions(self):
        has_rows = bool(self.rows)
        self.palette_btn.IsEnabled     = has_rows
        self.check_all_btn.IsEnabled   = has_rows
        self.uncheck_all_btn.IsEnabled = has_rows

        checked = sum(1 for r in self.rows if r.checkbox.IsChecked)
        self.assign_btn.IsEnabled = checked > 0
        # Se muestra el conteo tambien en 0 (con el boton deshabilitado): dice
        # por que no se puede pulsar, en vez de solo verse apagado.
        self.assign_btn.Content = "Asignar color a {} marcado{}...".format(
            checked, "" if checked == 1 else "s")

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
        # A diferencia de "Marcar todo", este ignora el filtro a proposito.
        # Filtrar-y-marcar-todo es un patron util; filtrar-y-desmarcar-todo
        # dejaria marcadas las filas ocultas, y como un clic sobre una fila
        # marcada pinta TODA la seleccion, se terminarian pintando valores
        # que no estan a la vista. Desmarcar siempre devuelve a cero.
        for row in self.rows:
            row.checkbox.IsChecked = False

    # ── pintar ────────────────────────────────────────────────────────────────

    def _checked_rows(self):
        return [r for r in self.rows if r.checkbox.IsChecked]

    def _paint(self, targets):
        """Pinta un conjunto de filas de un mismo color.

        Unico camino de escritura: lo usan tanto el clic en una fila como el
        boton masivo, para que no puedan divergir.
        """
        if not targets:
            return

        subtitle = (targets[0].label if len(targets) == 1
                    else "{} valores marcados".format(len(targets)))
        rgb = show_color_picker(subtitle, targets[0].current_rgb())
        if rgb is None:
            return

        r, g, b = rgb
        changed = 0
        with revit.Transaction("ARCH - Asignar color"):
            for row in targets:
                if row.is_color(r, g, b):
                    continue
                row.set_color(r, g, b)
                self.current_scheme.UpdateEntry(row.entry)
                changed += 1

        if changed == 0:
            self.set_status("{} ya tenia ese color.".format(targets[0].label)
                            if len(targets) == 1
                            else "Los valores marcados ya tenian ese color.")
        elif len(targets) == 1:
            self.set_status("{} pintado de {}.".format(targets[0].label, format_hex(rgb)))
        else:
            self.set_status("{} de {} valores pintados de {}."
                            .format(changed, len(targets), format_hex(rgb)))

    def _targets_for(self, row):
        # Clic sobre una fila YA marcada actua sobre toda la seleccion, como en
        # un explorador de archivos. Sobre una fila sin marcar, solo esa fila:
        # asi nunca se toca de mas por accidente.
        return self._checked_rows() if row.checkbox.IsChecked else [row]

    def _on_row_pick(self, row):
        self._paint(self._targets_for(row))

    def _on_row_pattern_pick(self, row):
        self._set_pattern(self._targets_for(row))

    def _set_pattern(self, targets):
        """Cambia la trama de relleno de un conjunto de filas.

        La trama decide como se dibuja el color en la vista: sin trama no se
        rellena nada, por mas color que tenga la entrada.
        """
        if not targets:
            return

        patterns = get_drafting_fill_patterns()
        rows_data = [(NO_PATTERN_LABEL, DB.ElementId.InvalidElementId)]
        rows_data += [(p.Name, p.Id) for p in patterns]

        subtitle = (targets[0].label if len(targets) == 1
                    else "{} valores marcados".format(len(targets)))
        picked, pattern_id = show_list_picker(
            rows_data,
            title="Trama de relleno",
            subtitle="{}   -   solo se listan patrones de tipo Drafting, los "
                     "unicos que Revit acepta en un Color Scheme.".format(subtitle)
        )
        if not picked:
            return

        changed = 0
        with revit.Transaction("ARCH - Asignar trama"):
            for row in targets:
                if row.is_pattern(pattern_id):
                    continue
                row.set_pattern(pattern_id)
                self.current_scheme.UpdateEntry(row.entry)
                changed += 1

        name = fill_pattern_name(pattern_id)
        if changed == 0:
            self.set_status("Ya tenian la trama '{}'.".format(name))
        elif len(targets) == 1:
            self.set_status("{}: trama '{}'.".format(targets[0].label, name))
        else:
            self.set_status("{} de {} valores con trama '{}'."
                            .format(changed, len(targets), name))

    def _on_assign_to_checked(self, sender, args):
        self._paint(self._checked_rows())

    def _on_load_palette(self, sender, args):
        ofd = framework.Forms.OpenFileDialog()
        ofd.Filter = (
            "Paletas (*.svg;*.txt;*.csv;*.gpl)|*.svg;*.txt;*.csv;*.gpl|"
            "Todos los archivos (*.*)|*.*"
        )
        if ofd.ShowDialog() != framework.Forms.DialogResult.OK:
            return

        try:
            colors = parse_palette_file(ofd.FileName)
        except Exception as ex:
            logger.debug("Error leyendo paleta: {}".format(ex))
            show_alert("No se pudo leer el archivo de paleta.", title="Error")
            return

        if not colors:
            show_alert(
                "No se encontraron colores en el archivo (se buscan codigos #RRGGBB, "
                "rgb(...) o lineas de GIMP .gpl).",
                title="Paleta vacia"
            )
            return

        n_rows   = len(self.rows)
        n_colors = len(colors)
        apply_n  = min(n_rows, n_colors)

        if n_colors != n_rows:
            proceed = show_confirm(
                "La paleta tiene {} color(es) y hay {} valor(es) en la lista.\n\n"
                "Se asignaran los primeros {} en orden (el orden alfabetico de la "
                "lista, no el del filtro de busqueda); el resto quedara sin cambios."
                .format(n_colors, n_rows, apply_n),
                title="Cantidad distinta"
            )
            if not proceed:
                return

        changed = 0
        with revit.Transaction("ARCH - Aplicar paleta"):
            for i in range(apply_n):
                r, g, b = colors[i]
                row = self.rows[i]
                if row.is_color(r, g, b):
                    continue
                row.set_color(r, g, b)
                self.current_scheme.UpdateEntry(row.entry)
                changed += 1

        if changed == 0:
            self.set_status("La paleta ya coincidia con los colores actuales.")
        else:
            self.set_status("Paleta aplicada a {} valor(es) (de {} evaluados)."
                            .format(changed, apply_n))

    def _on_close(self, sender, args):
        self.Close()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    if doc.ActiveView is None:
        show_alert("No hay una vista activa.", title="Sin vista")
        return

    has_any_scheme = any(get_schemes_for_category(bic) for _l, bic, _s, _p in CATEGORY_OPTIONS)
    if not has_any_scheme:
        show_alert(
            "Este proyecto no tiene ningun Color Fill Scheme definido para Rooms ni "
            "para Areas. Cree uno primero desde el dialogo nativo de Revit.",
            title="Sin Color Schemes"
        )
        return

    AssignColorsWindow(detect_from_active_view()).ShowDialog()


main()
