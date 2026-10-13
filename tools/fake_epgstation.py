#!/usr/bin/env python3
"""Serve captured EPGStation fixtures as a fake EPGStation API.

    tools/fake_epgstation.py tests/fixtures/dev2 --port 8889 [--multiply N]

Implements the read endpoints the addon uses, including /api/recorded filtering
and paging, so the addon can be developed and tested without a real server.
Anything not captured returns 404. Writes return 405, except the reservation DELETEs (see below).

Like EPGStation, recordings are full-width (the fixtures are captured with isHalfWidth=false, which
has no rawExtended); isHalfWidth=true gets to_half'd name/description/extended plus rawExtended.
That rawExtended is rebuilt from `extended` (items.raw_extended), so it differs from the real one
where EPGStation's `extended` lost information: enclosed characters (🈀, ㊙, ...) appear as their
replacements ("[ほか]", "[秘]") when the server has needToReplaceEnclosingCharacters, keys that
started with ◇ come back without it, and a value that is a single "◇…" line becomes a key.
Every GET is logged as (path, query dict) in REQUESTS[base_url] (tests) / Handler.requests.
Drop logs (backlog 002): /dropLogs/{id}?maxsize=KB serves api/dropLogs/{id}.txt as text/plain, 404 when not captured,
416 when the file is larger than maxsize KB (default 512), like EPGStation 2.10.0 (which has no limit when maxsize
is missing and answers 500 when the DB row exists but the file is gone). Handler.droplog_body: bytes every
/dropLogs/{id} answers instead (tests: an empty log).

--multiply N serves every recorded fixture N times (copy k: id + k*100000, startAt/endAt k weeks earlier,
same texts and files), so shows have N episodes and ~3000 recordings need no new fixtures. /recording
(live) is not multiplied; /recorded/options counts are. Tests can also set Handler.delay (seconds before
answering a /recorded page with limit > 1) and Handler.mutate (records -> records, applied to the full
/recorded list) through HANDLERS[base_url].

Live TV (backlog 012): /schedules (type flags, overlap window, isHalfWidth, isFree), /schedules/{channelId} and
/schedules/broadcasting serve tests/fixtures/live/schedules.json / broadcasting.json (one copy shared by every
fixture set: dev and dev2 come from the same server; broadcasting.json keeps only channel and program ids) with
every time shifted by now - live_meta.json capturedAt, so the captured programs are "on air" whenever the
tests run. The fixtures hold free channels only (020); live/synthetic_paid.json adds three invented paid channels
(（合成）有料ＢＳ, （合成）有料ＣＳ and the data service （合成）有料データ; isFree false) to /channels, /schedules and
/schedules/broadcasting.
/streams/live/{id}/m2ts answers the sample TS as video/mp2t, or (Handler.live_error = error body) HTTP 500 with
that body, like EPGStation when Mirakurun has no free tuner. Handler.config_patch is merged into /config
(e.g. {"isEnableTSLiveStream": false}).

Program guide (backlog 013): tests/fixtures/live/guide.json (slim programs, more hours and NHK総合1 for 8 days) is
merged into the schedules (dedupe by id, sorted by startAt; channelId filled in from the channel) and shifted the
same way. Handler.now (ms, None = the clock) is the "now" of that shift: tests fix it to capturedAt so local days
line up. /schedules/{id}?days=N answers N Schedule objects, one per window [startAt + k*24h, +24h] with an
inclusive overlap (EPGStation's findSchedule), and /schedules/detail/{id} one program (404 like EPGStation).

Reservations (backlog 007): every handler class keeps its own deep copy of api/reserves.json and api/recording.json
(Handler.reserves_state / recording_state; reset_reserves() restores them, writes and the knobs).
GET /reserves (type filter exactly as EPGStation's ReserveDB.setReserveTypeOption, ruleId, offset, limit default
24, order startAt, isHalfWidth), /reserves/{id} (isHalfWidth required like the real server, 404 body as the real
one), /reserves/cnts (computed, getCnts priority conflict > skip > overlap > normal), /reserves/lists?startAt&endAt
(inclusive overlap). /recording serves recording_state and /recorded/{id} falls back to it (an in-progress
recording has no captured detail).
Writes: every POST/PUT/DELETE is appended to Handler.writes as (method, path, body). DELETE /reserves/{id} applies
EPGStation's cancel (manual -> removed; rule overlap -> unchanged; rule -> isSkip, not a conflict) and, when the
reservation is recording, drops its recording from /recording (stop; Handler.stop_drops_recording); .../skip and
.../overlap apply removeSkip / removeOverlap (no-ops when not applicable). Answers: 200 with an empty body; an unknown
id -> 500 {"errors": "ReservationIsNotFound"} like EPGStation; Handler.write_error = (status, body dict) forces a
failure. PUT and every other write stay 405 (and are recorded).

Record from the guide (backlog 014): POST /reserves (recorded in writes; write_error applies) behaves like
EPGStation's ReservationManageModel.add for a programId: the body must be JSON with a boolean allowEndLack and an
integer programId (else 400 like the openapi validator); a programId that already has a reservation in any state ->
500 "ReservationManageModelCheckReservedProgramError"; not in the (shifted, merged) guide -> 500 "ProgramIsNotFound";
Handler.add_conflict = True -> 500 "ReservationManageModelAddReserveConflict"; else a manual reservation built from
the guide program (no ruleId, flags false, allowEndLack and encodeOption as given, id = max + 1) and 201
{"reserveId": id}. An airing program also appears in /recording (recording starts at once, like the real server).

Protect / delete (backlog 006; code hooks marked "protect / delete (006)"): per class Handler.recorded_deleted (ids)
and Handler.recorded_protect ({id: bool}), plus the test knob Handler.recorded_patch ({id: fields}), all reset by
reset_reserves() (= reset_state). They apply to /recorded (before filtering: total follows), /recorded/{id} (deleted ->
404 {"code":404,"message":"recorded is not Found"}) and /recording. Writes (recorded in writes; write_error applies):
PUT /recorded/{id}/protect|unprotect -> 200 {"code":200} (same state too), unknown id 500 "RecordedIsNull";
DELETE /recorded/{id} -> unknown 500 "RecordedIdIsNotFound", protected 500 "RecordedIsProtected", in progress ->
removed from /recording (stop and delete), else deleted; 200 {"code":200}.

Delete a file (backlog 018; code hooks marked "delete a file (018)"): per class Handler.video_deleted (video file ids,
reset by reset_state) is filtered out of every recording's videoFiles. DELETE /api/videos/{id} (recorded in writes;
write_error applies) like EPGStation's RecordedManageModel.deleteVideoFile: unknown or deleted id -> 500
"VideoFileIsNotFound"; protected recording -> 500 "RecordedIsProtected"; in progress -> the whole recording is stopped
and deleted; else the file goes, and when none is left the whole recording is deleted; 200 {"code":200}. With
--multiply > 1 it answers 405 (the copies share the original's video file ids).

Transcoded playback (backlog 011; code hooks marked "transcoded playback (011)"): per class Handler.streams ({id:
state}, reset by reset_state). GET /api/streams/recorded/{videoFileId}/hls?ss=&mode= (recorded in writes as ("GET",
path?query, ""): it starts a transcode) like EPGStation's StreamManageModel.start: ss and mode required (400), an
unknown file -> 500 "VideoFileIsNotFound", a mode outside /config streamConfig.recorded.{ts|encoded}.hls -> 500
"ConfigIsUndefined", ss beyond the captured duration -> 500 "OutOfRange", Handler.stream_error = (status, body) forces
an answer; else 200 {"streamId": the lowest free id}. GET /api/streams (isHalfWidth required) lists them in the
captured item shape of tests/fixtures/streams/streams.json (incl. 2.10.0's "viodeFileId"); a stream is isEnable from
its Handler.stream_ready_after-th listing on (None: never). PUT /api/streams/{id}/keep -> 200 {"code":200} (counted in
the state), a gone id -> 500 "StreamIsUndefined"; DELETE /api/streams/{id} -> 200 {"code":200} always (removes it).
/streamfiles/stream{id}.m3u8 answers a generated live-style playlist (video/MP2T, like express' mime table) while the
stream exists, else 404; /streamfiles/stream{id}-NNNNNNNNN.ts the NULL_TS packets (no video file in the tree).
Handler.stream_timer = seconds (None: off) applies EPGStation's stop timer (StreamBaseModel.setStopTimer, 15 s): a
stream not kept for that long after its start or last keep disappears; Handler.stream_clock (default
time.monotonic; tests: the Kodi stub's monitor clock) is its clock (011 review L4d). Handler.streams[id]["zombie"] =
True is EPGStation 2.10.0's dead listing (tester F1): still in GET /streams, but its /streamfiles answer 404. HEAD
/streamfiles/... answers like GET without a body.
"""
import argparse
import copy
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugin.video.epgstation"))
from resources.lib.epgstation.items import raw_extended, to_half  # noqa: E402

LIVE_FIXTURES = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures", "live")
STREAM_FIXTURES = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures", "streams")  # (011)
# 1 second of black MPEG-TS is enough for Kodi to "play" something in tests.
SAMPLE_VIDEO = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures", "sample.ts")


# Without sample.ts: 16 MPEG-TS null packets (PID 0x1FFF), enough for a client that only checks the response.
NULL_TS = (b"\x47\x1f\xff\x10" + b"\xff" * 184) * 16

REQUESTS = {}  # base_url -> [(path, query dict)]
HANDLERS = {}  # base_url -> handler class (test knobs: multiply, delay, mutate)
COPY_ID = 100000
WEEK_MS = 7 * 24 * 3600 * 1000
DAY_MS = 24 * 3600 * 1000  # program guide (013)


def multiplied(records, n):
    """records plus n-1 copies each: id + k*COPY_ID, k weeks earlier."""
    out = list(records)
    for k in range(1, n):
        for r in records:
            c = dict(r, id=r["id"] + k * COPY_ID, startAt=r["startAt"] - k * WEEK_MS, endAt=r["endAt"] - k * WEEK_MS)
            out.append(c)
    return out


def _int(qs, key, default=0):
    try:
        return int(qs[key][0])
    except (KeyError, IndexError, ValueError):
        return default


def _bool(qs, key):
    return qs.get(key, ["false"])[0].lower() == "true"


def half_width(rec):
    """What EPGStation returns with isHalfWidth=true for a (full-width) RecordedItem."""
    rec = copy.deepcopy(rec)
    for key in ("name", "description", "extended"):
        if rec.get(key) is not None:
            rec[key] = to_half(rec[key])
    if rec.get("extended"):
        rec["rawExtended"] = {to_half(k): to_half(v) for k, v in raw_extended(rec["extended"]).items()}
    return rec


def _texts(recs, qs):
    return [half_width(r) for r in recs] if _bool(qs, "isHalfWidth") else recs


def _keyword_match(rec, keywords):
    """EPGStation: every keyword in halfWidthName, or every one in halfWidthDescription (SQL LIKE: no case)."""
    return any(all(k.lower() in to_half(rec.get(field) or "").lower() for k in keywords)
               for field in ("name", "description"))


def filter_recorded(records, qs):
    out = list(records)
    if "ruleId" in qs:
        rid = int(qs["ruleId"][0])
        out = [r for r in out if r.get("ruleId") == rid]
    if "channelId" in qs:
        cid = int(qs["channelId"][0])
        out = [r for r in out if r.get("channelId") == cid]
    if "genre" in qs:
        g = int(qs["genre"][0])
        out = [r for r in out if g in (r.get("genre1"), r.get("genre2"), r.get("genre3"))]
    if "keyword" in qs:
        keywords = to_half(qs["keyword"][0]).split()
        out = [r for r in out if _keyword_match(r, keywords)]
    if _bool(qs, "hasOriginalFile"):
        out = [r for r in out if any(v["type"] == "ts" for v in r.get("videoFiles") or [])]
    out.sort(key=lambda r: r["startAt"], reverse=not _bool(qs, "isReverse"))
    return out


def _schedule_texts(sched, qs):
    """isHalfWidth=true: to_half'd channel and program texts (like EPGStation)."""
    if not _bool(qs, "isHalfWidth"):
        return sched
    sched = copy.deepcopy(sched)
    for s in sched:
        s["channel"]["name"] = to_half(s["channel"]["name"])
        for p in s["programs"]:
            for key in ("name", "description", "extended"):
                if p.get(key) is not None:
                    p[key] = to_half(p[key])
    return sched


def live_file(name):
    path = os.path.join(LIVE_FIXTURES, name + ".json")
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return json.loads(f.read())


def synthetic_paid():
    """tests/fixtures/live/synthetic_paid.json (020): three invented paid channels (BS, CS), programs with absolute
    startAt / endAt (capturedAt + startOffset / endOffset). {"schedules": [...], "channels": [...]}."""
    data = live_file("synthetic_paid")
    if not data:
        return {"schedules": [], "channels": []}
    cap = (live_file("live_meta") or {}).get("capturedAt", 0)
    def program(p):
        out = {k: v for k, v in p.items() if k not in ("startOffset", "endOffset")}
        out.update(startAt=cap + p["startOffset"], endAt=cap + p["endOffset"])
        return out
    schedules = [{"channel": dict(s["channel"]), "programs": [program(p) for p in s["programs"]]}
                 for s in data["schedules"]]
    return {"schedules": schedules, "channels": [dict(c) for c in data["channels"]]}


def merged_live(name):
    """tests/fixtures/live/<name>.json, unshifted, plus the synthetic paid channels (020); "schedules" with
    guide.json's programs merged in (program guide, 013): dedupe by id, sorted by startAt, channelId from the
    channel."""
    data = live_file(name) or []
    paid = synthetic_paid()["schedules"]
    if name == "broadcasting":
        return data + [{"channel": {"id": s["channel"]["id"]}, "programs": [{"id": s["programs"][0]["id"]}]}
                       for s in paid]
    if name != "schedules":
        return data
    data += paid
    by_id = {s["channel"]["id"]: s for s in data}
    for g in live_file("guide") or []:
        cid = g["channel"]["id"]
        sched = by_id.get(cid)
        if sched is None:
            sched = by_id[cid] = {"channel": g["channel"], "programs": []}
            data.append(sched)
        have = {p["id"] for p in sched["programs"]}
        sched["programs"] = sorted(sched["programs"] + [dict(p, channelId=cid) for p in g["programs"]
                                                        if p["id"] not in have], key=lambda p: p["startAt"])
    return data


# --- reservations (007) ------------------------------------------------------------------------------------------

def reserve_state(r):
    """EPGStation's getCnts / getLists class: conflict > skip > overlap > normal."""
    if r.get("isConflict"):
        return "conflicts"
    if r.get("isSkip"):
        return "skips"
    if r.get("isOverlap"):
        return "overlaps"
    return "normal"


TYPE_FLAGS = {  # ReserveDB.setReserveTypeOption: (isConflict, isSkip, isOverlap)
    "normal": (False, False, False), "conflict": (True, False, False), "skip": (False, True, False),
    "overlap": (False, False, True),
}


def filter_reserves(reserves, qs):
    out = list(reserves)
    flags = TYPE_FLAGS.get(qs.get("type", ["all"])[0])
    if flags:
        out = [r for r in out if (bool(r.get("isConflict")), bool(r.get("isSkip")), bool(r.get("isOverlap"))) == flags]
    if "ruleId" in qs:
        rid = _int(qs, "ruleId")
        out = [r for r in out if r.get("ruleId") == rid]
    out.sort(key=lambda r: r["startAt"])
    return out


def same_program(r, rec):
    """reserves.match_recording's rule: same channel and programId, or same channel and startAt without one."""
    if r.get("channelId") != rec.get("channelId"):
        return False
    if rec.get("programId") is not None:
        return r.get("programId") == rec["programId"]
    return r.get("startAt") == rec.get("startAt")


NOT_FOUND_500 = {"code": 500, "message": "Internal Server Error", "errors": "ReservationIsNotFound"}


def stream_template():  # transcoded playback (011)
    """The captured /streams item (tests/fixtures/streams/streams.json), or a minimal one without the fixture."""
    path = os.path.join(STREAM_FIXTURES, "streams.json")
    if os.path.isfile(path):
        with open(path, "rb") as f:
            items = json.loads(f.read()).get("items") or []
        if items:
            return items[0]
    return {"streamId": 0, "type": "RecordedHLS", "mode": 0, "isEnable": False}


def playlist_text(stream_id, segments=2):  # transcoded playback (011): EPGStation's live-style playlist (no ENDLIST)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-ALLOW-CACHE:YES", "#EXT-X-TARGETDURATION:5",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    for n in range(segments):
        lines += ["#EXTINF:3.000000,", f"stream{stream_id}-{n:09d}.ts"]
    return ("\n".join(lines) + "\n").encode()


def error_500(name):
    """EPGStation's error body for an exception name (service/api.ts)."""
    return {"code": 500, "message": "Internal Server Error", "errors": name}


class Handler(BaseHTTPRequestHandler):
    root = "."
    requests = []
    multiply = 1
    delay = 0.0
    mutate = None
    live_error = None  # live TV (012): error body for /streams/live (HTTP 500), None = stream the sample TS
    config_patch = None  # merged into /config
    droplog_body = None  # full detail view (002): test knob, bytes every /dropLogs/{id} answers with 200 (None: files)
    NOT_FOUND = object()  # program guide (013): _live's answer for a missing program
    now = None  # program guide (013): "now" in ms for the schedule shift, None = time.time()
    # reservations (007): set per class by reset_reserves()
    reserves_state = None
    recording_state = None
    writes = None  # [(method, path, body)] of every write request
    write_error = None  # (status, body dict): the next writes fail with it
    stop_drops_recording = True  # a cancel of a recording reservation removes it from /recording
    recording_delay = 0.0  # seconds /recording takes to answer once a write was made (a slow server after a stop)
    add_conflict = False  # record from the guide (014): POST /reserves answers the conflict error
    recording_start_delay = 0.0  # seconds before an airing add appears in /recording (EPGStation starts it async)
    recorded_deleted = None  # protect / delete (006): ids deleted through DELETE /recorded/{id}
    recorded_protect = None  # {id: bool} set by PUT protect / unprotect (tests flip it directly)
    recorded_patch = None  # test knob: {id: {field: value}} merged into the recording (e.g. isEncoding)
    video_deleted = None  # delete a file (018): video file ids deleted through DELETE /videos/{id}
    streams = None  # transcoded playback (011): {id: {"item", "listed", "keeps"}}
    stream_ready_after = 1  # listings of /api/streams before a stream is isEnable (None: never)
    stream_error = None  # (status, body dict): GET /streams/recorded/{id}/hls answers it
    stream_timer = None  # seconds without a keep before a stream is stopped (EPGStation: 15), None = never
    stream_clock = staticmethod(time.monotonic)  # the timer's clock (tests: the Kodi stub's waited seconds)

    @classmethod
    def reset_reserves(cls):
        data = cls._load("/api/reserves") or {"reserves": []}
        rec = cls._load("/api/recording") or {"records": []}
        cls.reserves_state = copy.deepcopy(data["reserves"])
        cls.recording_state = copy.deepcopy(rec["records"])
        cls.writes = []
        cls.write_error = None
        cls.stop_drops_recording = True
        cls.recording_delay = 0.0
        cls.add_conflict = False
        cls.recording_start_delay = 0.0
        cls.recorded_deleted = set()  # protect / delete (006)
        cls.recorded_protect = {}
        cls.recorded_patch = {}
        cls.video_deleted = set()  # delete a file (018)
        cls.streams = {}  # transcoded playback (011)
        cls.stream_ready_after = 1
        cls.stream_error = None
        cls.stream_timer = None
        cls.stream_clock = staticmethod(time.monotonic)

    @classmethod
    def reset_state(cls):  # protect / delete (006): every write state (reservations and recordings)
        cls.reset_reserves()

    @classmethod
    def _load(cls, rel):
        path = os.path.join(cls.root, rel.lstrip("/") + ".json")
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return json.loads(f.read())

    # --- protect / delete (006) ---------------------------------------------------------------------------------

    def _recorded_view(self, rec):
        """A recording as the server shows it now (protect flag, test patch), or None when deleted."""
        cls = type(self)
        rid = rec["id"]
        if rid in cls.recorded_deleted:
            return None
        if rid in cls.recorded_protect or rid in cls.recorded_patch:
            rec = dict(rec, **cls.recorded_patch.get(rid, {}))
            if rid in cls.recorded_protect:
                rec["isProtected"] = cls.recorded_protect[rid]
        if cls.video_deleted and any(v.get("id") in cls.video_deleted for v in rec.get("videoFiles") or []):
            rec = dict(rec, videoFiles=[v for v in rec["videoFiles"] if v.get("id") not in cls.video_deleted])  # 018
        return rec

    def _views(self, recs):
        return [v for v in (self._recorded_view(r) for r in recs) if v is not None]

    def _all_recorded(self):
        data = self._json_file("/api/recorded") or {"records": []}
        return multiplied(data["records"], self.multiply)

    def _current_recorded(self, rid):
        """(recording now or None, in progress?)."""
        for r in type(self).recording_state:
            if r["id"] == rid:
                return self._recorded_view(r), True
        rec = next((r for r in self._all_recorded() if r["id"] == rid), None)
        return (None if rec is None else self._recorded_view(rec)), False

    def _recorded_write(self, method, parts):
        """PUT /api/recorded/{id}/protect|unprotect and DELETE /api/recorded/{id} (EPGStation's checks)."""
        cls = type(self)
        if cls.write_error is not None:
            status, err = cls.write_error
            return self._send(status, json.dumps(err, ensure_ascii=False).encode())
        rid = int(parts[3])
        rec, live = self._current_recorded(rid)
        if method == "PUT":
            if rec is None:
                return self._send(500, json.dumps(error_500("RecordedIsNull")).encode())
            cls.recorded_protect[rid] = parts[4] == "protect"
            return self._send(200, b'{"code":200}')
        if rec is None:
            return self._send(500, json.dumps(error_500("RecordedIdIsNotFound")).encode())
        if rec.get("isProtected"):
            return self._send(500, json.dumps(error_500("RecordedIsProtected")).encode())
        if live:  # stop and delete
            cls.recording_state[:] = [r for r in cls.recording_state if r["id"] != rid]
        cls.recorded_deleted.add(rid)
        return self._send(200, b'{"code":200}')

    def _video_write(self, vid):  # delete a file (018)
        """DELETE /api/videos/{id} (RecordedManageModel.deleteVideoFile)."""
        cls = type(self)
        if self.multiply > 1:  # copies share the original's video file ids: a delete would hit every copy
            return self._send(405, b'{"code":405,"message":"fake server: DELETE /videos needs multiply 1"}')
        if cls.write_error is not None:
            status, err = cls.write_error
            return self._send(status, json.dumps(err, ensure_ascii=False).encode())
        found = None
        for live, recs in ((True, cls.recording_state), (False, self._all_recorded())):
            for r in recs:
                view = self._recorded_view(r)
                if view is not None and any(v.get("id") == vid for v in view.get("videoFiles") or []):
                    found = found or (view, live)
        if found is None:
            return self._send(500, json.dumps(error_500("VideoFileIsNotFound")).encode())
        rec, live = found
        if rec.get("isProtected"):
            return self._send(500, json.dumps(error_500("RecordedIsProtected")).encode())
        if live:  # in progress: EPGStation stops and deletes the whole recording
            cls.recording_state[:] = [r for r in cls.recording_state if r["id"] != rec["id"]]
            cls.recorded_deleted.add(rec["id"])
            return self._send(200, b'{"code":200}')
        cls.video_deleted.add(vid)
        if len(rec["videoFiles"]) <= 1:  # the last file: the whole recording goes
            cls.recorded_deleted.add(rec["id"])
        return self._send(200, b'{"code":200}')

    # --- transcoded playback (011) -------------------------------------------------------------------------------

    def _video_file(self, vid):
        """(recording, video file) of a video file id now, or (None, None)."""
        for recs in (type(self).recording_state, self._all_recorded()):
            for r in recs:
                view = self._recorded_view(r)
                for v in (view or {}).get("videoFiles") or []:
                    if v.get("id") == vid:
                        return view, v
        return None, None

    def _expire_streams(self):
        """EPGStation's stop timer (stream_timer s after the start or the last keep)."""
        cls = type(self)
        if cls.stream_timer is None:
            return
        now = cls.stream_clock()
        for sid in [k for k, st in cls.streams.items() if now - st["last"] > cls.stream_timer]:
            del cls.streams[sid]

    def _start_stream(self, vid, qs, raw_query):
        cls = type(self)
        self._expire_streams()
        cls.writes.append(("GET", f"/api/streams/recorded/{vid}/hls?{raw_query}", ""))
        if cls.stream_error is not None:
            status, err = cls.stream_error
            return self._send(status, json.dumps(err, ensure_ascii=False).encode())
        if "ss" not in qs or "mode" not in qs:
            return self._send(400, json.dumps({"status": 400, "errors": [{"message": "ss and mode"}]}).encode())
        rec, video = self._video_file(vid)
        if video is None:
            return self._send(500, json.dumps(error_500("VideoFileIsNotFound")).encode())
        kind = "encoded" if video.get("type") == "encoded" else "ts"
        config = self._live("/api/config", {}) or {}
        modes = (((config.get("streamConfig") or {}).get("recorded") or {}).get(kind) or {}).get("hls") or []
        mode = _int(qs, "mode", -1)
        if not 0 <= mode < len(modes):
            return self._send(500, json.dumps(error_500("ConfigIsUndefined")).encode())
        duration = (self._json_file(f"/api/videos/{vid}/duration") or {}).get("duration")
        if duration is not None and _int(qs, "ss") > duration:
            return self._send(500, json.dumps(error_500("OutOfRange")).encode())
        sid = 0
        while sid in cls.streams:
            sid += 1
        item = dict(stream_template(), streamId=sid, mode=mode, isEnable=False, viodeFileId=vid, recordedId=rec["id"],
                    channelId=rec.get("channelId"), name=rec.get("name"), startAt=rec.get("startAt"),
                    endAt=rec.get("endAt"), description=rec.get("description"), extended=rec.get("extended"))
        cls.streams[sid] = {"item": item, "listed": 0, "keeps": 0, "last": cls.stream_clock()}
        return self._send(200, json.dumps({"streamId": sid}).encode())

    def _list_streams(self, qs):
        cls = type(self)
        if "isHalfWidth" not in qs:
            return self._send(400, json.dumps({"status": 400, "errors": [{"message": "isHalfWidth"}]}).encode())
        self._expire_streams()
        out = []
        for sid in sorted(cls.streams):
            st = cls.streams[sid]
            st["listed"] += 1
            ready = cls.stream_ready_after is not None and st["listed"] >= cls.stream_ready_after
            out.append(dict(st["item"], isEnable=ready))
        return self._send(200, json.dumps({"items": out}, ensure_ascii=False).encode())

    def _stream_write(self, method, parts):
        """PUT /api/streams/{id}/keep, DELETE /api/streams/{id} (already recorded in writes)."""
        cls = type(self)
        self._expire_streams()
        sid = int(parts[3])
        if method == "DELETE":
            cls.streams.pop(sid, None)
            return self._send(200, b'{"code":200}')
        if sid not in cls.streams:
            return self._send(500, json.dumps(error_500("StreamIsUndefined")).encode())
        cls.streams[sid]["keeps"] += 1
        cls.streams[sid]["last"] = cls.stream_clock()
        return self._send(200, b'{"code":200}')

    def _streamfile(self, name, head=False):
        cls = type(self)
        self._expire_streams()

        def live(sid):
            return sid.isdigit() and int(sid) in cls.streams and not cls.streams[int(sid)].get("zombie")
        send = self._send_head if head else self._send
        if name.endswith(".m3u8") and name.startswith("stream") and live(name[6:-5]):
            return send(200, playlist_text(int(name[6:-5])), "video/MP2T")
        if name.endswith(".ts") and name.startswith("stream") and "-" in name and live(name[6:].split("-", 1)[0]):
            return send(200, NULL_TS, "video/mpeg")
        return send(404, b"Not Found", "text/html")

    def _send_head(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def do_HEAD(self):  # noqa: N802 - transcoded playback (011): HEAD of a stream file (stale cleanup)
        path = urlparse(self.path).path.rstrip("/")
        self.requests.append(("HEAD " + path, {}))
        parts = path.split("/")
        if len(parts) == 3 and parts[1] == "streamfiles":
            return self._streamfile(parts[2], head=True)
        return self._send_head(404)

    def _find_reserve(self, rid):
        return next((r for r in self.reserves_state if str(r["id"]) == str(rid)), None)

    def _reserves(self, path, qs):
        """Reservation GET routes: (status, body) or None when path is not one of them."""
        cls = type(self)
        if path == "/api/reserves":
            rs = filter_reserves(cls.reserves_state, qs)
            off, lim = _int(qs, "offset", 0), _int(qs, "limit", 24)
            return 200, {"reserves": _texts(rs[off:off + lim], qs), "total": len(rs)}
        if path == "/api/reserves/cnts":
            out = {"normal": 0, "conflicts": 0, "skips": 0, "overlaps": 0}
            for r in cls.reserves_state:
                out[reserve_state(r)] += 1
            return 200, out
        if path == "/api/reserves/lists":
            start, end = _int(qs, "startAt"), _int(qs, "endAt")
            out = {"normal": [], "conflicts": [], "skips": [], "overlaps": []}
            for r in cls.reserves_state:
                if r["startAt"] <= end and r["endAt"] >= start:
                    item = {k: r[k] for k in ("programId", "ruleId") if r.get(k) is not None}
                    out[reserve_state(r)].append(dict(item, reserveId=r["id"]))
            return 200, out
        parts = path.split("/")
        if len(parts) == 4 and parts[2] == "reserves" and parts[3].isdigit():
            if "isHalfWidth" not in qs:
                return 400, {"status": 400, "errors": [{"message": "must have required property 'isHalfWidth'"}]}
            r = self._find_reserve(parts[3])
            if r is None:
                return 404, {"code": 404, "message": "reserve is not found"}
            return 200, _texts([r], qs)[0]
        return None

    def _write(self, method):
        url = urlparse(self.path)
        path = url.path.rstrip("/")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        cls = type(self)
        cls.writes.append((method, path, body))
        if method == "POST" and path == "/api/reserves":  # record from the guide (014)
            return self._add_reserve(body)
        parts = path.split("/")
        if len(parts) >= 4 and parts[2] == "recorded" and parts[3].isdigit() and (  # protect / delete (006)
                (method == "DELETE" and len(parts) == 4)
                or (method == "PUT" and len(parts) == 5 and parts[4] in ("protect", "unprotect"))):
            return self._recorded_write(method, parts)
        if method == "DELETE" and len(parts) == 4 and parts[2] == "videos" and parts[3].isdigit():  # 018
            return self._video_write(int(parts[3]))
        if len(parts) >= 4 and parts[2] == "streams" and parts[3].isdigit() and (  # transcoded playback (011)
                (method == "DELETE" and len(parts) == 4) or (method == "PUT" and parts[4:] == ["keep"])):
            return self._stream_write(method, parts)
        is_reserve = method == "DELETE" and len(parts) in (4, 5) and parts[2] == "reserves" and parts[3].isdigit() \
            and (len(parts) == 4 or parts[4] in ("skip", "overlap"))
        if not is_reserve:
            return self._send(405, b'{"code":405,"message":"fake server: not a supported write"}')
        if cls.write_error is not None:
            status, err = cls.write_error
            return self._send(status, json.dumps(err, ensure_ascii=False).encode())
        r = self._find_reserve(parts[3])
        if r is None:
            return self._send(500, json.dumps(NOT_FOUND_500).encode())
        rule = r.get("ruleId") is not None
        if len(parts) == 4:  # cancel (ReservationManageModel.cancel)
            for rec in [x for x in cls.recording_state if same_program(r, x)] if cls.stop_drops_recording else []:
                cls.recording_state.remove(rec)  # stop: the recorder ends, the file stays a normal recording
            if not rule:
                cls.reserves_state.remove(r)
            else:
                if not r.get("isOverlap"):
                    r["isSkip"] = True
                r["isConflict"] = False
        elif parts[4] == "skip":  # removeSkip
            if rule and r.get("isSkip"):
                r["isSkip"] = False
        elif rule:  # removeOverlap
            r["isOverlap"] = False
        return self._send(200, b"")

    def _guide_program(self, program_id):
        for sch in self._shifted_schedules("schedules"):
            for p in sch["programs"]:
                if p["id"] == program_id:
                    return dict(p, channelId=sch["channel"]["id"])
        return None

    def _add_reserve(self, raw):
        """POST /reserves with a programId (record from the guide, 014): EPGStation's checks in its order."""
        cls = type(self)

        def error(status, name):
            return self._send(status, json.dumps({"code": status, "message": "Internal Server Error",
                                                  "errors": name}).encode())

        if cls.write_error is not None:
            status, err = cls.write_error
            return self._send(status, json.dumps(err, ensure_ascii=False).encode())
        try:
            body = json.loads(raw or "null")
        except ValueError:
            body = None
        if not (isinstance(body, dict) and isinstance(body.get("allowEndLack"), bool)
                and isinstance(body.get("programId"), int) and not isinstance(body["programId"], bool)):
            return self._send(400, json.dumps({"status": 400, "errors": [{"message": "bad request body"}]}).encode())
        pid = body["programId"]
        if any(r.get("programId") == pid for r in cls.reserves_state):
            return error(500, "ReservationManageModelCheckReservedProgramError")
        program = self._guide_program(pid)
        if program is None:
            return error(500, "ProgramIsNotFound")
        if cls.add_conflict:
            return error(500, "ReservationManageModelAddReserveConflict")
        rid = max([r["id"] for r in cls.reserves_state] + [0]) + 1
        r = {k: v for k, v in program.items() if k not in ("id", "isFree")}
        r.update(id=rid, programId=pid, isSkip=False, isConflict=False, isOverlap=False, isTimeSpecified=False,
                 allowEndLack=body["allowEndLack"], isDeleteOriginalAfterEncode=False)
        if body.get("encodeOption"):
            r["encodeOption"] = body["encodeOption"]
        cls.reserves_state.append(r)
        if r["startAt"] <= self._now_ms() < r["endAt"]:  # airing: EPGStation starts recording at once
            rec_id = max([x["id"] for x in cls.recording_state] + [99900]) + 1
            rec = {k: r[k] for k in ("channelId", "programId", "startAt", "endAt", "name", "description",
                                     "extended", "genre1", "subGenre1") if k in r}
            rec.update(id=rec_id, isRecording=True, isEncoding=False, isProtected=False, videoFiles=[],
                       thumbnails=[])
            if cls.recording_start_delay:
                rec["_visibleAt"] = time.time() + cls.recording_start_delay
            cls.recording_state.append(rec)
        return self._send(201, json.dumps({"reserveId": rid}).encode())

    def _now_ms(self):
        return int(self.now if self.now is not None else time.time() * 1000)

    def _shifted_schedules(self, name):
        """tests/fixtures/live/<name>.json with every program moved by now - capturedAt (deep copy)."""
        data = merged_live(name)
        meta = live_file("live_meta") or {}
        now = self._now_ms()
        shift = now - meta.get("capturedAt", now)
        data = copy.deepcopy(data)
        for s in data:
            for p in s["programs"]:
                for key in ("startAt", "endAt"):
                    if key in p:
                        p[key] += shift
        return data

    def _live(self, path, qs):
        """Live TV routes, or None when path is not one of them."""
        if path == "/api/schedules":
            start, end = _int(qs, "startAt"), _int(qs, "endAt")
            types = {t for t in ("GR", "BS", "CS", "SKY") if _bool(qs, t)}
            out = []
            for s in self._shifted_schedules("schedules"):
                if s["channel"]["channelType"] not in types:
                    continue
                progs = [p for p in s["programs"] if p["endAt"] > start and p["startAt"] <= end]
                if _bool(qs, "isFree"):
                    progs = [p for p in progs if p.get("isFree")]
                out.append(dict(s, programs=progs))
            return _schedule_texts(out, qs)
        if path == "/api/schedules/broadcasting":
            return _schedule_texts(self._shifted_schedules("broadcasting"), qs)
        parts = path.split("/")
        if len(parts) == 4 and parts[2] == "schedules" and parts[3].isdigit():
            # one Schedule per day window, overlap inclusive like findSchedule (program guide, 013)
            start = _int(qs, "startAt")
            out = []
            for s in self._shifted_schedules("schedules"):
                if s["channel"]["id"] != int(parts[3]):
                    continue
                for k in range(max(1, _int(qs, "days", 1))):
                    ws, we = start + k * DAY_MS, start + (k + 1) * DAY_MS
                    out.append(dict(s, programs=[p for p in s["programs"] if p["endAt"] >= ws and p["startAt"] <= we]))
            return _schedule_texts(out, qs)
        if len(parts) == 5 and parts[2:4] == ["schedules", "detail"]:  # program guide (013)
            for s in self._shifted_schedules("schedules"):
                for p in s["programs"]:
                    if str(p["id"]) == parts[4]:
                        return _texts([p], qs)[0]
            return self.NOT_FOUND
        if path == "/api/config":
            data = self._json_file(path) or {}
            data.update(self.config_patch or {})
            return data
        if path == "/api/channels":  # the captured free channels plus the synthetic paid ones (020)
            data = self._json_file(path)
            return None if data is None else data + synthetic_paid()["channels"]
        return None

    def _stream_live(self):
        if self.live_error is not None:
            return self._send(500, json.dumps(self.live_error, ensure_ascii=False).encode())
        if os.path.isfile(SAMPLE_VIDEO):
            with open(SAMPLE_VIDEO, "rb") as f:
                return self._send(200, f.read(), "video/mp2t")
        return self._send(200, NULL_TS, "video/mp2t")

    def log_message(self, fmt, *args):  # quiet by default
        if os.environ.get("FAKE_EPGS_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_file(self, rel):
        path = os.path.join(self.root, rel.lstrip("/") + ".json")
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return json.loads(f.read())

    def do_GET(self):  # noqa: N802 - http.server API
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        path = url.path.rstrip("/")
        self.requests.append((path, qs))

        reserves = self._reserves(path, qs)  # reservations (007)
        if reserves is not None:
            return self._send(reserves[0], json.dumps(reserves[1], ensure_ascii=False).encode())

        if path in ("/api/recorded", "/api/recording"):
            data = self._json_file(path) if path == "/api/recorded" else {"records": self._views([
                {k: v for k, v in r.items() if k != "_visibleAt"} for r in self.recording_state
                if r.get("_visibleAt", 0) <= time.time()])}
            data = data or {"records": []}
            off = int(qs.get("offset", ["0"])[0])
            lim = int(qs.get("limit", ["24"])[0])
            if path == "/api/recorded":
                recs = self._views(multiplied(data["records"], self.multiply))  # protect / delete (006)
                if self.mutate:
                    recs = type(self).mutate(recs)
                recs = filter_recorded(recs, qs)
                if lim > 1 and self.delay:
                    time.sleep(self.delay)
            else:
                recs = data["records"]
                if self.writes and self.recording_delay:
                    time.sleep(self.recording_delay)
            body = {"records": _texts(recs[off:off + lim], qs), "total": len(recs)}
            return self._send(200, json.dumps(body, ensure_ascii=False).encode())

        if path.startswith("/api/streams/live/") and path.endswith("/m2ts"):
            return self._stream_live()
        parts = path.split("/")  # transcoded playback (011)
        if len(parts) == 6 and parts[2:4] == ["streams", "recorded"] and parts[4].isdigit() and parts[5] == "hls":
            return self._start_stream(int(parts[4]), qs, url.query)
        if path == "/api/streams":
            return self._list_streams(qs)
        if len(parts) == 3 and parts[1] == "streamfiles":
            return self._streamfile(parts[2])
        live = self._live(path, qs)
        if live is self.NOT_FOUND:  # program guide (013)
            return self._send(404, b'{"code":404,"message":"program is not found"}')
        if live is not None:
            return self._send(200, json.dumps(live, ensure_ascii=False).encode())

        if path.startswith("/api/dropLogs/"):  # full detail view (002): captured text, 404 / 416 like 2.10.0
            # The real server: no size limit when maxsize is missing (the fake uses 512, the addon always sends it);
            # HTTP 500 when the DB row exists but the log file is gone (the fake has no such case: 404 only).
            if self.droplog_body is not None:
                return self._send(200, self.droplog_body, "text/plain; charset=utf-8")
            txt = os.path.join(self.root, path.lstrip("/") + ".txt")
            if not os.path.isfile(txt):
                return self._send(404, b'{"code":404,"message":"drop log file is not Found"}')
            with open(txt, "rb") as f:
                data = f.read()
            if len(data) > _int(qs, "maxsize", 512) * 1024:
                return self._send(416, b'{"code":416,"message":"log file is too large"}')
            return self._send(200, data, "text/plain; charset=utf-8")

        if path.startswith("/api/thumbnails/"):
            jpg = os.path.join(self.root, path.lstrip("/") + ".jpg")
            if os.path.isfile(jpg):
                with open(jpg, "rb") as f:
                    return self._send(200, f.read(), "image/jpeg")
            return self._send(404, b'{"code":404}')

        if path.startswith("/api/videos/") and path.count("/") == 3:
            if os.path.isfile(SAMPLE_VIDEO):
                with open(SAMPLE_VIDEO, "rb") as f:
                    return self._send(200, f.read(), "video/mp2t")
            return self._send(404, b'{"code":404}')

        if path == "/api/recorded/options":
            data = copy.deepcopy(self._json_file(path))
            for key in ("channels", "genres"):
                for c in (data or {}).get(key, []):
                    c["cnt"] *= self.multiply
            return self._send(200, json.dumps(data, ensure_ascii=False).encode())

        copy_k = 0
        if path.startswith("/api/recorded/") and path.count("/") == 3 and path.rsplit("/", 1)[1].isdigit():
            rid = int(path.rsplit("/", 1)[1])
            copy_k = rid // COPY_ID
            if copy_k and copy_k < self.multiply:
                path = f"/api/recorded/{rid % COPY_ID}"
        data = self._json_file(path)
        if data is None and path.startswith("/api/recorded/") and path.rsplit("/", 1)[1].isdigit():
            # reservations (007): an in-progress recording (only in /recording) also answers /recorded/{id}
            data = next((r for r in self.recording_state if str(r["id"]) == path.rsplit("/", 1)[1]), None)
        if data is None:
            return self._send(404, b'{"code":404,"message":"not captured"}')
        if path.startswith("/api/recorded/") and path.count("/") == 3 and isinstance(data, dict) and "name" in data:
            if copy_k:
                data = multiplied([data], copy_k + 1)[-1]
            data = self._recorded_view(data)  # protect / delete (006)
            if data is None:
                return self._send(404, b'{"code":404,"message":"recorded is not Found"}')
            data = _texts([data], qs)[0]
        return self._send(200, json.dumps(data, ensure_ascii=False).encode())

    def do_POST(self):  # noqa: N802
        self._write("POST")

    def do_PUT(self):  # noqa: N802
        self._write("PUT")

    def do_DELETE(self):  # noqa: N802
        self._write("DELETE")


def _handler(root, multiply=1):
    handler = type("BoundHandler", (Handler,), {"root": os.path.abspath(root), "requests": [], "multiply": multiply})
    handler.reset_reserves()
    return handler


def serve(root, host="127.0.0.1", port=0, multiply=1):
    """Start in a background thread; returns (server, base_url). Used by tests."""
    handler = _handler(root, multiply)
    srv = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://{host}:{srv.server_address[1]}"
    REQUESTS[url] = handler.requests
    HANDLERS[url] = handler
    return srv, url


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="fixture directory (contains api/)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8889)
    ap.add_argument("--multiply", type=int, default=1, help="serve every recording N times (copies k weeks earlier)")
    args = ap.parse_args()
    handler = _handler(args.root, max(1, args.multiply))
    print(f"fake EPGStation serving {args.root} x{handler.multiply} on http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
