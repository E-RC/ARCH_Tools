# -*- coding: utf-8 -*-
"""Versiones del actualizador: semver estricto y lectura de extension.json (IronPython 2.7 y CPython)."""
from __future__ import unicode_literals

import io
import json
import os
import re

_SEMVER = re.compile(r"^v?([0-9]+)\.([0-9]+)\.([0-9]+)\Z")
_TEXT = type("")


def parse_semver(text):
    """'v1.2.3' o '1.2.3' -> (1, 2, 3); cualquier otra forma -> None."""
    if not isinstance(text, (str, _TEXT)):
        return None
    match = _SEMVER.match(text)
    return tuple(int(g) for g in match.groups()) if match else None


def latest_tag(names):
    """Nombre original del mayor semver valido (el primero si hay empate), o None."""
    best_name, best = None, None
    for name in names:
        parsed = parse_semver(name)
        if parsed is not None and (best is None or parsed > best):
            best_name, best = name, parsed
    return best_name


def read_installed_version(ext_root):
    """Lee 'version' de <ext_root>/extension.json; None ante cualquier problema."""
    try:
        with io.open(os.path.join(ext_root, "extension.json"), encoding="utf-8-sig") as f:
            version = json.load(f)["version"]
    except (IOError, OSError, ValueError, KeyError, TypeError):
        return None
    return version if parse_semver(version) is not None else None
