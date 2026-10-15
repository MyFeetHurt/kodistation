"""Recordings: the menus, Movies / TV shows / lists, search, playback, file choice and the drop log."""
import xbmc
import xbmcgui

from .. import details, durations, items, library, thumbs, ui
from ..api import ApiError

DROPLOG_MAX_KB = 512  # GET /dropLogs maxsize (the server's default); a larger log answers HTTP 416

DETAIL_STRINGS = (  # details.DEFAULT_STRINGS keys -> string IDs
    ("detail_header", 30251), ("detail_broadcast", 30252), ("detail_genre", 30253), ("detail_video", 30254),
    ("detail_audio", 30255), ("detail_drops", 30256), ("detail_encoding", 30257), ("droplog", 30258),
    ("droplog_large", 30259), ("droplog_gone", 30260))
ITEM_STRINGS = (
    ("choose", 30011), ("partial", 30012), ("length", 30013), ("stop", 30036),
    ("protect", 30072), ("unprotect", 30073), ("delete", 30074), ("marker", 30075), ("protect_line", 30087),
    ("delete_file", 30245), ("stream", 30088), ("files", 30080), ("weekdays", 30026)) + DETAIL_STRINGS
LIST_STRINGS = (("recent", 30001), ("search", 30006), ("rules", 30005))

on_parse_error = ui.parse_error_logger("recording")


def item_strings():
    return ui.strings(ITEM_STRINGS)


def real_duration_on():
    return ui.setting("real_duration") != "false"


def page_size():
    return max(10, ui.int_setting("page_size", 50))


# --- caches in the addon profile ----------------------------------------------------------------------------------

def duration_cache():
    return durations.DurationCache(ui.profile_path("durations.json"), ui.server_url())


def library_cache():
    return library.Library(ui.profile_path(), ui.server_url())


def thumb_cache():
    return thumbs.ThumbCache(ui.profile_path("thumbs.json"), ui.server_url())


def snapshot(ctx, with_records=True):
    """All recordings from the library cache: one probe + /recording when it is current."""
    lib = library_cache()
    snap = lib.current(ctx.api, on_parse_error, with_records)
    for msg in lib.warnings:
        ui.log(msg, xbmc.LOGWARNING)
    return snap


def _thumb_work(work):
    """Run a thumbnail check; never lets an error cost the listing (logged, no thumbnail counts as blank)."""
    try:
        cache = thumb_cache()
        blank = work(cache)
        if not cache.save():
            ui.log("could not write thumbs.json", xbmc.LOGWARNING)
        return blank
    except Exception as e:  # noqa: BLE001 - any failure here must not cost the directory
        ui.log(f"thumbnail check failed: {e!r}", xbmc.LOGWARNING)
        return set()


def blank_thumbs(ctx, ids):
    """The blank ones of these thumbnail ids (cached, or fetched within this listing's budget)."""
    return _thumb_work(lambda cache: thumbs.check(ids, ctx.api, cache, thumbs.Budget()))


def file_durations(ctx, rec, files):
    """Real lengths of these files ({id: secs}); empty while recording or when turned off."""
    if rec.get("isRecording") or not real_duration_on():
        return {}
    cache = duration_cache()
    out = durations.for_files(files, ctx.api, cache)
    cache.save()
    return out


def recorded_items(ctx, records, channels=None, meta=None, logos=None):
    """Item dicts; meta: {id: cached metadata} (library), the rest are parsed. Real file lengths only for lists of up
    to items.REAL_DURATION_MAX recordings (else the EPG length). channels None: one GET /channels for the names and
    logos."""
    real = {}
    meta = meta or {}
    if real_duration_on() and len(records) <= items.REAL_DURATION_MAX:
        cache = duration_cache()
        real = durations.fill(records, ctx.api, cache, ui.prefer())
        cache.save()
    if channels is None:
        channels, logos = ctx.channels()
    blank = blank_thumbs(ctx, [items.newest_thumb(r) for r in records])
    lang, strings, icon = ui.lang(), item_strings(), ui.addon_icon()
    return [items.recorded_item(r, ctx.api, channels, lang, strings, real.get(r["id"]), on_parse_error,
                                meta.get(r["id"], items.PARSE), logos, blank, icon)
            for r in records]


def recording_item(ctx, rec, real_duration=None, channels_timeout=None):
    """Item dict of one recording for playing it; channel names and logos are optional (short timeout)."""
    try:
        channels, logos = ctx.channels(timeout=channels_timeout)
    except ApiError as e:  # only for the studio name and logo: never block playback on it
        ui.log(str(e), xbmc.LOGWARNING)
        channels, logos = {}, None
    # no thumbnail check before playback (an unchecked thumbnail counts as good)
    return items.recorded_item(rec, ctx.api, channels, ui.lang(), item_strings(), real_duration, on_parse_error,
                               logos=logos, fallback_icon=ui.addon_icon())


# --- menus ----------------------------------------------------------------------------------------------------------

def root(ctx):
    ctx.add(items.root_menu(ui.strings((("movies", 30007), ("tvshows", 30008), ("recent", 30001),
                                        ("recording", 30002), ("reserves", 30030), ("live", 30015),
                                        ("search", 30006), ("browse", 30009)))), content="files")


def browse(ctx):
    strings = ui.strings((("channels", 30003), ("genres", 30004), ("rules", 30005)))
    ctx.add(items.browse_menu(strings), content="files", category=ui.s(30009))


# --- library views (cache=False everywhere: a delete or protect must show when going back) --------------------------

def movies(ctx):
    snap = snapshot(ctx)
    entries = items.movie_entries(snap.entries)
    out = recorded_items(ctx, [snap.records[e["id"]] for e in entries], snap.channels, snap.meta, snap.logos)
    ctx.add(items.label_movies(out, entries, ui.lang()), content="movies", sort=ui.SORT_MOVIES,
            category=ui.s(30007), cache=False)


def tvshows(ctx):
    snap = snapshot(ctx)
    lang = ui.lang()
    # each show's newest thumbnail that is not known blank; settle() skips runs of blank episodes in one budget
    blank = _thumb_work(lambda cache: thumbs.settle(
        lambda known: items.show_thumb_candidates(snap.entries, known), ctx.api, cache, thumbs.Budget()))
    icon = ui.addon_icon()
    out = [items.tvshow_folder(show, snap.records, ctx.api, snap.channels, lang, ui.s(30014), snap.logos, icon)
           for show in items.group_shows(snap.entries, blank)]
    ctx.add(out, content="tvshows", sort=ui.SORT_TVSHOWS, category=ui.s(30008), cache=False)


def show(ctx):
    snap = snapshot(ctx)
    found = items.find_show(snap.entries, ctx.params.get("key", ""))
    records = [snap.records[i] for i in found["ids"]] if found else []
    out = items.label_episodes(recorded_items(ctx, records, snap.channels, snap.meta, snap.logos),
                               [snap.meta.get(r["id"]) for r in records], ui.lang())
    title = items.show_title(found, snap.channels, ui.s(30014)) if found else None
    ctx.add(out, content="episodes", sort=ui.SORT_EPISODES, category=title, cache=False)


def recorded_list(ctx):
    """?action=list: /recorded filtered by channel, genre, rule or keyword, paged, recordings in progress first."""
    p = ctx.params
    if p.get("subGenre") is not None:  # /recorded has no subgenre filter: from the library
        return _subgenre_list(ctx)
    offset = int(p.get("offset", 0))
    limit = page_size()
    data = ctx.api.recorded(offset=offset, limit=limit, rule_id=p.get("ruleId"), channel_id=p.get("channelId"),
                            genre=p.get("genre"), keyword=p.get("keyword"))
    records = data["records"]
    if offset == 0 and not p.get("keyword"):
        records = items.in_progress_matching(
            ctx.api.recording()["records"], p.get("channelId"), p.get("genre"), p.get("ruleId")) + records
    channels, logos = ctx.channels()
    out = recorded_items(ctx, records, channels, logos=logos)
    if offset + limit < data["total"]:
        out.append(items.folder(f"{ui.s(30010)} ({offset + limit + 1}-)", **dict(p, offset=offset + limit)))
    ctx.add(out, sort=ui.SORT_LIST, category=items.list_category(p, channels, ui.lang(), ui.strings(LIST_STRINGS)),
            cache=False)


def _subgenre_list(ctx):
    """?action=list&genre=G&subGenre=S from the library snapshot, unpaged, newest first."""
    genre, sub = ctx.int_param("genre"), ctx.int_param("subGenre")
    snap = snapshot(ctx)
    records = [snap.records[i] for i in items.subgenre_entries(snap.entries, genre, sub) if i in snap.records]
    out = recorded_items(ctx, records, snap.channels, snap.meta, snap.logos)
    category = items.list_category(ctx.params, snap.channels, ui.lang(), ui.strings(LIST_STRINGS))
    ctx.add(out, sort=ui.SORT_LIST, category=category, cache=False)


def recording(ctx):
    ctx.add(recorded_items(ctx, ctx.api.recording()["records"]), sort=ui.SORT_LIST, category=ui.s(30002),
            cache=False)


def channels(ctx):
    names, logos = ctx.channels()
    ctx.add(items.channel_folders(ctx.api.recorded_options(), names, logos, ctx.api), content="files",
            category=ui.s(30003), cache=False)


def genres(ctx):
    # counts of genre1-3 from the library (/recorded/options counts genre1 only)
    entries = snapshot(ctx, with_records=False).entries
    ctx.add(items.genre_folders(entries, ui.lang()), content="files", category=ui.s(30004), cache=False)


def genre(ctx):
    """All + the subgenres of one genre that have recordings."""
    number, lang = ctx.int_param("genre"), ui.lang()
    entries = snapshot(ctx, with_records=False).entries
    ctx.add(items.subgenre_folders(number, entries, lang, ui.s(30270) or "All ({0})"), content="files",
            category=items.list_category({"genre": str(number)}, {}, lang, {}), cache=False)


def rules(ctx):
    counts = items.rule_counts(snapshot(ctx, with_records=False).entries)
    ctx.add(items.rule_folders(ctx.api.rules(), counts), content="files", category=ui.s(30005), cache=False)


# --- search ---------------------------------------------------------------------------------------------------------

def search(ctx):
    """?action=search: with a keyword the results; without one (an old folder link: a favourite, a skin shortcut)
    ask and list in place."""
    if ctx.params.get("keyword"):
        recorded_list(ctx)
        return
    keyword = xbmcgui.Dialog().input(ui.s(30006))
    if ctx.handle < 0:  # run as a script (RunPlugin): open the results
        if keyword:
            _open_search_results(keyword)
        return
    if not keyword:
        ctx.fail_directory()
        return
    ctx.params["keyword"] = keyword
    recorded_list(ctx)


def search_ask(ctx):
    """The root's search entry (a click action): ask, then open the results at a URL that carries the keyword, so a
    refresh (after a delete) and Back list them again without asking. A handle here is a play request of the item
    (Kore, a favourite): resolved as failed, never a directory."""
    if ctx.handle >= 0:
        ctx.fail_resolve()
    keyword = xbmcgui.Dialog().input(ui.s(30006))
    if keyword:
        _open_search_results(keyword)


def _open_search_results(keyword):
    url = items.plugin_url(action="search", keyword=keyword)
    if xbmc.getCondVisibility("Window.IsActive(videos)"):
        xbmc.executebuiltin(f"Container.Update({url})")
    else:  # from Home (a favourite): open the video window there
        xbmc.executebuiltin(f"ActivateWindow(Videos,{url},return)")


# --- playback -------------------------------------------------------------------------------------------------------

def _play_item(ctx, rec, video, real_duration=None):
    li = ui.listitem(recording_item(ctx, rec, real_duration))
    li.setPath(ctx.api.video_url(video["id"]) + ctx.api.kodi_headers())
    return li


def play(ctx):
    rec = ctx.api.recorded_item(ctx.params["id"])
    video = items.pick_video(rec.get("videoFiles"), items.default_prefer(rec, ui.prefer()))
    if video is None:
        ctx.fail_resolve()
        return
    real = file_durations(ctx, rec, [video]).get(video["id"])
    ctx.resolve(_play_item(ctx, rec, video, real))


def choose(ctx):
    """Pick one of the recording's files (TS or an encode) and play it."""
    rec = ctx.api.recorded_item(ctx.params["id"])
    files = rec.get("videoFiles") or []
    real = file_durations(ctx, rec, files)
    idx = xbmcgui.Dialog().select(ui.s(30011), items.video_choices(files, real))
    if idx < 0:
        return
    li = _play_item(ctx, rec, files[idx], real.get(files[idx]["id"]))
    xbmc.Player().play(li.getPath(), li)


def droplog(ctx):
    """The drop log (context entry): one GET /dropLogs/{id}, shown in Kodi's text viewer, summary first."""
    log_id = ctx.int_param("log")
    s = details.block_strings(item_strings())
    s.update({"failed": ui.s(30050) or "Failed: {0}", "failed_net": ui.s(30059) or "Failed (connection error)"})
    try:
        text = ctx.api.drop_log(log_id, DROPLOG_MAX_KB)
    except ApiError as e:
        ui.log(f"drop log {log_id}: {e}", xbmc.LOGWARNING)
        if e.status == 404:
            msg = s["droplog_gone"]
        elif e.status == 416:
            msg = s["droplog_large"]
        elif e.status is None:
            msg = s["failed_net"]
        else:
            msg = s["failed"].format(f"HTTP {e.status}")
        ctx.notify(msg, xbmcgui.NOTIFICATION_WARNING)
        return
    text = details.droplog_text(text)
    if not text.strip():  # an empty log: nothing to show
        ctx.notify(s["droplog_gone"], xbmcgui.NOTIFICATION_WARNING)
        return
    xbmcgui.Dialog().textviewer(s["droplog"], text, usemono=True)

