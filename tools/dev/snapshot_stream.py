#!/usr/bin/env python3
"""DEV-ONLY capture of the recorded HLS stream fixtures (backlog 011): tests/fixtures/streams/.

Refuses every EPGStation but the dev one (tools/dev/test_reserve.check_base: $EPGS_DEV_URL from local.env, never
$EPGS_PROD_URL) and refuses to start while the server has any stream. Starts ONE recorded HLS stream on the newest
finished free-channel (GR / free BS) TS recording of at least 3 minutes, at ss=60 in the server's last ts hls mode,
waits for isEnable (sending PUT /streams/{id}/keep every KEEP_EVERY s, so a start slower than the server's 15 s timer
survives), captures GET /streams?isHalfWidth=true and the playlist text, and always DELETEs the stream
(also on any error), then checks that /streams is empty again. At most MAX_SECONDS from the start GET to the DELETE
(the readiness wait is cut to fit, and nothing is written when the capture took longer). When the start answer has no
streamId (an error or a timeout after the server may have started it), the streams of our file and mode in GET
/streams are DELETEd (the tool refused to start while any stream ran, so they are ours). Prints every request.

    tools/dev/snapshot_stream.py [--base URL] [--out tests/fixtures/streams]      (make snapshot-stream)

Writes (no video file, no .m3u8 file: the public audit refuses them):
    <out>/streams_empty.json   GET /streams?isHalfWidth=true before the start ({"items": []})
    <out>/streams.json         the same with our stream enabled (2.10.0 sends "viodeFileId")
    <out>/playlist.txt         the playlist text at that moment
    <out>/stream_meta.json     {"capturedAt": ms, "videoFileId", "recordedId", "ss", "mode", "readyAfter": s}
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import local_env  # noqa: E402
import snapshot_api  # noqa: E402  (is_free_channel: the one free-channel rule)
import test_reserve  # noqa: E402  (check_base: the one dev-only rule)

ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(ROOT, "tests", "fixtures", "streams")
MAX_SECONDS = 30
READY_TIMEOUT = 20
STOP_RESERVE = 5  # seconds of MAX_SECONDS kept for the playlist GET and the DELETE
KEEP_EVERY = 5  # PUT /streams/{id}/keep while waiting: the server stops a stream 15 s after its start or last keep
MIN_SECONDS = 180
USER_AGENT = "KODIstation-snapshot-stream/1"


def request(base, method, path, params=None, timeout=10):
    url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
    print(f"{method} {url}", flush=True)
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        e.close()
        return e.code, body


def get_json(base, path, params=None):
    status, body = request(base, "GET", path, params)
    if status != 200:
        raise SystemExit(f"GET {path}: HTTP {status} {body[:200]!r}")
    return json.loads(body)


def pick(records, channels):
    """The newest finished free-channel recording with a TS file: (record, video file)."""
    free = {c["id"] for c in channels if snapshot_api.is_free_channel(c)}
    for rec in sorted(records, key=lambda r: r["startAt"], reverse=True):
        if rec.get("isRecording") or rec.get("channelId") not in free:
            continue
        if (rec["endAt"] - rec["startAt"]) // 1000 < MIN_SECONDS:
            continue
        ts = [v for v in rec.get("videoFiles") or [] if v.get("type") == "ts"]
        if ts:
            return rec, ts[0]
    raise SystemExit("no finished free-channel TS recording of >= 3 min")


def _item_file(item):
    return item.get("viodeFileId", item.get("videoFileId"))  # 2.10.0 sends the typo


def cleanup(base, video_id, mode):
    """DELETE every stream of our file and mode (a start without a usable answer); the ids it stopped."""
    try:
        items = get_json(base, "/api/streams", {"isHalfWidth": "true"}).get("items") or []
    except (SystemExit, OSError, ValueError) as e:
        print(f"WARNING: could not list streams for the cleanup: {e}")
        return []
    stopped = []
    for item in items:
        if _item_file(item) == video_id and item.get("mode") == mode:
            status, _ = request(base, "DELETE", f"/api/streams/{int(item['streamId'])}")
            print(f"cleanup: stopped stream {item['streamId']}: HTTP {status}")
            stopped.append(item["streamId"])
    return stopped


def write_json(out, name, obj):
    with open(os.path.join(out, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def capture(base, out):
    empty = get_json(base, "/api/streams", {"isHalfWidth": "true"})
    if empty != {"items": []}:
        raise SystemExit(f"refused: the server has streams now: {empty}")
    channels = get_json(base, "/api/channels")
    rec, video = pick(get_json(base, "/api/recorded", {"isHalfWidth": "false", "limit": 50})["records"], channels)
    config = get_json(base, "/api/config")
    modes = config["streamConfig"]["recorded"]["ts"]["hls"]
    mode, ss = len(modes) - 1, 60
    t0 = time.monotonic()
    try:
        status, body = request(base, "GET", f"/api/streams/recorded/{video['id']}/hls", {"ss": ss, "mode": mode})
        stream_id = json.loads(body)["streamId"] if status == 200 else None
    except (OSError, ValueError, KeyError, TypeError) as e:
        status, body, stream_id = None, repr(e).encode(), None
    if not isinstance(stream_id, int):
        cleanup(base, video["id"], mode)
        raise SystemExit(f"start: HTTP {status} {body[:300]!r} (no streamId)")
    last_keep = t0

    def keep_if_due():
        nonlocal last_keep
        if time.monotonic() - last_keep >= KEEP_EVERY:
            last_keep = time.monotonic()
            status, _ = request(base, "PUT", f"/api/streams/{stream_id}/keep", timeout=3)
            print(f"keep: HTTP {status}")

    try:
        streams, ready_after = None, None
        while time.monotonic() - t0 < min(READY_TIMEOUT, MAX_SECONDS - STOP_RESERVE):
            keep_if_due()
            streams = get_json(base, "/api/streams", {"isHalfWidth": "true"})
            if any(i.get("streamId") == stream_id and i.get("isEnable") for i in streams["items"]):
                ready_after = round(time.monotonic() - t0, 1)
                break
            time.sleep(0.5)
        if ready_after is None:
            raise SystemExit(f"stream {stream_id} not ready in time")
        keep_if_due()
        status, playlist = request(base, "GET", f"/streamfiles/stream{stream_id}.m3u8")
        if status != 200:
            raise SystemExit(f"playlist: HTTP {status}")
        if len(streams["items"]) != 1:
            raise SystemExit(f"refused to write: other streams appeared: {streams}")
        if time.monotonic() - t0 > MAX_SECONDS:
            raise SystemExit(f"refused to write: the capture took more than {MAX_SECONDS} s")
        os.makedirs(out, exist_ok=True)
        write_json(out, "streams_empty.json", empty)
        write_json(out, "streams.json", streams)
        with open(os.path.join(out, "playlist.txt"), "wb") as f:
            f.write(playlist)
        write_json(out, "stream_meta.json", {"capturedAt": int(time.time() * 1000), "videoFileId": video["id"],
                                             "recordedId": rec["id"], "ss": ss, "mode": mode,
                                             "readyAfter": ready_after})
    finally:
        status, body = request(base, "DELETE", f"/api/streams/{stream_id}")
        print(f"stopped stream {stream_id} after {time.monotonic() - t0:.1f} s: HTTP {status} {body[:100]!r}")
    for _ in range(10):
        if get_json(base, "/api/streams", {"isHalfWidth": "true"}) == {"items": []}:
            print(f"/streams is empty; wrote {out}")
            return
        time.sleep(0.5)
    raise SystemExit("WARNING: /streams is not empty after the DELETE")


def main(argv=None):
    local_env.load()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("EPGS_DEV_URL", ""))
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    capture(test_reserve.check_base(args.base), args.out)


if __name__ == "__main__":
    main()
