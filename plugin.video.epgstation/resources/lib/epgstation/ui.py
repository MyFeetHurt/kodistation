"""Kodi glue shared by the views: settings, strings, ListItems, directory output, notifications, clocks.

Everything that imports xbmc* lives here or in views/; the modules next to this one are plain Python.
"""
import time
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from . import api as epgs_api
from . import items
from .api import EPGStation

PROFILE = "special://profile/addon_data/plugin.video.epgstation/"
KODI_CANCEL = 222  # Kodi's own "Cancel" string: the No button of the confirmations

_ADDON = None  # this run's xbmcaddon.Addon; released at the end so Kodi 22 finds nothing left in memory


def addon():
    global _ADDON
    if _ADDON is None:
        _ADDON = xbmcaddon.Addon()
    return _ADDON


def release_addon():
    global _ADDON
    _ADDON = None


def s(string_id):
    return addon().getLocalizedString(string_id)


def strings(table):
    """{key: localized text} of a ((key, string id), ...) table."""
    return {k: s(i) for k, i in table}


def setting(key):
    return addon().getSetting(key)


def int_setting(key, default=0):
    try:
        return int(setting(key) or default)
    except ValueError:
        return default


def server_url():
    """The EPGStation base URL from the settings ("" when not set yet)."""
    return epgs_api.normalize_url(setting("server_url"))


def make_api():
    return EPGStation(server_url(), setting("username"), setting("password"))


def lang():
    return "ja" if setting("genre_language") in ("", "0") else "en"


def show_subchannels():
    """Setting: list terrestrial sub-channels (NHK総合2, MBS2, ...) in live TV and the guide; hidden by default."""
    return setting("show_subchannels") == "true"


def prefer():
    return "ts" if setting("prefer_video") == "1" else "encoded"


def addon_icon():
    return addon().getAddonInfo("icon")


def profile_path(name=""):
    """A path in the addon's profile folder (created on first use)."""
    xbmcvfs.mkdirs(PROFILE)
    return xbmcvfs.translatePath(PROFILE + name)


def clock():
    """Monotonic seconds for deadlines (tests replace it)."""
    return time.monotonic()


def now_ms():
    """Wall clock in ms: one value per listing (tests replace it)."""
    return time.time() * 1000


def log(message, level=xbmc.LOGINFO):
    xbmc.log(f"[epgstation] {message}", level)


def parse_error_logger(what):
    """on_error callback for the metadata parser: logs which program it failed on."""
    def on_error(item, exc):
        log(f"metadata parser failed on {what} {item.get('id')}: {exc!r}", xbmc.LOGWARNING)
    return on_error


def refresh():
    xbmc.executebuiltin("Container.Refresh")


class BadParamError(Exception):
    """A plugin URL parameter that is missing or malformed."""


def listitem(d):
    """xbmcgui.ListItem of an item dict (items.recorded_item and friends)."""
    li = xbmcgui.ListItem(label=d["label"], offscreen=True)
    info = d["info"]
    tag = li.getVideoInfoTag()
    tag.setTitle(info.get("title", d["label"]))
    if "mediatype" in info:
        tag.setMediaType(info["mediatype"])
    for key, setter in (("originaltitle", tag.setOriginalTitle), ("tvshowtitle", tag.setTvShowTitle),
                        ("year", tag.setYear), ("directors", tag.setDirectors), ("writers", tag.setWriters),
                        ("tags", tag.setTags), ("plot", tag.setPlot), ("plotoutline", tag.setPlotOutline),
                        ("genres", tag.setGenres), ("studios", tag.setStudios), ("duration", tag.setDuration),
                        ("premiered", tag.setPremiered), ("firstaired", tag.setFirstAired),
                        ("dateadded", tag.setDateAdded), ("tagline", tag.setTagLine)):
        if info.get(key):
            setter(info[key])
    if "season" in info:
        tag.setSeason(info["season"])
    if "episode" in info:
        tag.setEpisode(info["episode"])
    if info.get("cast"):
        tag.setCast([xbmc.Actor(name, role, order) for name, role, order in info["cast"]])
    if "playcount" in info:  # live TV: an explicit 0 keeps Kodi's own playcount off the item
        tag.setPlaycount(info["playcount"])
    if d.get("stream"):
        st = d["stream"]
        tag.addVideoStream(xbmc.VideoStreamDetail(width=st["width"], height=st["height"], codec=st["codec"],
                                                  aspect=st.get("aspect", 0.0), duration=st.get("duration", 0)))
    if d.get("audio"):
        tag.addAudioStream(xbmc.AudioStreamDetail(channels=d["audio"]["channels"], codec=d["audio"]["codec"]))
    if d["art"]:
        li.setArt(d["art"])
    for k, v in d["props"].items():
        li.setProperty(k, v)
    if d["playable"]:
        li.setProperty("IsPlayable", "true")
    if d["context"]:
        # (label, url) runs the plugin as a script; (label, url, "update") opens the url in the current window
        li.addContextMenuItems([(c[0], f"Container.Update({c[1]})" if c[2:] == ("update",) else f"RunPlugin({c[1]})")
                                for c in d["context"]])
    if d.get("mimetype"):  # no HEAD request (content lookup) before playing
        li.setMimeType(d["mimetype"])
        li.setContentLookup(False)
    return li


def playing_file(player):
    try:
        return player.getPlayingFile() if player.isPlaying() else ""
    except RuntimeError:
        return ""


# Sort methods per view: (method, label2 mask). labelMask is always %L: without it Kodi shows the info-tag title for
# video items and drops the label markers ([Partial], the red recording dot). label2: duration, the date added when
# sorted by date, the year when sorted by year.
SORT_LIST = ((xbmcplugin.SORT_METHOD_UNSORTED, "%D"), (xbmcplugin.SORT_METHOD_DATEADDED, "%a"),
             (xbmcplugin.SORT_METHOD_LABEL, "%D"), (xbmcplugin.SORT_METHOD_DURATION, "%D"))
SORT_MOVIES = ((xbmcplugin.SORT_METHOD_UNSORTED, "%D"), (xbmcplugin.SORT_METHOD_LABEL, "%D"),
               (xbmcplugin.SORT_METHOD_DATEADDED, "%a"), (xbmcplugin.SORT_METHOD_VIDEO_YEAR, "%Y"),
               (xbmcplugin.SORT_METHOD_DURATION, "%D"))
SORT_TVSHOWS = ((xbmcplugin.SORT_METHOD_UNSORTED, "%a"), (xbmcplugin.SORT_METHOD_LABEL, "%a"),
                (xbmcplugin.SORT_METHOD_DATEADDED, "%a"))
SORT_EPISODES = ((xbmcplugin.SORT_METHOD_UNSORTED, "%D"), (xbmcplugin.SORT_METHOD_EPISODE, "%D"),
                 (xbmcplugin.SORT_METHOD_DATEADDED, "%a"), (xbmcplugin.SORT_METHOD_LABEL, "%D"))
SORT_LIVE = ((xbmcplugin.SORT_METHOD_UNSORTED, "%D"), (xbmcplugin.SORT_METHOD_LABEL, "%D"))
# Folder menus: our order only. Without any sort method Kodi shows and offers "Sort by: Date".
SORT_MENU = ((xbmcplugin.SORT_METHOD_UNSORTED, ""),)
# Guide and reservations: start time, then Kodi's genre (groups "main / sub"), studio (channel) and title sorts
SORT_GUIDE = ((xbmcplugin.SORT_METHOD_UNSORTED, "%D"), (xbmcplugin.SORT_METHOD_GENRE, "%D"),
              (xbmcplugin.SORT_METHOD_STUDIO, "%D"), (xbmcplugin.SORT_METHOD_TITLE, "%D"))


class Context:
    """One plugin call: Kodi's handle and URL parameters, the API client, and the output helpers."""

    def __init__(self, argv):
        self.base = argv[0]
        self.handle = int(argv[1])
        self.params = dict(urllib.parse.parse_qsl(argv[2].lstrip("?")))
        self.api = make_api()

    def add(self, dicts, content="videos", sort=SORT_MENU, category=None, cache=True):
        """List the item dicts. category: the window header instead of the addon name.
        cache=False: Kodi asks again when going back to the list (it may have changed)."""
        if category:
            xbmcplugin.setPluginCategory(self.handle, category)
        xbmcplugin.setContent(self.handle, content)
        for d in dicts:
            xbmcplugin.addDirectoryItem(self.handle, d["path"], listitem(d), isFolder=d["folder"])
        for method, label2 in sort:
            xbmcplugin.addSortMethod(self.handle, method, labelMask="%L", label2Mask=label2)
        xbmcplugin.endOfDirectory(self.handle, cacheToDisc=cache)

    def fail_directory(self):
        xbmcplugin.endOfDirectory(self.handle, succeeded=False)

    def resolve(self, li):
        xbmcplugin.setResolvedUrl(self.handle, True, li)

    def fail_resolve(self):
        xbmcplugin.setResolvedUrl(self.handle, False, xbmcgui.ListItem())

    def notify(self, message, icon=xbmcgui.NOTIFICATION_INFO):
        xbmcgui.Dialog().notification(addon().getAddonInfo("name"), message, icon)

    def confirm(self, heading, message, yes_label):
        """Yes/No dialog with Cancel focused (every write asks this way)."""
        return xbmcgui.Dialog().yesno(heading, message, nolabel=xbmc.getLocalizedString(KODI_CANCEL),
                                      yeslabel=yes_label, defaultbutton=xbmcgui.DLG_YESNO_NO_BTN)

    def int_param(self, name, allow=()):
        """An integer URL parameter (or one of `allow` as is); BadParamError when missing or malformed."""
        value = self.params.get(name)
        if value in allow:
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            raise BadParamError(f"{name}={value!r}") from None

    def channels(self, timeout=None):
        """({channelId: name}, channel ids with a logo) from one GET /channels."""
        got = self.api.channels(timeout=timeout) if timeout else self.api.channels()
        return items.channel_names(got), {c["id"] for c in got if c.get("hasLogoData")}
