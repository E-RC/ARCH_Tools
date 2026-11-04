# -*- coding: utf-8 -*-
"""Aplica Display Settings de Revit Links por lote (links x vistas).

La API de Revit no expone campo por campo las opciones del dialogo
"RVT Link Display Settings" (fases, detail level, categorias, etc.), pero
GetLinkOverrides/SetLinkOverrides transfieren la configuracion COMPLETA
(Basics + Model Categories + Annotation Categories + resto de pestanas).
Por eso el flujo es: configurar UN link de referencia a mano y replicarlo.
"""

from pyrevit import revit, DB, forms, script
from System import TimeSpan
from System.Windows.Controls import CheckBox
from System.Windows.Threading import DispatcherTimer

doc = revit.doc
output = script.get_output()

FILTER_DEBOUNCE_MS = 300

MODE_NAMES = {
    DB.LinkVisibility.ByHostView: 'By Host View',
    DB.LinkVisibility.ByLinkView: 'By Linked View',
    DB.LinkVisibility.Custom: 'Custom',
}

VIEWTYPE_LABELS = {
    DB.ViewType.FloorPlan: 'Floor Plan',
    DB.ViewType.CeilingPlan: 'Ceiling Plan',
    DB.ViewType.AreaPlan: 'Area Plan',
    DB.ViewType.EngineeringPlan: 'Structural Plan',
    DB.ViewType.Section: 'Section',
    DB.ViewType.Elevation: 'Elevation',
    DB.ViewType.ThreeD: '3D',
    DB.ViewType.Detail: 'Detail',
    DB.ViewType.DraftingView: 'Drafting',
}


def get_top_level_links():
    """Instancias de link RVT de primer nivel (excluye nested)."""
    result = []
    collector = DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance)
    for link in collector:
        link_type = doc.GetElement(link.GetTypeId())
        if link_type and getattr(link_type, 'IsNestedLink', False):
            continue
        result.append(link)
    return sorted(result, key=lambda x: x.Name)


def get_candidate_views():
    """Vistas graficas (y templates) que aceptan overrides de V/G."""
    views, templates = [], []
    for view in DB.FilteredElementCollector(doc).OfClass(DB.View):
        try:
            if not view.AreGraphicsOverridesAllowed():
                continue
        except Exception:
            continue
        if view.IsTemplate:
            templates.append(view)
        else:
            views.append(view)
    key = lambda v: (str(v.ViewType), v.Name.lower())
    return sorted(views, key=key), sorted(templates, key=key)


def view_label(view):
    if view.IsTemplate:
        return '[Template] {}'.format(view.Name)
    type_label = VIEWTYPE_LABELS.get(view.ViewType, str(view.ViewType))
    return '{}: {}'.format(type_label, view.Name)


def get_mode_text(view, link_id):
    """Modo actual del display setting de un link en una vista."""
    try:
        settings = view.GetLinkOverrides(link_id)
    except Exception:
        return '?'
    if settings is None or not settings.IsValidObject:
        return 'By Host View'
    return MODE_NAMES.get(settings.LinkVisibilityType,
                          str(settings.LinkVisibilityType))


def resolve_linked_view_remap(src_view, src_link, tgt_link):
    """Cuando la config origen apunta a una vista vinculada y el destino es
    OTRO link, busca en el documento del link destino una vista equivalente
    por nombre (y tipo). Devuelve (remap_id, error, warning); (None, None,
    None) significa que no hace falta remapear. remap_id = InvalidElementId
    significa limpiar la vista vinculada (residuo de modo Custom)."""
    settings = src_view.GetLinkOverrides(src_link.Id)
    if settings is None or not settings.IsValidObject:
        return None, None, None
    if settings.LinkedViewId == DB.ElementId.InvalidElementId:
        return None, None, None
    if tgt_link.Id == src_link.Id:
        return None, None, None

    # La vista vinculada solo es obligatoria en modo By Linked View; en
    # Custom puede ser un residuo, asi que ahi se limpia con aviso.
    needs_view = settings.LinkVisibilityType == DB.LinkVisibility.ByLinkView

    def fallback(message):
        if needs_view:
            return None, message, None
        return DB.ElementId.InvalidElementId, None, message

    src_doc = src_link.GetLinkDocument()
    src_linked_view = src_doc.GetElement(settings.LinkedViewId) if src_doc else None
    if src_linked_view is None:
        return fallback('el link origen no esta cargado; '
                        'no se pudo leer su vista vinculada')

    tgt_doc = tgt_link.GetLinkDocument()
    if tgt_doc is None:
        return fallback('el link destino no esta cargado')

    matches = [v for v in DB.FilteredElementCollector(tgt_doc).OfClass(DB.View)
               if not v.IsTemplate and v.Name == src_linked_view.Name]
    same_type = [v for v in matches if v.ViewType == src_linked_view.ViewType]
    pool = same_type if same_type else matches
    if not pool:
        return fallback('no existe una vista "{}" en el link destino'
                        .format(src_linked_view.Name))
    if len(pool) > 1:
        return fallback('hay varias vistas "{}" en el link destino; '
                        'remapeo ambiguo'.format(src_linked_view.Name))
    return pool[0].Id, None, None


def build_settings_for_target(src_view, src_link, remap_id):
    """Copia fresca de la config origen, con la vista vinculada remapeada
    si corresponde."""
    settings = src_view.GetLinkOverrides(src_link.Id)
    if settings is None or not settings.IsValidObject:
        settings = DB.RevitLinkGraphicsSettings()
        settings.LinkVisibilityType = DB.LinkVisibility.ByHostView
        return settings
    if remap_id is not None:
        settings.LinkedViewId = remap_id
    return settings


class LinkDisplayWindow(forms.WPFWindow):
    def __init__(self, xaml_file_name):
        forms.WPFWindow.__init__(self, xaml_file_name)

        self.links = get_top_level_links()
        self.views, self.templates = get_candidate_views()
        self.active_view = doc.ActiveView
        self.view_checked = {}
        self.link_checked = {}
        if not self.active_view.IsTemplate:
            self.view_checked[self.active_view.Id.IntegerValue] = True

        self.populate_sources()
        self.populate_links()
        self.populate_views()

        self.filter_timer = DispatcherTimer()
        self.filter_timer.Interval = TimeSpan.FromMilliseconds(FILTER_DEBOUNCE_MS)
        self.filter_timer.Tick += self.on_filter_tick

        self.src_view_cb.SelectionChanged += self.update_src_mode
        self.src_link_cb.SelectionChanged += self.update_src_mode
        self.view_filter_txt.TextChanged += self.on_filter_changed
        self.show_templates_chk.Checked += lambda s, e: self.populate_views()
        self.show_templates_chk.Unchecked += lambda s, e: self.populate_views()
        self.links_all_btn.Click += lambda s, e: self.set_all(self.links_lb, True)
        self.links_none_btn.Click += lambda s, e: self.set_all(self.links_lb, False)
        self.views_all_btn.Click += lambda s, e: self.set_all(self.views_lb, True)
        self.views_none_btn.Click += lambda s, e: self.set_all(self.views_lb, False)
        self.apply_btn.Click += self.on_apply
        self.close_btn.Click += lambda s, e: self.Close()
        self.update_src_mode(None, None)

    # ---------- poblado de la UI ----------

    def populate_sources(self):
        self.src_candidates = list(self.views) + list(self.templates)
        self.src_view_cb.ItemsSource = [view_label(v) for v in self.src_candidates]
        active_idx = 0
        for i, view in enumerate(self.src_candidates):
            if view.Id == self.active_view.Id:
                active_idx = i
                break
        self.src_view_cb.SelectedIndex = active_idx

        self.src_link_cb.ItemsSource = [l.Name for l in self.links]
        if self.links:
            self.src_link_cb.SelectedIndex = 0

    def populate_links(self):
        self.links_lb.Items.Clear()
        for link in self.links:
            link_id = link.Id.IntegerValue
            if link_id not in self.link_checked:
                self.link_checked[link_id] = True
            mode = get_mode_text(self.active_view, link.Id)
            checkbox = CheckBox()
            checkbox.Content = u'{}   [{} en vista activa]'.format(link.Name, mode)
            checkbox.Tag = link_id
            checkbox.IsChecked = self.link_checked[link_id]
            checkbox.Checked += self.on_link_toggled
            checkbox.Unchecked += self.on_link_toggled
            self.links_lb.Items.Add(checkbox)

    def populate_views(self):
        filter_text = (self.view_filter_txt.Text or '').strip().lower()
        candidates = list(self.views)
        if self.show_templates_chk.IsChecked:
            candidates += self.templates
        self.views_lb.Items.Clear()
        visible = 0
        for view in candidates:
            if filter_text and filter_text not in view.Name.lower():
                continue
            checkbox = CheckBox()
            checkbox.Content = view_label(view)
            checkbox.Tag = view.Id.IntegerValue
            checkbox.IsChecked = self.view_checked.get(view.Id.IntegerValue, False)
            checkbox.Checked += self.on_view_toggled
            checkbox.Unchecked += self.on_view_toggled
            self.views_lb.Items.Add(checkbox)
            visible += 1
        self.views_count_txt.Text = '{} vistas'.format(visible)

    # ---------- helpers de seleccion ----------

    def on_filter_changed(self, sender, args):
        self.filter_timer.Stop()
        self.filter_timer.Start()

    def on_filter_tick(self, sender, args):
        self.filter_timer.Stop()
        self.populate_views()

    def on_view_toggled(self, sender, args):
        self.view_checked[sender.Tag] = bool(sender.IsChecked)

    def on_link_toggled(self, sender, args):
        self.link_checked[sender.Tag] = bool(sender.IsChecked)

    def set_all(self, listbox, state):
        for item in listbox.Items:
            item.IsChecked = state

    def get_src_view(self):
        idx = self.src_view_cb.SelectedIndex
        return self.src_candidates[idx] if idx >= 0 else None

    def get_src_link(self):
        idx = self.src_link_cb.SelectedIndex
        return self.links[idx] if idx >= 0 else None

    def get_checked_links(self):
        checked_ids = set(link_id for link_id, on in self.link_checked.items() if on)
        return [l for l in self.links if l.Id.IntegerValue in checked_ids]

    def get_checked_views(self):
        checked_ids = set(view_id for view_id, on in self.view_checked.items() if on)
        all_views = list(self.views) + list(self.templates)
        return [v for v in all_views if v.Id.IntegerValue in checked_ids]

    def update_src_mode(self, sender, args):
        src_view, src_link = self.get_src_view(), self.get_src_link()
        if src_view and src_link:
            mode = get_mode_text(src_view, src_link.Id)
            self.src_mode_txt.Text = u'Config. actual del origen: {}'.format(mode)
        else:
            self.src_mode_txt.Text = ''

    # ---------- aplicacion ----------

    def apply_reset(self, view, link, failures):
        """Quita overrides; solo cuenta como aplicado si realmente quedo
        en default. Devuelve True si se aplico."""
        try:
            existing = view.GetLinkOverrides(link.Id)
        except Exception:
            existing = None
        if existing is None or not existing.IsValidObject:
            return True  # ya estaba en By Host View
        try:
            view.RemoveLinkOverrides(link.Id)
            return True
        except Exception as exc:
            failures.append((view, link, str(exc)))
            return False

    def on_apply(self, sender, args):
        is_reset = bool(self.reset_rb.IsChecked)
        src_view, src_link = self.get_src_view(), self.get_src_link()
        tgt_links = self.get_checked_links()
        tgt_views = self.get_checked_views()

        if not tgt_links or not tgt_views:
            forms.alert('Seleccione al menos un link y una vista de destino.',
                        title='Link Display')
            return
        if not is_reset and (src_view is None or src_link is None):
            forms.alert('Seleccione la vista y el link de origen.',
                        title='Link Display')
            return

        # El remapeo de vista vinculada depende solo del link destino:
        # se resuelve una vez por link, no por cada vista.
        remaps, warnings = {}, []
        if not is_reset:
            for link in tgt_links:
                remap_id, error, warning = resolve_linked_view_remap(
                    src_view, src_link, link)
                remaps[link.Id.IntegerValue] = (remap_id, error)
                if warning:
                    warnings.append((link, warning))

        applied, failures = 0, []
        transaction = DB.Transaction(doc, 'Batch RVT Link Display Settings')
        transaction.Start()
        try:
            for view in tgt_views:
                for link in tgt_links:
                    try:
                        if is_reset:
                            if self.apply_reset(view, link, failures):
                                applied += 1
                            continue
                        remap_id, error = remaps[link.Id.IntegerValue]
                        if error:
                            failures.append((view, link, error))
                            continue
                        settings = build_settings_for_target(
                            src_view, src_link, remap_id)
                        view.SetLinkOverrides(link.Id, settings)
                        applied += 1
                    except Exception as exc:
                        failures.append((view, link, str(exc)))
            status = transaction.Commit()
            if status != DB.TransactionStatus.Committed:
                forms.alert('Revit revirtio la transaccion; no se aplico '
                            'ningun cambio.', title='Link Display')
                return
        except Exception as exc:
            transaction.RollBack()
            forms.alert('Error inesperado, no se aplico ningun cambio:\n{}'
                        .format(exc), title='Link Display')
            return

        self.populate_links()
        self.report_results(applied, failures, warnings)

    def report_results(self, applied, failures, warnings=None):
        lines = ['Aplicado en {} combinaciones link x vista.'.format(applied)]
        if warnings:
            lines.append('')
            lines.append('Avisos:')
            for link, reason in warnings[:6]:
                lines.append(u'- {}: {}'.format(link.Name, reason))
        if failures:
            lines.append('')
            lines.append('{} fallaron:'.format(len(failures)))
            for view, link, reason in failures[:12]:
                lines.append(u'- {} / {}: {}'.format(
                    view.Name, link.Name, reason))
            if len(failures) > 12:
                lines.append('... y {} mas (ver Output).'.format(
                    len(failures) - 12))
            lines.append('')
            lines.append('Tip: si la vista tiene un View Template que controla '
                         'los RVT Links, aplique al template ("Ver templates").')
            for view, link, reason in failures:
                output.print_md(u'**{} / {}**: {}'.format(
                    view.Name, link.Name, reason))
        forms.alert('\n'.join(lines), title='Link Display')


if __name__ == '__main__':
    if not get_top_level_links():
        forms.alert('El modelo no tiene Revit Links cargados.',
                    title='Link Display')
    else:
        LinkDisplayWindow('ui.xaml').ShowDialog()
