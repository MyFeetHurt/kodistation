"""Local settings outside the repository (backlog 019).

The addresses of the machines a developer works with (Kodi box, dev and production EPGStation, ...) are not in the
tree. They live in one untracked file, by default ~/.config/epgstation-kodi/local.env (path: $LOCAL_ENV), with
`KEY=value` lines that both make (`-include`) and a shell (`set -a; . file`) read. Keep values free of spaces,
quotes, `$` and `|`; list values are comma-separated. local.env.example in the repository shows every key.

The tools call load() in main(): a key already in the environment (or a command-line flag) wins over the file.
"""
import os

DEFAULT = "~/.config/epgstation-kodi/local.env"
KEYS = ("KODI_HOST", "KODI_SSH_USER", "EPGS_DEV_URL", "EPGS_PROD_URL", "SCREENSHOT_SSH", "SCREENSHOT_VM",
        "PUBLIC_DIR", "PUBLIC_GIT_NAME", "PUBLIC_GIT_EMAIL", "PUBLIC_AUDIT_DENY", "PUBLIC_AUDIT_DENY_FILE",
        "PUBLIC_AUDIT_ALLOW_FILE", "PUBLIC_AUDIT_CREDENTIALS_FILE")


def path():
    return os.path.expanduser(os.environ.get("LOCAL_ENV") or DEFAULT)


def read(p=None):
    """{key: value} of a local.env file ({} when it does not exist). Blank lines and # comments are skipped."""
    p = os.path.expanduser(p) if p else path()
    out = {}
    if not os.path.isfile(p):
        return out
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            out[key] = value.strip()
    return out


def load(p=None):
    """Put the file's keys into os.environ unless they are already set there. Returns the file's keys."""
    values = read(p)
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def get(key, default=""):
    return os.environ.get(key) or default


def ssh_target(host, user=None):
    """user@host with user from $KODI_SSH_USER (no user part when unset: ssh's own default)."""
    user = get("KODI_SSH_USER") if user is None else user
    return f"{user}@{host}" if user else host
