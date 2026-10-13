#!/usr/bin/env python3
"""kodi-addon-checker with Kodi's repository indexes fetched up front and cached for a day.

The checker downloads addons.xml.gz for every Kodi branch from mirrors.kodi.tv, which redirects to a random mirror.
When that mirror is down, urllib3 retries it with backoff for about 5 minutes per branch, so `make check` hangs.
Here each index is fetched with a short timeout and retried (each retry gets a new mirror), kept in
~/.cache/epgstation-kodi/kodi-repo/ for a day, and the checker reads the cached copies. A branch whose index can't be
fetched and has no cached copy is skipped: the checker then treats that branch as empty, as it does on any failed
download.

    tools/addon_check.py --branch omega dist/plugin.video.epgstation
"""
import os
import sys
import time
import urllib.request
from pathlib import Path

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "epgstation-kodi" / "kodi-repo"
MAX_AGE = 24 * 3600
TRIES = 6


def fetch(url, dest):
    for attempt in range(TRIES):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = resp.read()
            if data[:2] != b"\x1f\x8b":
                raise ValueError("not gzip")
            tmp = dest.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return True
        except Exception as exc:  # noqa: BLE001 - any mirror failure: try another mirror
            print(f"addon_check: {url}: {exc} (try {attempt + 1}/{TRIES})", file=sys.stderr)
            time.sleep(2)
    return False


class _Cached:
    def __init__(self, content):
        self.content = content
        self.status_code = 200

    def raise_for_status(self):
        pass


def main():
    import requests
    from kodi_addon_checker import ValidKodiVersions, check_addon
    from kodi_addon_checker.__main__ import main as checker_main
    from kodi_addon_checker.addons.Repository import Repository

    CACHE.mkdir(parents=True, exist_ok=True)
    cached = {}
    for branch in ValidKodiVersions:
        url = check_addon.ROOT_URL.format(branch=branch)
        dest = CACHE / f"{branch}.xml.gz"
        fresh = dest.exists() and time.time() - dest.stat().st_mtime < MAX_AGE
        if fresh or fetch(url, dest) or dest.exists():
            cached[url] = dest.read_bytes()

    def get(url, *args, **kwargs):
        if url in cached:
            return _Cached(cached[url])
        raise requests.exceptions.ConnectionError(f"addon_check: no index for {url}")

    Repository._session.get = get
    sys.argv = ["kodi-addon-checker"] + sys.argv[1:]
    return checker_main()


if __name__ == "__main__":
    sys.exit(main())
