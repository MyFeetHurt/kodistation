#!/usr/bin/env python3
"""DEV-ONLY: regenerate the thumbnails of the finished dev recordings (backlog 003, user decision D2).

After the dev EPGStation's thumbnailPosition changed (D1: 5 -> 60 s, then pm2 restart), the existing thumbnails still
show the old frame. EPGStation has no "replace": POST /thumbnails only fills recordings that have none, and
POST /thumbnails/videos/{videoFileId} adds one more. So per finished recording with a video file:
POST /thumbnails/videos/{videoFileId} (the TS if it has one, else its largest file) first, and only once the new
thumbnail is there DELETE /thumbnails/{id} of each old one (review L6: a recording whose new frame never comes keeps its
old thumbnail). EPGStation queues the new frames (one ffmpeg run each, one after another).

Skipped: recordings in progress (isRecording, or in /recording), without a video file, and those whose file is shorter
than --position (ffmpeg seeks past the end and makes nothing; the real length from GET /videos/{id}/duration, else the
EPG slot). Recordings and video files are never touched.
Refuses every server but $EPGS_DEV_URL (the dev VM, from local.env, backlog 019; not even localhost), always refuses
$EPGS_PROD_URL (also by host), and refuses everything while EPGS_DEV_URL is unset.

    tools/dev/regen_thumbnails.py                 dry run: GET only, prints what it would send
    tools/dev/regen_thumbnails.py --apply         send it, then wait (--wait s) until every planned recording has
                                                  exactly one new thumbnail; prints before/after counts, exit 1 if not
    --position N      the server's thumbnailPosition in seconds (default 60)
    --only-missing    only recordings that have no thumbnail (POST only, nothing deleted)
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "KODIstation-dev-regen-thumbnails/1"
PAGE = 1000


def _url(value):
    return (value or "").strip().rstrip("/")


def _host(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _addresses(host):
    """Every address a host name resolves to (a DNS lookup only, no connection); empty when it does not resolve."""
    if not host:
        return set()
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (OSError, UnicodeError):
        return set()


def _same_server(a, b):
    ha, hb = _host(a), _host(b)
    return ha == hb or bool(_addresses(ha) & _addresses(hb))


def check_base(base):
    """base when it is exactly $EPGS_DEV_URL (and not the production host); SystemExit otherwise."""
    base, dev, prod = _url(base), _url(os.environ.get("EPGS_DEV_URL")), _url(os.environ.get("EPGS_PROD_URL"))
    if not dev or not prod:
        sys.exit("refusing: EPGS_DEV_URL and EPGS_PROD_URL must both be set (~/.config/epgstation-kodi/local.env)")
    if _same_server(base, prod) or _same_server(dev, prod):
        sys.exit(f"refusing {base!r}: that is the production EPGStation")
    if base != dev:
        sys.exit(f"refusing {base!r}: this tool only talks to the dev EPGStation {dev}")
    return base


class Dev:
    def __init__(self, base):
        self.base = check_base(base)

    def call(self, method, path, **params):
        """One request to /api{path}; the parsed JSON answer (None for an empty body). Prints every write."""
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        url = f"{self.base}/api{path}{query}"
        if method != "GET":
            print(f"  {method} {path}", flush=True)
        req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
        return json.loads(body) if body.strip() else None


def finished_records(dev):
    """Every recording of GET /recorded (newest first) and the ids in /recording."""
    out, offset = [], 0
    while True:
        page = dev.call("GET", "/recorded", isHalfWidth="true", offset=offset, limit=PAGE)
        out += page["records"]
        offset += PAGE
        if offset >= page.get("total", 0) or not page["records"]:
            break
    live = {r["id"] for r in dev.call("GET", "/recording", isHalfWidth="true", offset=0, limit=100)["records"]}
    return out, live


def pick_file(video_files):
    """The TS if there is one (EPGStation makes thumbnails from it), else the largest file."""
    ts = [v for v in video_files if v.get("type") == "ts"]
    return max(ts or video_files, key=lambda v: v.get("size", 0))


def file_durations(dev, records, live_ids):
    """{videoFileId: seconds or None} of the file each finished recording would use (GET /videos/{id}/duration)."""
    out = {}
    for r in records:
        if r.get("isRecording") or r["id"] in live_ids or not r.get("videoFiles"):
            continue
        vid = pick_file(r["videoFiles"])["id"]
        try:
            out[vid] = (dev.call("GET", f"/videos/{vid}/duration") or {}).get("duration")
        except (urllib.error.URLError, OSError, ValueError):
            out[vid] = None
    return out


def make_plan(records, live_ids, durations, position=60, only_missing=False):
    """([{"id", "name", "delete": [old thumbnail ids], "video": videoFileId}], {skipped id: reason})."""
    plan, skipped = [], {}
    for r in records:
        if r.get("isRecording") or r["id"] in live_ids:
            skipped[r["id"]] = "in progress"
            continue
        if not r.get("videoFiles"):
            skipped[r["id"]] = "no video file"
            continue
        if only_missing and r.get("thumbnails"):
            skipped[r["id"]] = "has a thumbnail"
            continue
        vid = pick_file(r["videoFiles"])["id"]
        secs = durations.get(vid)
        if secs is None and r.get("startAt") is not None and r.get("endAt") is not None:
            secs = (r["endAt"] - r["startAt"]) / 1000
        if secs is not None and secs < position:
            skipped[r["id"]] = f"{secs:.0f} s < position {position} s"
            continue
        plan.append({"id": r["id"], "name": r.get("name") or "", "delete": list(r.get("thumbnails") or []),
                     "video": vid})
    return plan, skipped


def counts(records):
    """{"0": n, "1": n, "2+": n} recordings by thumbnail count."""
    out = {"0": 0, "1": 0, "2+": 0}
    for r in records:
        n = len(r.get("thumbnails") or [])
        out["0" if n == 0 else "1" if n == 1 else "2+"] += 1
    return out


def current_thumbs(dev):
    records, _ = finished_records(dev)
    return {r["id"]: list(r.get("thumbnails") or []) for r in records}


def run(dev, apply=False, wait=600, poll=5, position=60, only_missing=False):
    records, live = finished_records(dev)
    durations = file_durations(dev, records, live)
    plan, skipped = make_plan(records, live, durations, position, only_missing)
    print(f"before: {len(records)} recordings, thumbnails per recording {counts(records)}; in progress {sorted(live)}")
    print(f"plan: {len(plan)} recordings, {len(plan)} POST + up to {sum(len(p['delete']) for p in plan)} DELETE "
          f"(position {position} s{', only missing' if only_missing else ''}); skipped {skipped}")
    for p in plan:
        print(f"  {p['id']:5} {p['name'][:40]:40} POST /thumbnails/videos/{p['video']}" + "".join(
            f", then DELETE /thumbnails/{t}" for t in p["delete"]))
    if not apply:
        print("dry run: nothing sent (--apply sends it)")
        return 0
    failed = []
    for p in plan:
        try:
            dev.call("POST", f"/thumbnails/videos/{p['video']}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"  FAIL POST for recording {p['id']}: {e}")
            failed.append(p["id"])
    waiting = {p["id"]: p for p in plan if p["id"] not in failed}
    end = time.monotonic() + wait
    while True:
        now = current_thumbs(dev)
        for rid, p in list(waiting.items()):
            if any(t not in p["delete"] for t in now.get(rid, [])):  # the new frame is there: drop the old ones
                for tid in p["delete"]:
                    if tid in now.get(rid, []):
                        dev.call("DELETE", f"/thumbnails/{tid}")
                del waiting[rid]
        if not waiting or time.monotonic() >= end:
            break
        time.sleep(poll)
    now = current_thumbs(dev)
    left = {p["id"]: now.get(p["id"]) for p in plan
            if now.get(p["id"]) is None or len(now[p["id"]]) != 1 or now[p["id"]][0] in p["delete"]}
    after, _ = finished_records(dev)
    print(f"after: {len(after)} recordings, thumbnails per recording {counts(after)}")
    if left:
        print(f"FAIL: {len(left)} planned recordings without exactly one new thumbnail (old ones kept): {left} "
              f"(POST errors: {failed})")
        return 1
    print(f"done: every planned recording has exactly one new thumbnail ({len(plan)})")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import local_env
    local_env.load()
    ap.add_argument("--base", default=os.environ.get("EPGS_DEV_URL", ""))
    ap.add_argument("--apply", action="store_true", help="send the POST/DELETE requests (default: dry run)")
    ap.add_argument("--wait", type=int, default=600, help="seconds to wait for the new thumbnails (--apply)")
    ap.add_argument("--position", type=int, default=60, help="the server's thumbnailPosition in seconds")
    ap.add_argument("--only-missing", action="store_true", help="only recordings without a thumbnail")
    args = ap.parse_args()
    return run(Dev(args.base), apply=args.apply, wait=args.wait, position=args.position,
               only_missing=args.only_missing)


if __name__ == "__main__":
    sys.exit(main())
