"""Live TV (backlog 012): channel lists with now/next and the live-stream helpers.

No Kodi imports here: everything is unit-testable (like items.py). plugin.py turns the dicts into ListItems.

The feature is meant to be removable. Hooks outside this module (code hooks are marked "live TV (012)"):
- items.py: root_menu's "live" folder (only when strings has "live")
- plugin.py: the "live" entry of do_root; PLAY_ACTIONS "live_play"; do_live, do_live_play, _watch_live_start,
  _report_live_failure, _LivePlayer, _playing_file, SORT_LIVE, _live_strings, _live_mode, _live_error,
  _add(cache=), the "mimetype" key of _listitem
- api.py: schedules(), channel_schedule(), config(), live_url(), live_probe()
- settings.xml: category "live" (setting live_mode); strings 30015-30019, 30101-30103, 30230-30235 (both .po)
- docs/api/README.md: the /config live keys (openapi.json vs real 2.10.0)
- tests: test_live.py; the live tests in test_plugin.py (incl. live_play in the play-action error test);
  test_smoke_tool.py test_expected_live_and_item_failures and test_check_live_single_type; kodi_stubs
  (xbmc.py Player callbacks / isPlaying / getPlayingFile / players / player_file, Monitor / waited /
  wait_hook, getCondVisibility / visible; xbmcgui.py setMimeType / setContentLookup; xbmcplugin.py
  state.cache), conftest.py resets of those knobs and of handler.live_error / config_patch; fixtures
  tests/fixtures/live/ and live_errors.json
- tools: fake_epgstation.py live routes (LIVE_FIXTURES, live_error, config_patch), snapshot_api.py --live,
  Makefile snapshot-live, kodi_smoke.py ROOT_ACTIONS "live" and the live checks (check_live, check_live_gui,
  live_item_failures, expected_live, streams_failures)
"""
import math
import re
import urllib.parse

from . import epgmeta
from .genres import item_genres
from .items import (
    MENU_ICONS,
    build_plot,
    extended_props,
    fmt_time,
    folder,
    metadata_info,
    plugin_url,
    raw_plot_texts,
    to_half,
)

# ARIB service_type of TV services (user decision: TV only, no radio or data): digital TV, temporary video,
# promotional video, 4K. A channel without a type is shown.
LIVE_SERVICE_TYPES = {0x01, 0xA1, 0xA5, 0xAD}
TYPE_ORDER = ("GR", "BS", "CS", "SKY")
WINDOW_MS = 6 * 3600 * 1000  # now/next window of the channel list (/schedules startAt..endAt)
LIVE_START_TIMEOUT = 20  # backstop: seconds the plugin watches Kodi's player after resolving a live stream
INFO_TIMEOUT = 3  # seconds for the program info of the play item: never hold up playback

DEFAULT_STRINGS = {"GR": "Terrestrial", "SKY": "SKY", "next": "Next", "left": "{0} min left",
                   "no_tuner": "No free tuner", "failed": "Could not start live stream"}


def _strings(strings):
    # Kodi returns "" for string IDs it has not loaded yet (addon updated without restart).
    return dict(DEFAULT_STRINGS, **{k: v for k, v in (strings or {}).items() if v})


def watchable(channel):
    """True for TV services (and channels without a service type); False for radio, data and download services."""
    return channel.get("type") is None or channel["type"] in LIVE_SERVICE_TYPES


def gr_subchannel_ids(channels):
    """Ids of terrestrial sub-channels (025): every service of a GR broadcaster shares its networkId and the main one
    has the lowest serviceId; the others (NHK総合2, MBS2, ...) air programmes only now and then. Only watchable
    services count. Works on /channels and /schedules channel items. BS/CS are left alone (one network, many
    broadcasters)."""
    main = {}
    gr = [c for c in channels if c.get("channelType") == "GR" and watchable(c) and c.get("networkId") is not None]
    for c in gr:
        if c["networkId"] not in main or c["serviceId"] < main[c["networkId"]]["serviceId"]:
            main[c["networkId"]] = c
    return {c["id"] for c in gr if main[c["networkId"]]["id"] != c["id"]}


def drop_subchannels(schedules):
    """A /schedules response without the GR sub-channels (025)."""
    sub = gr_subchannel_ids([sch["channel"] for sch in schedules])
    return [sch for sch in schedules if sch["channel"]["id"] not in sub]


def live_types(channels):
    """Broadcast types that have watchable channels, in TYPE_ORDER (from /channels)."""
    have = {c.get("channelType") for c in channels if watchable(c)}
    return [t for t in TYPE_ORDER if t in have]


def type_label(channel_type, strings=None):
    """地上波 / Terrestrial for GR, the localized SKY string, else the type itself (BS, CS)."""
    return _strings(strings).get(channel_type, channel_type)


def type_folders(types, strings=None):
    return [folder(type_label(t, strings), MENU_ICONS["live"], action="live", type=t) for t in types]


def now_next(programs, now_ms):
    """(current, next) of a channel's programs (sorted by startAt, as /schedules returns them).

    current: startAt <= now < endAt; next: the first program starting at or after the current one ends.
    (None, None) without a current program."""
    cur = next((p for p in programs if p["startAt"] <= now_ms < p["endAt"]), None)
    if cur is None:
        return None, None
    nxt = next((p for p in programs if p["startAt"] >= cur["endAt"]), None)
    return cur, nxt


def progress(program, now_ms):
    """Percent of the program that has aired, 0-100."""
    length = program["endAt"] - program["startAt"]
    if length <= 0:
        return 0
    return max(0, min(100, int((now_ms - program["startAt"]) * 100 // length)))


def remaining_minutes(program, now_ms):
    """Minutes left, rounded up (never 0 while the program is on air)."""
    return max(0, math.ceil((program["endAt"] - now_ms) / 60000))


def _hhmm(ms):
    return fmt_time(ms, "%H:%M")


def time_range(program, now_ms, strings=None):
    """"08:25-09:00 (残り5分)"."""
    s = _strings(strings)
    left = s["left"].format(remaining_minutes(program, now_ms))
    return f"{_hhmm(program['startAt'])}-{_hhmm(program['endAt'])} ({left})"


def _next_line(nxt, strings):
    """"次: 09:00-09:30 name"."""
    s = _strings(strings)
    return f"{s['next']}: {_hhmm(nxt['startAt'])}-{_hhmm(nxt['endAt'])} {to_half(nxt['name'])}"


def _next_details(nxt, lang, strings):
    """"次: name", then its genres and description (end of the plot)."""
    s = _strings(strings)
    lines = [f"{s['next']}: {to_half(nxt['name'])}"]
    genres = item_genres(nxt, lang)
    if genres:
        lines.append(", ".join(genres))
    if nxt.get("description"):
        lines.append(to_half(nxt["description"]))
    return "\n".join(lines)


def _program_info(cur, nxt, channel_name, now_ms, lang, strings, on_error):
    """InfoTagVideo fields of the current program (mediatype video: no watched/resume semantics)."""
    s = _strings(strings)
    name = to_half(cur["name"])
    meta = epgmeta.metadata(cur, on_error=(lambda e: on_error(cur, e)) if on_error else None)
    rng = time_range(cur, now_ms, s)
    texts = [to_half(meta["plot"]).replace("\r\n", "\n")] if meta is not None else raw_plot_texts(cur)
    info = {k: v for k, v in metadata_info(meta, name, lang).items() if k in ("cast", "directors", "writers", "tags")}
    outline = rng
    if nxt is not None:
        outline += f"  {s['next']}: {_hhmm(nxt['startAt'])} {to_half(nxt['name'])}"
    info.update({
        "mediatype": "video",
        "title": name,
        "genres": item_genres(cur, lang),
        "studios": [channel_name],
        "duration": (cur["endAt"] - cur["startAt"]) // 1000,
        "tagline": f"{channel_name}  {rng}",
        "plotoutline": outline,
        # The next program right under the time range: Estuary cuts long plots off at the bottom. Its genres
        # and description stay at the end.
        "plot": build_plot([rng + (f"\n{_next_line(nxt, s)}" if nxt is not None else "")] + texts
                           + ([_next_details(nxt, lang, s)] if nxt is not None else [])),
    })
    return info


def channel_item(channel, cur, nxt, api, now_ms, lang="ja", strings=None, on_error=None):
    """Item dict of one channel: label "channel  program", the current program's metadata, next in the plot."""
    name = to_half(channel["name"])
    info = _program_info(cur, nxt, name, now_ms, lang, strings, on_error)
    art = {}
    if channel.get("hasLogoData"):
        logo = api.logo_url(channel["id"]) + api.kodi_headers()
        art = {"icon": logo, "thumb": logo}
    props = {
        "epgstation.channelId": str(channel["id"]),
        "epgstation.channel": name,
        "epgstation.channelType": channel.get("channelType") or "",
        "epgstation.programId": str(cur["id"]),
        "epgstation.isFree": str(bool(cur.get("isFree"))).lower(),
        "epgstation.progress": str(progress(cur, now_ms)),
    }
    if nxt is not None:
        props["epgstation.next.title"] = to_half(nxt["name"])
        props["epgstation.next.startAt"] = _hhmm(nxt["startAt"])
    props.update(extended_props(cur))
    # Kodi stores playCount and stream details for the fixed live_play URL after any play (a live stream has no
    # length) and overlays them on the list: a watched mark and the last stream's "00:19 MPEG-2". An explicit
    # playcount and own (empty) stream details make Kodi 21 skip both (measured on the dev VM).
    info["playcount"] = 0
    return {
        "label": f"{name}  {info['title']}",
        "path": plugin_url(action="live_play", channelId=channel["id"], name=name),
        "folder": False,
        "playable": True,
        "info": info,
        "art": art,
        "props": props,
        "stream": {"codec": "", "width": 0, "height": 0, "duration": info["duration"]},
        "context": [],
    }


def channel_items(schedules, api, now_ms, lang="ja", strings=None, on_error=None):
    """Item dicts of the watchable channels with a program on air, in server order.

    schedules: a /schedules response (full-width). on_error(program, exception): the metadata parser failed."""
    out = []
    for sched in schedules:
        channel = sched["channel"]
        if not watchable(channel):
            continue
        cur, nxt = now_next(sched.get("programs") or [], now_ms)
        if cur is not None:
            out.append(channel_item(channel, cur, nxt, api, now_ms, lang, strings, on_error))
    return out


def play_info(cur, nxt, channel_name, now_ms, lang="ja", strings=None, on_error=None):
    """InfoTagVideo fields of the play item (OSD): the current program, or just the channel when unknown."""
    if cur is None:
        return {"title": channel_name, "studios": [channel_name], "mediatype": "video"}
    info = _program_info(cur, nxt, channel_name, now_ms, lang, strings, on_error)
    # No length: with one Kodi could store a resume point / watched state for the fixed live_play URL after a
    # long watch. The time range is in the tagline and the plot.
    info.pop("duration", None)
    return info


def live_enabled(config):
    """Live streaming on? Real 2.10.0 sends isEnableTSLiveStream, docs/api/openapi.json names isEnableLiveStream
    (see docs/api/README.md)."""
    return bool(config.get("isEnableTSLiveStream") or config.get("isEnableLiveStream"))


def m2ts_modes(config):
    """[{name, isUnconverted}] of /config streamConfig.live.ts.m2ts (real 2.10.0) or streamConfig.live.m2ts
    (openapi.json); the `mode` parameter is the index."""
    stream_live = (config.get("streamConfig") or {}).get("live") or {}
    return (stream_live.get("ts") or {}).get("m2ts") or stream_live.get("m2ts") or []


def mode_index(modes, choice):
    """Stream mode index for setting live_mode: 0 unconverted (default), 1 the server's first transcoded mode,
    2 its last one. Falls back to the unconverted mode, then 0."""
    unconverted = [i for i, m in enumerate(modes) if m.get("isUnconverted")]
    transcoded = [i for i, m in enumerate(modes) if not m.get("isUnconverted")]
    fallback = unconverted[0] if unconverted else 0
    if choice == 1 and transcoded:
        return transcoded[0]
    if choice == 2 and transcoded:
        return transcoded[-1]
    return fallback


def mode_name(modes, index):
    return modes[index].get("name", "") if 0 <= index < len(modes) else ""


_INNER_STATUS = re.compile(r"Bad status respond \((\d{3})")


def error_message(status, body, strings=None):
    """Short notification text for a live stream that did not start, from one diagnostic GET (api.live_probe).

    None for 200 (the user cancelled, or a tuner became free). EPGStation answers 500 with errors
    "Bad status respond (503 ...)." when Mirakurun has no free tuner -> "No free tuner"; else
    "Could not start live stream (HTTP <the inner Mirakurun status if given, else the HTTP status>)".
    The full server text goes to the log, not here (Kodi cuts long notifications off)."""
    s = _strings(strings)
    if status == 200:
        return None
    errors = str(body.get("errors") or "") if isinstance(body, dict) else ""
    inner = _INNER_STATUS.search(errors)
    if status == 500 and (inner.group(1) == "503" if inner else "503" in errors):
        return s["no_tuner"]
    code = inner.group(1) if inner else status
    return f"{s['failed']} (HTTP {code})" if code else s["failed"]


# --- start watch: after setResolvedUrl, did Kodi start our stream? ---------------------------------------------
STARTED, FAILED, CANCELLED, OTHER, STOPPED = "started", "failed", "cancelled", "other", "stopped"
STOP_VERDICT_WAIT = 2.0  # seconds to look for Kodi's "Playback failed" dialog after a stop before the start


class StartWatch:
    """What the plugin learns from Kodi's player callbacks after resolving a live stream (no Kodi imports).

    Kodi cannot say why a stream failed, so on a failure the plugin makes one status-only GET (api.live_probe).
    Measured on Kodi 21.3 (dev VM): a stream answering an HTTP error gives onPlayBackStarted (sent before the
    stream is opened), then onPlayBackStopped and Kodi's "Playback failed" dialog; no onPlayBackError, no
    onAVStarted. So:
    - onAVStarted for our file: STARTED, no probe.
    - onPlayBackStarted, or our file seen playing: only an attempt (tried). onPlayBackStarted for our item also
      ends the wait for the previous file's stop: switching from a recording, Kodi 21.3 sends only
      onPlayBackStarted and one onPlayBackStopped, and shows no failure dialog.
    - onPlayBackError: FAILED, probe now (Kodi has already closed its connection).
    - onPlayBackStopped / onPlayBackEnded before the start: STOPPED; the plugin then calls decide_stop() with
      whether Kodi's failure dialog shows: FAILED (probe now) or CANCELLED by the user (no probe). When another
      file was playing before (zapping from channel A to B), a stop/end before our onPlayBackStarted is A's and is
      not counted, and our own stop is FAILED at once (one probe after the stop, silent on 200, even when it was
      the user cancelling the switch).
    - another file starts: OTHER (the user went on), no probe.
    - nothing decided by the backstop: probe only if Kodi showed it tried and no player runs; without any
      callback (a Files.GetDirectory on the live_play URL) never.
    The file playing before is never taken as our start, even when it is the same channel's stream.
    """

    def __init__(self, channel_id, previous_file=""):
        self.channel_id = str(channel_id)
        self.previous_file = previous_file or ""
        self.result = None
        self.tried = False
        self._previous_stop_pending = bool(self.previous_file)
        self._probed = False

    def is_ours(self, path):
        if not path:
            return False
        if f"/streams/live/{self.channel_id}/" in path:
            return True
        if path.startswith("plugin://"):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            return query.get("action") == ["live_play"] and query.get("channelId") == [self.channel_id]
        return False

    def on_av_started(self, path):
        if self.result in (None, STOPPED) and self.is_ours(path):
            self.result = STARTED

    def on_playback_started(self, path):
        if self.result is not None:
            return
        if path and not self.is_ours(path) and path != self.previous_file:
            return  # some other file: not our attempt
        self.tried = True
        # Our attempt has begun: Kodi sends no separate stop for the file it replaced, so the next stop is ours.
        self._previous_stop_pending = False

    def on_error(self):
        if self.result in (None, STOPPED):
            self.tried = True
            self.result = FAILED

    def on_stopped(self):
        if self.result is not None:
            return
        self.tried = True
        if self._previous_stop_pending:
            self._previous_stop_pending = False  # the previous file stopped for ours
            return
        # Switching from another file, Kodi 21 shows no failure dialog: a stop of ours before the start is a
        # failure. A user cancel while switching costs one probe after the stop (silent on 200).
        self.result = FAILED if self.previous_file else STOPPED

    on_ended = on_stopped

    def decide_stop(self, kodi_failed):
        """After a STOPPED: Kodi's failure dialog shows -> FAILED, else the user cancelled."""
        if self.result == STOPPED:
            self.result = FAILED if kodi_failed else CANCELLED

    def poll(self, playing):
        """playing: the file Kodi plays now ("" for none)."""
        if self.result is not None or not playing or playing == self.previous_file:
            return
        if self.is_ours(playing):
            self.tried = True  # Kodi shows our file for a moment also on a start that fails: wait for AV
        else:
            self.result = OTHER

    def probe_now(self):
        """True once, right after a failed start."""
        if self.result == FAILED and not self._probed:
            self._probed = True
            return True
        return False

    def probe_at_backstop(self, player_active):
        if self.result is not None or player_active or not self.tried or self._probed:
            return False
        self._probed = True
        return True
