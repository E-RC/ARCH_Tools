# -*- coding: utf-8 -*-
"""Puente a pyRevit/LibGit2Sharp. Solo importable dentro de Revit (IronPython 2.7)."""
from __future__ import unicode_literals

from pyrevit.versionmgr import updater
from pyrevit.coreutils import git as libgit

from archi_update.repo import classify_remote
from archi_update.versioning import latest_tag


def check_remote(repo_info):
    """'offline' | 'no_branch' | 'up_to_date' | 'available'.

    Hace fetch solo si el clon sigue una rama; HEAD suelto o rama sin upstream da 'no_branch'.
    'offline' cubre cualquier fetch fallido, sea cual sea la causa.
    """
    tracked = repo_info.repo.Head.TrackedBranch is not None
    fetched = bool(updater.get_updates(repo_info)) if tracked else False
    divergence = libgit.compare_branch_heads(repo_info) if fetched else None
    behind_by = divergence.BehindBy if divergence is not None else None
    return classify_remote(tracked, fetched, behind_by)


def new_commit_messages(repo_info):
    """Mensajes cortos de los commits por delante del head local (requiere fetch previo).

    Llamar solo despues de que check_remote devolvio 'available' (desreferencia la rama seguida).
    """
    return list(libgit.get_all_new_commits(repo_info).values())


def latest_remote_tag(repo_info):
    """Mayor tag semver del clon, o None."""
    return latest_tag([tag.FriendlyName for tag in repo_info.repo.Tags])


def apply_update(repo_info):
    """git pull; devuelve el RepoInfo actualizado y levanta la excepcion de pyRevit si falla."""
    return updater.update_repo(repo_info)
