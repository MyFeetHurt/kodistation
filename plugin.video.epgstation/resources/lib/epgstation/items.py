"""Turn EPGStation API objects into plain directory-item dicts.

No Kodi imports here: everything is unit-testable. plugin.py turns the dicts
into xbmcgui.ListItem objects.

Item dict shape:
    {"label": str, "path": str, "folder": bool, "playable": bool,
     "info": {...InfoTagVideo fields...}, "art": {...}, "props": {...},
     "stream": {"codec", "width", "height", "aspect"?} | None, "context": [(label, action)],
     "audio": {"codec", "channels"} | None (recordings only)}
"""
import re
import time
import urllib.parse

from . import epgmeta
from .genres import GENRE_EN, GENRE_JA, genre_name, item_genres, subgenre_name

PLUGIN = "plugin://plugin.video.epgstation/"

RESOLUTIONS = {
    "240p": (320, 240), "480i": (720, 480), "480p": (720, 480),
    "720p": (1280, 720), "1080i": (1920, 1080), "1080p": (1920, 1080),
    "2160p": (3840, 2160), "4320p": (7680, 4320),
}
CODECS = {"mpeg2": "mpeg2video", "h.264": "h264", "h.265": "hevc"}

# English fallbacks; plugin.py passes the localized strings.
DEFAULT_STRINGS = {"choose": "Choose video file", "partial": "Partial", "length": "Length", "unknown": "Unknown",
                   "stop": "Stop recording",  # stop
                   # recorded.DEFAULT_STRINGS keys of the entries, marker and plot line
                   "protect": "Protect", "unprotect": "Unprotect", "delete": "Delete", "marker": "Protected",
                   "protect_line": "Protected (not auto-deleted)",
                   "delete_file": "Delete a file"}  # its entry

# Lists longer than this show EPG lengths: no real-duration probing of every file (Movies is unpaged).
REAL_DURATION_MAX = 300

# recorded_item(meta=PARSE): parse the recording (the default); pass a cached epgmeta.metadata() result instead.
PARSE = object()

# A finished file shorter than this share of its scheduled slot is flagged as partial.
PARTIAL_RATIO = 0.9


# EPGStation StrUtil.toHalf: full-width ASCII ！-～ -> ! - ~, plus a few quotes and spaces.
_HALF = {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}
_HALF.update({ord("\u201d"): '"', ord("\u2019"): "'", ord("\u2018"): "`", ord("\uffe5"): "\\",
              0x3000: " ", ord("\u301c"): "~"})


def to_half(text):
    """Exact port of EPGStation's StrUtil.toHalf (what isHalfWidth=true returns for name/description/extended)."""
    return text.translate(_HALF) if text else text


# A section header: "◇key" on a line of its own ending in a bare \n, at the start or after a bare \n.
# Values use \r\n line ends, so a value line such as "\r\n◇X(旧Twitter)\r\n" is never a header.
_EXT_HEADER = re.compile(r"(?:\A|(?<=[^\r]\n))\u25c7([^\r\n]*)(?:\n|\Z)")


def raw_extended(extended):
    """The rawExtended {key: value} that EPGStation omits with isHalfWidth=false, rebuilt from `extended`.

    EPGStation builds `extended` as "\n".join("◇" + key + "\n" + value) of rawExtended (values keep their
    CRLFs), so a header is a "◇key" line ending in a bare \n; each value runs to the \n before the next
    header. A key that already started with ◇ gets no second one, so such keys come back without their ◇.
    """
    headers = list(_EXT_HEADER.finditer(extended or ""))
    out = {}
    for i, m in enumerate(headers):
        end = headers[i + 1].start() - 1 if i + 1 < len(headers) else len(extended)
        out[m.group(1)] = extended[m.end():end]
    return out


def plugin_url(**params):
    params = {k: v for k, v in params.items() if v is not None}
    return PLUGIN + ("?" + urllib.parse.urlencode(params) if params else "")


def fmt_time(ms, fmt="%Y-%m-%d %H:%M"):
    return time.strftime(fmt, time.localtime(ms / 1000))


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return str(n)


def fmt_duration(secs):
    """Seconds -> "M:SS" with total minutes (65:00 for a 65 min program, like EPG lengths)."""
    secs = int(round(secs))
    return f"{secs // 60}:{secs % 60:02d}"


def channel_names(channels):
    return {c["id"]: c.get("halfWidthName") or c["name"] for c in channels}


# Flag / version tags in English (genre_language = en). Edition words ("4Kデジタルリマスター版") stay as in the EPG.
FLAG_TAG_EN = {
    "新番組": "New series", "最終回": "Final episode", "再放送": "Rerun", "字幕": "Subtitles",
    "二か国語": "Bilingual", "解説放送": "Audio description", "吹替": "Dubbed", "吹替版": "Dubbed",
    "字幕版": "Subtitled", "データ放送": "Data broadcast", "初放送": "First broadcast", "生放送": "Live",
    "手話": "Sign language",
}
DUB_ROLE_EN = "Dub: "
# Cast roles that come from an EPG credit label (genre_language = en), exact matches only: character names and any
# other role stay as in the EPG.
ROLE_EN = {
    "出演": "Cast", "出演者": "Cast", "レギュラー出演者": "Cast", "キャスト": "Cast",
    "声": "Voice", "声の出演": "Voice",
    "ゲスト": "Guest", "スタジオゲスト": "Guest", "VTRゲスト": "Guest",
    "MC": "Host", "司会": "Host", "司会者": "Host", "進行": "Host",
    "語り": "Narrator", "ナレーション": "Narrator", "ナレーター": "Narrator",
    "キャスター": "Anchor", "解説": "Commentator", "実況": "Play-by-play", "アナウンサー": "Announcer",
    "リポーター": "Reporter", "レポーター": "Reporter", "アシスタント": "Co-host", "ナビゲーター": "Presenter",
    "講師": "Lecturer", "パーソナリティ": "Host",
}


def tag_en(tag):
    return FLAG_TAG_EN.get(tag, tag)


def localized_tags(tags, lang):
    return list(dict.fromkeys(tag_en(t) for t in tags)) if lang == "en" else list(tags)


def localized_role(role, lang):
    """English mode: a credit label role "出演" -> "Cast" (ROLE_EN), the dub role "吹替: 森川陽太" ->
    "Dub: 森川陽太"; anything else (character names) unchanged."""
    if lang != "en" or not role:
        return role
    if role.startswith(epgmeta.DUB_ROLE):
        return DUB_ROLE_EN + role[len(epgmeta.DUB_ROLE):]
    return ROLE_EN.get(role, role)


def build_plot(texts, tech=()):
    """Text blocks (half-width), then the technical lines (recordings: the 【録画情報】 block, details.detail_lines),
    separated by blank lines."""
    parts = list(texts) + (["\n".join(tech)] if tech else [])
    return "\n\n".join(p for p in parts if p)


def raw_plot_texts(rec):
    """Plot without metadata: description, then the raw extended EPG text (when the parser is not used)."""
    return [to_half(rec.get("description") or ""), to_half((rec.get("extended") or "").replace("\r\n", "\n").strip())]


def metadata_info(meta, name, lang):
    """InfoTagVideo fields from epgmeta.metadata() (None: none). Texts are shown half-width."""
    if meta is None:
        return {"title": name, "mediatype": "video"}
    show = to_half(meta["show"])
    info = {"originaltitle": name}
    if meta["kind"] == "movie":
        info.update(mediatype="movie", title=show or name)
        if meta["year"]:
            info["year"] = meta["year"]
    else:
        info.update(mediatype="episode", tvshowtitle=show, title=to_half(meta["subtitle"]) or show or name)
        if meta["episode"] is not None:
            info.update(season=meta["season"] or 1, episode=meta["episode"])
    if meta["cast"]:
        info["cast"] = [(to_half(n), to_half(localized_role(r, lang) or ""), i)
                        for i, (n, r) in enumerate(meta["cast"])]
    if meta["directors"]:
        info["directors"] = [to_half(n) for n in meta["directors"]]
    if meta["writers"]:
        info["writers"] = [to_half(n) for n in meta["writers"]]
    if meta["tags"]:
        info["tags"] = localized_tags([to_half(t) for t in meta["tags"]], lang)
    return info


def metadata_props(meta):
    if meta is None:
        return {}
    out = {"epgstation.kind": meta["kind"], "epgstation.show": to_half(meta["show"])}
    for prop, key in (("episodeEnd", "episode_end"), ("version", "version"), ("edition", "edition")):
        if meta[key] not in (None, ""):
            out["epgstation." + prop] = to_half(str(meta[key]))
    return out


def extended_props(rec):
    """epgstation.extended.<key> per extended section, from rawExtended or (full-width API) rebuilt from extended."""
    raw = rec.get("rawExtended") or raw_extended(rec.get("extended"))
    out = {}
    for key, value in raw.items():
        key = to_half(key).strip().lstrip("\u25c7")
        if key:
            out["epgstation.extended." + key] = to_half(value).strip()
    return out


def recorded_item(rec, api, channels, lang="ja", strings=None, real_duration=None, on_error=None, meta=PARSE,
                  logos=None, blank=frozenset(), fallback_icon=""):
    """rec: a RecordedItem, full-width (isHalfWidth=false) or half-width; shown half-width either way.

    real_duration: seconds of the file that will play, if known. Ignored while recording.
    on_error(rec, exception): called when the metadata parser fails (the item then has no metadata).
    meta: epgmeta.metadata(rec) when the caller has it already (library cache), else it is parsed here.
    logos: channel ids with hasLogoData (None: unknown, no logo URL); blank: thumbnail ids known blank
    (thumbs.check); fallback_icon: the addon icon. See recording_art.
    """
    # Kodi returns "" for string IDs it has not loaded yet (addon updated without restart).
    strings = dict(DEFAULT_STRINGS, **{k: v for k, v in (strings or {}).items() if v})
    start, end = rec["startAt"], rec["endAt"]
    scheduled = (end - start) // 1000
    channel = channels.get(rec["channelId"], str(rec["channelId"]))
    if meta is PARSE:
        meta = epgmeta.metadata(rec, on_error=(lambda e: on_error(rec, e)) if on_error else None)
    name = to_half(rec["name"])
    label = name
    if rec.get("isRecording"):
        label = "[COLOR red]●[/COLOR] " + label
        real_duration = None

    from . import details  # here, details imports recorded, which imports items
    block = details.block_strings(strings)  # the 【録画情報】 block's labels: Kodi's language (strings.po)
    duration, extra_tech, partial = scheduled, [], False
    if real_duration is not None:
        duration = int(round(real_duration))
        if real_duration < PARTIAL_RATIO * scheduled:
            partial = True
            label += f" [COLOR yellow][{strings['partial']}][/COLOR]"
            extra_tech.append(f"{block['length']}: {fmt_duration(real_duration)} / {fmt_duration(scheduled)}")
    from . import recorded  # here, recorded imports items
    if recorded.is_protected(rec):  # gold [保護] suffix after [Partial]; the plot says it too (poster views)
        label += recorded.marker(rec, strings)
        extra_tech.append(recorded.protect_line(block))

    texts = [to_half(meta["plot"]).replace("\r\n", "\n")] if meta is not None else raw_plot_texts(rec)
    air_date = fmt_time(start, "%Y-%m-%d")
    info = metadata_info(meta, name, lang)
    genres = item_genres(rec, lang)
    info.update({
        # the 【録画情報】 block (broadcast, genres, video, audio, files, drops, ...) at the end
        "plot": build_plot(texts, details.detail_lines(rec, channel, genres, lang, strings, extra_tech)),
        "plotoutline": to_half(rec.get("description") or ""),
        "genres": genres,
        "studios": [channel],
        "duration": duration,
        "dateadded": fmt_time(start, "%Y-%m-%d %H:%M:%S"),
        "tagline": f"{channel}  {fmt_time(start)} - {fmt_time(end, '%H:%M')}",
    })
    if info["mediatype"] != "movie":
        # Not for movies: Kodi's year comes from premiered (an air date would overwrite the production year)
        # or, without one, from firstaired (the air year would show as the movie's year). The air date stays
        # in dateadded and the tagline.
        info.update(premiered=air_date, firstaired=air_date)

    art = recording_art(newest_thumb(rec), rec["channelId"], api, logos, blank, fallback_icon)

    props = {
        "epgstation.recordedId": str(rec["id"]),
        "epgstation.channel": channel,
        "epgstation.ruleId": str(rec.get("ruleId") or ""),
        "epgstation.isProtected": str(bool(rec.get("isProtected"))).lower(),
        "epgstation.isEncoding": str(bool(rec.get("isEncoding"))).lower(),
    }
    props.update(metadata_props(meta))
    props.update(extended_props(rec))
    if partial:
        props["epgstation.partial"] = "true"

    stream = None
    if rec.get("videoResolution") in RESOLUTIONS:
        w, h = RESOLUTIONS[rec["videoResolution"]]
        stream = {"codec": CODECS.get(rec.get("videoType"), rec.get("videoType") or ""), "width": w, "height": h,
                  "aspect": details.aspect(rec)}

    playable = bool(rec.get("videoFiles"))
    context = [(strings["choose"], plugin_url(action="choose", id=rec["id"]))] \
        if len(rec.get("videoFiles") or []) > 1 else []
    if playable and strings.get("stream"):  # 変換して再生 (the click checks /config)
        context.append((strings["stream"], plugin_url(action="stream", id=rec["id"])))
    if rec.get("isRecording"):  # 録画を停止 (stop, keep what was recorded)
        from . import reserves  # here: reserves imports items
        context.append(reserves.context_stop(rec["id"], strings))
    # 保護 / 保護を解除, 削除; ファイルを削除
    context += recorded.context_entries(rec, strings)
    if details.has_drop_problem(rec):  # ドロップログ, only when a count is > 0
        context.append((block["droplog"], plugin_url(action="droplog", id=rec["id"], log=rec["dropLogFile"]["id"])))
    return {
        "label": label,
        "path": plugin_url(action="play", id=rec["id"]),
        "folder": False,
        "playable": playable,
        "info": info,
        "art": art,
        "props": props,
        "stream": stream,
        "audio": details.audio_stream(rec),  # {"codec", "channels"} | None
        "context": context,
    }


# --- artwork -------------------------------------------------------------------------------------------------

# Kodi's built-in icons (Estuary's Textures.xbt has every one) for the menus and folders, by what they open.
MENU_ICONS = {
    "movies": "DefaultMovies.png", "tvshows": "DefaultTVShows.png", "recent": "DefaultRecentlyAddedEpisodes.png",
    "recording": "DefaultPVRRecordings.png", "reserves": "DefaultPVRTimers.png", "live": "DefaultPVRChannels.png",
    "search": "DefaultAddonsSearch.png", "browse": "DefaultFolder.png", "channels": "DefaultStudios.png",
    "genres": "DefaultGenre.png", "rules": "DefaultPVRTimerRules.png", "guide": "DefaultPVRGuide.png",
}


def newest_thumb(rec):
    """The recording's newest thumbnail id (max: a regenerated thumbnail has a higher id), or None."""
    thumbs = [t for t in rec.get("thumbnails") or [] if t is not None]
    return max(thumbs) if thumbs else None


def logo_art(channel_id, api, logos, fallback=""):
    """The channel's logo URL when it has one (channel id in logos: hasLogoData), else fallback. Never a URL that
    would 404 (logos None: unknown, no URL)."""
    if logos and channel_id in logos:
        return api.logo_url(channel_id) + api.kodi_headers()
    return fallback


def recording_art(thumb_id, channel_id, api, logos, blank=frozenset(), fallback=""):
    """Art of a recording (or a show folder): a thumbnail not known blank -> thumb, poster, fanart and landscape are
    the thumbnail, icon the channel logo (else fallback); no thumbnail or a blank one -> thumb and icon are the logo,
    else fallback, and no poster / fanart / landscape (a 64x36 logo stretched over the screen looks broken)."""
    icon = logo_art(channel_id, api, logos, fallback)
    if thumb_id is not None and thumb_id not in blank:
        thumb = api.thumbnail_url(thumb_id) + api.kodi_headers()
        art = {"thumb": thumb, "poster": thumb, "fanart": thumb, "landscape": thumb}
        if icon:
            art["icon"] = icon
        return art
    return {"thumb": icon, "icon": icon} if icon else {}


def icon_art(icon):
    """{"icon", "thumb"} of one image, or {} for none."""
    return {"icon": icon, "thumb": icon} if icon else {}


def folder(label, icon=None, **params):
    """A folder item; icon: a Kodi Default*.png or a logo URL, set as icon and thumb."""
    return {"label": label, "path": plugin_url(**params), "folder": True, "playable": False,
            "info": {"title": label}, "art": icon_art(icon), "props": {}, "stream": None, "context": []}


def root_menu(strings):
    out = [
        folder(strings["movies"], MENU_ICONS["movies"], action="movies"),
        folder(strings["tvshows"], MENU_ICONS["tvshows"], action="tvshows"),
        folder(strings["recent"], MENU_ICONS["recent"], action="list"),
        folder(strings["recording"], MENU_ICONS["recording"], action="recording"),
    ]
    if strings.get("reserves"):
        out.append(folder(strings["reserves"], MENU_ICONS["reserves"], action="reserves"))
    if strings.get("live"):
        out.append(folder(strings["live"], MENU_ICONS["live"], action="live"))
    out += [
        search_entry(strings["search"]),
        folder(strings["browse"], MENU_ICONS["browse"], action="browse"),
    ]
    return out


def search_entry(label):
    """検索: a click action, not a folder: views/recordings.search_ask asks for the keyword and opens
    the results at ?action=search&keyword=..., so Container.Refresh (after a delete) and Back re-list them without the
    keyboard. A folder could not do that: Kodi ignores Container.Update while it still handles that folder. Its own
    action (not "search"): Kodi passes a directory and a resolve request the same argv, so ?action=search with a
    handle is always an old folder link, ?action=search_ask with a handle always a play/queue request."""
    return dict(folder(label, MENU_ICONS["search"], action="search_ask"), folder=False)


def browse_menu(strings):
    return [
        folder(strings["channels"], MENU_ICONS["channels"], action="channels"),
        folder(strings["genres"], MENU_ICONS["genres"], action="genres"),
        folder(strings["rules"], MENU_ICONS["rules"], action="rules"),
    ]


def in_progress_matching(records, channel_id=None, genre=None, rule_id=None):
    """In-progress recordings that belong in a filtered list.

    /recorded leaves out recordings still in progress, but /recorded/options counts them, so a
    channel/genre/rule folder would otherwise open short (or empty) while one is recording.
    /recording has no filters, so match here: same fields as the /recorded filters.
    """
    if channel_id is None and genre is None and rule_id is None:
        return []
    out = []
    for r in records:
        if channel_id is not None and r.get("channelId") != int(channel_id):
            continue
        if rule_id is not None and r.get("ruleId") != int(rule_id):
            continue
        if genre is not None and int(genre) not in (r.get("genre1"), r.get("genre2"), r.get("genre3")):
            continue
        out.append(r)
    return out


def channel_folders(options, channels, logos=None, api=None):
    """One folder per channel with recordings; icon: its logo (logos: hasLogoData ids), else DefaultStudios.png."""
    out = []
    for c in options.get("channels", []):
        name = channels.get(c["channelId"], str(c["channelId"]))
        icon = logo_art(c["channelId"], api, logos, MENU_ICONS["channels"]) if api else MENU_ICONS["channels"]
        out.append(folder(f"{name} ({c['cnt']})", icon, action="list", channelId=c["channelId"]))
    return out


# --- genre and subgenre folders from library entries ("genres": genres.genre_pairs) -------------
# /recorded/options counts genre1 only, /recorded?genre=G matches genre1-3: counts come from the library instead.

def genre_counts(entries):
    """{genre: recordings with it in any of genre1-3} over library entries (finished and in-progress)."""
    out = {}
    for e in entries:
        for g in {p[0] for p in e.get("genres") or ()}:
            out[g] = out.get(g, 0) + 1
    return out


def subgenre_counts(entries, genre):
    """{subGenre (None: none): recordings with (genre, subGenre) in any slot} over library entries."""
    out = {}
    for e in entries:
        for s in {p[1] for p in e.get("genres") or () if p[0] == genre}:
            out[s] = out.get(s, 0) + 1
    return out


def _genre_label(genre, lang):
    return (GENRE_JA if lang == "ja" else GENRE_EN).get(genre, str(genre))


def genre_folders(entries, lang="ja"):
    """ジャンル別: one folder per genre with recordings, genre-id order, "映画 (59)" -> its subgenre folders
    (action=genre); a genre whose recordings all share one subgenre opens its list directly (action=list). Slots
    without a subGenre do not count as a subgenre: they are only in the genre's list (directly or via すべて)."""
    out = []
    counts = genre_counts(entries)
    for g in sorted(counts):
        single = len([s for s in subgenre_counts(entries, g) if s is not None]) <= 1
        out.append(folder(f"{_genre_label(g, lang)} ({counts[g]})", MENU_ICONS["genres"],
                          action="list" if single else "genre", genre=g))
    return out


def subgenre_folders(genre, entries, lang, all_label="All ({0})"):
    """A genre's folders: "すべて (N)" (the paged API list) first, then each subgenre with recordings in code order
    (その他 = 15 last), "洋画 (46)" -> action=list&genre=G&subGenre=S; a code without a name "#S". all_label: the
    localized "All ({0})"."""
    total = genre_counts(entries).get(genre, 0)
    out = [folder(all_label.format(total), MENU_ICONS["genres"], action="list", genre=genre)]
    for s, n in sorted((s, n) for s, n in subgenre_counts(entries, genre).items() if s is not None):
        name = subgenre_name(genre, s, lang) or f"#{s}"
        out.append(folder(f"{name} ({n})", MENU_ICONS["genres"], action="list", genre=genre, subGenre=s))
    return out


def subgenre_entries(entries, genre, sub_genre):
    """Ids of the entries with (genre, subGenre) in some slot, newest first."""
    return [e["id"] for e in _newest_first(entries) if [genre, sub_genre] in (e.get("genres") or ())]


def rule_name(rule):
    """A rule's display name: reserveOption.name, else the search keyword, else the save directory, else "Rule N"
    (rule folders)."""
    return ((rule.get("reserveOption") or {}).get("name") or (rule.get("searchOption") or {}).get("keyword")
            or (rule.get("saveOption") or {}).get("directory") or f"Rule {rule['id']}")


def rule_folders(rules, counts):
    """One folder per rule that has recordings, "name (N)"; counts: {ruleId: N} (rule_counts).

    Name: rule_name."""
    out = []
    for r in rules.get("rules", []):
        n = counts.get(r["id"], 0)
        if not n:
            continue
        name = rule_name(r)
        # name: for the window header of the folder (setPluginCategory) without another /rules request
        out.append(folder(f"{name} ({n})", MENU_ICONS["rules"], action="list", ruleId=r["id"], name=name))
    return out


def pick_video(video_files, prefer="encoded"):
    """Choose the default file to play: preferred type first, then the largest of that type."""
    if not video_files:
        return None
    preferred = [v for v in video_files if v["type"] == prefer]
    pool = preferred or video_files
    return max(pool, key=lambda v: v.get("size", 0))


def default_prefer(rec, setting_prefer):
    """While recording only the growing TS is playable."""
    return "ts" if rec.get("isRecording") else setting_prefer


def video_choices(video_files, durations):
    """Labels for the file chooser, e.g. "TS (ts) 512.0 MB  5:05" (length only when known)."""
    out = []
    for v in video_files:
        label = f"{v['name']} ({v['type']}) {fmt_size(v.get('size', 0))}"
        if durations.get(v["id"]) is not None:
            label += "  " + fmt_duration(durations[v["id"]])
        out.append(label)
    return out


# --- movies, TV shows, rule counts ------------------------------------------------------------------

def show_key(meta, rec):
    """TV-show grouping key: the parsed show, loosely compared (epgmeta.key: NFKC, lower case, no spaces or
    punctuation); the half-width name when the parser found no show. Never the rule name."""
    if meta and meta.get("show"):
        return epgmeta.key(to_half(meta["show"]))
    return to_half(rec.get("name") or "")


def rule_counts(entries):
    """{ruleId: recordings} over library entries (finished and in-progress)."""
    out = {}
    for e in entries:
        if e.get("ruleId") is not None:
            out[e["ruleId"]] = out.get(e["ruleId"], 0) + 1
    return out


def group_shows(entries, blank=frozenset()):
    """TV shows from library entries (movies left out), the show with the newest episode first.

    Each: {"key", "title" (most frequent spelling, ties: newest), "ids" (newest first), "count",
    "newest", "oldest", "thumb" (newest entry with a thumbnail not in blank, or None),
    "channels" (ids, newest first)}.
    """
    shows = {}
    for e in _newest_first(entries):
        if e["kind"] == "movie":
            continue
        s = shows.get(e["key"])
        if s is None:
            s = shows[e["key"]] = {"key": e["key"], "ids": [], "newest": e, "thumb": None, "channels": [],
                                   "_spell": {}}
        s["ids"].append(e["id"])
        s["oldest"] = e
        if s["thumb"] is None and e.get("thumb") is not None and e["thumb"] not in blank:
            s["thumb"] = e
        if e["channelId"] not in s["channels"]:
            s["channels"].append(e["channelId"])
        spell = e["show"] or e["name"]
        s["_spell"][spell] = s["_spell"].get(spell, 0) + 1
    out = []
    for s in shows.values():  # insertion order: newest episode first
        spell = s.pop("_spell")
        s["title"] = max(spell, key=lambda t: spell[t])  # max keeps the first (newest) of equal counts
        s["count"] = len(s["ids"])
        out.append(s)
    return out


def show_thumb_candidates(entries, blank=frozenset()):
    """the thumbnail id each show would use with these blank ids (what to check before group_shows)."""
    return [s["thumb"]["thumb"] for s in group_shows(entries, blank) if s["thumb"] is not None]


def tvshow_folder(show, records, api, channels, lang="ja", unknown="", logos=None, fallback_icon=""):
    """Folder item (mediatype tvshow) of a group_shows() show; records: {id: RecordedItem} for its newest episode.

    A show without a name (blank EPG title) is labelled by the channel of its newest episode, else `unknown`
    (localized "Unknown"; Kodi returns "" for strings it has not loaded yet, then the English word).
    recording_art of the show's thumbnail (group_shows) and the channel of that episode (no
    thumbnail: of the newest episode); logos: hasLogoData channel ids; fallback_icon: the addon icon."""
    title = show_title(show, channels, unknown)
    newest = records.get(show["newest"]["id"]) or {}
    d = folder(f"{title} ({show['count']})", action="show", key=show["key"])
    d["info"] = {
        "mediatype": "tvshow", "title": title, "tvshowtitle": title,
        "plot": to_half(newest.get("description") or ""),
        "genres": item_genres(newest, lang) if newest else [],
        "studios": [channels.get(c, str(c)) for c in show["channels"]],
        "premiered": fmt_time(show["oldest"]["startAt"], "%Y-%m-%d"),
        "dateadded": fmt_time(show["newest"]["startAt"], "%Y-%m-%d %H:%M:%S"),
    }
    d["props"] = {"TotalEpisodes": str(show["count"])}
    source = show["thumb"] or show["newest"]
    d["art"] = recording_art(show["thumb"]["thumb"] if show["thumb"] else None, source["channelId"], api, logos,
                             frozenset(), fallback_icon)
    return d


def movie_label(item, meta, lang="ja"):
    """Movies list label: the clean title plus the version ("スーパーマン [字幕版]", en "[Subtitled]") in place of
    the broadcast name, so title sort puts all versions and repeats of a film together. Keeps the recording
    and partial markers. meta: epgmeta.metadata() or a library entry (uses "version")."""
    info = item["info"]
    title = info.get("title") or item["label"]
    version = to_half((meta or {}).get("version") or "")
    if version:
        title += f" [{tag_en(version) if lang == 'en' else version}]"
    name = info.get("originaltitle")
    if name and name in item["label"]:
        return item["label"].replace(name, title, 1)
    return title


def _newest_first(entries):
    return sorted(entries, key=lambda e: (e["startAt"], e["id"]), reverse=True)


def movie_entries(entries):
    """The movie entries of a library snapshot, newest first."""
    return [e for e in _newest_first(entries) if e["kind"] == "movie"]


def label_movies(dicts, entries, lang="ja"):
    """Copies of the movie item dicts (same order as entries) with movie_label() labels."""
    return [dict(d, label=movie_label(d, e, lang)) for d, e in zip(dicts, entries)]


def find_show(entries, key):
    """The group_shows() show with this key, or None."""
    return next((s for s in group_shows(entries) if s["key"] == key), None)


def show_title(show, channels, unknown=""):
    """Display title of a group_shows() show: its name, else the channel of its newest episode, else `unknown`
    (localized "Unknown"; Kodi returns "" for strings it has not loaded yet, then the English word)."""
    return (to_half(show["title"]) or channels.get(show["newest"]["channelId"])
            or unknown or DEFAULT_STRINGS["unknown"])


_FLAGS_ONLY = re.compile(r"^(?:\[[^\]]{1,6}\]\s*)*$")
_LEAD_FLAGS = r"^(?:\[[^\]]{1,6}\]\s*)*"


def episode_label(item, meta, lang="ja"):
    """Label of an episode inside a show folder: "SxE subtitle" ("6x8 傑作選 #9"; "SxE YYYY-MM-DD" without a
    subtitle), or without an episode number "YYYY-MM-DD subtitle" / "YYYY-MM-DD rest of the name" (the name
    without the show in front; nothing when only flags remain). The version ("[字幕版]") and the recording /
    partial markers of item["label"] are kept. meta: epgmeta.metadata() or None."""
    info = item["info"]
    name = info.get("originaltitle") or info.get("title") or ""
    date = (info.get("dateadded") or "")[:10]
    sub = to_half(meta["subtitle"]) if meta else ""
    if meta and meta["episode"] is not None:
        number = f"{meta['season'] or 1}x{meta['episode']}"
        if meta.get("episode_end") not in (None, "", meta["episode"]):
            number += f"-{meta['episode_end']}"
        text = f"{number} {sub or date}"
    else:
        rest = sub
        if not rest:
            rest = name
            show = to_half(meta["show"]) if meta else ""
            m = re.match(_LEAD_FLAGS + re.escape(show), name) if show else None
            if m:
                rest = name[m.end():].strip(" \u3000・:/")
            if _FLAGS_ONLY.match(rest):
                rest = ""
        text = f"{date} {rest}".strip()
    version = to_half((meta or {}).get("version") or "")
    if version:
        text += f" [{tag_en(version) if lang == 'en' else version}]"
    if name and name in item["label"]:
        return item["label"].replace(name, text, 1)
    return text


def label_episodes(dicts, metas, lang="ja"):
    """Copies of the episode item dicts with episode_label() labels; metas: metadata per dict (or None)."""
    return [dict(d, label=episode_label(d, m, lang)) for d, m in zip(dicts, metas)]


def list_category(params, channels, lang, strings):
    """Window header (setPluginCategory) of a recordings list from its folder params: the channel / genre /
    rule name of a filtered list, "Search" for a keyword search, else "Recent recordings"."""
    if params.get("keyword"):
        return strings["search"]
    if params.get("channelId"):
        cid = params["channelId"]
        try:
            return channels.get(int(cid), cid)
        except ValueError:
            return cid
    if params.get("genre"):
        try:
            genre = int(params["genre"])
        except ValueError:
            return params["genre"]
        if genre not in GENRE_JA:
            return params["genre"]
        if params.get("subGenre"):  # "映画 / 洋画"
            try:
                sub = int(params["subGenre"])
            except ValueError:
                sub = None
            if subgenre_name(genre, sub, lang) is None:
                return f"{_genre_label(genre, lang)} / #{params['subGenre']}"
            return genre_name(genre, sub, lang)
        return _genre_label(genre, lang)
    if params.get("ruleId"):
        return params.get("name") or strings["rules"]
    return strings["recent"]
