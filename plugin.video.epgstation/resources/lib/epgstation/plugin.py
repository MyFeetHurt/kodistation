"""Kodi adapter: routing, settings and ListItem construction.

Keep logic out of here - put it in items.py where it can be unit-tested.
"""
import sys
import time
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from . import api as epgs_api  # review L5: epgs_api.join_workers

# reservations (007): reserves; protect / delete (006): recorded; artwork (003): thumbs
from . import details, durations, guide, items, library, live, recorded, reserves, thumbs  # program guide (013)
from .api import ApiError, EPGStation

ADDON = xbmcaddon.Addon()
PROFILE = "special://profile/addon_data/plugin.video.epgstation/"


def _s(string_id):
    return ADDON.getLocalizedString(string_id)


def _setting(key):
    return ADDON.getSetting(key)


def _server_url():
    return _setting("server_url") or "http://localhost:8888"


def _api():
    return EPGStation(
        _server_url(),
        _setting("username"),
        _setting("password"),
    )


def _lang():
    return "ja" if _setting("genre_language") in ("", "0") else "en"


def _prefer():
    return "ts" if _setting("prefer_video") == "1" else "encoded"


def _real_duration_on():
    return _setting("real_duration") != "false"


def _item_strings():
    return {"choose": _s(30011), "partial": _s(30012), "length": _s(30013),
            "stop": _s(30036),  # reservations (007): stop
            # protect / delete (006): entries, marker, plot line
            "protect": _s(30072), "unprotect": _s(30073), "delete": _s(30074), "marker": _s(30075),
            "protect_line": _s(30087),
            "delete_file": _s(30245),  # delete a file (018)
            # full detail view (002): the 【録画情報】 block and the ドロップログ entry
            "files": _s(30080), "weekdays": _s(30026), **{k: _s(i) for k, i in DETAIL_STRINGS}}


DETAIL_STRINGS = (  # full detail view (002): details.DEFAULT_STRINGS keys -> string IDs
    ("detail_header", 30251), ("detail_broadcast", 30252), ("detail_genre", 30253), ("detail_video", 30254),
    ("detail_audio", 30255), ("detail_drops", 30256), ("detail_encoding", 30257), ("droplog", 30258),
    ("droplog_large", 30259), ("droplog_gone", 30260))


def _cache():
    xbmcvfs.mkdirs(PROFILE)
    return durations.DurationCache(xbmcvfs.translatePath(PROFILE + "durations.json"), _server_url())


def _library():
    xbmcvfs.mkdirs(PROFILE)
    return library.Library(xbmcvfs.translatePath(PROFILE), _server_url())


def _thumb_cache():  # artwork (003)
    xbmcvfs.mkdirs(PROFILE)
    return thumbs.ThumbCache(xbmcvfs.translatePath(PROFILE + "thumbs.json"), _server_url())


def _addon_icon():  # artwork (003): the last fallback of recording and show art
    return ADDON.getAddonInfo("icon")


def _live_strings():  # live TV (012)
    return {k: _s(i) for k, i in (("GR", 30016), ("SKY", 30017), ("next", 30018), ("left", 30019),
                                  ("no_tuner", 30101), ("failed", 30102))}


def _live_mode():  # live TV (012): 0 unconverted, 1 first transcoded mode, 2 last transcoded mode
    try:
        return int(_setting("live_mode") or 0)
    except ValueError:
        return 0


def _live_error(program, exc):  # live TV (012)
    xbmc.log(f"[epgstation] metadata parser failed on program {program.get('id')}: {exc!r}", xbmc.LOGWARNING)


def _guide_strings():  # program guide (013)
    return dict(_live_strings(), **{k: _s(i) for k, i in (
        ("whats_on", 30020), ("by_genre", 30021), ("schedule", 30022), ("now", 30023), ("today", 30024),
        ("tomorrow", 30025), ("weekdays", 30026), ("channel_schedule", 30027), ("gone", 30028))})


def _guide_cache():  # program guide (013)
    xbmcvfs.mkdirs(PROFILE)
    return guide.GuideCache(xbmcvfs.translatePath(PROFILE), _server_url(), now=lambda: _now_ms() / 1000)


def _clock():  # reservations (007): monotonic seconds for deadlines (tests replace it)
    return time.monotonic()


def _now_ms():  # program guide (013): one clock for a whole listing (tests fix it)
    return time.time() * 1000


RESERVE_STRINGS = (  # reservations (007): reserves.DEFAULT_STRINGS keys -> string IDs
    ("reserves", 30030), ("conflict", 30031), ("overlap", 30032), ("skip", 30033), ("delete", 30034),
    ("skip_this", 30035), ("stop", 30036), ("unskip", 30037), ("unoverlap", 30038), ("q_delete", 30039),
    ("q_skip", 30040), ("q_stop", 30041), ("q_unskip", 30042), ("q_unoverlap", 30043), ("q_moves", 30044),
    ("done_delete", 30045), ("done_skip", 30046), ("done_stop", 30047), ("done_unskip", 30048),
    ("done_unoverlap", 30049), ("failed", 30050), ("gone", 30051), ("changed", 30052), ("rule", 30053),
    ("manual", 30054), ("line_conflict", 30055), ("line_overlap", 30056), ("line_skip", 30057),
    ("recording", 30002), ("weekdays", 30026), ("q_airing", 30058), ("failed_net", 30059), ("q_stop_rest", 30060))
KODI_CANCEL = 222  # Kodi's own "Cancel" (ja: キャンセル): the No button of the confirmations


def _reserve_strings():  # reservations (007)
    return {k: _s(i) for k, i in RESERVE_STRINGS}


RECORD_STRINGS = (  # record from the guide (014): reserves.DEFAULT_STRINGS keys -> string IDs
    ("record", 30061), ("q_record", 30062), ("q_record_airing", 30063), ("encode", 30064), ("done_record", 30065),
    ("done_record_now", 30066), ("already", 30067), ("add_conflict", 30068), ("ended", 30069), ("by_rule", 30070),
    ("by_manual", 30071), ("program_gone", 30028))
LISTS_TIMEOUT = 5  # record from the guide (014): seconds for the guide's GET /reserves/lists and /recording


def _record_strings():  # record from the guide (014): do_record
    out = _reserve_strings()
    out.update({k: _s(i) for k, i in RECORD_STRINGS})
    return out


def _mark_strings():  # record from the guide (014): guide listings (guide strings win: "gone" is the guide's)
    out = _record_strings()
    out.update(_guide_strings())
    return out


RECORDED_STRINGS = (  # protect / delete (006): recorded.DEFAULT_STRINGS keys -> string IDs
    ("protect", 30072), ("unprotect", 30073), ("delete", 30074), ("marker", 30075), ("q_protect", 30076),
    ("q_unprotect", 30077), ("q_delete", 30078), ("q_encoding", 30079), ("files", 30080), ("done_protect", 30081),
    ("done_unprotect", 30082), ("done_delete", 30083), ("gone", 30084), ("changed", 30085), ("protected", 30086),
    ("protect_line", 30087),
    # delete a file (018)
    ("delete_file", 30245), ("choose_file", 30246), ("q_delete_file", 30247), ("delete_keep", 30248),
    ("done_delete_file", 30249), ("file_gone", 30250))


def _recorded_strings():  # protect / delete (006): do_recorded_action
    out = {k: _s(i) for k, i in RECORDED_STRINGS}
    out.update({k: _s(i) for k, i in (("failed", 30050), ("failed_net", 30059), ("weekdays", 30026))})
    return out


def _reserve_error(reserve, exc):  # reservations (007)
    xbmc.log(f"[epgstation] metadata parser failed on reservation {reserve.get('id')}: {exc!r}", xbmc.LOGWARNING)


class BadParamError(Exception):  # program guide (013): a plugin URL parameter that is missing or malformed
    pass


DROPLOG_MAX_KB = 512  # full detail view (002): GET /dropLogs maxsize (the server's default); larger: HTTP 416


def _page_size():
    try:
        return max(10, int(_setting("page_size") or 50))
    except ValueError:
        return 50


def _metadata_error(rec, exc):
    xbmc.log(f"[epgstation] metadata parser failed on recording {rec.get('id')}: {exc!r}", xbmc.LOGWARNING)


def _listitem(d):
    li = xbmcgui.ListItem(label=d["label"], offscreen=True)
    info = d["info"]
    tag = li.getVideoInfoTag()
    tag.setTitle(info.get("title", d["label"]))
    if "mediatype" in info:
        tag.setMediaType(info["mediatype"])
    if info.get("originaltitle"):
        tag.setOriginalTitle(info["originaltitle"])
    if info.get("tvshowtitle"):
        tag.setTvShowTitle(info["tvshowtitle"])
    if "season" in info:
        tag.setSeason(info["season"])
    if "episode" in info:
        tag.setEpisode(info["episode"])
    if info.get("year"):
        tag.setYear(info["year"])
    if info.get("cast"):
        tag.setCast([xbmc.Actor(name, role, order) for name, role, order in info["cast"]])
    if info.get("directors"):
        tag.setDirectors(info["directors"])
    if info.get("writers"):
        tag.setWriters(info["writers"])
    if info.get("tags"):
        tag.setTags(info["tags"])
    if info.get("plot"):
        tag.setPlot(info["plot"])
    if info.get("plotoutline"):
        tag.setPlotOutline(info["plotoutline"])
    if info.get("genres"):
        tag.setGenres(info["genres"])
    if info.get("studios"):
        tag.setStudios(info["studios"])
    if info.get("duration"):
        tag.setDuration(info["duration"])
    if info.get("premiered"):
        tag.setPremiered(info["premiered"])
    if info.get("firstaired"):
        tag.setFirstAired(info["firstaired"])
    if info.get("dateadded"):
        tag.setDateAdded(info["dateadded"])
    if "playcount" in info:  # live TV (012): an explicit 0 keeps Kodi's DB playcount off the item
        tag.setPlaycount(info["playcount"])
    if info.get("tagline"):
        tag.setTagLine(info["tagline"])
    if d.get("stream"):
        st = d["stream"]
        tag.addVideoStream(xbmc.VideoStreamDetail(width=st["width"], height=st["height"], codec=st["codec"],
                                                  aspect=st.get("aspect", 0.0), duration=st.get("duration", 0)))
    if d.get("audio"):  # full detail view (002): the AAC / channels flags
        tag.addAudioStream(xbmc.AudioStreamDetail(channels=d["audio"]["channels"], codec=d["audio"]["codec"]))
    if d["art"]:
        li.setArt(d["art"])
    for k, v in d["props"].items():
        li.setProperty(k, v)
    if d["playable"]:
        li.setProperty("IsPlayable", "true")
    if d["context"]:
        # (label, url) -> RunPlugin; program guide (013): (label, url, "update") -> Container.Update
        li.addContextMenuItems([(c[0], f"Container.Update({c[1]})" if c[2:] == ("update",) else f"RunPlugin({c[1]})")
                                for c in d["context"]])
    if d.get("mimetype"):  # live TV (012): no HEAD request (content lookup) before playing
        li.setMimeType(d["mimetype"])
        li.setContentLookup(False)
    return li


def _playing_file(player):
    try:
        return player.getPlayingFile() if player.isPlaying() else ""
    except RuntimeError:
        return ""


class _LivePlayer(xbmc.Player):  # live TV (012): forwards Kodi's player callbacks to a live.StartWatch
    watch = None

    def _event(self, name, *args):
        shown = args[0].split("|", 1)[0][:80] if args else ""  # never the |Authorization= suffix
        xbmc.log(f"[epgstation] live: player {name} {shown}".rstrip(), xbmc.LOGINFO)
        if self.watch:
            getattr(self.watch, name)(*args)

    def onAVStarted(self):  # noqa: N802 - Kodi API
        self._event("on_av_started", _playing_file(self))

    def onPlayBackStarted(self):  # noqa: N802
        self._event("on_playback_started", _playing_file(self))

    def onPlayBackError(self):  # noqa: N802
        self._event("on_error")

    def onPlayBackStopped(self):  # noqa: N802
        self._event("on_stopped")

    def onPlayBackEnded(self):  # noqa: N802
        self._event("on_ended")


class Plugin:
    def __init__(self, argv):
        self.base = argv[0]
        self.handle = int(argv[1])
        self.params = dict(urllib.parse.parse_qsl(argv[2].lstrip("?")))
        self.api = _api()

    PLAY_ACTIONS = ("play", "live_play", "choose")
    # program guide (013): run by a click, Kodi expects no directory; reservations (007): reserve_info, reserve_action
    # record from the guide (014): record; protect / delete (006): recorded_action, search_ask
    NON_DIRECTORY_ACTIONS = ("guide_info", "reserve_info", "reserve_action", "record", "recorded_action", "search_ask",
                             "droplog")  # full detail view (002)

    def run(self):
        action = self.params.get("action", "root")
        handler = getattr(self, "do_" + action, None)
        if handler is None:
            xbmc.log(f"[epgstation] unknown action {action}", xbmc.LOGWARNING)
            return
        try:
            self._run(action, handler)
        finally:
            # review L5: after endOfDirectory / setResolvedUrl: no worker thread may outlive the run
            epgs_api.join_workers()

    def _run(self, action, handler):
        try:
            handler()
        except BadParamError as e:  # program guide (013)
            xbmc.log(f"[epgstation] {action}: bad parameter {e}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification(ADDON.getAddonInfo("name"), _s(30029), xbmcgui.NOTIFICATION_ERROR)
            if action not in self.NON_DIRECTORY_ACTIONS:
                xbmcplugin.endOfDirectory(self.handle, succeeded=False)
        except ApiError as e:
            xbmc.log(f"[epgstation] {e}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification(ADDON.getAddonInfo("name"), f"{_s(30100)}: {e}", xbmcgui.NOTIFICATION_ERROR)
            if action in self.PLAY_ACTIONS:  # Kodi waits for a resolved URL, not a directory
                xbmcplugin.setResolvedUrl(self.handle, False, xbmcgui.ListItem())
            elif action in self.NON_DIRECTORY_ACTIONS:  # program guide (013)
                pass
            else:
                xbmcplugin.endOfDirectory(self.handle, succeeded=False)

    # --- directory helpers --------------------------------------------------

    # Sort methods per view: (method, label2 mask). labelMask is always %L: without it Kodi shows the
    # info-tag title for video items and drops the label markers ([Partial], the red recording dot).
    # label2: duration, the date added when sorted by date, the year when sorted by year.
    SORT_LIST = ((xbmcplugin.SORT_METHOD_UNSORTED, "%D"), (xbmcplugin.SORT_METHOD_DATEADDED, "%a"),
                 (xbmcplugin.SORT_METHOD_LABEL, "%D"), (xbmcplugin.SORT_METHOD_DURATION, "%D"))
    SORT_MOVIES = ((xbmcplugin.SORT_METHOD_UNSORTED, "%D"), (xbmcplugin.SORT_METHOD_LABEL, "%D"),
                   (xbmcplugin.SORT_METHOD_DATEADDED, "%a"), (xbmcplugin.SORT_METHOD_VIDEO_YEAR, "%Y"),
                   (xbmcplugin.SORT_METHOD_DURATION, "%D"))
    SORT_TVSHOWS = ((xbmcplugin.SORT_METHOD_UNSORTED, "%a"), (xbmcplugin.SORT_METHOD_LABEL, "%a"),
                    (xbmcplugin.SORT_METHOD_DATEADDED, "%a"))
    SORT_EPISODES = ((xbmcplugin.SORT_METHOD_UNSORTED, "%D"), (xbmcplugin.SORT_METHOD_EPISODE, "%D"),
                     (xbmcplugin.SORT_METHOD_DATEADDED, "%a"), (xbmcplugin.SORT_METHOD_LABEL, "%D"))

    SORT_LIVE = ((xbmcplugin.SORT_METHOD_UNSORTED, "%D"), (xbmcplugin.SORT_METHOD_LABEL, "%D"))  # live TV (012)
    # Folder menus (root, Browse and its folders, live TV types): our order only. Without any sort method Kodi
    # shows and offers "Sort by: Date".
    SORT_MENU = ((xbmcplugin.SORT_METHOD_UNSORTED, ""),)
    # program guide (013): our order (start time), then Kodi's genre (groups the "main / sub" genre strings),
    # studio (channel) and title sorts
    SORT_GUIDE = ((xbmcplugin.SORT_METHOD_UNSORTED, "%D"), (xbmcplugin.SORT_METHOD_GENRE, "%D"),
                  (xbmcplugin.SORT_METHOD_STUDIO, "%D"), (xbmcplugin.SORT_METHOD_TITLE, "%D"))
    SORT_RESERVES = SORT_GUIDE  # reservations (007): start time, then genre, studio (channel), title

    def _add(self, dicts, content="videos", sort=SORT_MENU, category=None, cache=True):
        """category: the window header (the view's own label) instead of the addon name.
        sort: (method, label2 mask) pairs, default SORT_MENU.
        cache=False: Kodi fetches the list again when going back to it (live TV)."""
        if category:
            xbmcplugin.setPluginCategory(self.handle, category)
        xbmcplugin.setContent(self.handle, content)
        for d in dicts:
            xbmcplugin.addDirectoryItem(self.handle, d["path"], _listitem(d), isFolder=d["folder"])
        for method, label2 in sort:
            xbmcplugin.addSortMethod(self.handle, method, labelMask="%L", label2Mask=label2)
        xbmcplugin.endOfDirectory(self.handle, cacheToDisc=cache)

    def _channels(self):
        """({channelId: name}, channel ids with hasLogoData) from one GET /channels."""
        got = self.api.channels()
        return items.channel_names(got), {c["id"] for c in got if c.get("hasLogoData")}

    def _blank_thumbs(self, ids):
        """artwork (003): the blank ones of these thumbnail ids (thumbs.check: cached, or fetched within the budget of
        this list open); the cache is saved at once."""
        return self._thumb_work(lambda cache: thumbs.check(ids, self.api, cache, thumbs.Budget()))

    def _settle_thumbs(self, candidates):
        """review M1: thumbs.settle over candidates(blank) (TV shows), one budget; the cache is saved at once."""
        return self._thumb_work(lambda cache: thumbs.settle(candidates, self.api, cache, thumbs.Budget()))

    def _thumb_work(self, work):
        """review L1: never lets an error skip the listing: logged, and no thumbnail counts as blank."""
        try:
            cache = _thumb_cache()
            blank = work(cache)
            if not cache.save():
                xbmc.log("[epgstation] could not write thumbs.json", xbmc.LOGWARNING)
            return blank
        except Exception as e:  # noqa: BLE001 - any failure here must not cost the directory
            xbmc.log(f"[epgstation] thumbnail check failed: {e!r}", xbmc.LOGWARNING)
            return set()

    def _recorded_items(self, records, channels=None, meta=None, logos=None):
        """Item dicts; meta: {id: cached metadata} (library), the rest are parsed. Real file lengths only for
        lists of up to items.REAL_DURATION_MAX recordings (else the EPG length). channels None: one GET /channels for
        the names and logos; logos: channel ids with hasLogoData (artwork (003))."""
        real = {}
        meta = meta or {}
        if _real_duration_on() and len(records) <= items.REAL_DURATION_MAX:
            cache = _cache()
            real = durations.fill(records, self.api, cache, _prefer())
            cache.save()
        if channels is None:
            channels, logos = self._channels()
        blank = self._blank_thumbs([items.newest_thumb(r) for r in records])  # artwork (003)
        lang, strings, icon = _lang(), _item_strings(), _addon_icon()
        return [items.recorded_item(r, self.api, channels, lang, strings, real.get(r["id"]), _metadata_error,
                                    meta.get(r["id"], items.PARSE), logos, blank, icon)
                for r in records]

    def _snapshot(self, with_records=True):
        """All recordings from the library cache (library.py): one probe + /recording when it is current."""
        lib = _library()
        snap = lib.current(self.api, _metadata_error, with_records)
        for msg in lib.warnings:
            xbmc.log(f"[epgstation] {msg}", xbmc.LOGWARNING)
        return snap

    # --- actions -----------------------------------------------------------

    def do_root(self):
        strings = {k: _s(i) for k, i in (("movies", 30007), ("tvshows", 30008), ("recent", 30001),
                                         ("recording", 30002), ("reserves", 30030),  # reservations (007)
                                         ("live", 30015),  # live TV (012)
                                         ("search", 30006),
                                         ("browse", 30009))}
        self._add(items.root_menu(strings), content="files")

    def do_browse(self):
        strings = {k: _s(i) for k, i in (("channels", 30003), ("genres", 30004), ("rules", 30005))}
        self._add(items.browse_menu(strings), content="files", category=_s(30009))

    def do_movies(self):
        snap = self._snapshot()
        movies = items.movie_entries(snap.entries)
        out = self._recorded_items([snap.records[e["id"]] for e in movies], snap.channels, snap.meta, snap.logos)
        self._add(items.label_movies(out, movies, _lang()), content="movies", sort=self.SORT_MOVIES,
                  category=_s(30007), cache=False)  # protect / delete (006): Back shows a delete at once

    def do_tvshows(self):
        snap = self._snapshot()
        lang = _lang()
        # artwork (003): each show's newest thumbnail not known blank; review M1: thumbs.settle advances through runs
        # of blank episodes (cached ones without a request) within one budget
        blank = self._settle_thumbs(lambda known: items.show_thumb_candidates(snap.entries, known))
        icon = _addon_icon()
        out = [items.tvshow_folder(show, snap.records, self.api, snap.channels, lang, _s(30014), snap.logos, icon)
               for show in items.group_shows(snap.entries, blank)]
        self._add(out, content="tvshows", sort=self.SORT_TVSHOWS, category=_s(30008), cache=False)  # (006)

    def do_show(self):
        snap = self._snapshot()
        show = items.find_show(snap.entries, self.params.get("key", ""))
        records = [snap.records[i] for i in show["ids"]] if show else []
        out = items.label_episodes(self._recorded_items(records, snap.channels, snap.meta, snap.logos),
                                   [snap.meta.get(r["id"]) for r in records], _lang())
        title = items.show_title(show, snap.channels, _s(30014)) if show else None
        self._add(out, content="episodes", sort=self.SORT_EPISODES, category=title, cache=False)  # (006)

    def do_list(self):
        p = self.params
        if p.get("subGenre") is not None:  # subgenres (009): /recorded has no subgenre filter
            return self._subgenre_list()
        offset = int(p.get("offset", 0))
        limit = _page_size()
        data = self.api.recorded(
            offset=offset, limit=limit, rule_id=p.get("ruleId"), channel_id=p.get("channelId"),
            genre=p.get("genre"), keyword=p.get("keyword"),
        )
        records = data["records"]
        if offset == 0 and not p.get("keyword"):
            records = items.in_progress_matching(
                self.api.recording()["records"], p.get("channelId"), p.get("genre"), p.get("ruleId")) + records
        channels, logos = self._channels()
        out = self._recorded_items(records, channels, logos=logos)
        if offset + limit < data["total"]:
            nxt = dict(p, offset=offset + limit)
            out.append(items.folder(f"{_s(30010)} ({offset + limit + 1}-)", **nxt))
        strings = {"recent": _s(30001), "search": _s(30006), "rules": _s(30005)}
        self._add(out, sort=self.SORT_LIST, category=items.list_category(p, channels, _lang(), strings),
                  cache=False)  # protect / delete (006)

    def _subgenre_list(self):
        """subgenres (009): ?action=list&genre=G&subGenre=S from the library snapshot, unpaged, newest first."""
        genre, sub = self._int_param("genre"), self._int_param("subGenre")
        snap = self._snapshot()
        records = [snap.records[i] for i in items.subgenre_entries(snap.entries, genre, sub) if i in snap.records]
        out = self._recorded_items(records, snap.channels, snap.meta, snap.logos)
        strings = {"recent": _s(30001), "search": _s(30006), "rules": _s(30005)}
        self._add(out, sort=self.SORT_LIST, category=items.list_category(self.params, snap.channels, _lang(), strings),
                  cache=False)  # protect / delete (006)

    def do_recording(self):
        data = self.api.recording()
        self._add(self._recorded_items(data["records"]), sort=self.SORT_LIST, category=_s(30002),
                  cache=False)  # protect / delete (006)

    def do_channels(self):
        names, logos = self._channels()
        self._add(items.channel_folders(self.api.recorded_options(), names, logos, self.api), content="files",
                  category=_s(30003), cache=False)  # protect / delete (006): counts

    def do_genres(self):
        # subgenres (009): counts of genre1-3 from the library (/recorded/options counts genre1 only)
        entries = self._snapshot(with_records=False).entries
        self._add(items.genre_folders(entries, _lang()), content="files", category=_s(30004),
                  cache=False)  # protect / delete (006): counts

    def do_genre(self):
        """subgenres (009): すべて + the subgenres of one genre that have recordings."""
        genre, lang = self._int_param("genre"), _lang()
        entries = self._snapshot(with_records=False).entries
        self._add(items.subgenre_folders(genre, entries, lang, _s(30270) or "All ({0})"), content="files",
                  category=items.list_category({"genre": str(genre)}, {}, lang, {}), cache=False)

    def do_rules(self):
        counts = items.rule_counts(self._snapshot(with_records=False).entries)
        self._add(items.rule_folders(self.api.rules(), counts), content="files", category=_s(30005),
                  cache=False)  # protect / delete (006): counts

    def do_search(self):
        """?action=search: with a keyword the results; without one an old 検索 folder link (a favourite, a skin
        shortcut, a window start path: protect / delete (006) review M3) asks and lists in place, as before 006."""
        if self.params.get("keyword"):
            self.do_list()
            return
        keyword = xbmcgui.Dialog().input(_s(30006))
        if self.handle < 0:  # run as a script (RunPlugin): open the results
            if keyword:
                self._open_search_results(keyword)
            return
        if not keyword:
            xbmcplugin.endOfDirectory(self.handle, succeeded=False)
            return
        self.params["keyword"] = keyword
        self.do_list()

    def do_search_ask(self):
        """protect / delete (006): the root's 検索 (items.search_entry, a click action): ask, then open the results at a
        URL that carries the keyword, so Container.Refresh (after a delete) and Back re-list them without asking again.
        A handle here is a play/queue request of the item (Kore, a favourite): resolve False, never a directory."""
        if self.handle >= 0:
            xbmcplugin.setResolvedUrl(self.handle, False, xbmcgui.ListItem())
        keyword = xbmcgui.Dialog().input(_s(30006))
        if keyword:
            self._open_search_results(keyword)

    def _open_search_results(self, keyword):  # protect / delete (006)
        url = items.plugin_url(action="search", keyword=keyword)
        if xbmc.getCondVisibility("Window.IsActive(videos)"):
            xbmc.executebuiltin(f"Container.Update({url})")
        else:  # from Home (a favourite): open the video window there
            xbmc.executebuiltin(f"ActivateWindow(Videos,{url},return)")

    def _file_durations(self, rec, files):
        """Real lengths of these files ({id: secs}); empty while recording or when turned off."""
        if rec.get("isRecording") or not _real_duration_on():
            return {}
        cache = _cache()
        out = durations.for_files(files, self.api, cache)
        cache.save()
        return out

    def _play_item(self, rec, video, real_duration=None):
        """ListItem for playing video with the recording's full metadata."""
        try:
            channels, logos = self._channels()  # only for the studio name and logo: never block playback on it
        except ApiError as e:
            xbmc.log(f"[epgstation] {e}", xbmc.LOGWARNING)
            channels, logos = {}, None
        # artwork (003): no thumbnail check before playback (an unchecked thumbnail counts as good)
        d = items.recorded_item(rec, self.api, channels, _lang(), _item_strings(), real_duration, _metadata_error,
                                logos=logos, fallback_icon=_addon_icon())
        li = _listitem(d)
        li.setPath(self.api.video_url(video["id"]) + self.api.kodi_headers())
        return li

    def do_play(self):
        rec = self.api.recorded_item(self.params["id"])
        video = items.pick_video(rec.get("videoFiles"), items.default_prefer(rec, _prefer()))
        if video is None:
            xbmcplugin.setResolvedUrl(self.handle, False, xbmcgui.ListItem())
            return
        real = self._file_durations(rec, [video]).get(video["id"])
        xbmcplugin.setResolvedUrl(self.handle, True, self._play_item(rec, video, real))

    def do_choose(self):
        rec = self.api.recorded_item(self.params["id"])
        files = rec.get("videoFiles") or []
        real = self._file_durations(rec, files)
        idx = xbmcgui.Dialog().select(_s(30011), items.video_choices(files, real))
        if idx < 0:
            return
        li = self._play_item(rec, files[idx], real.get(files[idx]["id"]))
        xbmc.Player().play(li.getPath(), li)

    # --- live TV (012) -------------------------------------------------------------------------------------------

    def do_live(self):
        """No type: one folder per broadcast type (or that type's channels when there is only one)."""
        channel_type = self.params.get("type")
        strings = _live_strings()
        category = live.type_label(channel_type, strings) if channel_type else _s(30015)
        top = []
        if not channel_type:
            types = live.live_types(self.api.channels())
            top = guide.menu(_guide_strings())  # program guide (013): 放送中, ジャンル別 first
            if len(types) != 1:
                self._add(top + live.type_folders(types, strings), content="files", category=category)
                return
            channel_type = types[0]
        now = _now_ms()
        schedules = self.api.schedules(now, now + live.WINDOW_MS, [channel_type])
        out = live.channel_items(schedules, self.api, now, _lang(), strings, _live_error)
        out = guide.with_schedule_context(out, _guide_strings())  # program guide (013): 番組表 context entry
        # record from the guide (014): markers and the reservation entry for each channel's current program
        current = {}
        for sched in schedules:
            cur, _ = live.now_next(sched.get("programs") or [], now)
            if cur is not None:
                current[cur["id"]] = dict(cur, channelId=sched["channel"]["id"])
        if out:
            index, records = self._marks(list(current.values()), now, (now, now))
            guide.mark_channels(out, current, index, records, now, _mark_strings())
        self._add(top + out, sort=self.SORT_LIVE, category=category, cache=False)

    def do_live_play(self):
        channel_id = self.params.get("channelId")
        if not channel_id:
            xbmc.log("[epgstation] live_play without channelId", xbmc.LOGERROR)
            xbmcgui.Dialog().notification(ADDON.getAddonInfo("name"), _s(30102), xbmcgui.NOTIFICATION_ERROR)
            xbmcplugin.setResolvedUrl(self.handle, False, xbmcgui.ListItem())
            return
        name = self.params.get("name") or channel_id
        config = self.api.config()
        if not live.live_enabled(config):
            xbmcgui.Dialog().notification(ADDON.getAddonInfo("name"), _s(30103), xbmcgui.NOTIFICATION_ERROR)
            xbmcplugin.setResolvedUrl(self.handle, False, xbmcgui.ListItem())
            return
        modes = live.m2ts_modes(config)
        mode = live.mode_index(modes, _live_mode())
        now = time.time() * 1000
        try:  # only for the OSD: never block playback on it
            sched = self.api.channel_schedule(channel_id, now, days=1, timeout=live.INFO_TIMEOUT)
            programs = sched[0]["programs"] if sched else []
        except (ApiError, LookupError, TypeError) as e:
            xbmc.log(f"[epgstation] live program info: {e}", xbmc.LOGWARNING)
            programs = []
        cur, nxt = live.now_next(programs, now)
        info = live.play_info(cur, nxt, name, now, _lang(), _live_strings(), _live_error)
        d = {"label": name, "info": info, "art": {}, "props": {}, "playable": False, "context": [],
             "mimetype": "video/mp2t"}
        li = _listitem(d)
        li.setPath(self.api.live_url(channel_id, mode) + self.api.kodi_headers())
        xbmc.log(f"[epgstation] live: channel {channel_id} mode {mode} ({live.mode_name(modes, mode)})", xbmc.LOGINFO)
        player = _LivePlayer()
        watch = live.StartWatch(channel_id, _playing_file(player))
        player.watch = watch  # callbacks only count after setResolvedUrl
        xbmcplugin.setResolvedUrl(self.handle, True, li)
        self._watch_live_start(watch, player, mode)

    def _watch_live_start(self, watch, player, mode):
        """Until Kodi starts our stream, the user cancels or live.LIVE_START_TIMEOUT s pass: on a failed start
        ask the server once (one GET, status only) and show why (no free tuner, other error)."""
        monitor = xbmc.Monitor()
        waited = 0.0
        while waited < live.LIVE_START_TIMEOUT:
            if monitor.waitForAbort(0.25):
                return
            waited += 0.25
            watch.poll(_playing_file(player))
            if watch.result == live.STOPPED:  # stopped before the start: Kodi's failure, or the user?
                verdict_wait = 0.0
                while not xbmc.getCondVisibility("Window.IsActive(okdialog)") and \
                        verdict_wait < live.STOP_VERDICT_WAIT:
                    if monitor.waitForAbort(0.1):
                        return
                    verdict_wait += 0.1
                watch.decide_stop(xbmc.getCondVisibility("Window.IsActive(okdialog)"))
            if watch.probe_now():
                if not player.isPlaying():  # never a second tuner while something plays
                    self._report_live_failure(watch, mode)
                return
            if watch.result is not None:
                return
        if watch.probe_at_backstop(player.isPlaying()):
            self._report_live_failure(watch, mode)

    def _report_live_failure(self, watch, mode):
        status, body = self.api.live_probe(watch.channel_id, mode)
        msg = live.error_message(status, body, _live_strings())
        xbmc.log(f"[epgstation] live: channel {watch.channel_id} did not start (probe: {status} {body})",
                 xbmc.LOGWARNING)
        if msg:
            xbmcgui.Dialog().notification(ADDON.getAddonInfo("name"), msg, xbmcgui.NOTIFICATION_ERROR)


    # --- program guide (013) -------------------------------------------------------------------------------------

    def _int_param(self, name, allow=()):
        """An integer URL parameter (or one of `allow` as is); BadParamError when missing or malformed."""
        value = self.params.get(name)
        if value in allow:
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            raise BadParamError(f"{name}={value!r}") from None

    def _guide_schedules(self):
        """The next 24 h of every watchable broadcast type (disk cache, guide.TTL)."""
        types = live.live_types(self.api.channels())
        return _guide_cache().schedules(self.api, types)

    def do_guide_now(self):
        """No t: the hour folders (no request). t=now or ms: one program per channel airing at t."""
        strings, now = _guide_strings(), _now_ms()
        t = self.params.get("t")
        if not t:
            self._add(guide.slot_folders(now, strings), content="files", category=strings["whats_on"] or None,
                      cache=False)  # labels follow the clock
            return
        at = now if t == "now" else self._int_param("t")
        rows = guide.on_air_at(self._guide_schedules(), at)
        index, records = self._marks([p for _, p in rows], now)  # record from the guide (014)
        out = guide.program_items(rows, self.api, now, _lang(), _mark_strings(), "slot", _live_error, index=index,
                                  records=records)
        self._add(out, sort=self.SORT_GUIDE, category=guide.slot_header(at, now, strings), cache=False)

    def do_guide_genres(self):
        now = _now_ms()
        counts = guide.genre_counts(self._guide_schedules(), now)
        self._add(guide.genre_folders(counts, _lang()), content="files", category=_s(30021), cache=False)

    def do_guide_genre(self):
        genre, now = self._int_param("genre"), _now_ms()
        rows = guide.genre_programs(self._guide_schedules(), genre, now)
        index, records = self._marks([p for _, p in rows], now)  # record from the guide (014)
        out = guide.program_items(rows, self.api, now, _lang(), _mark_strings(), "genre", _live_error, index=index,
                                  records=records)
        self._add(out, sort=self.SORT_GUIDE, category=guide.genre_name(genre, _lang()), cache=False)

    def do_guide_channel(self):
        """No day: the day folders of the next 8 days. day=<local midnight ms>: that day's programs."""
        p, strings, now = self.params, _guide_strings(), _now_ms()
        channel_id = self._int_param("channelId")
        name = p.get("name") or str(channel_id)
        if "day" not in p:
            days = self.api.channel_schedule(channel_id, guide.local_midnight(now), days=guide.DAYS)
            channel = days[0]["channel"] if days else {"id": channel_id, "name": name}
            out = guide.day_folders(channel, guide.day_buckets(days, now), now, strings, self.api)  # (003): logo
            self._add(out, content="files", category=strings["channel_schedule"].format(name), cache=False)
            return
        start = guide.local_midnight(self._int_param("day"))  # day=<local midnight ms> (absolute: survives midnight)
        sched = self.api.channel_schedule(channel_id, start, days=1)
        channel = sched[0]["channel"] if sched else {"id": channel_id, "name": name}
        progs = guide.day_programs(sched, start, now)
        index, records = self._marks(progs, now)  # record from the guide (014)
        out = guide.program_items([(channel, x) for x in progs], self.api, now, _lang(), _mark_strings(), "day",
                                  _live_error, context=False, index=index, records=records)
        self._add(out, sort=self.SORT_GUIDE, category=f"{name} {guide.date_text(start, strings)}", cache=False)

    # --- record from the guide (014) ------------------------------------------------------------------------------

    def _reserve_index(self, start, end):
        """reserves.lists_index of GET /reserves/lists for [start, end] (never cached); None when it fails or the answer
        has another shape (the listing then shows no markers and no reservation entries)."""
        try:
            return reserves.lists_index(self.api.reserve_lists(start, end, timeout=LISTS_TIMEOUT))
        except (ApiError, KeyError, TypeError, AttributeError) as e:
            xbmc.log(f"[epgstation] reserve lists: {e} (no reservation markers or entries)", xbmc.LOGWARNING)
            return None

    def _records_for(self, index, programs, now):
        """GET /recording (for ●) only when a listed program on air has a normal reservation."""
        if not reserves.needs_records(index, programs, now):
            return ()
        try:
            return self.api.recording(timeout=LISTS_TIMEOUT)["records"]
        except (ApiError, KeyError, TypeError) as e:
            xbmc.log(f"[epgstation] recording for markers: {e}", xbmc.LOGWARNING)
            return ()

    def _marks(self, programs, now, window=None):
        """(index, records) of a guide listing: one lists GET over the listed programs (window: its own range)."""
        window = window or reserves.lists_window(programs)
        if window is None:
            return {}, ()
        index = self._reserve_index(*window)
        return index, self._records_for(index, programs, now)

    def do_guide_info(self):
        """Click on a future program: Kodi's information dialog with the program as it is now in the guide."""
        p, strings = self.params, _guide_strings()
        program_id = self._int_param("programId")
        try:
            program = self.api.schedule_detail(program_id)
        except ApiError as e:
            xbmc.log(f"[epgstation] guide info {p.get('programId')}: {e}", xbmc.LOGWARNING)
            msg = strings["gone"] if e.status == 404 else f"{_s(30100)}: {e}"
            xbmcgui.Dialog().notification(ADDON.getAddonInfo("name"), msg, xbmcgui.NOTIFICATION_ERROR)
            return
        channel = {"id": p.get("channelId") or program.get("channelId"), "name": p.get("name") or "",
                   "hasLogoData": p.get("logo") == "1"}
        d = guide.program_item(channel, program, self.api, _now_ms(), _lang(), strings, "day", _live_error,
                               context=False)
        li = _listitem(d)
        if d["playable"]:
            # on air since the list was made: the dialog's 再生 plays it through live_play (one stream, no HEAD).
            # A future program keeps no path: 再生 then gives Kodi's own "Playback failed" (no request, no stream);
            # Estuary always shows that button and a plugin cannot hide it.
            li.setPath(d["path"])
        xbmcgui.Dialog().info(li)

    # --- full detail view (002) ----------------------------------------------------------------------------------

    def do_droplog(self):
        """ドロップログ (context entry): one GET /dropLogs/{id}, shown in Kodi's text viewer, summary first."""
        log_id = self._int_param("log")
        s = details.block_strings(_item_strings())
        s.update({"failed": _s(30050) or "Failed: {0}", "failed_net": _s(30059) or "Failed (connection error)"})
        try:
            text = self.api.drop_log(log_id, DROPLOG_MAX_KB)
        except ApiError as e:
            xbmc.log(f"[epgstation] drop log {log_id}: {e}", xbmc.LOGWARNING)
            if e.status == 404:
                msg = s["droplog_gone"]
            elif e.status == 416:
                msg = s["droplog_large"]
            elif e.status is None:
                msg = s["failed_net"]
            else:
                msg = s["failed"].format(f"HTTP {e.status}")
            self._notify(msg, xbmcgui.NOTIFICATION_WARNING)
            return
        text = details.droplog_text(text)
        if not text.strip():  # an empty log: nothing to show
            self._notify(s["droplog_gone"], xbmcgui.NOTIFICATION_WARNING)
            return
        xbmcgui.Dialog().textviewer(s["droplog"], text, usemono=True)

    # --- reservations (007) --------------------------------------------------------------------------------------

    def _all_reserves(self):
        """Every reservation (GET /reserves?type=all, pages of reserves.PAGE until total)."""
        out, offset = [], 0
        while True:
            page = self.api.reserves(offset=offset, limit=reserves.PAGE)
            out += page["reserves"]
            offset += reserves.PAGE
            if offset >= page.get("total", 0) or not page["reserves"]:
                return out

    def do_reserves(self):
        """録画予約: the 競合 / 重複 / 除外 folders and every upcoming normal reservation; type=<state>: that state."""
        state = self.params.get("type")
        if state is not None and state not in reserves.STATES:
            raise BadParamError(f"type={state!r}")
        strings = _reserve_strings()
        all_reserves = self._all_reserves()
        channels = self.api.channels()
        rules = {r["id"]: items.rule_name(r) for r in self.api.rules().get("rules", [])}
        records = self.api.recording()["records"]  # ● and 録画を停止 only for what really records (L3)
        out = reserves.listing(all_reserves, state, items.channel_names(channels),
                               {c["id"] for c in channels if c.get("hasLogoData")}, rules, self.api, _now_ms(),
                               _lang(), strings, _reserve_error, records)
        category = strings[state] if state else strings["reserves"]
        self._add(out, sort=self.SORT_RESERVES, category=category or None, cache=False)

    def _notify(self, message, icon=xbmcgui.NOTIFICATION_INFO):
        xbmcgui.Dialog().notification(ADDON.getAddonInfo("name"), message, icon)

    def _gone(self, strings):
        """The reservation (or recording) is not there any more: say so and reload the list."""
        self._notify(strings["gone"], xbmcgui.NOTIFICATION_WARNING)
        xbmc.executebuiltin("Container.Refresh")

    def do_reserve_info(self):
        """Click on a reservation: Kodi's information dialog with the reservation as it is now (one GET)."""
        p, strings = self.params, _reserve_strings()
        reserve_id = self._int_param("reserveId")
        try:
            r = self.api.reserve(reserve_id)
        except ApiError as e:
            if e.status != 404:
                raise
            self._gone(strings)
            return
        cid = r["channelId"]
        rules = {r["ruleId"]: p["rule"]} if r.get("ruleId") is not None and p.get("rule") else {}
        # recording=1: the list saw it in /recording (the 録画中 line; no extra GET for the dialog)
        records = [{k: r[k] for k in ("channelId", "programId", "startAt") if k in r}] \
            if p.get("recording") == "1" else []
        d = reserves.reserve_item(r, {cid: p.get("name") or str(cid)}, {cid} if p.get("logo") == "1" else set(),
                                  rules, self.api, _now_ms(), _lang(), strings, _reserve_error, records=records)
        xbmcgui.Dialog().info(_listitem(d))  # no path: Kodi's 再生 button gives its own "Playback failed"

    def _fresh_reserve(self, reserve_id):
        """GET /reserves/{id}, None when it no longer exists."""
        try:
            return self.api.reserve(reserve_id)
        except ApiError as e:
            if e.status == 404:
                return None
            raise

    def _reserve_of_recording(self, recorded_id):
        """The reservation of an in-progress recording (RecordedItem has no reserveId): the reservations at its
        start (GET /reserves/lists), matched by channel + programId / startAt. None when it is not recording."""
        try:
            rec = self.api.recorded_item(recorded_id)
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        if not rec.get("isRecording"):
            return None
        lists = self.api.reserve_lists(rec["startAt"], rec["startAt"])
        ids = [x["reserveId"] for key in ("normal", "conflicts", "skips", "overlaps") for x in lists.get(key) or []]
        candidates = [r for r in (self._fresh_reserve(i) for i in ids) if r is not None]
        return reserves.match_recording(candidates, rec)

    def _wait_stopped(self, r):
        """The server stops a recording asynchronously: wait until /recording no longer lists it, so the refreshed
        録画中 does not show it. At most reserves.STOP_WAIT s in all (monotonic deadline): each request's timeout
        and each pause is capped to what is left."""
        monitor, deadline = xbmc.Monitor(), _clock() + reserves.STOP_WAIT
        while True:
            left = deadline - _clock()
            if left <= 0.05:
                return
            try:
                if not reserves.recording_still_listed(self.api.recording(timeout=left)["records"], r):
                    return
            except ApiError as e:
                xbmc.log(f"[epgstation] reserve: stop check: {e}", xbmc.LOGWARNING)
                return
            left = deadline - _clock()
            if left <= 0 or monitor.waitForAbort(min(reserves.STOP_STEP, left)):
                return

    def _reserve_target(self, op):
        """The reservation the action is for, read now (reserveId, or recordedId of an in-progress recording), and
        whether `op` is still its action: (reserve or None, still_valid)."""
        p = self.params
        if "recordedId" in p:
            r = self._reserve_of_recording(self._int_param("recordedId"))
        else:
            r = self._fresh_reserve(self._int_param("reserveId"))
        if r is None:
            return None, False
        return r, reserves.action_for(r, _now_ms(), self.api.recording()["records"]) == op

    def _not_now(self, r, strings):
        """Gone or changed since the list (or since the dialog opened): say so, refresh, write nothing."""
        if r is None:
            self._gone(strings)
            return
        self._notify(strings["changed"], xbmcgui.NOTIFICATION_WARNING)
        xbmc.executebuiltin("Container.Refresh")

    def do_reserve_action(self):
        """The only write path: re-read the reservation, check its state, ask (No focused), re-read and check again
        (the dialog may stay open while the state changes; a double click opens two), one write, notify, refresh.
        URL: op=stop|cancel|unskip|unoverlap and reserveId=N, or op=stop and recordedId=N (録画中)."""
        p, strings = self.params, _reserve_strings()
        op = p.get("op")
        if op not in reserves.OPS:
            raise BadParamError(f"op={op!r}")
        if "recordedId" in p and op != "stop":
            raise BadParamError(f"op={op!r} with recordedId")
        try:
            r, valid = self._reserve_target(op)
        except ApiError as e:
            if e.status is not None:
                raise
            xbmc.log(f"[epgstation] reserve: {op} lookup failed: {e}", xbmc.LOGERROR)
            self._notify(strings["failed_net"], xbmcgui.NOTIFICATION_ERROR)
            return
        if not valid:  # changed since the list was made
            self._not_now(r, strings)
            return
        try:
            channel = items.channel_names(self.api.channels()).get(r["channelId"])
        except ApiError as e:
            xbmc.log(f"[epgstation] {e}", xbmc.LOGWARNING)
            channel = None
        channel = items.to_half(channel or str(r["channelId"]))
        label = reserves.entry_label(op, r, strings)
        if not xbmcgui.Dialog().yesno(label, reserves.confirm_message(op, r, channel, strings, _now_ms()),
                                      nolabel=xbmc.getLocalizedString(KODI_CANCEL), yeslabel=label,
                                      defaultbutton=xbmcgui.DLG_YESNO_NO_BTN):
            return
        r, valid = self._reserve_target(op)  # again: what the yes was for may have changed meanwhile
        if not valid:
            self._not_now(r, strings)
            return
        what = f"{op} reserve {r['id']} ({items.to_half(r['name'])})"
        xbmc.log(f"[epgstation] reserve: {what}", xbmc.LOGINFO)
        try:
            if op in ("stop", "cancel"):
                self.api.cancel_reserve(r["id"])
            elif op == "unskip":
                self.api.remove_skip(r["id"])
            else:
                self.api.remove_overlap(r["id"])
        except ApiError as e:
            maybe = "" if e.status else " (no answer: the server may still have applied it)"
            xbmc.log(f"[epgstation] reserve: {what} failed: {e}{maybe}", xbmc.LOGERROR)
            self._notify(reserves.error_message(e.status, e.detail, strings), xbmcgui.NOTIFICATION_ERROR)
            xbmc.executebuiltin("Container.Refresh")
            return
        xbmc.log(f"[epgstation] reserve: {what} done", xbmc.LOGINFO)
        self._notify(reserves.done_message(op, r, strings))
        if op == "stop":
            self._wait_stopped(r)
        xbmc.executebuiltin("Container.Refresh")

    # --- record from the guide (014) ------------------------------------------------------------------------------

    def _encode_mode(self):
        """Setting 録画後のエンコード: 1 -> the server's first encode mode (GET /config encode); else or none: None."""
        if _setting("record_encode") != "1":
            return None
        modes = self.api.config().get("encode") or []
        if not modes:
            xbmc.log("[epgstation] reserve: the server has no encode mode: recording without encode", xbmc.LOGINFO)
            return None
        return str(modes[0])

    def _recordable(self, program_id, strings):
        """The guide program read now if it can still be reserved: in the guide, not ended, no reservation for it
        (GET /reserves/lists over its time). Else say why, refresh and return None."""
        try:
            program = self.api.schedule_detail(program_id)
        except ApiError as e:
            if e.status != 404:
                raise
            reason = "program_gone"
        else:
            if not reserves.can_record(program, _now_ms()):
                reason = "ended"
            elif program_id in self._lists_index_or_error(program):
                reason = "already"
            else:
                return program
        self._notify(strings[reason], xbmcgui.NOTIFICATION_WARNING)
        xbmc.executebuiltin("Container.Refresh")
        return None

    def _lists_index_or_error(self, program):
        """The reservations over the program's time; an answer of another shape is an ApiError (no write)."""
        lists = self.api.reserve_lists(program["startAt"], program["endAt"])
        try:
            return reserves.lists_index(lists)
        except (KeyError, TypeError, AttributeError) as e:
            raise ApiError(f"/reserves/lists: unexpected answer: {e!r}", 200, "unexpected answer") from e

    def _lookup_failed(self, exc, strings):
        """A read before or after the dialog failed: short toast, no write."""
        xbmc.log(f"[epgstation] reserve: record lookup failed: {exc}", xbmc.LOGERROR)
        self._notify(reserves.add_error(exc.status, exc.detail, strings), xbmcgui.NOTIFICATION_ERROR)

    def _confirm_record(self, program, channel, mode, strings):
        heading = strings["record"]
        return xbmcgui.Dialog().yesno(heading, reserves.record_message(program, channel, _now_ms(), mode, strings),
                                      nolabel=xbmc.getLocalizedString(KODI_CANCEL), yeslabel=heading,
                                      defaultbutton=xbmcgui.DLG_YESNO_NO_BTN)

    def _recheck(self, program_id, strings):
        """_recordable after a yes; a failed read is a short toast (no write)."""
        try:
            return self._recordable(program_id, strings)
        except ApiError as e:
            self._lookup_failed(e, strings)
            return None

    def do_record(self):
        """The second audited write path: re-read the program (in the guide, not ended, not reserved), ask (No
        focused), re-read again after the yes, exactly one POST /reserves, notify, refresh. URL: programId=P,
        channelId=C, name=<channel>."""
        p, strings = self.params, _record_strings()
        program_id = self._int_param("programId")
        channel = p.get("name") or p.get("channelId") or ""
        try:
            program = self._recordable(program_id, strings)
            if program is None:
                return
            mode = self._encode_mode()
        except ApiError as e:
            self._lookup_failed(e, strings)
            return
        asked_on_air = program["startAt"] <= _now_ms()
        if not self._confirm_record(program, channel, mode, strings):
            return
        program = self._recheck(program_id, strings)  # ended or reserved (another dialog, the web UI) meanwhile?
        if program is None:
            return
        if not asked_on_air and program["startAt"] <= _now_ms():
            # it started while the dialog was open: the yes was for a future program, ask once more with the warning
            if not self._confirm_record(program, channel, mode, strings):
                return
            program = self._recheck(program_id, strings)
            if program is None:
                return
        what = f"add program {program_id} ({items.to_half(program['name'])})"
        xbmc.log(f"[epgstation] reserve: {what}", xbmc.LOGINFO)
        try:
            reserve_id = self.api.create_reserve(program_id, mode)
        except ApiError as e:
            maybe = " (no answer: the server may still have applied it)" if reserves.maybe_applied(e.status, e.detail) \
                else ""
            xbmc.log(f"[epgstation] reserve: {what} failed: {e}{maybe}", xbmc.LOGERROR)
            icon = xbmcgui.NOTIFICATION_WARNING if e.detail in reserves.ADD_ERRORS else xbmcgui.NOTIFICATION_ERROR
            self._notify(reserves.add_error(e.status, e.detail, strings), icon)
            xbmc.executebuiltin("Container.Refresh")
            return
        xbmc.log(f"[epgstation] reserve: {what} done: reserve {reserve_id}", xbmc.LOGINFO)
        self._notify(reserves.record_done(program, _now_ms(), strings))
        xbmc.executebuiltin("Container.Refresh")
        if program["startAt"] <= _now_ms() and self._wait_recording(program):
            xbmc.executebuiltin("Container.Refresh")  # now with ● and 録画を停止

    def _wait_recording(self, program):
        """An on-air add: EPGStation starts the recorder a moment after the 201. Wait until /recording lists it, at
        most reserves.RECORD_WAIT s in all (monotonic deadline, each request and pause capped to what is left)."""
        r = {"channelId": program.get("channelId"), "programId": program["id"], "startAt": program["startAt"]}
        monitor, deadline = xbmc.Monitor(), _clock() + reserves.RECORD_WAIT
        while True:
            left = deadline - _clock()
            if left <= 0.05:
                return False
            try:
                if reserves.recording_still_listed(self.api.recording(timeout=left)["records"], r):
                    return True
            except (ApiError, KeyError, TypeError) as e:
                xbmc.log(f"[epgstation] reserve: recording check: {e}", xbmc.LOGWARNING)
                return False
            left = deadline - _clock()
            if left <= 0 or monitor.waitForAbort(min(reserves.STOP_STEP, left)):
                return False

    # --- protect / delete (006) ----------------------------------------------------------------------------------

    def _fresh_recorded(self, recorded_id):
        """GET /recorded/{id} (full-width), None when it no longer exists."""
        try:
            return self.api.recorded_item(recorded_id)
        except ApiError as e:
            if e.status == 404:
                return None
            raise

    def _recorded_target(self, op, recorded_id, strings):
        """The recording read now if op is still possible on it; else say why (gone / protected / changed), refresh
        and return None. A failed read is a short toast (no dialog, no write)."""
        try:
            rec = self._fresh_recorded(recorded_id)
        except ApiError as e:
            xbmc.log(f"[epgstation] recorded: {op} lookup failed: {e}", xbmc.LOGERROR)
            self._notify(recorded.error_message(e.status, e.detail, strings), xbmcgui.NOTIFICATION_ERROR)
            return None
        if rec is None:
            message = strings["gone"]
            self._mark_changed(recorded_id, "delete")  # deleted elsewhere: never shown again from a cache
        elif not recorded.can(op, rec):
            message = recorded.refusal(op, rec, strings)
            self._mark_changed(recorded_id, None)  # changed elsewhere: the warm library refreshes (review P2b)
        else:
            return rec
        self._notify(message, xbmcgui.NOTIFICATION_WARNING)
        xbmc.executebuiltin("Container.Refresh")
        return None

    def do_recorded_action(self):
        """The third audited write path: re-read the recording, check its state, ask (キャンセル focused), re-read and
        check again after the yes, exactly one write, notify, refresh. URL: op=protect|unprotect|delete|delete_file and
        id=N (delete_file: the file is chosen in a select list after the re-read, never from the URL)."""
        strings = _recorded_strings()
        op = self.params.get("op")
        if op not in recorded.OPS:
            raise BadParamError(f"op={op!r}")
        recorded_id = self._int_param("id")
        rec = self._recorded_target(op, recorded_id, strings)
        if rec is None:
            return
        video = None
        if op == "delete_file":  # delete a file (018): which file, picked by name and size
            video = self._recorded_file(rec, strings)
            if video is None:
                return
        try:
            channel = items.channel_names(self.api.channels()).get(rec["channelId"])
        except ApiError as e:
            xbmc.log(f"[epgstation] {e}", xbmc.LOGWARNING)
            channel = None
        channel = items.to_half(channel or str(rec["channelId"]))
        label = yes = recorded.entry_label(op, strings)
        if video is not None:  # delete a file (018): heading ファイルを削除, yes button 削除
            message = recorded.confirm_file_message(rec, video["id"], channel, strings)
            yes = recorded.entry_label("delete", strings)
        else:
            message = recorded.confirm_message(op, rec, channel, strings)
        if not xbmcgui.Dialog().yesno(label, message,
                                      nolabel=xbmc.getLocalizedString(KODI_CANCEL), yeslabel=yes,
                                      defaultbutton=xbmcgui.DLG_YESNO_NO_BTN):
            return
        shown = recorded.fingerprint(rec)
        rec = self._recorded_target(op, recorded_id, strings)  # again: the dialog may have stayed open a while
        if rec is None:
            return
        # another recording than the dialog showed (files, encode), or (018) the chosen file is no longer in it
        if recorded.fingerprint(rec) != shown or (video is not None and video["id"] not in
                                                  [v.get("id") for v in rec.get("videoFiles") or []]):
            self._mark_changed(recorded_id, None)
            self._notify(strings["changed"], xbmcgui.NOTIFICATION_WARNING)
            xbmc.executebuiltin("Container.Refresh")
            return
        self._recorded_write(op, rec, strings, video)

    def _recorded_file(self, rec, strings):  # delete a file (018)
        """The video file the user picks in Kodi's select list (any one: can() keeps 2+ files), None on Back."""
        files = recorded.delete_file_choices(rec)
        idx = xbmcgui.Dialog().select(strings["choose_file"],
                                      items.video_choices(files, self._file_durations(rec, files)))
        return files[idx] if 0 <= idx < len(files) else None

    def _recorded_write(self, op, rec, strings, video=None):
        """Exactly one write for a confirmed op, then the library changed-marker, a toast and Container.Refresh."""
        what = f"{op} recorded {rec['id']} ({items.to_half(rec['name'])})"
        if video is not None:  # delete a file (018)
            what += f" file {video['id']} ({video.get('name')})"
        # delete a file (018): the recording stays, so its library marker is only a refresh
        mark = None if op == "delete_file" else op
        xbmc.log(f"[epgstation] recorded: {what}", xbmc.LOGINFO)
        try:
            if op == "protect":
                self.api.protect_recorded(rec["id"])
            elif op == "unprotect":
                self.api.unprotect_recorded(rec["id"])
            elif op == "delete_file":  # delete a file (018)
                self.api.delete_video_file(video["id"])
            else:
                self.api.delete_recorded(rec["id"])
        except ApiError as e:
            maybe = recorded.maybe_applied(e.status, e.detail)
            note = " (no answer: the server may still have applied it)" if maybe else ""
            xbmc.log(f"[epgstation] recorded: {what} failed: {e}{note}", xbmc.LOGERROR)
            # always refresh the library; record the op when it may have applied, a deletion when the server says
            # the recording is gone (review L4)
            gone = e.detail in recorded.GONE_ERRORS
            self._mark_changed(rec["id"], "delete" if gone else mark if maybe else None)
            icon = xbmcgui.NOTIFICATION_WARNING if recorded.is_refusal(e.detail) else xbmcgui.NOTIFICATION_ERROR
            self._notify(recorded.error_message(e.status, e.detail, strings), icon)
            xbmc.executebuiltin("Container.Refresh")
            return
        xbmc.log(f"[epgstation] recorded: {what} done", xbmc.LOGINFO)
        self._mark_changed(rec["id"], mark)
        self._notify(recorded.done_message(op, strings))
        xbmc.executebuiltin("Container.Refresh")

    def _mark_changed(self, recorded_id, op):
        xbmcvfs.mkdirs(PROFILE)
        if not library.mark_changed(xbmcvfs.translatePath(PROFILE), _server_url(), recorded_id, op):
            xbmc.log("[epgstation] recorded: could not write the library changed-marker", xbmc.LOGWARNING)


def run():
    Plugin(sys.argv).run()
