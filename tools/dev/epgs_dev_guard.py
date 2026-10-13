#!/usr/bin/env python3
"""Keep the DEV EPGStation inside the limits the user granted (dev VM only!).

- At most MAX_CONCURRENT recordings at once: extra future reservations are removed
  (for rule reservations EPGStation turns the delete into a skip), latest-starting first.
- At most MAX_BYTES of recordings: oldest unprotected, finished recordings are deleted.

Runs every 10 minutes from a systemd timer on the dev VM. Never point this at production.
"""
import json
import sys
import time
import urllib.request

B = "http://localhost:8888/api"
MAX_CONCURRENT = 8
MAX_BYTES = 3 * 1000**4  # 3 TB
DRY = "--dry-run" in sys.argv


def call(method, path):
    req = urllib.request.Request(B + path, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
    return json.loads(body) if body else None


def log(msg):
    print(time.strftime("%F %T"), ("DRY " if DRY else "") + msg, flush=True)


def cap_concurrency():
    now = time.time() * 1000
    res = [r for r in call("GET", "/reserves?isHalfWidth=true&type=normal&limit=5000")["reserves"]
           if r["endAt"] > now and not r.get("isSkip")]
    removed = set()
    for r in sorted(res, key=lambda r: r["startAt"]):
        live = [o for o in res if o["id"] not in removed and o["startAt"] < r["endAt"] and o["endAt"] > r["startAt"]]
        # Check concurrency at each start instant inside r's window.
        for t in sorted({max(o["startAt"], r["startAt"]) for o in live}):
            at = [o for o in live if o["startAt"] <= t < o["endAt"]]
            while len(at) > MAX_CONCURRENT:
                victim = max((o for o in at if o["startAt"] > now), key=lambda o: o["startAt"], default=None)
                if victim is None:
                    break  # all already recording; nothing safe to remove
                log(f"concurrency {len(at)} > {MAX_CONCURRENT}: remove reserve {victim['id']} {victim['name']}")
                if not DRY:
                    call("DELETE", f"/reserves/{victim['id']}")
                removed.add(victim["id"])
                at.remove(victim)


def size(rec):
    return sum(v.get("size") or 0 for v in rec.get("videoFiles") or [])


def cap_storage():
    recs, offset = [], 0
    while True:
        page = call("GET", f"/recorded?isHalfWidth=true&isReverse=true&offset={offset}&limit=200")
        recs += page["records"]
        offset += 200
        if offset >= page["total"]:
            break
    total = sum(size(r) for r in recs)
    log(f"storage {total / 1e12:.2f} TB in {len(recs)} recordings (limit {MAX_BYTES / 1e12:.1f} TB)")
    for r in sorted(recs, key=lambda r: r["startAt"]):  # oldest first
        if total <= MAX_BYTES * 0.95:
            break
        if r.get("isProtected") or r.get("isRecording") or r.get("isEncoding"):
            continue
        log(f"delete recorded {r['id']} {r['name']} ({size(r) / 1e9:.1f} GB)")
        if not DRY:
            call("DELETE", f"/recorded/{r['id']}")
        total -= size(r)


if __name__ == "__main__":
    cap_concurrency()
    cap_storage()
