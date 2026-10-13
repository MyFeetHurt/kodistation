"""Transcoded playback (backlog 011): play a recording as EPGStation HLS in one of the server's recorded-stream modes.

No Kodi imports here: everything is unit-testable (like items.py and live.py). plugin.py drives the dialogs and the
player loop with these helpers.

How EPGStation 2.10.0 does it (docs/api/README.md "Recorded HLS"): GET /streams/recorded/{videoFileId}/hls?ss=&mode=
starts ffmpeg and answers {"streamId": n}; the playlist is the static file /streamfiles/stream<n>.m3u8 (not under
/api), usable once GET /streams?isHalfWidth=true shows isEnable for that id. The server stops a stream 15 s after
its start or the last PUT /streams/{id}/keep; DELETE /streams/{id} stops it at once. Stream ids are reused, so the
played URL carries our own nonce (?k=...; the static server ignores the query).

Kodi (spike on Kodi 21.3, backlog 011 Log): inputstream.adaptive with play_timeshift_buffer starts the live-style
playlist at 0 and seeks within the encoded part; Kodi's own ffmpeg also starts at 0 but cannot seek. The absolute
position is ss + Kodi's player time.

The feature is meant to be removable. Hooks outside this module (code hooks are marked "transcoded playback (011)"):
- items.py: recorded_item's "変換して再生" context entry (only when strings has "stream")
- plugin.py: _item_strings "stream"; NON_DIRECTORY_ACTIONS "stream"; _channels(timeout=); do_stream,
  _save_position, _stream_duration, _use_isa, _stream_state, _wait_ready, _stream_listitem, _keep, _play_stream,
  _stop_stream, _remember_stream, _stop_stale_streams, _StreamPlayer, _playing_tag, _player_time, _stream_strings,
  _stream_mode, _positions, _active_streams
- api.py: start_recorded_hls(), streams(), keep_stream(), stop_stream(), stream_playlist_url(), stream_playlist_status()
- settings.xml: category "stream" (setting stream_mode); strings 30088-30092, 30104-30109, 30280-30285 (both .po)
- docs/api/README.md: "Recorded HLS"
- tests: test_transcode.py, test_snapshot_stream.py; the stream tests in test_plugin.py and test_items.py; the write
  audits in test_reserves.py / test_recorded.py (stream start / keep / stop); test_guide.py's import check (the wrapped
  import); kodi_stubs (xbmc.py Player getTime / getPlayingItem / stop / stop_calls, player_time, player_tag,
  abort_after; xbmcgui.py DialogProgress, Dialog.numeric); conftest.py resets; fixtures tests/fixtures/streams/;
  test_fixture_policy.py "streams"
- tools: fake_epgstation.py stream routes (Handler.streams, stream_ready_after, stream_error, stream_timer,
  stream_clock, /streamfiles), dev/snapshot_stream.py + Makefile snapshot-stream, kodi_smoke.py streams_failures at
  the end
"""
import hashlib
import json
import os
import re

from .store import atomic_write_json

MIME = "application/vnd.apple.mpegurl"  # the server sends the playlist as video/MP2T: Kodi must not look it up
ISA = "inputstream.adaptive"
READY_TIMEOUT = 20  # seconds to wait for isEnable (decision D7)
READY_STEP = 0.5  # seconds between GET /streams polls
SERVER_TIMER = 15  # EPGStation stops a stream 15 s after its start or its last keep (StreamBaseModel.setStopTimer)
# 011 review M1: one keep that times out plus its retry must still reach the server within SERVER_TIMER of the last
# good keep: KEEP_EVERY + KEEP_TIMEOUT + KEEP_RETRY + KEEP_TIMEOUT + 1 s loop step = 14 s.
KEEP_EVERY = 5  # seconds between keeps
KEEP_TIMEOUT = 3  # seconds a keep (and the ownership check after it) may take
KEEP_RETRY = 2  # seconds after a failed keep (connection error, timeout) before the next try (at most: keep_failed)
VERIFY_EVERY = 3  # the ownership check (GET /streams) follows the 1st, 4th, 7th ... keep (and any keep after a gap)
TRUST_WINDOW = 12  # seconds after the last good keep: the DELETE is sent even when GET /streams cannot be asked
GONE_AFTER = 2  # seconds our stream may be missing from the player before the stream is stopped
START_TIMEOUT = 20  # seconds Kodi may take to start playing after Player().play()
FAIL_WAIT = 2.5  # seconds after Kodi started and stopped our item without playing it: the start failed (L1, L-4)
PAUSE_LIMIT = 30 * 60  # seconds the player time may stand still (paused) before the stream is stopped (L3)
RESUME_MIN = 60  # a saved position below this is not offered
END_MARGIN = 60  # a position this close to the end counts as watched to the end (cleared)
POSITIONS_MAX = 200  # entries in stream_positions.json

DEFAULT_STRINGS = {"from_start": "From the beginning", "from_pos": "From {0}", "from_custom": "From a position…",
                   "off": "Server: streaming off", "no_encoder": "No free encoder on the server",
                   "failed": "Could not start transcoded stream", "out_of_range": "Start position is past the end",
                   "ended": "Transcoded stream ended"}

# GET /streams/recorded/{id}/hls errors (HTTP 500, "errors"): StreamManageModel / EncodeProcessManageModel
NO_ENCODER = ("EncodeProcessManageModelCreateError", "CreateStreamProcessError")
OUT_OF_RANGE = ("OutOfRange",)
NO_MODE = ("ConfigIsUndefined",)
STREAM_GONE = "StreamIsUndefined"  # PUT keep on a stopped stream


def with_defaults(strings):
    # Kodi returns "" for string IDs it has not loaded yet (addon updated without restart).
    return dict(DEFAULT_STRINGS, **{k: v for k, v in (strings or {}).items() if v})


def _kind(file_type):
    return "encoded" if file_type == "encoded" else "ts"


def recorded_enabled(config, file_type):
    """Config.isEnableTSRecordedStream / isEnableEncodedRecordedStream for this video file type."""
    key = "isEnableEncodedRecordedStream" if _kind(file_type) == "encoded" else "isEnableTSRecordedStream"
    return bool((config or {}).get(key))


def hls_modes(config, file_type):
    """The server's recorded HLS mode names for this file type (Config.streamConfig.recorded.{ts|encoded}.hls); the
    `mode` parameter is the index into this list. [] when missing."""
    try:
        modes = config["streamConfig"]["recorded"][_kind(file_type)]["hls"]
    except (KeyError, TypeError):
        return []
    return [str(m) for m in modes] if isinstance(modes, list) else []


ASK = 2  # setting stream_mode: 0 the server's first mode, 1 its last, 2 ask each time


def mode_index(modes, choice):
    """Index into modes for setting stream_mode: 0 first (default), 1 last, ASK -> None (the plugin asks). None when
    there is no mode."""
    if not modes or choice == ASK:
        return None
    return len(modes) - 1 if choice == 1 else 0


def _item_file(item):
    return item.get("viodeFileId", item.get("videoFileId"))  # 2.10.0 sends "viodeFileId" (typo), the spec videoFileId


def stream_state(streams, stream_id, video_file_id=None, mode=None):
    """Our stream in GET /streams: "ready" (listed with isEnable), "waiting" (listed, not yet enabled), "gone" (not
    listed: ffmpeg failed or it was stopped) or "other" (011 review L2: the id is listed with another file or mode:
    the server reused the id of our stopped stream for another client). video_file_id / mode None: not compared."""
    items = (streams or {}).get("items") if isinstance(streams, dict) else None
    for item in items or []:
        if isinstance(item, dict) and item.get("streamId") == stream_id:
            # review round 2 L-1: compare only what the item carries (a server without the file id or mode is not
            # "another client" for every stream)
            if (video_file_id is not None and _item_file(item) is not None and _item_file(item) != video_file_id) \
                    or (mode is not None and item.get("mode") is not None and item.get("mode") != mode):
                return "other"
            return "ready" if item.get("isEnable") else "waiting"
    return "gone"


def is_ready(streams, stream_id, video_file_id=None, mode=None):
    return stream_state(streams, stream_id, video_file_id, mode) == "ready"


def error_message(status, detail, strings=None):
    """Short toast for a stream that could not start: (ApiError.status, ApiError.detail = the server's "errors")."""
    s = with_defaults(strings)
    detail = str(detail or "")
    if detail in NO_ENCODER:
        return s["no_encoder"]
    if detail in OUT_OF_RANGE:
        return s["out_of_range"]
    if detail in NO_MODE:
        return s["off"]
    return f"{s['failed']} (HTTP {status})" if status else s["failed"]


def fmt_position(secs):
    """Seconds -> "M:SS", or "H:MM:SS" from one hour on."""
    secs = max(0, int(secs))
    h, rest = divmod(secs, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def can_resume(position, duration):
    """A saved position worth offering: >= RESUME_MIN and (duration unknown or) more than END_MARGIN before the end."""
    if position is None or position < RESUME_MIN:
        return False
    return duration is None or position < duration - END_MARGIN


def start_choices(saved_pos, duration, strings=None):
    """[(label, start seconds or None for "ask")]: 最初から, "12:34 から" (a resumable saved position), 位置を指定…."""
    s = with_defaults(strings)
    out = [(s["from_start"], 0)]
    if can_resume(saved_pos, duration):
        out.append((s["from_pos"].format(fmt_position(saved_pos)), int(saved_pos)))
    out.append((s["from_custom"], None))
    return out


_TIME = re.compile(r"^\s*(\d{1,3})\s*:\s*(\d{1,2})\s*$")


def parse_position(text, duration=None):
    """Kodi's time dialog (Dialog().numeric(2): "HH:MM", here hours:minutes of the recording) -> seconds, clamped to
    30 s before the end when the duration is known. None for an empty or unreadable answer (cancel)."""
    m = _TIME.match(text or "")
    if not m:
        return None
    hours, minutes = int(m.group(1)), int(m.group(2))
    if minutes > 59:
        return None
    secs = hours * 3600 + minutes * 60
    if duration is not None and duration > 0:
        secs = min(secs, max(0, int(duration) - 30))
    return secs


class KeepAlive:
    """What the plugin does from the start GET until the stream should stop (no Kodi imports; monotonic seconds).

    Created right after the start GET: the server's timer runs from there, so keep_due() counts from that moment (also
    while the plugin waits for isEnable), not from the play() call (measured on the dev Kodi: a first keep 16 s after
    the start came too late, the server had stopped the stream). playing_from() marks the play() call. A due keep
    schedules the next after KEEP_EVERY s; a failed one is retried after KEEP_RETRY s, sooner when the server's timer
    (from the last good keep) would run out first (keep_failed).

    last_good: when the last good keep was sent, while the chain of good keeps since the start GET is unbroken (each
    within SERVER_TIMER of the one before): the server cannot have stopped our stream and given its id to another
    client in between. None after a gap, until a GET /streams shows our stream again (verified). needs_verify(),
    trusted() (the DELETE when GET /streams cannot be asked) and the retry deadline follow from it.

    tick() is fed with what Kodi plays now: the file, the nonce property of the playing ListItem ("" when Kodi cannot
    say: Kodi 20's Player.getPlayingItem, or none) and the player time. It answers "keep" when one is due, "stop" once
    the stream should go, else None; `position` is the absolute position (ss + player time) and `reason` why it
    stopped:
    - "stopped": our item had played and Kodi sent onPlayBackStopped/Ended (on_stopped) and no longer plays it
    - "ended": our item had played and has been missing from the player for GONE_AFTER s
    - "other": another file plays (after ours started, or instead of it; not the file playing before play())
    - "failed": Kodi started our item (on_started after play()), then stopped it before it played, and nothing of
      ours played for FAIL_WAIT s (without on_started a stop is left to START_TIMEOUT)
    - "timeout": ours never played within START_TIMEOUT s of play()
    - "paused": the player time stood still for PAUSE_LIMIT s (a paused player would keep the stream forever)
    - "gone": the server no longer has our stream (keep -> StreamIsUndefined, or GET /streams: server_lost)
    Ours: the nonce property when Kodi gives one, else the playlist path with our ?k=nonce (inputstream.adaptive
    reports the path without its query: then the stream path alone, ids are unique while ours runs).
    """

    def __init__(self, nonce, stream_path, ss, now, previous_file=""):
        """now: when the start GET answered."""
        self.nonce, self.stream_path = nonce, stream_path
        self.ss = int(ss or 0)
        self.position = float(self.ss)
        self.previous_file = previous_file or ""
        self.previous_pending = False
        self.t0 = now
        self.next_keep = now + KEEP_EVERY
        self.last_good = now  # the start GET answered: ours for sure
        self.keeps = 0
        self.played = False  # play() was called
        self.kodi_started = False  # Kodi's start callback came for our play()
        self.started = False
        self.stop_seen = False
        self.missing_since = None
        self.failed_since = None
        self.last_time = self.last_advance = None
        self.reason = None

    def is_ours(self, path, tag=""):
        if tag:
            return tag == self.nonce
        if not path or self.stream_path not in path.split("|", 1)[0]:
            return False
        query = path.split("|", 1)[0].partition("?")[2]
        return not query or f"k={self.nonce}" in query.split("&")

    def playing_from(self, now, previous_file=""):
        """play() was called now; previous_file: what Kodi played just before (its stop is not ours)."""
        self.t0 = now
        self.previous_file = previous_file or ""
        self.previous_pending = bool(self.previous_file)
        self.played = True
        self.kodi_started = False
        self.stop_seen = False

    def keep_due(self, now):
        """True (and the next one scheduled KEEP_EVERY s later) when a keep is due."""
        if self.reason is None and now >= self.next_keep:
            self.next_keep = now + KEEP_EVERY
            self.keeps += 1
            return True
        return False

    def keep_ok(self, sent, now):
        """The keep sent at `sent` answered 200 at `now`: the chain goes on when it came within the server's timer."""
        if self.last_good is not None and now - self.last_good < SERVER_TIMER:
            self.last_good = sent
        else:
            self.last_good = None  # a gap: the id may have been someone else's meanwhile

    def needs_verify(self):
        """After this keep, GET /streams must show our stream: every VERIFY_EVERY-th keep, and after a gap."""
        return self.last_good is None or self.keeps % VERIFY_EVERY == 1

    def verified(self, sent):
        """GET /streams showed our stream after the keep sent at `sent`."""
        self.last_good = sent

    def trusted(self, now):
        """Our id is surely still ours (good keep less than TRUST_WINDOW s ago, unbroken chain)."""
        return self.last_good is not None and now - self.last_good < TRUST_WINDOW

    def on_started(self):
        """onPlayBackStarted / onAVStarted after our play(): Kodi began our item, so the next stop is ours (switching
        from another file, Kodi 21.3 sends one stop, before our start, for the file it replaced)."""
        if self.played:
            self.previous_pending = False
            self.kodi_started = True

    def on_stopped(self):
        if not self.started and (self.previous_pending or not self.kodi_started):
            self.previous_pending = False  # the replaced file's stop, or one Kodi never started ours for
            return
        self.stop_seen = True

    def _stop(self, reason):
        if self.reason is None:
            self.reason = reason
        return "stop"

    def tick(self, now, path="", tag="", time=None):
        if self.reason is not None:
            return "stop"
        if self.is_ours(path, tag):
            self.started = True
            self.stop_seen = False
            self.missing_since = self.failed_since = None
            if time is not None and time >= 0:
                if self.last_time is None or abs(time - self.last_time) > 0.5:
                    self.last_advance = now
                    self.last_time = time
                self.position = self.ss + float(time)
            if self.last_advance is None:
                self.last_advance = now
            if now - self.last_advance >= PAUSE_LIMIT:
                return self._stop("paused")
        elif self.started:
            if path:
                return self._stop("other")
            if self.stop_seen:
                return self._stop("stopped")
            if self.missing_since is None:
                self.missing_since = now
            if now - self.missing_since >= GONE_AFTER:
                return self._stop("ended")
        else:
            if path and path != self.previous_file:
                return self._stop("other")
            if self.stop_seen:
                if self.failed_since is None:
                    self.failed_since = now
                if now - self.failed_since >= FAIL_WAIT:
                    return self._stop("failed")
            if now - self.t0 >= START_TIMEOUT:
                return self._stop("timeout")
        return "keep" if self.keep_due(now) else None

    def keep_failed(self, status, detail, now):
        """A keep failed: the stream is gone when the server says so (500 StreamIsUndefined, or 404) -> "stop"; a
        connection error or timeout is retried KEEP_RETRY s later -> None."""
        if detail == STREAM_GONE or status == 404:
            return self._stop("gone")
        if self.reason is None:
            retry = now + KEEP_RETRY
            if self.last_good is not None:  # review round 2 L-3: before the server's timer runs out (one loop step)
                retry = min(retry, self.last_good + SERVER_TIMER - KEEP_TIMEOUT - 1)
            self.next_keep = retry
        return None

    def server_lost(self):
        """GET /streams no longer shows our stream (gone, or the id now belongs to another client's stream)."""
        return self._stop("gone")


STOP_PLAYER = ("gone", "timeout", "paused")  # reasons the plugin also stops Kodi's player when it still plays ours
CHANNELS_TIMEOUT = 3  # seconds for GET /channels (only the studio name and logo of the play item)
STREAMS_TIMEOUT = 3  # seconds for a GET /streams (readiness, ownership checks)


def end_message(reason, strings=None, started=True):
    """Toast when the stream ended by itself (011 review nit 3 / L1 / L3); None when the user ended it. started: our
    item had played (a "gone" before that is a failed start, review round 2)."""
    s = with_defaults(strings)
    if reason in ("failed", "timeout") or (reason == "gone" and not started):
        return s["failed"]
    if reason in ("gone", "paused"):
        return s["ended"]
    return None


def stream_item(d, nonce):
    """The play item dict for a transcoded stream from items.recorded_item's dict: the same metadata, no duration
    (the HLS timeline starts at ss; no resume point under the stream URL), playcount 0, the HLS mimetype (no HEAD /
    content lookup) and our nonce as a ListItem property."""
    info = {k: v for k, v in d["info"].items() if k != "duration"}
    info["playcount"] = 0
    stream = dict(d["stream"], duration=0) if d.get("stream") else None
    return dict(d, info=info, stream=stream, props=dict(d.get("props") or {}, **{"epgstation.stream": nonce}),
                playable=False, context=[], mimetype=MIME)


def isa_properties(auth_header=""):
    """ListItem properties for inputstream.adaptive (decision D6): seek within the encoded part, start at 0."""
    props = {"inputstream": ISA, "inputstream.adaptive.play_timeshift_buffer": "true"}
    if auth_header:
        props["inputstream.adaptive.manifest_headers"] = auth_header
        props["inputstream.adaptive.stream_headers"] = auth_header
    return props


STALE_MAX_AGE_MS = 7 * 24 * 3600 * 1000  # an entry of stream_active/ older than this is dropped unchecked


class ActiveStreams:
    """Streams this Kodi started and has not stopped yet (011 tester F1): one file per stream in the directory
    stream_active/, `<sha1(server_url)[:12]>-<streamId>.json` = {"server_url", "streamId", "videoFileId", "mode",
    "startedAt"}, so each add is one atomic create and each remove one unlink (concurrent plugin runs never rewrite
    each other's entries).

    EPGStation 2.10.0 keeps a stream listed in /api/streams after its 15 s timer stopped it (double unlink, ENOENT)
    until someone DELETEs it; its files (playlist included) are gone. When Kodi dies during playback (no DELETE), the
    next transcoded start DELETEs such a dead listing (stale_state "match" and the playlist answers 404)."""

    def __init__(self, directory):
        self.directory = directory

    def _file(self, server_url, stream_id):
        key = hashlib.sha1(server_url.encode("utf-8")).hexdigest()[:12]
        return os.path.join(self.directory, f"{key}-{int(stream_id)}.json")

    def of(self, server_url):
        """[entry] of this server (the others are left alone); unreadable files are skipped."""
        try:
            names = sorted(os.listdir(self.directory))
        except OSError:
            return []
        out = []
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.directory, name), encoding="utf-8") as f:
                    entry = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(entry, dict) and entry.get("server_url") == server_url and \
                    isinstance(entry.get("streamId"), int) and not isinstance(entry["streamId"], bool):
                out.append(entry)
        return out

    def add(self, server_url, stream_id, video_file_id, mode, started_at):
        try:
            os.makedirs(self.directory, exist_ok=True)
        except OSError:
            return False
        return atomic_write_json(self._file(server_url, stream_id), {
            "server_url": server_url, "streamId": int(stream_id), "videoFileId": video_file_id, "mode": mode,
            "startedAt": int(started_at)})

    def remove(self, server_url, stream_id):
        """True when no entry is left for it (removed now, or there was none)."""
        try:
            os.remove(self._file(server_url, stream_id))
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True


def stale_state(entry, streams, now_ms):
    """What to do with an entry of stream_active/ at the next start (final review round), from GET /streams:
    - "old": older than STALE_MAX_AGE_MS -> forget it unchecked
    - "forget": the id is not listed, or listed with another (or no) file / mode -> forget it, no DELETE
    - "live": listed with the same file and mode but not yet enabled -> keep it, no DELETE
    - "check": listed with isEnable and the same file and mode (both present: a strict match) -> a dead listing only
      when the playlist is gone (HEAD 404); a live stream, ours or anyone's, always has its playlist."""
    if now_ms - (entry.get("startedAt") or 0) > STALE_MAX_AGE_MS:
        return "old"
    items = (streams or {}).get("items") if isinstance(streams, dict) else None
    for item in items or []:
        if isinstance(item, dict) and item.get("streamId") == entry["streamId"]:
            file_id, mode = _item_file(item), item.get("mode")
            if file_id is None or mode is None or file_id != entry.get("videoFileId") or mode != entry.get("mode"):
                return "forget"
            return "check" if item.get("isEnable") else "live"
    return "forget"


class PositionStore:
    """Where transcoded playback stopped, per recording (decision D2 B: separate from Kodi's direct-play resume).

    stream_positions.json: {"server_url": ..., "positions": {"<recordedId>": seconds}} (oldest first, at most
    POSITIONS_MAX); another server_url starts empty. An unreadable file counts as empty."""

    def __init__(self, path, server_url):
        self.path, self.server_url = path, server_url
        self.positions = {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.get("server_url") == server_url and isinstance(data.get("positions"), dict):
            for key, value in data["positions"].items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.positions[str(key)] = float(value)

    def get(self, recorded_id):
        return self.positions.get(str(recorded_id))

    def put(self, recorded_id, position, duration=None):
        """Remember position; forget it when it is not resumable (near the end, or under RESUME_MIN)."""
        key = str(recorded_id)
        self.positions.pop(key, None)
        if can_resume(position, duration):
            self.positions[key] = float(int(position))
            while len(self.positions) > POSITIONS_MAX:
                self.positions.pop(next(iter(self.positions)))

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        return atomic_write_json(self.path, {"server_url": self.server_url, "positions": self.positions})
