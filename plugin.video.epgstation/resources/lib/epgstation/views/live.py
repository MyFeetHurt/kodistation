"""Live TV: the channel lists (with the current program) and playback of a channel's live stream."""
import time

import xbmc
import xbmcgui

from .. import guide, live, ui
from ..api import ApiError
from . import guide as guide_view

LIVE_STRINGS = (("GR", 30016), ("SKY", 30017), ("next", 30018), ("left", 30019), ("no_tuner", 30101),
                ("failed", 30102))

on_parse_error = ui.parse_error_logger("program")


def live_strings():
    return ui.strings(LIVE_STRINGS)


class _LivePlayer(xbmc.Player):
    """Forwards Kodi's player callbacks to a live.StartWatch."""
    watch = None

    def _event(self, name, *args):
        shown = args[0].split("|", 1)[0][:80] if args else ""  # never the |Authorization= suffix
        ui.log(f"live: player {name} {shown}".rstrip())
        if self.watch:
            getattr(self.watch, name)(*args)

    def onAVStarted(self):  # noqa: N802 - Kodi API
        self._event("on_av_started", ui.playing_file(self))

    def onPlayBackStarted(self):  # noqa: N802
        self._event("on_playback_started", ui.playing_file(self))

    def onPlayBackError(self):  # noqa: N802
        self._event("on_error")

    def onPlayBackStopped(self):  # noqa: N802
        self._event("on_stopped")

    def onPlayBackEnded(self):  # noqa: N802
        self._event("on_ended")


def live_menu(ctx):
    """No type: the guide entries and one folder per broadcast type (or that type's channels when there is only
    one). type=GR|BS|CS|SKY: the channels with their current program."""
    channel_type = ctx.params.get("type")
    strings = live_strings()
    category = live.type_label(channel_type, strings) if channel_type else ui.s(30015)
    top = []
    if not channel_type:
        types = live.live_types(ctx.api.channels())
        top = guide.menu(guide_view.guide_strings())
        if len(types) != 1:
            ctx.add(top + live.type_folders(types, strings), content="files", category=category)
            return
        channel_type = types[0]
    now = ui.now_ms()
    schedules = ctx.api.schedules(now, now + live.WINDOW_MS, [channel_type])
    if not ui.show_subchannels():
        schedules = live.drop_subchannels(schedules)
    out = live.channel_items(schedules, ctx.api, now, ui.lang(), strings, on_parse_error)
    out = guide.with_schedule_context(out, guide_view.guide_strings())
    # reservation markers and entries for each channel's current program
    current = {}
    for sched in schedules:
        cur, _ = live.now_next(sched.get("programs") or [], now)
        if cur is not None:
            current[cur["id"]] = dict(cur, channelId=sched["channel"]["id"])
    if out:
        index, records = guide_view.marks(ctx, list(current.values()), now, (now, now))
        guide.mark_channels(out, current, index, records, now, guide_view.mark_strings())
    ctx.add(top + out, sort=ui.SORT_LIVE, category=category, cache=False)


def live_play(ctx):
    channel_id = ctx.params.get("channelId")
    if not channel_id:
        ui.log("live_play without channelId", xbmc.LOGERROR)
        ctx.notify(ui.s(30102), xbmcgui.NOTIFICATION_ERROR)
        ctx.fail_resolve()
        return
    name = ctx.params.get("name") or channel_id
    config = ctx.api.config()
    if not live.live_enabled(config):
        ctx.notify(ui.s(30103), xbmcgui.NOTIFICATION_ERROR)
        ctx.fail_resolve()
        return
    modes = live.m2ts_modes(config)
    mode = live.mode_index(modes, ui.int_setting("live_mode"))  # 0 unconverted, 1 first, 2 last transcoded mode
    now = time.time() * 1000
    try:  # only for the OSD: never block playback on it
        sched = ctx.api.channel_schedule(channel_id, now, days=1, timeout=live.INFO_TIMEOUT)
        programs = sched[0]["programs"] if sched else []
    except (ApiError, LookupError, TypeError) as e:
        ui.log(f"live program info: {e}", xbmc.LOGWARNING)
        programs = []
    cur, nxt = live.now_next(programs, now)
    info = live.play_info(cur, nxt, name, now, ui.lang(), live_strings(), on_parse_error)
    li = ui.listitem({"label": name, "info": info, "art": {}, "props": {}, "playable": False, "context": [],
                      "mimetype": "video/mp2t"})
    # Kodi 22 stats every resolved URL (HEAD) before playing it; EPGStation never answers a HEAD on a live stream,
    # so the start waited 3 x 20 s. As a headers-only GET the stat is answered at once.
    li.setPath(ctx.api.live_url(channel_id, mode) + ctx.api.kodi_headers(customrequest="GET"))
    ui.log(f"live: channel {channel_id} mode {mode} ({live.mode_name(modes, mode)})")
    player = _LivePlayer()
    watch = live.StartWatch(channel_id, ui.playing_file(player))
    player.watch = watch  # callbacks only count after setResolvedUrl
    ctx.resolve(li)
    _watch_start(ctx, watch, player, mode)


def _watch_start(ctx, watch, player, mode):
    """Until Kodi starts our stream, the user cancels or live.LIVE_START_TIMEOUT s pass: on a failed start ask the
    server once (one GET, status only) and show why (no free tuner, other error)."""
    monitor = xbmc.Monitor()
    waited = 0.0
    while waited < live.LIVE_START_TIMEOUT:
        if monitor.waitForAbort(0.25):
            return
        waited += 0.25
        watch.poll(ui.playing_file(player))
        if watch.result == live.STOPPED:  # stopped before the start: Kodi's failure, or the user?
            verdict_wait = 0.0
            while not xbmc.getCondVisibility("Window.IsActive(okdialog)") and verdict_wait < live.STOP_VERDICT_WAIT:
                if monitor.waitForAbort(0.1):
                    return
                verdict_wait += 0.1
            watch.decide_stop(xbmc.getCondVisibility("Window.IsActive(okdialog)"))
        if watch.probe_now():
            if not player.isPlaying():  # never a second tuner while something plays
                _report_failure(ctx, watch, mode)
            return
        if watch.result is not None:
            return
    if watch.probe_at_backstop(player.isPlaying()):
        _report_failure(ctx, watch, mode)


def _report_failure(ctx, watch, mode):
    status, body = ctx.api.live_probe(watch.channel_id, mode)
    msg = live.error_message(status, body, live_strings())
    ui.log(f"live: channel {watch.channel_id} did not start (probe: {status} {body})", xbmc.LOGWARNING)
    if msg:
        ctx.notify(msg, xbmcgui.NOTIFICATION_ERROR)
