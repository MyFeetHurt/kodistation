"""Small file helpers shared by the caches (stdlib only, no Kodi imports)."""
import json
import os
import tempfile


def atomic_write_json(path, obj):
    """Write obj as UTF-8 JSON to path via a temp file + os.replace. Returns False if it could not write
    (the old file, if any, is left as it was, and no temp file stays behind)."""
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    return True
