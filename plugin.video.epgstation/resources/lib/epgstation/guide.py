"""Program guide (backlog 013): what's on now or at an hour, programs by genre, a channel's schedule by day.

No Kodi imports here: everything is unit-testable (like items.py and live.py). plugin.py turns the dicts into
ListItems. It builds on live.py (watchable, time_range, progress, the live_play path): remove the guide before
removing live TV.

The feature is meant to be removable. Hooks outside this module (code hooks are marked "program guide (013)"):
- plugin.py: do_live (guide.menu folders, guide.with_schedule_context), do_guide_now, do_guide_genres,
  do_guide_genre, do_guide_channel, do_guide_info, _guide_strings, _guide_cache, _now_ms, _guide_channel,
  SORT_GUIDE, the "update" context entries of _listitem, NON_DIRECTORY_ACTIONS, BadParamError, _int_param, the guide
  import
- api.py: schedule_detail(), ApiError.status
- strings 30020-30029 (both .po)
- tests: test_guide.py; the guide tests in test_plugin.py; test_smoke_tool.py guide tests; kodi_stubs
  (xbmcplugin.py SORT_METHOD_TITLE / GENRE / STUDIO, xbmcgui.py Dialog.info / info_shown), conftest.py resets of
  info_shown and handler.now; fixture tests/fixtures/live/guide.json
- tools: fake_epgstation.py (guide.json merge, Handler.now, per-day /schedules/{id}, /schedules/detail),
  snapshot_api.py snapshot_guide, kodi_smoke.py check_guide / check_guide_gui / guide checks in check_live
Record from the guide (014) hooks into program_item / program_items / mark_channels here; its removal list is in
reserves.py.
"""
import bisect
import json
import time

from . import epgmeta, live, reserves, store  # record from the guide (014): reserves
from .genres import GENRE_EN, GENRE_JA, item_genres
from .items import (
    MENU_ICONS,
    build_plot,
    extended_props,
    fmt_time,
    folder,
    logo_art,
    metadata_info,
    plugin_url,
    raw_plot_texts,
    to_half,
)

TTL = 600  # seconds the 24 h all-channel schedule is reused (EPGStation updates the EPG every 10 min)
DEADLINE = 15.0  # seconds for that request
HOUR_MS = 3600 * 1000
WINDOW_MS = 24 * HOUR_MS  # 放送中 and ジャンル別 show the next 24 h
DAYS = 8  # a channel's schedule: today + 7 days
SLOTS = 24  # 放送中: now + the next 24 full hours
CACHE_FILE = "guide-schedules.json"
CACHE_VERSION = 1
ON_AIR = "[COLOR yellow]▶[/COLOR] "  # plays; the red ● stays the recording marker

DEFAULT_STRINGS = {"whats_on": "What's on", "by_genre": "By genre", "schedule": "Schedule", "now": "Now",
                   "today": "Today", "tomorrow": "Tomorrow", "weekdays": "Sun,Mon,Tue,Wed,Thu,Fri,Sat",
                   "channel_schedule": "{0} schedule", "gone": "Program no longer in the guide",
                   "left": "{0} min left"}


def _strings(strings):
    # Kodi returns "" for string IDs it has not loaded yet (addon updated without restart).
    return dict(DEFAULT_STRINGS, **{k: v for k, v in (strings or {}).items() if v})


# --- local days and hours ----------------------------------------------------------------------------------------

def _valid_schedules(data):
    """A /schedules response shape: [{channel: {id}, programs: [{id, startAt, endAt}]}] (cache file check)."""
    if not isinstance(data, list):
        return False
    for sch in data:
        if not (isinstance(sch, dict) and isinstance(sch.get("channel"), dict) and "id" in sch["channel"]
                and isinstance(sch.get("programs"), list)):
            return False
        for p in sch["programs"]:
            if not (isinstance(p, dict) and "id" in p and isinstance(p.get("startAt"), int)
                    and isinstance(p.get("endAt"), int)):
                return False
    return True


def _midnight_of(ms, days=0):
    lt = time.localtime(ms / 1000)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + days, 0, 0, 0, 0, 0, -1)) * 1000)


def local_midnight(now_ms):
    """Start of the local calendar day of now_ms (ms)."""
    return _midnight_of(now_ms)


def day_start(now_ms, index):
    """Local midnight `index` days after today's (ms)."""
    return _midnight_of(now_ms, index)


def day_index(ms, now_ms):
    """Local calendar days from today to the day of ms (0 today, 1 tomorrow, -1 yesterday)."""
    starts = [_midnight_of(now_ms, k) for k in range(-1, DAYS + 2)]
    return bisect.bisect_right(starts, ms) - 2


def date_text(ms, strings=None):
    """"10/12(月)" for non-ASCII weekday names (Japanese), else "Mon 10/12"."""
    s = _strings(strings)
    names = s["weekdays"].split(",")
    lt = time.localtime(ms / 1000)
    wd = names[(lt.tm_wday + 1) % 7] if len(names) == 7 else ""  # tm_wday: Monday = 0; the list starts Sunday
    if wd.isascii():
        return f"{wd} {lt.tm_mon}/{lt.tm_mday}".strip()
    return f"{lt.tm_mon}/{lt.tm_mday}({wd})"


def day_label(day_ms, now_ms, strings=None):
    """"今日 10/11(日)", "明日 10/12(月)", then "10/13(火)" (en: "Today Sun 10/11", "Tue 10/13")."""
    s = _strings(strings)
    idx = day_index(day_ms, now_ms)
    prefix = {0: s["today"], 1: s["tomorrow"]}.get(idx)
    return f"{prefix} {date_text(day_ms, s)}" if prefix else date_text(day_ms, s)


def _hhmm(ms):
    return fmt_time(ms, "%H:%M")


def time_slots(now_ms):
    """[now, the next SLOTS full hours] (ms)."""
    # local-time arithmetic (mktime normalizes hour 24+), so zones such as +5:30 or +12:45 get local full hours
    lt = time.localtime(now_ms / 1000)
    return [int(now_ms)] + [int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour + 1 + k, 0, 0, 0, 0, -1))
                                * 1000) for k in range(SLOTS)]


def _hour_label(t, now_ms, s):
    hour = f"{time.localtime(t / 1000).tm_hour}:00"
    return f"{s['tomorrow']} {hour}" if day_index(t, now_ms) == 1 else hour


def slot_folders(now_ms, strings=None):
    """放送中: "現在 (15:28)", then "16:00" … "明日 15:00"."""
    s = _strings(strings)
    slots = time_slots(now_ms)
    icon = MENU_ICONS["guide"]
    out = [folder(f"{s['now']} ({_hhmm(now_ms)})", icon, action="guide_now", t="now")]
    out += [folder(_hour_label(t, now_ms, s), icon, action="guide_now", t=t) for t in slots[1:]]
    return out


def slot_header(t, now_ms, strings=None):
    """Window header of a slot: "放送中 21:00", "放送中 明日 1:00", now: "放送中 15:28"."""
    s = _strings(strings)
    when = _hhmm(t) if t == now_ms else _hour_label(t, now_ms, s)
    return f"{s['whats_on']} {when}"


def menu(strings=None):
    """The guide folders at the top of ライブTV."""
    s = _strings(strings)
    return [folder(s["whats_on"], MENU_ICONS["guide"], action="guide_now"),
            folder(s["by_genre"], MENU_ICONS["genres"], action="guide_genres")]


# --- selections --------------------------------------------------------------------------------------------------

def _watchable_schedules(schedules):
    return [sch for sch in schedules if live.watchable(sch["channel"])]


def on_air_at(schedules, t_ms):
    """[(channel, program)] airing at t (startAt <= t < endAt), one per watchable channel, in server order."""
    out = []
    for sch in _watchable_schedules(schedules):
        p = next((p for p in sch.get("programs") or [] if p["startAt"] <= t_ms < p["endAt"]), None)
        if p is not None:
            out.append((sch["channel"], p))
    return out


def _in_window(p, now_ms):
    return p["endAt"] > now_ms and p["startAt"] < now_ms + WINDOW_MS


def _genres_of(p):
    return {p.get(k) for k in ("genre1", "genre2", "genre3") if p.get(k) in GENRE_JA}


def _window_programs(schedules, now_ms):
    """[(order, channel, program)] of the next 24 h on watchable channels, each program once."""
    seen, out = set(), []
    for order, sch in enumerate(_watchable_schedules(schedules)):
        for p in sch.get("programs") or []:
            if p["id"] not in seen and _in_window(p, now_ms):
                seen.add(p["id"])
                out.append((order, sch["channel"], p))
    return out


def genre_counts(schedules, now_ms):
    """{genre: programs of the next 24 h with that genre in genre1-3} (watchable channels; no genre: none)."""
    counts = {}
    for _, _, p in _window_programs(schedules, now_ms):
        for g in _genres_of(p):
            counts[g] = counts.get(g, 0) + 1
    return counts


def genre_name(genre, lang="ja"):
    return (GENRE_JA if lang == "ja" else GENRE_EN).get(genre, str(genre))


def genre_folders(counts, lang="ja"):
    """"映画 (112)" per genre with programs, in genre-id order (web UI order)."""
    return [folder(f"{genre_name(g, lang)} ({counts[g]})", MENU_ICONS["genres"], action="guide_genre", genre=g)
            for g in sorted(counts) if counts[g]]


def genre_programs(schedules, genre, now_ms):
    """[(channel, program)] of the next 24 h in this genre, by (startAt, server order)."""
    rows = [(p["startAt"], order, ch, p) for order, ch, p in _window_programs(schedules, now_ms)
            if genre in _genres_of(p)]
    rows.sort(key=lambda r: (r[0], r[1]))
    return [(ch, p) for _, _, ch, p in rows]


def day_buckets(channel_days, now_ms):
    """{day index: [programs]} of a /schedules/{id} response (one Schedule per day; a program crossing midnight is
    in two of them). A program belongs to the day it starts in; today also holds the one on air (it may have
    started yesterday); ended programs and days beyond today + DAYS-1 are left out."""
    seen, out = set(), {}
    for sch in channel_days or []:
        for p in sch.get("programs") or []:
            if p["id"] in seen or p["endAt"] <= now_ms:
                continue
            seen.add(p["id"])
            idx = max(0, day_index(p["startAt"], now_ms))
            if idx < DAYS:
                out.setdefault(idx, []).append(p)
    for progs in out.values():
        progs.sort(key=lambda p: p["startAt"])
    return out


def day_programs(channel_days, day_ms, now_ms):
    """Programs of the local day starting at day_ms (absolute, from the day folder's URL): those starting that day
    and not ended; the day that is today also holds the one on air that started earlier. Correct also when the
    folder was listed before midnight and opened after it."""
    day_ms, today = local_midnight(day_ms), local_midnight(now_ms)
    seen, out = set(), []
    for sch in channel_days or []:
        for p in sch.get("programs") or []:
            if p["id"] in seen or p["endAt"] <= now_ms:
                continue
            seen.add(p["id"])
            start_day = local_midnight(p["startAt"])
            if start_day == day_ms or (day_ms == today and start_day < today):
                out.append(p)
    out.sort(key=lambda p: p["startAt"])
    return out


def _channel_params(channel):
    params = {"channelId": channel["id"], "name": to_half(channel.get("name") or str(channel["id"]))}
    if channel.get("hasLogoData"):
        params["logo"] = 1
    return params


def day_folders(channel, buckets, now_ms, strings=None, api=None):
    """"今日 10/11(日) (18)" … per day with programs (the channel's 番組表). artwork (003): icon = the channel's logo
    when it has one (hasLogoData, and api given), else DefaultPVRGuide.png."""
    s = _strings(strings)
    icon = MENU_ICONS["guide"]
    if api is not None and channel.get("hasLogoData"):
        icon = logo_art(channel["id"], api, {channel["id"]}, icon)
    return [folder(f"{day_label(day_start(now_ms, k), now_ms, s)} ({len(buckets[k])})", icon, action="guide_channel",
                   day=day_start(now_ms, k), **_channel_params(channel))  # the day's local midnight (ms)
            for k in sorted(buckets)]


# --- items -------------------------------------------------------------------------------------------------------

def schedule_context(channel, strings=None):
    """Context entry "番組表": Container.Update to the channel's day folders."""
    s = _strings(strings)
    return (s["schedule"], plugin_url(action="guide_channel", **_channel_params(channel)), "update")


def with_schedule_context(dicts, strings=None):
    """Adds the 番組表 entry to 012 channel items (live.channel_items)."""
    for d in dicts:
        p = d["props"]
        channel = {"id": p["epgstation.channelId"], "name": p["epgstation.channel"], "hasLogoData": bool(d["art"])}
        d["context"] = list(d["context"]) + [schedule_context(channel, strings)]
    return dicts


def time_line(program, now_ms, strings=None):
    """On air: "15:18-15:48 (残り20分)"; else "10/12(月) 21:00-22:54"."""
    s = _strings(strings)
    if program["startAt"] <= now_ms < program["endAt"]:
        return live.time_range(program, now_ms, s)
    return f"{date_text(program['startAt'], s)} {_hhmm(program['startAt'])}-{_hhmm(program['endAt'])}"


def program_info(channel_name, program, now_ms, lang="ja", strings=None, on_error=None, reserve_line=""):
    """InfoTagVideo fields of a guide program (mediatype video, like the live channels). reserve_line (014): the
    reservation line(s) right under the time line."""
    name = to_half(program["name"])
    meta = epgmeta.metadata(program, on_error=(lambda e: on_error(program, e)) if on_error else None)
    texts = [to_half(meta["plot"]).replace("\r\n", "\n")] if meta is not None else raw_plot_texts(program)
    info = {k: v for k, v in metadata_info(meta, name, lang).items() if k in ("cast", "directors", "writers", "tags")}
    line = time_line(program, now_ms, strings)
    air_date = fmt_time(program["startAt"], "%Y-%m-%d")
    info.update({
        "mediatype": "video",
        "title": name,
        "genres": item_genres(program, lang),
        "studios": [channel_name],
        "duration": (program["endAt"] - program["startAt"]) // 1000,
        "firstaired": air_date,
        "premiered": air_date,
        "tagline": f"{channel_name}  {line}",
        "plotoutline": line,
        "plot": build_plot([line + (f"\n{reserve_line}" if reserve_line else "")] + texts),
    })
    return info


def program_item(channel, program, api, now_ms, lang="ja", strings=None, style="slot", on_error=None,
                 context=True, index=None, records=()):
    """Item dict of a guide program. style: "slot" ("channel  name"), "genre" ("HH:MM channel  name", tomorrow
    "明日 HH:MM …") or "day" ("HH:MM  name"). On air: ▶, plays the channel (live_play); else guide_info (Kodi's
    information dialog). context=False: no 番組表 entry (inside the channel's own schedule).
    Record from the guide (014): index = reserves.lists_index of the listing's GET /reserves/lists (None: it failed,
    no marker and no reservation entry), records = /recording; the marker goes after ▶, the reservation entry
    (録画 or 007's action) after 番組表."""
    s = _strings(strings)
    ch_name = to_half(channel.get("name") or str(channel["id"]))
    name = to_half(program["name"])
    on_air = program["startAt"] <= now_ms < program["endAt"]
    start = _hhmm(program["startAt"])
    if style == "genre":
        day = f"{s['tomorrow']} " if day_index(program["startAt"], now_ms) == 1 else ""
        label = f"{day}{start} {ch_name}  {name}"
    elif style == "day":
        label = f"{start}  {name}"
    else:
        label = f"{ch_name}  {name}"
    mark, line, entry = reserves.guide_mark(dict(program, channelId=channel["id"]), ch_name, index, records, now_ms,
                                            strings)  # record from the guide (014)
    info = program_info(ch_name, program, now_ms, lang, s, on_error, line)
    art = {}
    if channel.get("hasLogoData"):
        logo = api.logo_url(channel["id"]) + api.kodi_headers()
        art = {"icon": logo, "thumb": logo}
    props = {
        "epgstation.programId": str(program["id"]),
        "epgstation.channelId": str(channel["id"]),
        "epgstation.channel": ch_name,
        "epgstation.channelType": channel.get("channelType") or "",
        "epgstation.isFree": str(bool(program.get("isFree"))).lower(),
        "epgstation.startAt": fmt_time(program["startAt"]),
        "epgstation.endAt": fmt_time(program["endAt"]),
        "epgstation.onAir": str(on_air).lower(),
    }
    if on_air:
        props["epgstation.progress"] = str(live.progress(program, now_ms))
    props.update(extended_props(program))
    d = {
        "label": (ON_AIR if on_air else "") + mark + label,
        "folder": False,
        "info": info,
        "art": art,
        "props": props,
        "stream": None,
        "context": ([schedule_context(channel, s)] if context else []) + ([entry] if entry else []),
    }
    if on_air:
        # the existing play path (one stream, no HEAD, start watch); playcount 0 and own empty stream details keep
        # Kodi's DB state of the live_play URL off the item (012's watched-mark fix)
        info["playcount"] = 0
        d.update(path=plugin_url(action="live_play", channelId=channel["id"], name=ch_name), playable=True,
                 stream={"codec": "", "width": 0, "height": 0, "duration": info["duration"]})
    else:
        d.update(path=plugin_url(action="guide_info", programId=program["id"], **_channel_params(channel)),
                 playable=False)
    return d


def program_items(rows, api, now_ms, lang="ja", strings=None, style="slot", on_error=None, context=True,
                  index=None, records=()):
    return [program_item(ch, p, api, now_ms, lang, strings, style, on_error, context, index, records)
            for ch, p in rows]


def mark_channels(dicts, programs, index, records, now_ms, strings=None):
    """Record from the guide (014): marker and reservation entry (after 番組表) on 012 channel items
    (live.channel_items) for their current program. programs: {programId: program}; index None: unchanged."""
    if index is None:
        return dicts
    for d in dicts:
        p = d["props"]
        program = programs.get(int(p["epgstation.programId"]))
        if program is None:
            continue
        program = dict(program, channelId=program.get("channelId", int(p["epgstation.channelId"])))
        mark, _, entry = reserves.guide_mark(program, p["epgstation.channel"], index, records, now_ms, strings)
        d["label"] = mark + d["label"]
        if entry:
            d["context"] = list(d["context"]) + [entry]
    return dicts


# --- cache -------------------------------------------------------------------------------------------------------

class GuideCache:
    """The 24 h all-channel /schedules response in the addon profile, reused for TTL seconds.

    The file (guide-schedules.json) has {"v", "server", "types", "t" (fetch ms), "end" (endAt asked),
    "schedules"}. It counts only for the same format, server and types, within TTL of the fetch, while it
    still covers now + 24 h. A missing or corrupt file means a fetch; a file that cannot be written is
    ignored; a failed fetch raises the ApiError (no stale fallback). On-air status is never cached: the
    items compute it from the clock."""

    def __init__(self, directory, server_url, now=time.time):
        self.path = f"{directory.rstrip('/')}/{CACHE_FILE}"
        self.server = server_url
        self.now = now

    def _read(self, types, now_ms):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        try:
            ok = (data["v"] == CACHE_VERSION and data["server"] == self.server and data["types"] == list(types)
                  and 0 <= now_ms - data["t"] < TTL * 1000 and data["end"] >= now_ms + WINDOW_MS
                  and _valid_schedules(data["schedules"]))
        except (KeyError, TypeError):
            return None
        return data["schedules"] if ok else None

    def schedules(self, api, types):
        now_ms = int(self.now() * 1000)
        cached = self._read(types, now_ms)
        if cached is not None:
            return cached
        end = now_ms + WINDOW_MS + TTL * 1000
        data = api.schedules(now_ms, end, types, timeout=DEADLINE)
        store.atomic_write_json(self.path, {"v": CACHE_VERSION, "server": self.server, "types": list(types),
                                            "t": now_ms, "end": end, "schedules": data})
        return data
