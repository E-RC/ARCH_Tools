# -*- coding: utf-8 -*-
"""
PRO-Batch Export tool for pyRevit.
Enables simultaneous export of Revit sheets to DWG and PDF formats
with parametric naming rules.
"""

from pyrevit import revit, DB, forms, script
import os
import json
import datetime
import re
import System
from System.Collections.ObjectModel import ObservableCollection
from System.Collections.Generic import List

doc = revit.doc
uidoc = revit.uidoc

class SheetRow(object):
    """View model class for displaying sheet rows in the DataGrid with two-way selection support."""
    def __init__(self, sheet, number, name, revision, phase, is_selected=True):
        self.sheet = sheet
        self._sheet_number = number or ""
        self._sheet_name = name or ""
        self._current_revision = revision or ""
        self._current_phase = phase or ""
        self._custom_file_name = ""
        self._is_selected = is_selected

    @property
    def CustomFileName(self):
        return self._custom_file_name
        
    @CustomFileName.setter
    def CustomFileName(self, value):
        self._custom_file_name = value

    @property
    def IsSelected(self):
        return self._is_selected
        
    @IsSelected.setter
    def IsSelected(self, value):
        self._is_selected = value

    @property
    def SheetNumber(self):
        return self._sheet_number

    @property
    def SheetName(self):
        return self._sheet_name

    @property
    def CurrentRevision(self):
        return self._current_revision

    @property
    def CurrentPhase(self):
        return self._current_phase


class NamingParamItem(object):
    """Item structure for displaying parameters in the nomenclature ListBox."""
    def __init__(self, display_name, internal_name):
        self.display_name = display_name
        self.internal_name = internal_name


class PROBatchExportWindow(forms.WPFWindow):
    def __init__(self, xaml_file_name):
        forms.WPFWindow.__init__(self, xaml_file_name)
        
        # Load sheets from the project
        self.all_sheets = self.get_project_sheets()
        if not self.all_sheets:
            forms.alert("No se encontraron láminas (ViewSheet) válidas en el proyecto.", exitscript=True)
            
        self.all_sheet_rows = []
        for s in self.all_sheets:
            num = s.SheetNumber or ""
            name = s.Name or ""
            
            # Safe revision lookup
            rev = getattr(s, "CurrentRevision", None)
            if rev is None:
                p = s.LookupParameter("Revision de la lamina") or s.LookupParameter("Current Revision")
                rev = p.AsString() if (p and p.HasValue) else ""
            
            phase = self.get_sheet_phase(s)
            
            row = SheetRow(s, num, name, rev, phase, is_selected=True)
            self.all_sheet_rows.append(row)
            
        self.sheets_datagrid.ItemsSource = ObservableCollection[object](self.all_sheet_rows)
        
        # Load Native PDF Paper Sizes
        paper_sizes = ["<Use Sheet Size>"]
        for name in sorted(list(System.Enum.GetNames(DB.ExportPaperFormat))):
            if name != "Default":
                paper_sizes.append(name)
        if hasattr(self, 'pdf_size_cb'):
            self.pdf_size_cb.ItemsSource = paper_sizes
            self.pdf_size_cb.SelectedIndex = 0
            
        # Load Revit DWG Export Setups
        dwg_settings_col = DB.FilteredElementCollector(doc).OfClass(DB.ExportDWGSettings).ToElements()
        self.dwg_settings = sorted(list(dwg_settings_col), key=lambda x: x.Name)
        if self.dwg_settings:
            self.dwg_settings_cb.ItemsSource = self.dwg_settings
            self.dwg_settings_cb.SelectedIndex = 0
            
        # Load ViewSheetSets (Saved Sets)
        self.view_sheet_sets = sorted(list(DB.FilteredElementCollector(doc).OfClass(DB.ViewSheetSet).ToElements()), key=lambda x: x.Name)
        if self.view_sheet_sets:
            self.sheet_sets_cb.ItemsSource = self.view_sheet_sets
            
        # Initialize search watermark
        self.init_search_watermark()
        
        # Populate Naming Parameters
        self.populate_naming_parameters()
        
        # Load persistent configurations
        self.config = script.get_config()
        self.load_persistent_config()
        
        # Load Profiles
        self.profiles_dir = os.path.join(os.environ["APPDATA"], "Archiplan", "PROBatchExport", "Presets")
        if not os.path.exists(self.profiles_dir):
            try:
                os.makedirs(self.profiles_dir)
            except Exception:
                pass
        self.load_available_profiles()
        
        # Initial live preview update
        self.update_live_preview()

    def get_project_sheets(self):
        """Collect all sheets in the document, excluding placeholders."""
        collector = DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet).ToElements()
        sheets = []
        for s in collector:
            if not s.IsPlaceholder:
                sheets.append(s)
        return sorted(sheets, key=lambda x: x.SheetNumber)

    def get_sheet_phase(self, sheet):
        """Tries to extract phase information from the sheet using Archiplan shared parameters or standard properties."""
        # Archiplan shared parameter
        param = sheet.LookupParameter("ARCH_Fase")
        if param and param.HasValue:
            return param.AsValueString() or param.AsString() or ""
            
        # Common phase parameters
        for p_name in ["Fase", "Phase", "Fase de construccion", "Construction Phase"]:
            param = sheet.LookupParameter(p_name)
            if param and param.HasValue:
                return param.AsValueString() or param.AsString() or ""
        return ""

    def get_parameter_value(self, element, param_name):
        """Safe extraction of a parameter value from a Revit element."""
        if not element:
            return ""
        param = element.LookupParameter(param_name)
        if param and param.HasValue:
            val = param.AsValueString()
            if not val:
                val = param.AsString()
            if not val:
                if param.StorageType == DB.StorageType.Integer:
                    val = str(param.AsInteger())
                elif param.StorageType == DB.StorageType.Double:
                    val = str(param.AsDouble())
                elif param.StorageType == DB.StorageType.String:
                    val = param.AsString()
            return val or ""
        return ""

    def clean_filename(self, filename):
        """Removes characters prohibited by Windows OS filenames."""
        cleaned = re.sub(r'[\\/:*?"<>|]', '', filename)
        return cleaned.strip()

    def init_search_watermark(self):
        """Sets up the watermark text for the search box."""
        self.watermark = "Search sheets by number, name or phase..."
        self.search_txt.Text = self.watermark
        import System.Windows.Media as Media
        self.search_txt.Foreground = Media.Brushes.Gray
        
        # Bind events for focus states
        self.search_txt.GotFocus += self.search_txt_GotFocus
        self.search_txt.LostFocus += self.search_txt_LostFocus

    def search_txt_GotFocus(self, sender, args):
        if self.search_txt.Text == self.watermark:
            self.search_txt.Text = ""
            import System.Windows.Media as Media
            self.search_txt.Foreground = Media.Brushes.Black

    def search_txt_LostFocus(self, sender, args):
        if not self.search_txt.Text.strip():
            self.search_txt.Text = self.watermark
            import System.Windows.Media as Media
            self.search_txt.Foreground = Media.Brushes.Gray

    def update_datagrid_filter(self):
        text = self.search_txt.Text
        search_term = ""
        if text != getattr(self, 'watermark', ''):
            search_term = text.lower().strip()
            
        hide_unselected = getattr(self.hide_unselected_chk, 'IsChecked', False)
        
        filtered = []
        for row in self.all_sheet_rows:
            # Condition A: Search Box
            match_search = True
            if search_term:
                match_search = (search_term in row.SheetNumber.lower() or 
                                search_term in row.SheetName.lower() or 
                                search_term in row.CurrentRevision.lower() or 
                                search_term in row.CurrentPhase.lower())
                                
            # Condition B: Hide Unselected
            match_hide = True
            if hide_unselected:
                match_hide = row.IsSelected
                
            if match_search and match_hide:
                filtered.append(row)
                
        self.sheets_datagrid.ItemsSource = ObservableCollection[object](filtered)
        self.sheets_datagrid.Items.Refresh()

    def search_txt_TextChanged(self, sender, args):
        # Debounce: rebuilding the DataGrid's ItemsSource on every keystroke causes
        # a visible render stutter/blur, so wait for a short pause in typing instead.
        if not hasattr(self, '_search_debounce_timer'):
            import System.Windows.Threading as Threading
            self._search_debounce_timer = Threading.DispatcherTimer()
            self._search_debounce_timer.Interval = System.TimeSpan.FromMilliseconds(200)
            self._search_debounce_timer.Tick += self._search_debounce_tick
        self._search_debounce_timer.Stop()
        self._search_debounce_timer.Start()

    def _search_debounce_tick(self, sender, args):
        self._search_debounce_timer.Stop()
        self.update_datagrid_filter()

    def filter_event_triggered(self, sender, args):
        self.update_datagrid_filter()

    def populate_naming_parameters(self):
        """Populates the lists of eligible parameters in the TabControl."""
        sheet_items = []
        view_items = []
        proj_items = []
        
        # Native Sheet Parameters
        sheet_items.append(NamingParamItem("Sheet Number", "Sheet Number"))
        sheet_items.append(NamingParamItem("Sheet Name", "Sheet Name"))
        sheet_items.append(NamingParamItem("Current Revision", "Current Revision"))
        sheet_items.append(NamingParamItem("Current Phase", "Current Phase"))
        
        # Project Information
        proj_items.append(NamingParamItem("Project Name", "Project Name"))
        proj_items.append(NamingParamItem("Project Number", "Project Number"))
        proj_items.append(NamingParamItem("Current Date (YYYYMMDD)", "Current Date"))
        
        # View Parameters
        view_items.append(NamingParamItem("View Name", "View Name"))
        view_items.append(NamingParamItem("Scale", "Scale"))
        view_items.append(NamingParamItem("Detail Level", "Detail Level"))
        view_items.append(NamingParamItem("Title on Sheet", "Title on Sheet"))
        
        # Scan first few sheets to find custom sheet parameters
        sheet_params = set()
        for s in self.all_sheets[:5]:
            for p in s.Parameters:
                if p.Definition and p.Definition.Name:
                    name = p.Definition.Name
                    if name not in ["Sheet Number", "Sheet Name", "Current Revision", "Fase", "Phase", "ARCH_Fase"]:
                        sheet_params.add(name)
                        
        for name in sorted(list(sheet_params)):
            sheet_items.append(NamingParamItem("Sheet: {}".format(name), name))
            
        # Collect Project Info parameters
        proj_info = doc.ProjectInformation
        if proj_info:
            proj_params = set()
            for p in proj_info.Parameters:
                if p.Definition and p.Definition.Name and p.HasValue:
                    proj_params.add(p.Definition.Name)
            for name in sorted(list(proj_params)):
                proj_items.append(NamingParamItem("Project: {}".format(name), name))
                
        self.sheet_params_lb.ItemsSource = sheet_items
        self.view_params_lb.ItemsSource = view_items
        self.proj_params_lb.ItemsSource = proj_items

    def load_persistent_config(self):
        """Loads values from the pyRevit configuration manager."""
        # Restore window position and size
        win_w = self.config.get_option('window_width', 1200)
        win_h = self.config.get_option('window_height', 750)
        win_t = self.config.get_option('window_top', -9999)
        win_l = self.config.get_option('window_left', -9999)
        self.Width = win_w
        self.Height = win_h
        if win_t != -9999:
            self.Top = win_t
        if win_l != -9999:
            self.Left = win_l

        # Folder path
        last_folder = self.config.get_option('folder_path', '')
        if last_folder and os.path.exists(last_folder):
            self.folder_path_txt.Text = last_folder
            
        # Naming pattern
        last_pattern = self.config.get_option('naming_pattern', '[Sheet Number]_[Sheet Name]')
        self.naming_pattern_txt.Text = last_pattern
        
        # Separator index
        last_sep_idx = self.config.get_option('separator_index', 0)
        if 0 <= last_sep_idx < self.separator_cb.Items.Count:
            self.separator_cb.SelectedIndex = last_sep_idx
            
        # Checkbox states
        self.export_pdf_chk.IsChecked = self.config.get_option('export_pdf', True)
        self.pdf_combine_true.IsChecked = self.config.get_option('pdf_combine', False)
        self.pdf_combine_false.IsChecked = not self.pdf_combine_true.IsChecked
        self.export_dwg_chk.IsChecked = self.config.get_option('export_dwg', False)
        self.create_subfolders_chk.IsChecked = self.config.get_option('create_subfolders', True)
        
        # New PDF Options
        self.pdf_size_cb.SelectedIndex = self.config.get_option('pdf_size', 0)
        self.pdf_orient_landscape.IsChecked = self.config.get_option('pdf_landscape', True)
        self.pdf_orient_portrait.IsChecked = not self.pdf_orient_landscape.IsChecked
        self.pdf_placement_cb.SelectedIndex = self.config.get_option('pdf_placement', 0)
        self.pdf_zoom_custom.IsChecked = self.config.get_option('pdf_zoom_custom', False)
        self.pdf_zoom_fit.IsChecked = not self.pdf_zoom_custom.IsChecked
        self.pdf_zoom_val_txt.Text = str(self.config.get_option('pdf_zoom_val', 100))
        
        self.pdf_quality_cb.SelectedIndex = self.config.get_option('pdf_quality', 2)
        self.pdf_color_depth_cb.SelectedIndex = self.config.get_option('pdf_color_depth', 0)
        
        self.pdf_blue_links_chk.IsChecked = self.config.get_option('pdf_blue_links', False)
        self.pdf_hide_unref_chk.IsChecked = self.config.get_option('pdf_hide_unref', True)
        self.pdf_mask_lines_chk.IsChecked = self.config.get_option('pdf_mask_lines', False)
        self.pdf_hide_scope_chk.IsChecked = self.config.get_option('pdf_hide_scope', True)
        self.pdf_hide_crop_chk.IsChecked = self.config.get_option('pdf_hide_crop', True)
        self.pdf_hide_ref_chk.IsChecked = self.config.get_option('pdf_hide_ref', True)
        
        # ComboBox selections
        last_dwg_setting = self.config.get_option('selected_dwg_setting', '')
        if last_dwg_setting and self.dwg_settings:
            for item in self.dwg_settings:
                if item.Name == last_dwg_setting:
                    self.dwg_settings_cb.SelectedItem = item
                    break

    def save_persistent_config(self):
        """Saves current GUI selections to pyRevit configuration manager."""
        self.config.window_width = self.Width
        self.config.window_height = self.Height
        self.config.window_top = self.Top
        self.config.window_left = self.Left
        
        self.config.folder_path = self.folder_path_txt.Text
        self.config.naming_pattern = self.naming_pattern_txt.Text
        self.config.separator_index = self.separator_cb.SelectedIndex
        self.config.export_pdf = self.export_pdf_chk.IsChecked
        self.config.export_dwg = self.export_dwg_chk.IsChecked
        self.config.pdf_combine = self.pdf_combine_true.IsChecked
        self.config.create_subfolders = self.create_subfolders_chk.IsChecked
        
        self.config.pdf_size = self.pdf_size_cb.SelectedIndex
        self.config.pdf_landscape = self.pdf_orient_landscape.IsChecked
        self.config.pdf_placement = self.pdf_placement_cb.SelectedIndex
        self.config.pdf_zoom_custom = self.pdf_zoom_custom.IsChecked
        try:
            self.config.pdf_zoom_val = int(self.pdf_zoom_val_txt.Text)
        except:
            self.config.pdf_zoom_val = 100
            
        self.config.pdf_quality = self.pdf_quality_cb.SelectedIndex
        self.config.pdf_color_depth = self.pdf_color_depth_cb.SelectedIndex
        
        self.config.pdf_blue_links = self.pdf_blue_links_chk.IsChecked
        self.config.pdf_hide_unref = self.pdf_hide_unref_chk.IsChecked
        self.config.pdf_mask_lines = self.pdf_mask_lines_chk.IsChecked
        self.config.pdf_hide_scope = self.pdf_hide_scope_chk.IsChecked
        self.config.pdf_hide_crop = self.pdf_hide_crop_chk.IsChecked
        self.config.pdf_hide_ref = self.pdf_hide_ref_chk.IsChecked
        
        sel_dwg = self.dwg_settings_cb.SelectedItem
        self.config.selected_dwg_setting = sel_dwg.Name if sel_dwg else ''
        
        script.save_config()

    def resolve_name(self, sheet, pattern):
        """Resolves the naming pattern for a given sheet using parameters."""
        if not pattern:
            return sheet.SheetNumber or "LAMINA"
            
        tags = re.findall(r'\[([^\]]+)\]', pattern)
        resolved = pattern
        
        for tag in tags:
            val = ""
            if tag == "Sheet Number":
                val = sheet.SheetNumber or ""
            elif tag == "Sheet Name":
                val = sheet.Name or ""
            elif tag == "Current Revision":
                val = getattr(sheet, "CurrentRevision", None)
                if val is None:
                    p = sheet.LookupParameter("Revision de la lamina") or sheet.LookupParameter("Current Revision")
                    val = p.AsString() if (p and p.HasValue) else ""
                val = val or ""
            elif tag == "Current Phase":
                val = self.get_sheet_phase(sheet)
            elif tag == "Project Name":
                val = doc.ProjectInformation.Name if doc.ProjectInformation else ""
            elif tag == "Project Number":
                val = doc.ProjectInformation.Number if doc.ProjectInformation else ""
            elif tag == "Current Date":
                val = datetime.datetime.now().strftime("%Y%m%d")
            else:
                val = self.get_parameter_value(sheet, tag)
                if not val:
                    val = self.get_parameter_value(doc.ProjectInformation, tag)
                if not val and hasattr(sheet, "GetAllPlacedViews"):
                    views = sheet.GetAllPlacedViews()
                    if views and views.Count > 0:
                        first_view_id = None
                        for v_id in views:
                            first_view_id = v_id
                            break
                        if first_view_id:
                            first_view = doc.GetElement(first_view_id)
                            val = self.get_parameter_value(first_view, tag)
                        
            resolved = resolved.replace("[{}]".format(tag), val)
            
        resolved = self.clean_filename(resolved)
        return resolved if resolved.strip() else "LAMINA_SIN_NOMBRE"

    def update_live_preview(self):
        """Updates the live preview text and custom filenames in the grid."""
        if not self.all_sheet_rows:
            self.naming_preview_txt.Text = "No hay láminas en el modelo."
            return
            
        pattern = self.naming_pattern_txt.Text or ""
        
        for row in self.all_sheet_rows:
            if pattern:
                row.CustomFileName = self.resolve_name(row.sheet, pattern)
            else:
                row.CustomFileName = ""
                
        if hasattr(self, 'sheets_datagrid') and self.sheets_datagrid.ItemsSource:
            self.sheets_datagrid.Items.Refresh()
            
        preview_row = self.all_sheet_rows[0]
        if pattern:
            resolved_name = preview_row.CustomFileName
        else:
            self.naming_preview_txt.Text = "Adjust pattern to see preview..."
            return
        
        exts = []
        if self.export_pdf_chk.IsChecked:
            exts.append(".pdf")
        if self.export_dwg_chk.IsChecked:
            exts.append(".dwg")
            
        if exts:
            ext_str = " | ".join(["{}{}".format(resolved_name, ext) for ext in exts])
            self.naming_preview_txt.Text = ext_str
        else:
            self.naming_preview_txt.Text = resolved_name

    # Obsolete mapping functions removed for Native PDF

    # --- UI EVENT HANDLERS ---
    def Header_MouseDown(self, sender, args):
        if args.LeftButton == System.Windows.Input.MouseButtonState.Pressed:
            self.DragMove()

    def minimize_btn_Click(self, sender, args):
        self.WindowState = System.Windows.WindowState.Minimized

    def close_btn_Click(self, sender, args):
        self.Close()

    def select_all_btn_Click(self, sender, args):
        for row in self.all_sheet_rows:
            row.IsSelected = True
        self.update_datagrid_filter()
        self.update_live_preview()

    def select_none_btn_Click(self, sender, args):
        for row in self.all_sheet_rows:
            row.IsSelected = False
        self.update_datagrid_filter()
        self.update_live_preview()
        
    def sheet_sets_cb_SelectionChanged(self, sender, args):
        sel_set = self.sheet_sets_cb.SelectedItem
        if not sel_set:
            return
            
        set_sheet_ids = [v.Id.IntegerValue for v in sel_set.Views]
        
        for row in self.all_sheet_rows:
            if row.sheet.Id.IntegerValue in set_sheet_ids:
                row.IsSelected = True
            else:
                row.IsSelected = False
                
        self.update_datagrid_filter()
        self.update_live_preview()

    def Format_CheckedChange(self, sender, args):
        is_pdf = self.export_pdf_chk.IsChecked if self.export_pdf_chk else False
        is_dwg = self.export_dwg_chk.IsChecked if self.export_dwg_chk else False
        if self.export_btn:
            self.export_btn.IsEnabled = is_pdf or is_dwg
        self.update_live_preview()

    def pdf_zoom_Checked(self, sender, args):
        if hasattr(self, 'pdf_zoom_custom') and hasattr(self, 'pdf_zoom_val_txt'):
            self.pdf_zoom_val_txt.IsEnabled = self.pdf_zoom_custom.IsChecked

    def param_lb_MouseDoubleClick(self, sender, args):
        if sender.SelectedItem:
            self.add_parameter_to_pattern(sender.SelectedItem)

    def add_param_btn_Click(self, sender, args):
        # Determine which list is active
        selected = None
        tab_header = self.param_tabs.SelectedItem.Header if self.param_tabs.SelectedItem else ""
        if tab_header == "Lámina":
            selected = self.sheet_params_lb.SelectedItem
        elif tab_header == "Vista":
            selected = self.view_params_lb.SelectedItem
        elif tab_header == "Proyecto":
            selected = self.proj_params_lb.SelectedItem
            
        if selected:
            self.add_parameter_to_pattern(selected)

    def add_parameter_to_pattern(self, selected_item):
        if not selected_item:
            return
            
        tag = "[{}]".format(selected_item.internal_name)
        pattern = self.naming_pattern_txt.Text or ""
        
        # Determine cursor position
        cursor_pos = self.naming_pattern_txt.SelectionStart
        
        # Add separator if requested and we are not at the very beginning of the string
        sep = ""
        if self.separator_cb.SelectedItem:
            sel_content = self.separator_cb.SelectedItem.Content
            if sel_content != "[Ninguno]":
                sep = str(sel_content)
                
        # Only prepend separator if there's text before the cursor
        if cursor_pos > 0:
            tag = sep + tag
            
        new_pattern = pattern[:cursor_pos] + tag + pattern[cursor_pos:]
        self.naming_pattern_txt.Text = new_pattern
        
        # Move cursor to the end of the newly inserted tag
        new_cursor_pos = cursor_pos + len(tag)
        self.naming_pattern_txt.SelectionStart = new_cursor_pos
        self.naming_pattern_txt.Focus()
        
        self.update_live_preview()

    def naming_pattern_txt_TextChanged(self, sender, args):
        self.update_live_preview()

    def clear_pattern_btn_Click(self, sender, args):
        self.naming_pattern_txt.Text = ""
        self.update_live_preview()
        
    def open_naming_btn_Click(self, sender, args):
        self.NamingPopup.IsOpen = True

    def load_available_profiles(self):
        """Loads available JSON profiles from the profiles directory."""
        if not os.path.exists(self.profiles_dir):
            return
            
        profiles = []
        for f in os.listdir(self.profiles_dir):
            if f.endswith(".json"):
                profiles.append(f.replace(".json", ""))
                
        self.profiles_cb.ItemsSource = sorted(profiles)
        self.profiles_cb.SelectedIndex = -1

    def profiles_cb_SelectionChanged(self, sender, args):
        if self.profiles_cb.SelectedIndex == -1:
            return
            
        profile_name = self.profiles_cb.SelectedItem
        if not profile_name:
            return
            
        file_path = os.path.join(self.profiles_dir, profile_name + ".json")
        if os.path.exists(file_path):
            try:
                with open(file_path, "r") as f:
                    data = json.load(f)
                    
                # Apply data to UI
                self.naming_pattern_txt.Text = data.get("naming_pattern", "")
                self.separator_cb.SelectedIndex = data.get("separator_index", 0)
                
                self.export_pdf_chk.IsChecked = data.get("export_pdf", True)
                self.pdf_combine_true.IsChecked = data.get("pdf_combine", False)
                self.pdf_combine_false.IsChecked = not self.pdf_combine_true.IsChecked
                self.export_dwg_chk.IsChecked = data.get("export_dwg", False)
                self.create_subfolders_chk.IsChecked = data.get("create_subfolders", True)
                
                # Restore New PDF Settings
                self.pdf_size_cb.SelectedIndex = data.get("pdf_size", 0)
                self.pdf_orient_landscape.IsChecked = data.get("pdf_landscape", True)
                self.pdf_orient_portrait.IsChecked = not self.pdf_orient_landscape.IsChecked
                self.pdf_placement_cb.SelectedIndex = data.get("pdf_placement", 0)
                self.pdf_zoom_custom.IsChecked = data.get("pdf_zoom_custom", False)
                self.pdf_zoom_fit.IsChecked = not self.pdf_zoom_custom.IsChecked
                self.pdf_zoom_val_txt.Text = str(data.get("pdf_zoom_val", "100"))
                
                self.pdf_quality_cb.SelectedIndex = data.get("pdf_quality", 2)
                self.pdf_color_depth_cb.SelectedIndex = data.get("pdf_color_depth", 0)
                
                self.pdf_blue_links_chk.IsChecked = data.get("pdf_blue_links", False)
                self.pdf_hide_unref_chk.IsChecked = data.get("pdf_hide_unref", True)
                self.pdf_mask_lines_chk.IsChecked = data.get("pdf_mask_lines", False)
                self.pdf_hide_scope_chk.IsChecked = data.get("pdf_hide_scope", True)
                self.pdf_hide_crop_chk.IsChecked = data.get("pdf_hide_crop", True)
                self.pdf_hide_ref_chk.IsChecked = data.get("pdf_hide_ref", True)
                
                # Restore DWG Settings
                dwg_setting_name = data.get("selected_dwg_setting", "")
                if dwg_setting_name and self.dwg_settings:
                    for item in self.dwg_settings:
                        if item.Name == dwg_setting_name:
                            self.dwg_settings_cb.SelectedItem = item
                            break
                            
                self.update_live_preview()
            except Exception as e:
                forms.alert("Error al cargar el perfil:\n{}".format(e))

    def save_profile_btn_Click(self, sender, args):
        profile_name = forms.ask_for_string(prompt="Ingrese el nombre para el nuevo perfil:", title="Guardar Perfil")
        if not profile_name:
            return
            
        # Clean the name for Windows file system
        profile_name = self.clean_filename(profile_name)
        if not profile_name:
            forms.alert("Nombre de perfil inválido.")
            return
            
        if not os.path.exists(self.profiles_dir):
            try:
                os.makedirs(self.profiles_dir)
            except Exception as e:
                forms.alert("No se pudo crear la carpeta de perfiles:\n{}".format(e))
                return
                
        file_path = os.path.join(self.profiles_dir, profile_name + ".json")
        
        # Build dictionary with explicit python types to avoid JSON serialization errors of .NET objects
        data = {
            "naming_pattern": unicode(self.naming_pattern_txt.Text or ""),
            "separator_index": int(self.separator_cb.SelectedIndex),
            "export_pdf": bool(self.export_pdf_chk.IsChecked),
            "pdf_combine": bool(self.pdf_combine_true.IsChecked),
            "export_dwg": bool(self.export_dwg_chk.IsChecked),
            "create_subfolders": bool(self.create_subfolders_chk.IsChecked),
            "pdf_size": int(self.pdf_size_cb.SelectedIndex),
            "pdf_landscape": bool(self.pdf_orient_landscape.IsChecked),
            "pdf_placement": int(self.pdf_placement_cb.SelectedIndex),
            "pdf_zoom_custom": bool(self.pdf_zoom_custom.IsChecked),
            "pdf_zoom_val": unicode(self.pdf_zoom_val_txt.Text or "100"),
            "pdf_quality": int(self.pdf_quality_cb.SelectedIndex),
            "pdf_color_depth": int(self.pdf_color_depth_cb.SelectedIndex),
            "pdf_blue_links": bool(self.pdf_blue_links_chk.IsChecked),
            "pdf_hide_unref": bool(self.pdf_hide_unref_chk.IsChecked),
            "pdf_mask_lines": bool(self.pdf_mask_lines_chk.IsChecked),
            "pdf_hide_scope": bool(self.pdf_hide_scope_chk.IsChecked),
            "pdf_hide_crop": bool(self.pdf_hide_crop_chk.IsChecked),
            "pdf_hide_ref": bool(self.pdf_hide_ref_chk.IsChecked)
        }
        
        sel_dwg = self.dwg_settings_cb.SelectedItem
        data["selected_dwg_setting"] = unicode(sel_dwg.Name) if sel_dwg else ""
        
        try:
            import codecs
            with codecs.open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
                
            # Reload list and select the new profile
            self.load_available_profiles()
            # Suppress event momentarily or just set it
            for item in self.profiles_cb.ItemsSource:
                if item == profile_name:
                    self.profiles_cb.SelectedItem = item
                    break
                    
            forms.toast(title="Perfil Guardado", message="El perfil '{}' se guardó correctamente.".format(profile_name))
        except Exception as e:
            forms.alert("Error al guardar el perfil:\n{}".format(e))

    def delete_profile_btn_Click(self, sender, args):
        if self.profiles_cb.SelectedIndex == -1:
            forms.alert("Seleccione un perfil para eliminar.")
            return
            
        profile_name = self.profiles_cb.SelectedItem
        file_path = os.path.join(self.profiles_dir, profile_name + ".json")
        
        if os.path.exists(file_path):
            res = forms.alert("¿Está seguro que desea eliminar el perfil '{}'?".format(profile_name), yes=True, no=True)
            if res:
                try:
                    os.remove(file_path)
                    self.profiles_cb.SelectedIndex = -1
                    self.load_available_profiles()
                except Exception as e:
                    forms.alert("No se pudo eliminar el archivo:\n{}".format(e))

    def export_xml_btn_Click(self, sender, args):
        import clr
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Xml")
        from System.Windows.Forms import SaveFileDialog, DialogResult
        from System.Xml import XmlDocument
        
        dialog = SaveFileDialog()
        dialog.Filter = "XML files (*.xml)|*.xml|All files (*.*)|*.*"
        dialog.Title = "Exportar Configuración a XML"
        dialog.FileName = "PROBatchExport_Config.xml"
        
        if dialog.ShowDialog() == DialogResult.OK:
            file_path = dialog.FileName
            
            data = {
                "naming_pattern": self.naming_pattern_txt.Text,
                "separator_index": str(self.separator_cb.SelectedIndex),
                "export_pdf": str(self.export_pdf_chk.IsChecked),
                "pdf_combine": str(self.pdf_combine_true.IsChecked),
                "export_dwg": str(self.export_dwg_chk.IsChecked),
                "create_subfolders": str(self.create_subfolders_chk.IsChecked),
                "pdf_size": str(self.pdf_size_cb.SelectedIndex),
                "pdf_landscape": str(self.pdf_orient_landscape.IsChecked),
                "pdf_placement": str(self.pdf_placement_cb.SelectedIndex),
                "pdf_zoom_custom": str(self.pdf_zoom_custom.IsChecked),
                "pdf_zoom_val": str(self.pdf_zoom_val_txt.Text),
                "pdf_quality": str(self.pdf_quality_cb.SelectedIndex),
                "pdf_color_depth": str(self.pdf_color_depth_cb.SelectedIndex),
                "pdf_blue_links": str(self.pdf_blue_links_chk.IsChecked),
                "pdf_hide_unref": str(self.pdf_hide_unref_chk.IsChecked),
                "pdf_mask_lines": str(self.pdf_mask_lines_chk.IsChecked),
                "pdf_hide_scope": str(self.pdf_hide_scope_chk.IsChecked),
                "pdf_hide_crop": str(self.pdf_hide_crop_chk.IsChecked),
                "pdf_hide_ref": str(self.pdf_hide_ref_chk.IsChecked)
            }
            
            sel_dwg = self.dwg_settings_cb.SelectedItem
            data["selected_dwg_setting"] = sel_dwg.Name if sel_dwg else ""
            
            try:
                xml_doc = XmlDocument()
                xml_declaration = xml_doc.CreateXmlDeclaration("1.0", "UTF-8", None)
                root = xml_doc.DocumentElement
                xml_doc.InsertBefore(xml_declaration, root)
                
                main_element = xml_doc.CreateElement("PROBatchExportConfig")
                xml_doc.AppendChild(main_element)
                
                for key, value in data.items():
                    elem = xml_doc.CreateElement(key)
                    elem.InnerText = value if value is not None else ""
                    main_element.AppendChild(elem)
                    
                xml_doc.Save(file_path)
                    
                forms.toast(title="Configuración Exportada", message="La configuración se exportó correctamente a XML.")
            except Exception as e:
                forms.alert("Error al exportar a XML:\n{}".format(e))

    def import_xml_btn_Click(self, sender, args):
        import clr
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Xml")
        from System.Windows.Forms import OpenFileDialog, DialogResult
        from System.Xml import XmlDocument
        
        dialog = OpenFileDialog()
        dialog.Filter = "XML files (*.xml)|*.xml|All files (*.*)|*.*"
        dialog.Title = "Importar Configuración desde XML"
        
        if dialog.ShowDialog() == DialogResult.OK:
            file_path = dialog.FileName
            
            try:
                xml_doc = XmlDocument()
                xml_doc.Load(file_path)
                
                root = xml_doc.DocumentElement
                if root.Name != "PROBatchExportConfig":
                    forms.alert("El archivo XML no tiene el formato correcto para PRO-Batch Export.")
                    return
                
                data = {}
                for child in root.ChildNodes:
                    data[child.Name] = child.InnerText
                
                def str2bool(v):
                    return str(v).lower() in ("yes", "true", "t", "1")
                
                self.naming_pattern_txt.Text = data.get("naming_pattern", "")
                self.separator_cb.SelectedIndex = int(data.get("separator_index", 0))
                
                self.export_pdf_chk.IsChecked = str2bool(data.get("export_pdf", "True"))
                self.pdf_combine_true.IsChecked = str2bool(data.get("pdf_combine", "False"))
                self.pdf_combine_false.IsChecked = not self.pdf_combine_true.IsChecked
                self.export_dwg_chk.IsChecked = str2bool(data.get("export_dwg", "False"))
                self.create_subfolders_chk.IsChecked = str2bool(data.get("create_subfolders", "True"))
                
                self.pdf_size_cb.SelectedIndex = int(data.get("pdf_size", 0))
                self.pdf_orient_landscape.IsChecked = str2bool(data.get("pdf_landscape", "True"))
                self.pdf_orient_portrait.IsChecked = not self.pdf_orient_landscape.IsChecked
                self.pdf_placement_cb.SelectedIndex = int(data.get("pdf_placement", 0))
                self.pdf_zoom_custom.IsChecked = str2bool(data.get("pdf_zoom_custom", "False"))
                self.pdf_zoom_fit.IsChecked = not self.pdf_zoom_custom.IsChecked
                self.pdf_zoom_val_txt.Text = str(data.get("pdf_zoom_val", "100"))
                
                self.pdf_quality_cb.SelectedIndex = int(data.get("pdf_quality", 2))
                self.pdf_color_depth_cb.SelectedIndex = int(data.get("pdf_color_depth", 0))
                
                self.pdf_blue_links_chk.IsChecked = str2bool(data.get("pdf_blue_links", "False"))
                self.pdf_hide_unref_chk.IsChecked = str2bool(data.get("pdf_hide_unref", "True"))
                self.pdf_mask_lines_chk.IsChecked = str2bool(data.get("pdf_mask_lines", "False"))
                self.pdf_hide_scope_chk.IsChecked = str2bool(data.get("pdf_hide_scope", "True"))
                self.pdf_hide_crop_chk.IsChecked = str2bool(data.get("pdf_hide_crop", "True"))
                self.pdf_hide_ref_chk.IsChecked = str2bool(data.get("pdf_hide_ref", "True"))
                
                dwg_setting_name = data.get("selected_dwg_setting", "")
                if dwg_setting_name and self.dwg_settings:
                    for item in self.dwg_settings:
                        if item.Name == dwg_setting_name:
                            self.dwg_settings_cb.SelectedItem = item
                            break
                            
                self.update_live_preview()
                forms.toast(title="Configuración Importada", message="La configuración se importó correctamente desde XML.")
            except Exception as e:
                forms.alert("Error al importar desde XML:\n{}".format(e))

    def browse_btn_Click(self, sender, args):
        folder = forms.pick_folder(title="Seleccionar Carpeta de Destino")
        if folder:
            self.folder_path_txt.Text = folder

    def export_btn_Click(self, sender, args):
        # 1. Validation of selections
        selected_rows = [row for row in self.all_sheet_rows if row.IsSelected]
        if not selected_rows:
            forms.alert("No ha seleccionado ninguna lámina para exportar.", exitscript=False)
            return
            
        dest_folder = self.folder_path_txt.Text
        if not dest_folder or not os.path.exists(dest_folder):
            forms.alert("Por favor, seleccione una carpeta de destino válida.", exitscript=False)
            return
            
        pattern = self.naming_pattern_txt.Text
        if not pattern:
            forms.alert("Por favor, defina un patrón de nomenclatura para los archivos.", exitscript=False)
            return
            
        # Validate selections based on formats checked
        is_pdf = self.export_pdf_chk.IsChecked
        is_dwg = self.export_dwg_chk.IsChecked
        is_subfolders = self.create_subfolders_chk.IsChecked
        
        dwg_setting = None
        if is_dwg:
            dwg_setting = self.dwg_settings_cb.SelectedItem
            if not dwg_setting:
                forms.alert("Por favor, seleccione una configuración de exportación DWG.", exitscript=False)
                return
                
        # Show embedded progress bar
        self.progress_panel.Visibility = System.Windows.Visibility.Visible
        self.export_btn.IsEnabled = False
        self.close_btn.IsEnabled = False
        
        def do_events():
            import System
            from System.Windows.Threading import DispatcherPriority
            self.Dispatcher.Invoke(System.Action(lambda: None), DispatcherPriority.Background)

        def update_pb(msg, current, total):
            self.export_status_txt.Text = msg
            if total > 0:
                self.export_pb.Maximum = total
                self.export_pb.Value = current
            do_events()
            
        # Save config
        self.save_persistent_config()
        
        # Count operations to do
        is_combined = self.pdf_combine_true.IsChecked
        total_ops = 0
        if is_pdf:
            total_ops += 1 if is_combined else len(selected_rows)
        if is_dwg:
            total_ops += len(selected_rows)
            
        success_count = 0
        error_count = 0
        
        # Use single TransactionGroup
        with DB.TransactionGroup(doc, "PRO-Batch Export"):
            if True:
                current_op = 0
                update_pb("Iniciando Exportación...", 0, total_ops)
                
                # --- PDF EXPORT ---
                if is_pdf:
                    pdf_dest_folder = dest_folder
                    if is_subfolders:
                        pdf_dest_folder = os.path.join(dest_folder, "PDF")
                        if not os.path.exists(pdf_dest_folder):
                            os.makedirs(pdf_dest_folder)
                            
                    # Construct PDFExportOptions manually from Native UI
                    pdf_options = DB.PDFExportOptions()
                    
                    # Page Setup
                    size_sel = self.pdf_size_cb.SelectedItem
                    if size_sel and size_sel != "<Use Sheet Size>":
                        try:
                            pdf_options.PaperFormat = getattr(DB.ExportPaperFormat, size_sel)
                        except Exception:
                            pdf_options.PaperFormat = DB.ExportPaperFormat.Default
                    else:
                        pdf_options.PaperFormat = DB.ExportPaperFormat.Default
                        
                    pdf_options.PaperOrientation = DB.PageOrientationType.Landscape if self.pdf_orient_landscape.IsChecked else DB.PageOrientationType.Portrait
                    pdf_options.PaperPlacement = DB.PaperPlacementType.Center if self.pdf_placement_cb.SelectedIndex == 0 else DB.PaperPlacementType.Margins
                    
                    if self.pdf_zoom_custom.IsChecked:
                        pdf_options.ZoomType = DB.ZoomType.Zoom
                        try:
                            pdf_options.ZoomPercentage = int(self.pdf_zoom_val_txt.Text)
                        except:
                            pdf_options.ZoomPercentage = 100
                    else:
                        pdf_options.ZoomType = DB.ZoomType.FitToPage
                        
                    # Appearance
                    q_idx = self.pdf_quality_cb.SelectedIndex
                    if q_idx == 0: pdf_options.RasterQuality = DB.RasterQualityType.Low
                    elif q_idx == 1: pdf_options.RasterQuality = DB.RasterQualityType.Medium
                    elif q_idx == 2: pdf_options.RasterQuality = DB.RasterQualityType.High
                    else: pdf_options.RasterQuality = DB.RasterQualityType.Presentation
                    
                    c_idx = self.pdf_color_depth_cb.SelectedIndex
                    if c_idx == 1: pdf_options.ColorDepth = DB.ColorDepthType.GrayScale
                    elif c_idx == 2: pdf_options.ColorDepth = DB.ColorDepthType.BlackLine
                    else: pdf_options.ColorDepth = DB.ColorDepthType.Color
                    
                    # Visibility
                    pdf_options.ViewLinksInBlue = self.pdf_blue_links_chk.IsChecked
                    pdf_options.HideUnreferencedViewTags = self.pdf_hide_unref_chk.IsChecked
                    pdf_options.MaskCoincidentLines = self.pdf_mask_lines_chk.IsChecked
                    pdf_options.HideScopeBoxes = self.pdf_hide_scope_chk.IsChecked
                    pdf_options.HideCropBoundaries = self.pdf_hide_crop_chk.IsChecked
                    pdf_options.HideReferencePlane = self.pdf_hide_ref_chk.IsChecked
                    
                    if is_combined:
                        # Find name based on first sheet
                        combined_name = self.resolve_name(selected_rows[0].sheet, pattern) + "_Combined"
                        
                        update_pb("Exportando PDF Combinado: {}...".format(combined_name), current_op, total_ops)
                        
                        sheet_ids = List[DB.ElementId]()
                        for r in selected_rows:
                            sheet_ids.Add(r.sheet.Id)
                            
                        pdf_options.Combine = True
                        pdf_options.FileName = combined_name
                        
                        try:
                            # Native export (Revit 2022+)
                            doc.Export(pdf_dest_folder, sheet_ids, pdf_options)
                            success_count += 1
                        except Exception as e:
                            error_count += 1
                            print("Error exportando PDF Combinado:\n{}".format(e))
                        current_op += 1
                    else:
                        for r in selected_rows:
                            sheet = r.sheet
                            sheet_filename = self.resolve_name(sheet, pattern)
                            
                            update_pb("Exportando PDF: {}...".format(sheet_filename), current_op, total_ops)
                            
                            sheet_ids = List[DB.ElementId]()
                            sheet_ids.Add(sheet.Id)
                            
                            # Al usar Combine=True con una sola lámina, Revit respeta el nombre exacto especificado en FileName
                            # Si usamos Combine=False, Revit ignora FileName y usa su propia regla de nomenclatura (Number - Name).
                            pdf_options.Combine = True
                            pdf_options.FileName = sheet_filename
                            
                            try:
                                doc.Export(pdf_dest_folder, sheet_ids, pdf_options)
                                success_count += 1
                            except Exception as e:
                                error_count += 1
                                print("Error exportando PDF para la lámina {}:\n{}".format(sheet.SheetNumber, e))
                            current_op += 1
                            
                # --- DWG EXPORT ---
                if is_dwg:
                    dwg_dest_folder = dest_folder
                    if is_subfolders:
                        dwg_dest_folder = os.path.join(dest_folder, "DWG")
                        if not os.path.exists(dwg_dest_folder):
                            os.makedirs(dwg_dest_folder)
                            
                    dwg_options = dwg_setting.GetDWGExportOptions()
                    
                    for r in selected_rows:
                        sheet = r.sheet
                        sheet_filename = self.resolve_name(sheet, pattern)
                        
                        update_pb("Exportando DWG: {}...".format(sheet_filename), current_op, total_ops)
                        
                        sheet_ids = List[DB.ElementId]()
                        sheet_ids.Add(sheet.Id)
                        
                        try:
                            doc.Export(dwg_dest_folder, sheet_filename, sheet_ids, dwg_options)
                            success_count += 1
                        except Exception as e:
                            error_count += 1
                            print("Error exportando DWG para la lámina {}:\n{}".format(sheet.SheetNumber, e))
                        current_op += 1
                        
        # Display completion feedback
        if success_count > 0:
            forms.toast(
                title="PRO-Batch Export",
                message="Se completó la exportación. {} archivo(s) creados en:\n{}".format(success_count, dest_folder)
            )
            if error_count > 0:
                forms.alert("La exportación finalizó con {} error(es). Revise la consola de pyRevit para más detalles.".format(error_count))
        else:
            forms.alert("No se pudo exportar ningún archivo. Revise los errores en la consola de pyRevit.")
            
        # Revert UI state instead of closing
        self.progress_panel.Visibility = System.Windows.Visibility.Collapsed
        self.export_btn.IsEnabled = True
        self.close_btn.IsEnabled = True

    def Window_Closing(self, sender, args):
        self.save_persistent_config()


if __name__ == '__main__':
    window = PROBatchExportWindow('ui.xaml')
    window.ShowDialog()
