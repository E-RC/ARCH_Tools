# -*- coding: utf-8 -*-
"""Ubicar el clon propio entre los repos que conoce pyRevit (IronPython 2.7 y CPython)."""
from __future__ import unicode_literals

import os


def _canon(path):
    return os.path.normcase(os.path.normpath(path))


def classify_remote(has_tracked_branch, fetched, behind_by):
    """'no_branch' | 'offline' | 'available' | 'up_to_date'; sin rama que seguir gana a un fetch fallido."""
    if not has_tracked_branch:
        return "no_branch"
    if not fetched:
        return "offline"
    if behind_by is None:
        return "no_branch"
    return "available" if behind_by > 0 else "up_to_date"


def find_own_repo(ext_root, repos):
    """Primer repo cuyo .directory es ext_root (sin tocar el disco); None si no hay."""
    target = _canon(ext_root)
    for repo in repos:
        if _canon(repo.directory) == target:
            return repo
    return None


def find_extension_root(start_dir, max_levels=6):
    """Sube desde start_dir hasta una carpeta con extension.json (normalizada); None si no aparece.

    Mira start_dir y hasta max_levels carpetas por encima.
    """
    current = os.path.abspath(start_dir)
    for _ in range(max_levels + 1):
        if os.path.isfile(os.path.join(current, "extension.json")):
            return os.path.normpath(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None
