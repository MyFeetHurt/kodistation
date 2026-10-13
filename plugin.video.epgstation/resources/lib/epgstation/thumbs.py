"""Blank (black) thumbnail detection with a persistent cache (backlog 003; stdlib only, no Kodi imports).

EPGStation makes one JPEG frame per recording (thumbnailPosition seconds in). A black or near-blank frame
compresses to very few bytes: on the dev and prod servers every black 480x270 thumbnail is <= 0.0272 bytes per
pixel and everything with content >= 0.0287, so a thumbnail is blank when its size is below BLANK_BPP x width x
height (dimensions from the JPEG's SOF marker; no decoder needed, no PIL in Kodi).

A thumbnail id never gets new content (a regenerated one is a new id), so each id is fetched once ever: thumbs.json
in the addon profile caches [bytes, width, height] per id (raw values, so BLANK_BPP can change without refetching),
keyed by server URL like durations.json. A fetch the server answers with HTTP 404 or 5xx is cached as {"t": time}
for NEGATIVE_TTL (review L2, like durations' UNKNOWN_TTL): shown, not asked again before then. Timeouts are not
cached. Unknown and failed thumbnails count as good (they are shown). Each list open checks at most MAX_CHECKS unknown
ids within DEADLINE seconds (Budget); the rest are checked on the next open. A warm open makes no request.
"""
import json
import time

from . import store
from .api import ApiError

BLANK_BPP = 0.030
MAX_ENTRIES = 20000
MAX_CHECKS = 100
WORKERS = 4
TIMEOUT = 3
DEADLINE = 1.0
NEGATIVE_TTL = 24 * 3600  # review L2: a thumbnail id that answered 404 / 5xx is asked again after a day
MAX_BYTES = 2 * 1024 * 1024  # review L4: read at most this much of a thumbnail; anything larger is not blank

# Start-of-frame markers carry the frame size; C4 (DHT), C8 (JPG extension) and CC (DAC) are not frames.
_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
_NO_LENGTH = frozenset(range(0xD0, 0xD9)) | {0x01}  # RSTn, SOI, EOI, TEM: markers without a length field


def jpeg_size(data):
    """(width, height) from a JPEG's first SOF marker, or None (not a JPEG, truncated, no SOF before the scan)."""
    if not data or data[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(data)
    while i + 1 < n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in _NO_LENGTH:
            i += 2
            continue
        if i + 4 > n:
            return None
        length = (data[i + 2] << 8) | data[i + 3]
        if marker in _SOF:
            if i + 9 > n:
                return None
            height = (data[i + 5] << 8) | data[i + 6]
            width = (data[i + 7] << 8) | data[i + 8]
            return width, height
        if marker == 0xDA or length < 2:  # start of scan: no frame header came first
            return None
        i += 2 + length
    return None


def is_blank(nbytes, width, height):
    """True when the JPEG has fewer than BLANK_BPP bytes per pixel. Unknown dimensions: never blank."""
    if not width or not height:
        return False
    return nbytes < BLANK_BPP * width * height


def summary(data):
    """(bytes, width, height) of a JPEG body: what the cache keeps. A body over MAX_BYTES (cut off by the capped read)
    gets no dimensions, so it never counts as blank."""
    if len(data) > MAX_BYTES:
        return len(data), 0, 0
    width, height = jpeg_size(data) or (0, 0)
    return len(data), width, height


def blank_data(data):
    """is_blank() of a whole JPEG body."""
    return is_blank(*summary(data))


class Budget:
    """What one list open may still spend on checks: MAX_CHECKS fetches within DEADLINE seconds (shared by every
    check() call of the open)."""

    def __init__(self, checks=None, seconds=None, clock=time.monotonic):
        self.checks = MAX_CHECKS if checks is None else checks
        self.clock = clock
        self.end = clock() + (DEADLINE if seconds is None else seconds)

    def seconds_left(self):
        return self.end - self.clock()


class ThumbCache:
    def __init__(self, path, server_url, now=time.time):
        self.path = path
        self.server = server_url
        self.now = now
        self.dirty = False
        self.d = self._load()  # insertion-ordered (oldest first): str(id) -> [bytes, w, h] or {"t": time} (failed)

    def _load(self):
        """This server's entries in the file ({} when missing, another server's or unreadable)."""
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("server") == self.server:
                return {str(int(k)): _entry(v) for k, v in data["d"].items()}
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            pass
        return {}

    def state(self, thumbnail_id):
        """True blank, False not blank (or failed within NEGATIVE_TTL), None unknown (not checked yet, or the failure
        is older)."""
        v = self.d.get(str(thumbnail_id))
        if isinstance(v, dict):
            return False if 0 <= self.now() - v["t"] < NEGATIVE_TTL else None
        return None if v is None else is_blank(*v)

    def put(self, thumbnail_id, info):
        """info: summary() of the thumbnail, or None: the server answered 404 / 5xx."""
        key = str(thumbnail_id)
        self.d.pop(key, None)
        self.d[key] = {"t": self.now()} if info is None else [int(x) for x in info]
        while len(self.d) > MAX_ENTRIES:
            del self.d[next(iter(self.d))]
        self.dirty = True

    def save(self):
        """Atomic write, only when something changed. Returns False if it could not write."""
        if not self.dirty:
            return True
        # review L3: another plugin run may have saved meanwhile: keep its entries (ours win for the same id)
        merged = {k: v for k, v in self._load().items() if k not in self.d}
        merged.update(self.d)
        while len(merged) > MAX_ENTRIES:
            del merged[next(iter(merged))]
        self.d = merged
        if not store.atomic_write_json(self.path, {"server": self.server, "d": self.d}):
            return False
        self.dirty = False
        return True


def _entry(v):
    if isinstance(v, dict) and isinstance(v.get("t"), (int, float)) and not isinstance(v["t"], bool):
        return {"t": float(v["t"])}
    if not (isinstance(v, list) and len(v) == 3 and all(isinstance(x, int) and not isinstance(x, bool) for x in v)):
        raise ValueError(f"bad thumbs.json entry {v!r}")
    return list(v)


def check(ids, api, cache, budget=None):
    """The blank ones of these thumbnail ids: cached ones, plus unknown ones fetched now within the budget (in the
    given order, at most budget.checks, in parallel until budget's deadline). None ids are ignored."""
    budget = budget or Budget()
    wanted = list(dict.fromkeys(i for i in ids if i is not None))
    unknown = [i for i in wanted if cache.state(i) is None]
    fetch = unknown[:max(0, budget.checks)]
    left = budget.seconds_left()
    if fetch and left > 0:
        budget.checks -= len(fetch)
        try:
            got = api.thumbnails(fetch, workers=WORKERS, timeout=TIMEOUT, deadline=left, reduce=summary,
                                 limit=MAX_BYTES + 1)
        except ApiError:
            got = {}
        for tid, info in got.items():
            cache.put(tid, info)
    return {i for i in wanted if cache.state(i)}


def settle(candidates, api, cache, budget):
    """review M1: the blank thumbnail ids that decide a list whose choice depends on them (TV shows: candidates(blank)
    = each show's newest thumbnail not in blank). Repeats: every candidate already known blank in the cache is marked
    (no request), then the unknown candidates are checked; while new blanks turn up and the budget lasts, the next
    candidates are checked. A run of blank episodes of any length ends on the first good one (or none: the logo)."""
    blank = set()
    while True:
        ids = [i for i in candidates(blank) if i is not None]
        known = {i for i in ids if cache.state(i)}
        if known:
            blank |= known
            continue
        unknown = [i for i in ids if cache.state(i) is None]
        if not unknown or budget.checks <= 0 or budget.seconds_left() <= 0:
            return blank
        found = check(unknown, api, cache, budget)
        if not found:
            return blank
        blank |= found
