# -*- coding: UTF-8 -*-
"""Boton Actualizar: busca una version nueva en GitHub y la instala si el usuario acepta.

Los modulos de `archi_update` (en `lib/`) hacen el trabajo; este archivo solo
muestra los mensajes. Requiere pyRevit 7.0.0 o superior (API de actualizacion
de extensiones de terceros).
"""

import os

from pyrevit import forms

MIN_PYREVIT_MAJOR = 7

try:
    from pyrevit.versionmgr import updater, get_pyrevit_version
    from pyrevit.coreutils import git as libgit  # noqa: F401 - solo comprueba que existe
    from pyrevit.loader import sessionmgr
    updater.get_thirdparty_ext_repos  # noqa: B018 - sonda: debe existir
    # Si la API de version falta o falla, se trata como "no es 7".
    HAS_UPDATER = get_pyrevit_version().major >= MIN_PYREVIT_MAJOR
except Exception:
    HAS_UPDATER = False

TITLE = u"Actualizar"
REINSTALL_COMMAND = u"pyrevit extend ui Archiplan https://github.com/E-RC/ARCH_Tools.git --branch=main"
RELOAD_NOW = u"Recargar pyRevit ahora"
RELOAD_LATER = u"Lo haré después"
UNKNOWN_VERSION = u"(versión desconocida)"


def _exception_text(ex):
    try:
        return unicode(ex)  # noqa: F821 - IronPython 2.7
    except Exception:
        return repr(ex)


def _first_line(ex):
    lines = _exception_text(ex).strip().splitlines()
    return lines[0] if lines else ex.__class__.__name__


def _files_in_use(ex):
    text = _first_line(ex).lower()
    return any(word in text for word in (u"unlink", u"access", u"in use"))


def _version_label(version):
    return u"v" + version.lstrip(u"v") if version else None


def _show(msg, sub_msg=None, warn=True):
    forms.alert(msg, sub_msg=sub_msg, title=TITLE, warn_icon=warn)


def _confirm_update(installed, repo):
    from archi_update.notes import format_notes
    from archi_update.pyrevit_git import latest_remote_tag, new_commit_messages

    lines = [u"Versión instalada: " + (_version_label(installed) or UNKNOWN_VERSION)]
    latest = latest_remote_tag(repo)
    if latest is not None:
        lines.append(u"Última versión publicada: " + latest)
    notes = format_notes(new_commit_messages(repo))
    if notes:
        lines.extend([u"", u"Cambios:", notes])
    return forms.alert(
        u"Hay una actualización disponible. ¿Quieres instalarla ahora?",
        sub_msg=u"\n".join(lines),
        title=TITLE, yes=True, no=True, ok=False, warn_icon=False)


def _update(repo):
    from archi_update.pyrevit_git import apply_update

    try:
        apply_update(repo)
    except Exception as ex:
        if _files_in_use(ex):
            _show(u"No se pudo actualizar porque hay archivos en uso. "
                  u"Cierra Revit y vuelve a intentarlo.")
        else:
            _show(u"No se pudo actualizar: {0}. Si modificaste archivos de la extensión, "
                  u"restáuralos o reinstala.".format(_first_line(ex)))
        return
    choice = forms.alert(
        u"Archi_Tools se actualizó.",
        sub_msg=u"Los cambios de ScheduleBoard requieren reiniciar Revit. "
                u"El resto se aplica al recargar pyRevit.",
        title=TITLE, options=[RELOAD_NOW, RELOAD_LATER], warn_icon=False)
    if choice == RELOAD_NOW:
        sessionmgr.reload_pyrevit()


def _run():
    if not HAS_UPDATER:
        _show(u"Esta función requiere pyRevit 7.0.0.")
        return

    from archi_update.pyrevit_git import check_remote
    from archi_update.repo import find_extension_root, find_own_repo
    from archi_update.versioning import read_installed_version

    # Sube hasta extension.json; si no aparece: boton -> panel -> tab -> raiz.
    here = os.path.dirname(os.path.abspath(__file__))
    ext_root = find_extension_root(here) or os.path.normpath(os.path.join(here, "..", "..", ".."))
    repo = find_own_repo(ext_root, updater.get_thirdparty_ext_repos())
    if repo is None:
        _show(u"Esta copia de Archi_Tools no se instaló como clon de GitHub, así que no "
              u"puede actualizarse sola.",
              sub_msg=u"Para poder actualizarla, primero borra la carpeta actual:\n\n"
                      + ext_root
                      + u"\n\nLuego ejecuta este comando en una terminal:\n\n"
                      + REINSTALL_COMMAND)
        return

    state = check_remote(repo)
    if state == "offline":
        _show(u"No se pudo consultar GitHub. Revisa tu conexión a internet.")
    elif state == "no_branch":
        _show(u"Esta copia no sigue ninguna rama de GitHub, así que no puede actualizarse sola.")
    elif state == "up_to_date":
        label = _version_label(read_installed_version(ext_root)) or UNKNOWN_VERSION
        _show(u"Ya tienes la última versión ({0}).".format(label), warn=False)
    elif _confirm_update(read_installed_version(ext_root), repo):
        _update(repo)


def main():
    try:
        _run()
    except Exception as ex:  # SystemExit y KeyboardInterrupt no se capturan
        _show(u"No se pudo completar la comprobación: {0}".format(_first_line(ex)))


main()
