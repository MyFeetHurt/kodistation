import os
import re

settings = {}
ICON = "/kodi/addons/plugin.video.epgstation/resources/icon.png"  # artwork (003): getAddonInfo("icon")
_STRINGS = None


def _strings():
    global _STRINGS
    if _STRINGS is None:
        po = os.path.join(os.path.dirname(__file__), "..", "..", "plugin.video.epgstation", "resources",
                          "language", "resource.language.en_gb", "strings.po")
        text = open(po, encoding="utf-8").read()
        _STRINGS = {int(k): v for k, v in re.findall(r'msgctxt "#(\d+)"\nmsgid "([^"]*)"', text)}
    return _STRINGS


class Addon:
    def __init__(self, id=None):
        pass

    def getSetting(self, key):  # noqa: N802
        return settings.get(key, "")

    def getLocalizedString(self, string_id):  # noqa: N802
        return _strings().get(string_id, "")

    def getAddonInfo(self, key):  # noqa: N802
        return {"id": "plugin.video.epgstation", "name": "KODIstation", "version": "test",
                "icon": ICON}.get(key, "")
