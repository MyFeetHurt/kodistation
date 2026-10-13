"""Minimal EPGStation 2.x REST client (stdlib only, no Kodi imports).

Spec: docs/api/openapi.json (captured from EPGStation 2.10.0).
"""
import base64
import concurrent.futures
import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Sent with every request: the dev smoke tells the addon's requests apart in EPGStation's access log by it.
USER_AGENT = "KODIstation/1 (plugin.video.epgstation)"

# review L5: every worker pool started by this run; join_workers() waits for them after the plugin handed its answer to
# Kodi, so no worker thread outlives the run into Kodi's interpreter teardown (one LanguageInvoker segfault was seen).
PENDING = []
_PENDING_LOCK = threading.Lock()


def _track(pool):
    with _PENDING_LOCK:
        PENDING.append(pool)


def join_workers():
    """Wait for every worker pool of this run (requests are bounded by their timeouts) and forget them."""
    with _PENDING_LOCK:
        pools = list(PENDING)
        del PENDING[:]
    for pool in pools:
        pool.shutdown(wait=True)


class ApiError(Exception):
    def __init__(self, message, status=None, detail=None):
        super().__init__(message)
        self.status = status  # program guide (013): HTTP status when the server answered with an error
        self.detail = detail  # reservations (007): the server's "errors" / "message" text of a JSON error body


def _error_detail(err):
    """The "errors" (else "message") text of an HTTPError's JSON body, or None."""
    try:
        body = json.loads(err.read(4096) or b"null")
    except (ValueError, OSError, http.client.HTTPException):
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("errors") or body.get("message")
    if isinstance(detail, list):  # validation errors: [{"message": ...}]
        detail = "; ".join(str(d.get("message", d)) if isinstance(d, dict) else str(d) for d in detail)
    return str(detail) if detail else None


class EPGStation:
    def __init__(self, base_url, username="", password="", timeout=15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._auth = None
        if username:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            self._auth = "Basic " + token

    # --- low level -------------------------------------------------------

    def url(self, path, **params):
        """Absolute URL for an API path. None-valued params are dropped."""
        params = {k: _q(v) for k, v in params.items() if v is not None}
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        return f"{self.base_url}/api{path}{query}"

    def _request(self, method, path, params=None, timeout=None, body=None, raw=False, limit=None):
        """One request; the parsed JSON body, or None for an empty body (raw=True: the body bytes as they came; limit:
        read at most that many bytes). ApiError on any failure. body: sent as JSON (Content-Type: application/json)."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url(path, **(params or {})), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", USER_AGENT)
        if self._auth:
            req.add_header("Authorization", self._auth)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read() if limit is None else resp.read(limit)
            if raw:
                return body
            return json.loads(body) if body.strip() else None
        except urllib.error.HTTPError as e:
            detail = _error_detail(e)
            e.close()
            raise ApiError(f"HTTP {e.code} for {path}" + (f": {detail}" if detail else ""), e.code, detail) from e
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            raise ApiError(f"{path}: {e}") from e

    def get(self, path, _timeout=None, _raw=False, _limit=None, _empty_ok=False, **params):
        """GET: the parsed JSON (_raw=True: the body bytes, at most _limit of them, artwork (003)). ApiError also for an
        empty answer, unless _empty_ok (002: an empty raw body comes back as b"")."""
        data = self._request("GET", path, params, _timeout, raw=_raw, limit=_limit)
        if not data and (data is None or _raw) and not (_empty_ok and _raw):
            raise ApiError(f"{path}: empty answer")
        return data

    # --- endpoints ---------------------------------------------------------
    # Recordings come full-width (isHalfWidth=false): the metadata parser needs the original EPG text
    # (full-width ／ and U+3000 separate credits), and items.to_half makes the display text. EPGStation
    # then leaves out rawExtended; items.raw_extended rebuilds it from `extended`.

    def version(self):
        return self.get("/version")["version"]

    def recorded(self, offset=0, limit=50, rule_id=None, channel_id=None, genre=None,
                 keyword=None, reverse=False, half_width=False, timeout=None):
        return self.get(
            "/recorded", _timeout=timeout, isHalfWidth=half_width, offset=offset, limit=limit,
            isReverse=reverse or None, ruleId=rule_id, channelId=channel_id,
            genre=genre, keyword=keyword,
        )

    def recorded_pages(self, total, page=500, workers=4, deadline=15.0):
        """Every page of /recorded (full-width, newest first) for `total` recordings, fetched in parallel.

        Returns the Records responses in offset order (at least page 0: it carries the current total).
        Raises ApiError if a page fails or they are not all in within `deadline` seconds; unfinished
        requests are abandoned (same style as durations()).
        """
        offsets = list(range(0, max(int(total), 1), page))
        timeout = max(0.1, min(self.timeout, deadline))
        end = time.monotonic() + deadline
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(offsets)))
        _track(pool)  # review L5
        try:
            futures = [pool.submit(self.recorded, offset=off, limit=page, timeout=timeout) for off in offsets]
            _, not_done = concurrent.futures.wait(futures, timeout=max(0.0, end - time.monotonic()))
            for fut in not_done:
                fut.cancel()
        finally:
            pool.shutdown(wait=False)
        if not_done:
            raise ApiError(f"/recorded: {len(not_done)} of {len(offsets)} pages not in within {deadline:.0f} s")
        return [fut.result() for fut in futures]  # raises the ApiError of a failed page

    def recorded_item(self, recorded_id, half_width=False):
        return self.get(f"/recorded/{recorded_id}", isHalfWidth=half_width)

    def drop_log(self, drop_log_file_id, maxsize=512, timeout=None):  # full detail view (002): a GET, not a write
        """GET /dropLogs/{dropLogFileId}?maxsize=<KB>: the drop log text (text/plain; "" for an empty log). ApiError
        status 404 (unknown id) or 416 (larger than maxsize KB). The id is RecordedItem.dropLogFile.id (the spec's
        `dropLog`)."""
        body = self.get(f"/dropLogs/{int(drop_log_file_id)}", _timeout=timeout, _raw=True, _empty_ok=True,
                        maxsize=int(maxsize))
        return body.decode("utf-8", "replace")

    def recorded_options(self):
        return self.get("/recorded/options")

    def recording(self, half_width=False, timeout=None):
        return self.get("/recording", _timeout=timeout, isHalfWidth=half_width, offset=0, limit=100)

    def channels(self, timeout=None):
        return self.get("/channels", _timeout=timeout)

    def rules(self):
        return self.get("/rules", type="all", limit=1000)

    def thumbnail_url(self, thumbnail_id):
        return self.url(f"/thumbnails/{thumbnail_id}")

    def logo_url(self, channel_id):
        return self.url(f"/channels/{channel_id}/logo")

    def video_url(self, video_file_id):
        return self.url(f"/videos/{video_file_id}")

    def duration(self, video_file_id, timeout=None):
        """Real length of a video file in seconds (the server runs ffprobe, ~60 ms).

        None when the server cannot tell (it answers {"duration": null} for some broken
        or very large files)."""
        data = self.get(f"/videos/{video_file_id}/duration", _timeout=timeout)
        try:
            secs = data["duration"]
            return None if secs is None else float(secs)
        except (KeyError, TypeError, ValueError) as e:
            raise ApiError(f"bad duration for video {video_file_id}: {data!r}") from e

    def durations(self, ids, workers=4, timeout=5, deadline=3.0):
        """{id: seconds or None} fetched in parallel; ids whose request fails are left out.

        Returns within about `deadline` seconds whatever happens: unfinished requests are
        abandoned, and after the first timed-out request no new ones are started.
        """
        return _parallel(self.duration, ids, workers, timeout, deadline)

    def thumbnail(self, thumbnail_id, timeout=None, limit=None):  # artwork (003): blank-thumbnail check (thumbs.py)
        """GET /thumbnails/{id}: the JPEG bytes (at most `limit`). ApiError (status 404) when it does not exist."""
        return self.get(f"/thumbnails/{thumbnail_id}", _timeout=timeout, _raw=True, _limit=limit)

    def thumbnails(self, ids, workers=4, timeout=3, deadline=1.0, reduce=None, limit=None):
        """{id: JPEG bytes, or reduce(bytes) computed in the worker (review L4: thumbs.summary, so no body is kept)}
        fetched in parallel, like durations(): back within about `deadline` seconds. review L2: an id the server
        answers with HTTP 404 or 5xx maps to None (thumbs.py caches that for a while); other failures (timeouts, no
        connection) are left out."""
        def one(tid, t):
            try:
                data = self.thumbnail(tid, t, limit)
            except ApiError as e:
                if e.status is not None and (e.status == 404 or e.status >= 500):
                    return None
                raise
            return reduce(data) if reduce else data

        return _parallel(one, ids, workers, timeout, deadline)

    # --- live TV (012) -------------------------------------------------------------------------------------------

    def schedules(self, start_at, end_at, types, half_width=False, timeout=None):
        """Schedules: per channel (server order), programs overlapping [start_at, end_at] sorted by startAt.
        types: broadcast types to include ("GR", "BS", "CS", "SKY"); the others are asked as false."""
        flags = {t: t in types for t in ("GR", "BS", "CS", "SKY")}
        return self.get("/schedules", _timeout=timeout, startAt=int(start_at), endAt=int(end_at),
                        isHalfWidth=half_width, **flags)

    def channel_schedule(self, channel_id, start_at, days=1, half_width=False, timeout=None):
        """Schedules (one channel) from start_at for `days` days."""
        return self.get(f"/schedules/{channel_id}", _timeout=timeout, startAt=int(start_at), days=days,
                        isHalfWidth=half_width)

    def schedule_detail(self, program_id, half_width=False, timeout=None):  # program guide (013)
        """One guide program (ScheduleProgramItem); ApiError with status 404 when it is no longer in the guide."""
        return self.get(f"/schedules/detail/{program_id}", _timeout=timeout, isHalfWidth=half_width)

    def config(self, timeout=None):
        return self.get("/config", _timeout=timeout)

    def live_url(self, channel_id, mode):
        """Live m2ts stream; Kodi opens it (the plugin itself never does, except live_probe)."""
        return self.url(f"/streams/live/{channel_id}/m2ts", mode=mode)

    def live_probe(self, channel_id, mode, timeout=10):
        """One diagnostic GET of a live stream that Kodi could not start: (HTTP status, parsed error body or None).

        Reads only the status (and a small error body) and closes the connection at once, which makes
        EPGStation release the stream. (None, None) when the server cannot be reached."""
        req = urllib.request.Request(self.live_url(channel_id, mode), method="GET")
        req.add_header("User-Agent", USER_AGENT)
        if self._auth:
            req.add_header("Authorization", self._auth)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, None
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read(4096))
            except (ValueError, OSError, http.client.HTTPException):
                body = None
            finally:
                e.close()
            return e.code, body if isinstance(body, dict) else None
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            return None, None

    # --- reservations (007) ---------------------------------------------------------------------------------------

    def reserves(self, type="all", half_width=False, offset=0, limit=1000, timeout=None):  # noqa: A002 - API name
        """Reserves {reserves, total} ordered by startAt. Always pass limit: the server's default is 24."""
        return self.get("/reserves", _timeout=timeout, type=type, isHalfWidth=half_width, offset=offset, limit=limit)

    def reserve(self, reserve_id, half_width=False, timeout=None):
        """One ReserveItem; ApiError with status 404 when it no longer exists (isHalfWidth is required)."""
        return self.get(f"/reserves/{int(reserve_id)}", _timeout=timeout, isHalfWidth=half_width)

    def reserve_lists(self, start_at, end_at, timeout=None):
        """{normal, conflicts, skips, overlaps: [{reserveId, programId, ruleId}]} of reservations overlapping
        [start_at, end_at] (inclusive)."""
        return self.get("/reserves/lists", _timeout=timeout, startAt=int(start_at), endAt=int(end_at))

    # --- writes (006, 007, 014, 018): only plugin.do_reserve_action (the reservation DELETEs), plugin.do_record (the
    # POST) and plugin.do_recorded_action (protect / unprotect / delete a recording, delete one file) call these, each
    # after an explicit yes; plugin.do_stream (011) starts, keeps and stops its own transcoded stream (section below) -
    # The reservation DELETEs answer 200 with an empty body; errors (also an unknown id) come back as HTTP 500 with
    # {"errors": "ReservationIsNotFound"} etc. in ApiError.detail.

    def delete(self, path, timeout=None):
        """DELETE an API path (the parsed answer or None). Only the reservation DELETEs below, delete_recorded,
        delete_video_file and stop_stream (011) call it."""
        return self._request("DELETE", path, None, timeout)

    def cancel_reserve(self, reserve_id):
        """DELETE /reserves/{id}: manual -> deleted; rule -> skipped; recording now -> stopped (file kept)."""
        return self.delete(f"/reserves/{int(reserve_id)}")

    def remove_skip(self, reserve_id):
        """DELETE /reserves/{id}/skip: a skipped rule reservation records again."""
        return self.delete(f"/reserves/{int(reserve_id)}/skip")

    def remove_overlap(self, reserve_id):
        """DELETE /reserves/{id}/overlap: a rule reservation already recorded before records anyway."""
        return self.delete(f"/reserves/{int(reserve_id)}/overlap")

    def post(self, path, body, timeout=None):  # record from the guide (014)
        """POST a JSON body to an API path; the parsed answer. Only create_reserve calls it."""
        return self._request("POST", path, None, timeout, body)

    def create_reserve(self, program_id, encode_mode=None):
        """POST /reserves {programId, allowEndLack: true} (+ encodeOption {mode1, isDeleteOriginalAfterEncode: false}
        with a mode from GET /config encode): a manual reservation of a guide program, like the web UI's 予約 button.
        Returns the new reserveId (201 {"reserveId": N}). The server refuses with HTTP 500 and ApiError.detail
        "ReservationManageModelCheckReservedProgramError" (programId already reserved, any state),
        "ProgramIsNotFound" (not in its guide) or "ReservationManageModelAddReserveConflict" (no free tuner):
        nothing is created then. An airing program starts recording at once (from now on)."""
        body = {"programId": int(program_id), "allowEndLack": True}
        if encode_mode:
            body["encodeOption"] = {"mode1": encode_mode, "isDeleteOriginalAfterEncode": False}
        data = self.post("/reserves", body)
        try:
            return int(data["reserveId"])
        except (KeyError, TypeError, ValueError) as e:
            # e.g. a redirect that urllib followed as a GET: the reservation may or may not exist (no status, so the
            # toast is the short connection text, never "HTTP 201")
            raise ApiError(f"/reserves: unexpected answer {str(data)[:200]}", None, "unexpected answer") from e

    def put(self, path, timeout=None):  # protect / delete (006)
        """PUT an API path without a body; the parsed answer ({"code": 200}). Only the two protect methods and
        keep_stream (011) call it."""
        return self._request("PUT", path, None, timeout)

    def protect_recorded(self, recorded_id):
        """PUT /recorded/{id}/protect: never auto-deleted (the storage limit and the dev guard skip it), and EPGStation
        refuses to delete it. 200 {"code": 200}, also when it already is protected; unknown id -> HTTP 500
        ApiError.detail "RecordedIsNull"."""
        return self.put(f"/recorded/{int(recorded_id)}/protect")

    def unprotect_recorded(self, recorded_id):
        """PUT /recorded/{id}/unprotect: the recording can be deleted again (also automatically). Answers as
        protect_recorded."""
        return self.put(f"/recorded/{int(recorded_id)}/unprotect")

    def delete_recorded(self, recorded_id):
        """DELETE /recorded/{id}: the whole recording (every video file, thumbnails, drop log); EPGStation first cancels
        its encodes; one in progress is stopped and deleted. 200 {"code": 200}. HTTP 500 ApiError.detail:
        "RecordedIsProtected" (refused), "RecordedIdIsNotFound" (unknown id), "IPCTimeout" (5 s IPC limit: the server
        keeps deleting)."""
        return self.delete(f"/recorded/{int(recorded_id)}")

    def delete_video_file(self, video_file_id):  # delete a file (018)
        """DELETE /videos/{videoFileId}: that one file and its row; the recording, its thumbnails and drop log stay.
        Encodes are NOT cancelled. On a recording in progress EPGStation stops and deletes the whole recording, and
        deleting its last file deletes the whole recording (the addon never sends either). 200 {"code": 200}. HTTP 500
        ApiError.detail: "RecordedIsProtected" (refused), "VideoFileIsNotFound" (unknown id), "IPCTimeout" (5 s IPC
        limit: the server keeps going)."""
        return self.delete(f"/videos/{int(video_file_id)}")

    # --- transcoded playback (011): plugin.do_stream only. The start is a GET that starts ffmpeg on the server; the
    # keep (PUT) and the stop (DELETE) are the stream's own writes (decision D5), never a recording or a reservation.

    def start_recorded_hls(self, video_file_id, ss, mode, timeout=None):
        """GET /streams/recorded/{videoFileId}/hls?ss=<sec>&mode=<index>: starts a transcode; the streamId (int).
        HTTP 500 ApiError.detail: "OutOfRange", "ConfigIsUndefined", "VideoFileIsNotFound", "VideoIsNull",
        "CreateStreamProcessError" / "EncodeProcessManageModelCreateError" (no free encoder slot)."""
        data = self.get(f"/streams/recorded/{int(video_file_id)}/hls", _timeout=timeout, ss=int(ss), mode=int(mode))
        try:
            return int(data["streamId"])
        except (KeyError, TypeError, ValueError) as e:
            raise ApiError(f"/streams/recorded: unexpected answer {str(data)[:200]}", None, "unexpected answer") from e

    def streams(self, timeout=None):
        """GET /streams?isHalfWidth=true (StreamInfo {items}; isHalfWidth is required)."""
        return self.get("/streams", _timeout=timeout, isHalfWidth=True)

    def keep_stream(self, stream_id, timeout=3):
        """PUT /streams/{id}/keep: 200 {"code":200}; a stopped stream: HTTP 500, ApiError.detail "StreamIsUndefined"."""
        return self.put(f"/streams/{int(stream_id)}/keep", timeout)

    def stop_stream(self, stream_id, timeout=3):
        """DELETE /streams/{id}: stops ffmpeg and deletes the stream files; 200 also for an unknown id."""
        return self.delete(f"/streams/{int(stream_id)}", timeout)

    def stream_playlist_url(self, stream_id, nonce):
        """The HLS playlist (served statically under /streamfiles, not /api); ?k=<nonce> makes the URL ours (the
        static server ignores the query, stream ids are reused)."""
        return f"{self.base_url}/streamfiles/stream{int(stream_id)}.m3u8?k={urllib.parse.quote(str(nonce))}"

    # --- end of writes ---------------------------------------------------------------------------------------------

    def stream_playlist_status(self, stream_id, timeout=3):  # transcoded playback (011): stale cleanup
        """HEAD /streamfiles/stream{id}.m3u8 (a static file, no side effect): the HTTP status (200 while the stream has
        its playlist, 404 once its files are gone), None when the server cannot be reached."""
        req = urllib.request.Request(f"{self.base_url}/streamfiles/stream{int(stream_id)}.m3u8", method="HEAD")
        req.add_header("User-Agent", USER_AGENT)
        if self._auth:
            req.add_header("Authorization", self._auth)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            return None

    def kodi_headers(self):
        """Header suffix for Kodi URLs ('|Authorization=...'), or ''."""
        if not self._auth:
            return ""
        return "|Authorization=" + urllib.parse.quote(self._auth)


def _parallel(fetch, ids, workers, timeout, deadline):
    """{id: fetch(id, timeout)} run in parallel; ids whose request fails are left out (durations, thumbnails).

    Returns within about `deadline` seconds whatever happens: unfinished requests are abandoned, and after the first
    timed-out request no new ones are started.
    """
    ids = list(ids)
    if not ids:
        return {}
    end = time.monotonic() + deadline
    tripped = threading.Event()

    def one(key):
        left = end - time.monotonic()
        if tripped.is_set() or left <= 0:
            raise ApiError("skipped: server timed out or deadline passed")
        try:
            return fetch(key, max(0.05, min(timeout, left)))  # review L5: never past the deadline
        except ApiError as e:
            if _is_timeout(e):
                tripped.set()
            raise

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(ids)))
    _track(pool)  # review L5: joined by join_workers() after the run
    try:
        futures = {pool.submit(one, i): i for i in ids}
        done, not_done = concurrent.futures.wait(futures, timeout=max(0.0, deadline))
        for fut in not_done:
            fut.cancel()
        tripped.set()  # requests already queued but not started return at once
    finally:
        pool.shutdown(wait=False)
    out = {}
    for fut in done:
        try:
            out[futures[fut]] = fut.result()
        except ApiError:
            pass
    return out


def _is_timeout(exc):
    """True if this ApiError came from a network timeout."""
    cause = exc.__cause__
    if isinstance(cause, urllib.error.URLError) and not isinstance(cause, urllib.error.HTTPError):
        cause = cause.reason
    return isinstance(cause, socket.timeout) or "timed out" in str(cause)


def _q(v):
    return ("true" if v else "false") if isinstance(v, bool) else v
