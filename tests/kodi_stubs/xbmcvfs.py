"""Fake xbmcvfs: special://profile/ maps to a per-test directory (set by conftest)."""
import os

profile_dir = "/tmp/kodi-stub-profile"


def translatePath(path):  # noqa: N802
    prefix = "special://profile/"
    if path.startswith(prefix):
        return os.path.join(profile_dir, path[len(prefix):])
    return path


def mkdirs(path):
    os.makedirs(translatePath(path), exist_ok=True)
    return True
