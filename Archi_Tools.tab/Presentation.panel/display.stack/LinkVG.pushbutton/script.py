# -*- coding: utf-8 -*-
"""Edita en simultaneo los Display Settings de varios Revit Links
en la vista activa.

Ajustes directos que permite la API: visibilidad del link, halftone
(override de elemento), modo de display (By Host View / By Linked View /
Custom) y vista vinculada. Las opciones internas del modo Custom (fases,
detail level, Model/Annotation Categories del link...) no son editables
campo a campo por la API: para replicarlas se usa "Copiar configuracion
desde" un link ya configurado (GetLinkOverrides/SetLinkOverrides
transfieren la configuracion completa).
"""

from pyrevit import revit, DB, forms, script
from System.Collections.Generic import List
from System.Windows.Controls import CheckBox

doc = revit.doc
output = script.get_output()

PRESET_PREFIX = '_LINKVG_PRESET_'
INVALID_NAME_CHARS = u'\\:{}[]|;<>?`~'

NO_CHANGE = 'No cambiar'
VIS_OPTIONS = [NO_CHANGE, 'Visible', 'Oculto']
HALFTONE_OPTIONS = [NO_CHANGE, 'Activado', 'Desactivado']
MODE_BY_HOST = 'By Host View'
MODE_BY_LINK = 'By Linked View'
MODE_CUSTOM = 'Custom'
MODE_OPTIONS = [NO_CHANGE, MODE_BY_HOST, MODE_BY_LINK, MODE_CUSTOM]

MODE_NAMES = {
    DB.LinkVisibility.ByHostView: MODE_BY_HOST,
    DB.LinkVisibility.ByLinkView: MODE_BY_LINK,
    DB.LinkVisibility.Custom: MODE_CUSTOM,
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


def get_mode_text(view, link_id):
    try:
        settings = view.GetLinkOverrides(link_id)
    except Exception:
        return '?'
    if settings is None or not settings.IsValidObject:
        return MODE_BY_HOST
    return MODE_NAMES.get(settings.LinkVisibilityType,
                          str(settings.LinkVisibilityType))


def get_link_state_text(view, link):
    parts = [get_mode_text(view, link.Id)]
    try:
        if view.GetElementOverrides(link.Id).Halftone:
            parts.append('Halftone')
    except Exception:
        pass
    try:
        if link.IsHidden(view):
            parts.append('Oculto')
    except Exception:
        pass
    return ', '.join(parts)


def get_linked_view_names(links, view_type):
    """Union de nombres de vistas (no template, mismo tipo que la vista
    activa) en los documentos de los links cargados."""
    names = set()
    for link in links:
        link_doc = link.GetLinkDocument()
        if not link_doc:
            continue
        for view in DB.FilteredElementCollector(link_doc).OfClass(DB.View):
            if not view.IsTemplate and view.ViewType == view_type:
                names.add(view.Name)
    return sorted(names)


def find_linked_view(link, name, view_type):
    """Devuelve (view_id, error) para la vista 'name' en el doc del link."""
    link_doc = link.GetLinkDocument()
    if link_doc is None:
        return None, 'el link no esta cargado'
    matches = [v for v in DB.FilteredElementCollector(link_doc).OfClass(DB.View)
               if not v.IsTemplate and v.ViewType == view_type
               and v.Name == name]
    if not matches:
        return None, 'no existe una vista "{}" en el link'.format(name)
    if len(matches) > 1:
        return None, 'hay varias vistas "{}" en el link'.format(name)
    return matches[0].Id, None


def get_settings_or_default(view, link_id):
    settings = view.GetLinkOverrides(link_id)
    if settings is None or not settings.IsValidObject:
        settings = DB.RevitLinkGraphicsSettings()
        settings.LinkVisibilityType = DB.LinkVisibility.ByHostView
    return settings


def get_preset_views():
    """Vistas-banco de presets: (nombre visible, vista), ordenadas."""
    presets = []
    for view in DB.FilteredElementCollector(doc).OfClass(DB.View):
        if view.IsTemplate:
            continue
        if view.Name.startswith(PRESET_PREFIX):
            presets.append((view.Name[len(PRESET_PREFIX):], view))
    return sorted(presets, key=lambda pair: pair[0].lower())


def sanitize_preset_name(raw_name):
    name = (raw_name or '').strip()
    for char in INVALID_NAME_CHARS:
        name = name.replace(char, '-')
    return name


class LinkVGWindow(forms.WPFWindow):
    def __init__(self, xaml_file_name, active_view, links):
        forms.WPFWindow.__init__(self, xaml_file_name)
        self.active_view = active_view
        self.links = links
        self.link_checked = {}

        self.view_name_txt.Text = u'Vista activa: {}'.format(active_view.Name)
        self.vis_cb.ItemsSource = VIS_OPTIONS
        self.vis_cb.SelectedIndex = 0
        self.halftone_cb.ItemsSource = HALFTONE_OPTIONS
        self.halftone_cb.SelectedIndex = 0
        self.mode_cb.ItemsSource = MODE_OPTIONS
        self.mode_cb.SelectedIndex = 0
        self.linked_view_cb.ItemsSource = get_linked_view_names(
            links, active_view.ViewType)
        self.copy_src_cb.ItemsSource = [l.Name for l in links]
        if links:
            self.copy_src_cb.SelectedIndex = 0

        self.populate_links()
        self.populate_presets()

        self.mode_cb.SelectionChanged += self.update_enabled
        self.copy_chk.Checked += self.update_enabled
        self.copy_chk.Unchecked += self.update_enabled
        self.preset_apply_btn.Click += self.on_preset_apply
        self.preset_save_btn.Click += self.on_preset_save
        self.preset_del_btn.Click += self.on_preset_delete
        self.links_all_btn.Click += lambda s, e: self.set_all(True)
        self.links_none_btn.Click += lambda s, e: self.set_all(False)
        self.apply_btn.Click += self.on_apply
        self.close_btn.Click += lambda s, e: self.Close()

    # ---------- UI ----------

    def populate_links(self):
        self.links_lb.Items.Clear()
        for link in self.links:
            link_id = link.Id.IntegerValue
            if link_id not in self.link_checked:
                self.link_checked[link_id] = True
            state = get_link_state_text(self.active_view, link)
            checkbox = CheckBox()
            checkbox.Content = u'{}   [{}]'.format(link.Name, state)
            checkbox.Tag = link_id
            checkbox.IsChecked = self.link_checked[link_id]
            checkbox.Checked += self.on_link_toggled
            checkbox.Unchecked += self.on_link_toggled
            self.links_lb.Items.Add(checkbox)

    def populate_presets(self, select_name=None):
        self.presets = get_preset_views()
        self.preset_cb.ItemsSource = [name for name, _ in self.presets]
        if not self.presets:
            self.preset_cb.SelectedIndex = -1
            return
        index = 0
        if select_name:
            for i, (name, _) in enumerate(self.presets):
                if name == select_name:
                    index = i
                    break
        self.preset_cb.SelectedIndex = index

    def get_selected_preset(self):
        idx = self.preset_cb.SelectedIndex
        return self.presets[idx] if 0 <= idx < len(self.presets) else None

    def on_link_toggled(self, sender, args):
        self.link_checked[sender.Tag] = bool(sender.IsChecked)

    def set_all(self, state):
        for item in self.links_lb.Items:
            item.IsChecked = state

    def update_enabled(self, sender, args):
        copying = bool(self.copy_chk.IsChecked)
        self.copy_src_cb.IsEnabled = copying
        self.mode_cb.IsEnabled = not copying
        self.linked_view_cb.IsEnabled = (
            not copying and self.mode_cb.SelectedItem == MODE_BY_LINK)

    # ---------- lectura de ajustes ----------

    def get_checked_links(self):
        checked_ids = set(i for i, on in self.link_checked.items() if on)
        return [l for l in self.links if l.Id.IntegerValue in checked_ids]

    def get_copy_source(self):
        idx = self.copy_src_cb.SelectedIndex
        return self.links[idx] if idx >= 0 else None

    # ---------- aplicacion ----------

    def apply_copy(self, link, src_link, failures, warnings):
        settings = get_settings_or_default(self.active_view, src_link.Id)
        # La vista vinculada solo es obligatoria en modo By Linked View;
        # en Custom puede ser un residuo de una config anterior.
        needs_view = (settings.LinkVisibilityType
                      == DB.LinkVisibility.ByLinkView)
        if (settings.LinkedViewId != DB.ElementId.InvalidElementId
                and link.Id != src_link.Id):
            src_doc = src_link.GetLinkDocument()
            src_view = (src_doc.GetElement(settings.LinkedViewId)
                        if src_doc else None)
            if src_view is None:
                new_id, error = None, ('no se pudo leer la vista vinculada '
                                       'del link origen')
            else:
                new_id, error = find_linked_view(link, src_view.Name,
                                                 src_view.ViewType)
            if new_id is not None:
                settings.LinkedViewId = new_id
            elif needs_view:
                failures.append((link, error))
                return False
            else:
                try:
                    settings.LinkedViewId = DB.ElementId.InvalidElementId
                    warnings.append((link, 'vista vinculada sin equivalente; '
                                           'quedo en <None> ({})'.format(error)))
                except Exception:
                    failures.append((link, error))
                    return False
        self.active_view.SetLinkOverrides(link.Id, settings)
        return True

    def apply_mode(self, link, mode_label, linked_view_name, failures):
        if mode_label == MODE_BY_HOST:
            settings = DB.RevitLinkGraphicsSettings()
            settings.LinkVisibilityType = DB.LinkVisibility.ByHostView
            self.active_view.SetLinkOverrides(link.Id, settings)
            return True
        settings = get_settings_or_default(self.active_view, link.Id)
        if mode_label == MODE_CUSTOM:
            settings.LinkVisibilityType = DB.LinkVisibility.Custom
            self.active_view.SetLinkOverrides(link.Id, settings)
            return True
        # By Linked View
        view_id, error = find_linked_view(link, linked_view_name,
                                          self.active_view.ViewType)
        if error:
            failures.append((link, error))
            return False
        settings.LinkVisibilityType = DB.LinkVisibility.ByLinkView
        settings.LinkedViewId = view_id
        self.active_view.SetLinkOverrides(link.Id, settings)
        return True

    def apply_halftone(self, link, turn_on):
        overrides = self.active_view.GetElementOverrides(link.Id)
        overrides.SetHalftone(turn_on)
        self.active_view.SetElementOverrides(link.Id, overrides)
        return True

    def apply_visibility(self, link, make_visible, failures):
        ids = List[DB.ElementId]([link.Id])
        if make_visible:
            if link.IsHidden(self.active_view):
                self.active_view.UnhideElements(ids)
            return True
        if link.IsHidden(self.active_view):
            return True
        if not link.CanBeHidden(self.active_view):
            failures.append((link, 'no se puede ocultar en esta vista'))
            return False
        self.active_view.HideElements(ids)
        return True

    # ---------- presets ----------

    def on_preset_save(self, sender, args):
        """Guarda la config de links de la vista activa en una vista-banco."""
        raw_name = forms.ask_for_string(
            default='',
            prompt='Nombre del preset (se guarda dentro del modelo):',
            title='Link VG - Guardar preset')
        name = sanitize_preset_name(raw_name)
        if not name:
            return
        existing = None
        for preset_name, view in get_preset_views():
            if preset_name.lower() == name.lower():
                existing = view
                name = preset_name
                break
        if existing and not forms.alert(
                u'El preset "{}" ya existe. Sobrescribir?'.format(name),
                title='Link VG', yes=True, no=True):
            return

        transaction = DB.Transaction(doc, 'Link VG - guardar preset')
        transaction.Start()
        try:
            bank = existing
            if bank is None:
                option = DB.ViewDuplicateOption.Duplicate
                if not self.active_view.CanViewBeDuplicated(option):
                    transaction.RollBack()
                    forms.alert('Esta vista no se puede duplicar; abra otra '
                                'vista para guardar el preset.',
                                title='Link VG')
                    return
                bank = doc.GetElement(self.active_view.Duplicate(option))
                bank.Name = PRESET_PREFIX + name
                # Sin template la vista-banco no bloquea sus link overrides
                bank.ViewTemplateId = DB.ElementId.InvalidElementId
            for link in self.links:
                settings = get_settings_or_default(self.active_view, link.Id)
                bank.SetLinkOverrides(link.Id, settings)
            status = transaction.Commit()
            if status != DB.TransactionStatus.Committed:
                forms.alert('Revit revirtio la transaccion; el preset no se '
                            'guardo.', title='Link VG')
                return
        except Exception as exc:
            transaction.RollBack()
            forms.alert('No se pudo guardar el preset:\n{}'.format(exc),
                        title='Link VG')
            return
        self.populate_presets(select_name=name)
        forms.alert(u'Preset "{}" guardado (vista {}{}).'
                    .format(name, PRESET_PREFIX, name), title='Link VG')

    def on_preset_apply(self, sender, args):
        preset = self.get_selected_preset()
        if preset is None:
            forms.alert('No hay preset seleccionado. Guarde uno primero.',
                        title='Link VG')
            return
        targets = self.get_checked_links()
        if not targets:
            forms.alert('Marque al menos un Revit Link.', title='Link VG')
            return
        preset_name, bank = preset

        applied, failures = 0, []
        transaction = DB.Transaction(doc, 'Link VG - aplicar preset')
        transaction.Start()
        try:
            for link in targets:
                try:
                    settings = get_settings_or_default(bank, link.Id)
                    self.active_view.SetLinkOverrides(link.Id, settings)
                    applied += 1
                except Exception as exc:
                    failures.append((link, str(exc)))
            status = transaction.Commit()
            if status != DB.TransactionStatus.Committed:
                forms.alert('Revit revirtio la transaccion; no se aplico '
                            'ningun cambio.', title='Link VG')
                return
        except Exception as exc:
            transaction.RollBack()
            forms.alert('Error inesperado, no se aplico ningun cambio:\n{}'
                        .format(exc), title='Link VG')
            return

        self.populate_links()
        self.report_results(applied, 0, failures, [])

    def on_preset_delete(self, sender, args):
        preset = self.get_selected_preset()
        if preset is None:
            return
        preset_name, bank = preset
        if not forms.alert(u'Eliminar el preset "{}"? (se borra la vista '
                           '{}{})'.format(preset_name, PRESET_PREFIX,
                                          preset_name),
                           title='Link VG', yes=True, no=True):
            return
        if bank.Id == self.active_view.Id:
            forms.alert('No se puede eliminar el preset de la vista activa. '
                        'Cambie de vista primero.', title='Link VG')
            return
        transaction = DB.Transaction(doc, 'Link VG - eliminar preset')
        transaction.Start()
        try:
            doc.Delete(bank.Id)
            transaction.Commit()
        except Exception as exc:
            transaction.RollBack()
            forms.alert('No se pudo eliminar el preset:\n{}'.format(exc),
                        title='Link VG')
            return
        self.populate_presets()

    # ---------- aplicacion manual ----------

    def on_apply(self, sender, args):
        targets = self.get_checked_links()
        if not targets:
            forms.alert('Marque al menos un Revit Link.', title='Link VG')
            return

        copying = bool(self.copy_chk.IsChecked)
        src_link = self.get_copy_source() if copying else None
        vis_choice = self.vis_cb.SelectedItem
        halftone_choice = self.halftone_cb.SelectedItem
        mode_choice = NO_CHANGE if copying else self.mode_cb.SelectedItem
        linked_view_name = self.linked_view_cb.SelectedItem

        if copying and src_link is None:
            forms.alert('Seleccione el link origen para copiar.',
                        title='Link VG')
            return
        if mode_choice == MODE_BY_LINK and not linked_view_name:
            forms.alert('Seleccione la vista vinculada para "By Linked View".',
                        title='Link VG')
            return
        if (not copying and vis_choice == NO_CHANGE
                and halftone_choice == NO_CHANGE and mode_choice == NO_CHANGE):
            forms.alert('No hay ajustes que aplicar: todo esta en '
                        '"No cambiar".', title='Link VG')
            return

        applied, partial = 0, 0
        failures, warnings = [], []
        transaction = DB.Transaction(doc, 'Link VG - vista activa')
        transaction.Start()
        try:
            for link in targets:
                changed = False
                fails_before = len(failures)
                try:
                    if copying:
                        changed = self.apply_copy(link, src_link, failures,
                                                  warnings) or changed
                    elif mode_choice != NO_CHANGE:
                        changed = self.apply_mode(link, mode_choice,
                                                  linked_view_name,
                                                  failures) or changed
                    if halftone_choice != NO_CHANGE:
                        changed = self.apply_halftone(
                            link, halftone_choice == 'Activado') or changed
                    if vis_choice != NO_CHANGE:
                        changed = self.apply_visibility(
                            link, vis_choice == 'Visible', failures) or changed
                except Exception as exc:
                    failures.append((link, str(exc)))
                if len(failures) > fails_before:
                    if changed:
                        partial += 1
                elif changed:
                    applied += 1
            status = transaction.Commit()
            if status != DB.TransactionStatus.Committed:
                forms.alert('Revit revirtio la transaccion; no se aplico '
                            'ningun cambio.', title='Link VG')
                return
        except Exception as exc:
            transaction.RollBack()
            forms.alert('Error inesperado, no se aplico ningun cambio:\n{}'
                        .format(exc), title='Link VG')
            return

        self.populate_links()
        self.report_results(applied, partial, failures, warnings)

    def report_results(self, applied, partial, failures, warnings):
        lines = ['Procesados sin errores: {} links.'.format(applied)]
        if partial:
            lines.append('Aplicados parcialmente (con problemas): {} links.'
                         .format(partial))
        if warnings:
            lines.append('')
            lines.append('Avisos:')
            for link, reason in warnings[:6]:
                lines.append(u'- {}: {}'.format(link.Name, reason))
        if failures:
            lines.append('')
            lines.append('{} problemas:'.format(len(failures)))
            for link, reason in failures[:12]:
                lines.append(u'- {}: {}'.format(link.Name, reason))
            if len(failures) > 12:
                lines.append('... y {} mas (ver Output).'.format(
                    len(failures) - 12))
            for link, reason in failures:
                output.print_md(u'**{}**: {}'.format(link.Name, reason))
        forms.alert('\n'.join(lines), title='Link VG')


if __name__ == '__main__':
    active_view = doc.ActiveView
    can_override = False
    try:
        can_override = active_view.AreGraphicsOverridesAllowed()
    except Exception:
        pass
    if not can_override:
        forms.alert('La vista activa no admite overrides de '
                    'Visibility/Graphics.', title='Link VG')
    else:
        all_links = get_top_level_links()
        if not all_links:
            forms.alert('El modelo no tiene Revit Links cargados.',
                        title='Link VG')
        else:
            LinkVGWindow('ui.xaml', active_view, all_links).ShowDialog()
