"""Transcoded playback (変換して再生): play a recording as an EPGStation HLS stream in one of the server's modes.

The logic (modes, start choices, keep-alive timing, ownership checks) is in transcode.py; this module drives the
dialogs, the player and the stream's lifetime on the server: start, keep alive while Kodi plays it, always stop it.
"""
import secrets

import xbmc
import xbmcgui

from .. import items, transcode, ui
from ..api import ApiError
from . import recordings

STREAM_STRINGS = (("from_start", 30089), ("from_pos", 30090), ("from_custom", 30091), ("off", 30104),
                  ("no_encoder", 30105), ("failed", 30106), ("out_of_range", 30107), ("ended", 30109))


class _StreamPlayer(xbmc.Player):
    """Forwards Kodi's player callbacks to a transcode.KeepAlive."""
    keep = None

    def onPlayBackStarted(self):  # noqa: N802 - Kodi API
        if self.keep:
            self.keep.on_started()

    def onAVStarted(self):  # noqa: N802
        if self.keep:
            self.keep.on_started()

    def onPlayBackStopped(self):  # noqa: N802
        if self.keep:
            self.keep.on_stopped()

    def onPlayBackEnded(self):  # noqa: N802
        if self.keep:
            self.keep.on_stopped()


def _playing_tag(player):
    """Our nonce property of the playing item ("" when unknown)."""
    try:
        return player.getPlayingItem().getProperty("epgstation.stream") if player.isPlaying() else ""
    except (AttributeError, RuntimeError):  # Kodi < 20 has no getPlayingItem
        return ""


def _player_time(player):
    try:
        return player.getTime()
    except RuntimeError:
        return None


def _positions():
    return transcode.PositionStore(ui.profile_path("stream_positions.json"), ui.server_url())


def _active_streams():
    return transcode.ActiveStreams(ui.profile_path("stream_active"))


def _use_isa():
    return bool(xbmc.getCondVisibility(f"System.AddonIsEnabled({transcode.ISA})"))


def stream(ctx):
    """Context entry: pick the mode and the start, start an HLS stream, wait until it is ready, play it, keep it alive
    while Kodi plays it and always stop it (DELETE) at the end."""
    if ctx.handle >= 0:  # a play request of the URL (not the context menu): never a directory or resolved item
        ctx.fail_resolve()
    strings = transcode.with_defaults(ui.strings(STREAM_STRINGS))
    rec = ctx.api.recorded_item(ctx.int_param("id"))
    video = items.pick_video(rec.get("videoFiles"), items.default_prefer(rec, ui.prefer()))
    if video is None:
        ctx.notify(transcode.error_message(None, None, strings), xbmcgui.NOTIFICATION_ERROR)
        return
    config = ctx.api.config()
    modes = transcode.hls_modes(config, video.get("type"))
    if not transcode.recorded_enabled(config, video.get("type")) or not modes:
        ctx.notify(strings["off"], xbmcgui.NOTIFICATION_ERROR)
        return
    mode = transcode.mode_index(modes, ui.int_setting("stream_mode"))  # 0 first mode, 1 last mode, 2 ask
    if mode is None:
        mode = xbmcgui.Dialog().select(ui.s(30281), modes)
        if mode < 0:
            return
    duration = _duration(ctx, rec, video)
    choices = transcode.start_choices(_positions().get(rec["id"]), duration, strings)
    idx = xbmcgui.Dialog().select(ui.s(30088), [label for label, _ in choices])
    if idx < 0:
        return
    ss = choices[idx][1]
    if ss is None:
        ss = transcode.parse_position(xbmcgui.Dialog().numeric(2, ui.s(30108), "00:00"), duration)
        if ss is None:
            return
    nonce = secrets.token_hex(6)
    li = _listitem(ctx, rec, nonce)  # all metadata before the start: no request after it
    _stop_stale_streams(ctx)
    what = f"recorded {rec['id']} file {video['id']} ss={ss} mode={mode} ({transcode.ISA if _use_isa() else 'ffmpeg'})"
    ui.log(f"stream: start {what}")
    try:
        stream_id = ctx.api.start_recorded_hls(video["id"], ss, mode)
    except ApiError as e:
        ui.log(f"stream: start {what} failed: {e}", xbmc.LOGERROR)
        ctx.notify(transcode.error_message(e.status, e.detail, strings), xbmcgui.NOTIFICATION_ERROR)
        return
    ui.log(f"stream: {stream_id} started")
    # the server's 15 s timer runs from the start: count from here, also while waiting for the stream to be ready
    keep = transcode.KeepAlive(nonce, f"/streamfiles/stream{stream_id}.m3u8", ss, ui.clock())
    ours = (stream_id, video["id"], mode)  # what GET /streams must show for our stream (ids are reused)
    try:
        _remember(stream_id, video["id"], mode, True)  # inside the try: never skips the DELETE
        if not _wait_ready(ctx, ours, strings, keep):
            return
        url = ctx.api.stream_playlist_url(stream_id, nonce)
        li.setPath(url if _use_isa() else url + ctx.api.kodi_headers())
        _play(ctx, ours, keep, li)
    finally:
        _stop(ctx, ours, keep)
    message = transcode.end_message(keep.reason, strings, keep.started)
    if message:
        ctx.notify(message, xbmcgui.NOTIFICATION_WARNING)
    if keep.started:
        _save_position(rec["id"], keep.position, duration)


def _remember(stream_id, video_file_id, mode, active):
    """stream_active/: add our stream after the start, remove it once it is stopped or gone (one file per stream:
    one atomic create or unlink, never a rewrite of other runs' entries)."""
    store = _active_streams()
    if active:
        done = store.add(ui.server_url(), stream_id, video_file_id, mode, ui.now_ms())
    else:
        done = store.remove(ui.server_url(), stream_id)
    if not done:
        ui.log(f"stream: {stream_id}: could not update stream_active", xbmc.LOGWARNING)


def _stop_stale_streams(ctx):
    """Dead listings an earlier run left (Kodi killed during playback; EPGStation 2.10 keeps a timed-out stream
    listed, without its files, until a DELETE). DELETE an entry only when GET /streams lists its id enabled with the
    same file and mode AND its playlist answers 404: a live stream, ours or another client's, always has its
    playlist. Not listed / another file or mode / older than transcode.STALE_MAX_AGE_MS: forgotten; live or not
    checkable: kept for the next start."""
    store, server = _active_streams(), ui.server_url()
    entries = store.of(server)
    if not entries:
        return
    try:
        streams = ctx.api.streams(timeout=transcode.STREAMS_TIMEOUT)
    except ApiError as e:
        ui.log(f"stream: stale check: {e}", xbmc.LOGWARNING)
        return
    for entry in entries:
        sid = entry["streamId"]
        state = transcode.stale_state(entry, streams, ui.now_ms())
        if state == "check":
            status = ctx.api.stream_playlist_status(sid, timeout=transcode.STREAMS_TIMEOUT)
            if status != 404:
                ui.log(f"stream: {sid} of an earlier run is live (playlist {status}): kept")
                continue
            try:
                ctx.api.stop_stream(sid)
                ui.log(f"stream: {sid} dead listing of an earlier run: stopped")
            except ApiError as e:
                ui.log(f"stream: {sid} dead listing of an earlier run: stop: {e}", xbmc.LOGWARNING)
                continue
        elif state == "live":
            ui.log(f"stream: {sid} of an earlier run is starting: kept")
            continue
        else:
            ui.log(f"stream: {sid} of an earlier run: {state}, forgotten")
        store.remove(server, sid)


def _save_position(recorded_id, position, duration):
    """Merged into the store as it is now (another transcoded play may have saved meanwhile)."""
    store = _positions()
    store.put(recorded_id, position, duration)
    if not store.save():
        ui.log("stream: could not write stream_positions.json", xbmc.LOGWARNING)


def _duration(ctx, rec, video):
    """Seconds of the file (real length when known, else the EPG length); None while recording."""
    if rec.get("isRecording"):
        return None
    real = recordings.file_durations(ctx, rec, [video]).get(video["id"])
    return real if real else max(0, (rec["endAt"] - rec["startAt"]) // 1000) or None


def _state(ctx, ours):
    """transcode.stream_state of our stream in GET /streams, or None when the server cannot be asked."""
    try:
        return transcode.stream_state(ctx.api.streams(timeout=transcode.STREAMS_TIMEOUT), *ours)
    except ApiError as e:
        ui.log(f"stream: {ours[0]} state: {e}", xbmc.LOGWARNING)
        return None


def _wait_ready(ctx, ours, strings, keep):
    """Poll GET /streams until our stream is enabled (playlist + 2 segments), keeping it alive meanwhile. False on
    cancel, abort, the stream disappearing (or its id showing another file / mode) or transcode.READY_TIMEOUT s
    (with a toast, except on cancel / abort)."""
    monitor, progress = xbmc.Monitor(), xbmcgui.DialogProgress()
    progress.create(ui.s(30088), ui.s(30092))
    deadline, state = ui.clock() + transcode.READY_TIMEOUT, "waiting"
    try:
        while True:
            state = _state(ctx, ours) or state
            if state in ("gone", "other"):
                keep.server_lost()
            if state != "waiting":
                break
            if keep.keep_due(ui.clock()) and _keep(ctx, ours, keep, verify=False) == "stop":
                state = "gone"
                break
            left = deadline - ui.clock()
            if progress.iscanceled():
                ui.log(f"stream: {ours[0]} cancelled")
                return False
            if left <= 0:
                break
            progress.update(int(100 * (1 - left / transcode.READY_TIMEOUT)))
            if monitor.waitForAbort(min(transcode.READY_STEP, left)):
                return False
    finally:
        progress.close()
    if state == "ready":
        return True
    ui.log(f"stream: {ours[0]} not ready ({state})", xbmc.LOGWARNING)
    ctx.notify(strings["failed"], xbmcgui.NOTIFICATION_ERROR)
    return False


def _listitem(ctx, rec, nonce):
    """The play item without its path: the recording's metadata, HLS mimetype, ISA properties when enabled."""
    d = transcode.stream_item(recordings.recording_item(ctx, rec, channels_timeout=transcode.CHANNELS_TIMEOUT),
                              nonce)
    if _use_isa():
        d["props"].update(transcode.isa_properties(ctx.api.kodi_headers()[len("|"):]))
    return ui.listitem(d)


def _keep(ctx, ours, keep, verify=True):
    """One PUT keep (timeout transcode.KEEP_TIMEOUT); verify: when keep.needs_verify(), GET /streams must still show
    our stream. "stop" when the server no longer has it, else None (a failed keep is retried soon)."""
    sent = ui.clock()
    try:
        ctx.api.keep_stream(ours[0], timeout=transcode.KEEP_TIMEOUT)
    except ApiError as e:
        ui.log(f"stream: {ours[0]} keep: {e}", xbmc.LOGWARNING)
        return keep.keep_failed(e.status, e.detail, ui.clock())
    keep.keep_ok(sent, ui.clock())
    if verify and keep.needs_verify():
        state = _state(ctx, ours)
        if state in ("gone", "other"):
            ui.log(f"stream: {ours[0]} is no longer ours on the server", xbmc.LOGWARNING)
            return keep.server_lost()
        if state is not None:
            keep.verified(sent)
    return None


def _play(ctx, ours, keep, li):
    """Play the ready stream and keep it alive until it should stop."""
    if keep.keep_due(ui.clock()) and _keep(ctx, ours, keep) == "stop":  # fresh before play()
        return
    player = _StreamPlayer()
    keep.playing_from(ui.clock(), ui.playing_file(player))
    player.keep = keep
    player.play(li.getPath(), li)
    monitor = xbmc.Monitor()
    while True:
        if monitor.waitForAbort(1):
            ui.log(f"stream: {ours[0]} Kodi is exiting")
            break
        path, tag = ui.playing_file(player), _playing_tag(player)
        mine = keep.is_ours(path, tag)
        action = keep.tick(ui.clock(), path, tag, _player_time(player) if mine else None)
        if action == "keep":
            action = _keep(ctx, ours, keep)
        if action == "stop":
            ui.log(f"stream: {ours[0]} ends ({keep.reason}) at {keep.position:.0f} s")
            if keep.reason in transcode.STOP_PLAYER and keep.is_ours(ui.playing_file(player), _playing_tag(player)):
                player.stop()
            break


def _stop(ctx, ours, keep):
    """DELETE our stream only when GET /streams shows it as ours (id + file + mode: ids are reused, and another
    client, e.g. the web UI, may stop ours and get the id at once). Only when GET /streams cannot be asked: DELETE
    while keep.trusted() (a good keep less than transcode.TRUST_WINDOW s ago), else not (the server's 15 s timer is
    the backstop: leaving ours for 15 s beats stopping someone else's). stream_active forgets the stream once it is
    stopped or known gone; else the next start retries."""
    if keep.reason == "gone":  # the server said it is gone or no longer ours: nothing of ours to stop
        ui.log(f"stream: {ours[0]} not stopped: gone")
        _remember(*ours, False)
        return
    state = _state(ctx, ours)
    if state is None and keep.trusted(ui.clock()):
        state = "trusted"
    if state not in ("trusted", "ready", "waiting"):
        ui.log(f"stream: {ours[0]} not stopped: {state or 'server not reachable'}")
        if state is not None:  # gone or another client's: nothing of ours left
            _remember(*ours, False)
        return
    try:
        ctx.api.stop_stream(ours[0])
        ui.log(f"stream: {ours[0]} stopped ({state})")
    except ApiError as e:  # the server's 15 s timer stops it anyway
        ui.log(f"stream: {ours[0]} stop: {e}", xbmc.LOGWARNING)
        return
    _remember(*ours, False)
