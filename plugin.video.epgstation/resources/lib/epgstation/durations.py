"""Persistent cache of real video file lengths (stdlib only, no Kodi imports).

A finished file never changes length, so (server, videoFileId, size) is an exact key.
Recordings in progress are never fetched or cached: their file is still growing.
The server answers null for files it cannot measure; that is cached as unknown
({"t": <unix time>}) so it is not asked again on every list, and re-asked after
UNKNOWN_TTL. Failed requests are not cached.
"""
import json
import time

from . import store
from .items import default_prefer, pick_video

MAX_ENTRIES = 5000
UNKNOWN_TTL = 7 * 24 * 3600


class DurationCache:
    def __init__(self, path, server_url, now=time.time):
        self.path = path
        self.server = server_url
        self.now = now
        self.dirty = False
        self.d = {}  # insertion-ordered: oldest first; value: seconds or {"t": time} if unknown
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("server") == server_url:
                self.d = {str(k): _load_value(v) for k, v in data["d"].items()}
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            self.d = {}

    @staticmethod
    def key(video):
        return f"{video['id']}:{video.get('size', 0)}"

    def __contains__(self, video):
        """Cached, and if the length was unknown, recently enough not to ask again."""
        v = self.d.get(self.key(video))
        if isinstance(v, dict):
            return self.now() - v["t"] < UNKNOWN_TTL
        return v is not None

    def get(self, video):
        """Seconds, or None if unknown (not cached, or the server could not tell)."""
        v = self.d.get(self.key(video))
        return None if isinstance(v, dict) else v

    def put(self, video, secs):
        key = self.key(video)
        self.d.pop(key, None)
        self.d[key] = {"t": self.now()} if secs is None else float(secs)
        while len(self.d) > MAX_ENTRIES:
            del self.d[next(iter(self.d))]
        self.dirty = True

    def save(self):
        """Atomic write, only when something changed. Returns False if it could not write."""
        if not self.dirty:
            return True
        if not store.atomic_write_json(self.path, {"server": self.server, "d": self.d}):
            return False
        self.dirty = False
        return True


def _load_value(v):
    if isinstance(v, dict):
        return {"t": float(v["t"])}
    if v is None:  # older format: unknown without a time, ask again
        return {"t": 0.0}
    return float(v)


def for_files(video_files, api, cache):
    """{videoFileId: seconds} for these files: cached ones, then one parallel fetch of the misses.
    Files of unknown length are left out."""
    found, misses = {}, {}
    for v in video_files:
        if v in cache:
            found[v["id"]] = cache.get(v)
        else:
            misses[v["id"]] = v
    if misses:
        for vid, secs in api.durations(list(misses)).items():
            cache.put(misses[vid], secs)
            found[vid] = secs
    return {vid: secs for vid, secs in found.items() if secs is not None}


def fill(records, api, cache, prefer):
    """{recordedId: seconds} of the default file of every finished record."""
    picked = {}
    for rec in records:
        if rec.get("isRecording"):
            continue
        video = pick_video(rec.get("videoFiles"), default_prefer(rec, prefer))
        if video is not None:
            picked[rec["id"]] = video
    secs = for_files(list(picked.values()), api, cache)
    return {rid: secs[v["id"]] for rid, v in picked.items() if v["id"] in secs}
