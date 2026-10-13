"""Backlog 003: blank-thumbnail detection (thumbs.py) and the thumbnail GETs (api.py)."""
import glob
import json
import os
import threading
import time

import pytest
from resources.lib.epgstation import thumbs
from resources.lib.epgstation.api import ApiError, EPGStation

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
DEV_THUMBS = os.path.join(FIX, "dev", "api", "thumbnails")


def _jpg(name, fixture_set="dev"):
    with open(os.path.join(FIX, fixture_set, "api", "thumbnails", name), "rb") as f:
        return f.read()


def test_jpeg_size_on_every_captured_thumbnail():
    paths = glob.glob(os.path.join(FIX, "*", "api", "thumbnails", "*.jpg"))
    assert len(paths) >= 20
    for path in paths:
        with open(path, "rb") as f:
            assert thumbs.jpeg_size(f.read()) == (480, 270), path


def test_jpeg_size_truncated_or_not_jpeg():
    data = _jpg("11.jpg")
    assert thumbs.jpeg_size(data[:2]) is None
    assert thumbs.jpeg_size(data[:30]) is None  # SOF not reached
    assert thumbs.jpeg_size(b"") is None
    assert thumbs.jpeg_size(b"\x89PNG\r\n\x1a\n" + b"\0" * 64) is None
    assert thumbs.jpeg_size(b'{"code":404}') is None
    assert thumbs.jpeg_size(None) is None


def test_jpeg_size_skips_segments_and_fill_bytes():
    # SOI, APP0 (len 4), fill 0xFF, SOF2 (progressive): len 17, precision 8, h=90, w=160
    sof = b"\xff\xc2\x00\x11\x08\x00\x5a\x00\xa0\x03" + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    data = b"\xff\xd8" + b"\xff\xe0\x00\x04ab" + b"\xff" + sof
    assert thumbs.jpeg_size(data) == (160, 90)
    # DHT (0xC4) and DAC (0xCC) are not frames
    assert thumbs.jpeg_size(b"\xff\xd8\xff\xc4\x00\x04\x00\x00" + sof) == (160, 90)


def test_is_blank_threshold_edges():
    assert thumbs.BLANK_BPP == 0.030
    assert thumbs.is_blank(3887, 480, 270)
    assert not thumbs.is_blank(3889, 480, 270)
    assert thumbs.is_blank(3887 * 4 - 10, 960, 540) and not thumbs.is_blank(3888 * 4 + 10, 960, 540)
    assert not thumbs.is_blank(10, 0, 0)  # no SOF found: never blank
    assert not thumbs.is_blank(10, None, None)


def test_fixture_black_frame_is_blank_and_others_not():
    assert thumbs.blank_data(_jpg("22.jpg")) is True  # recording 21: the blank placeholder (019)
    for path in glob.glob(os.path.join(FIX, "*", "api", "thumbnails", "*.jpg")):
        if not path.endswith(os.path.join("dev", "api", "thumbnails", "22.jpg")):
            with open(path, "rb") as f:
                assert thumbs.blank_data(f.read()) is False, path


# --- ThumbCache ---------------------------------------------------------------------------------------------------

def test_cache_put_state_save_and_reload(tmp_path):
    path = str(tmp_path / "thumbs.json")
    c = thumbs.ThumbCache(path, "http://a")
    assert c.state(22) is None
    c.put(22, thumbs.summary(_jpg("22.jpg")))
    c.put(11, thumbs.summary(_jpg("11.jpg")))
    c.put(5, thumbs.summary(b"garbage"))  # no SOF: cached as not blank
    assert (c.state(22), c.state(11), c.state(5)) == (True, False, False)
    assert c.save()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["server"] == "http://a" and data["d"]["5"] == [7, 0, 0]
    assert data["d"]["22"] == [len(_jpg("22.jpg")), 480, 270]
    again = thumbs.ThumbCache(path, "http://a")
    assert (again.state(22), again.state(11), again.state(5), again.state(1)) == (True, False, False, None)
    assert os.listdir(tmp_path) == ["thumbs.json"]  # no temp file left behind


def test_cache_server_change_drops_entries(tmp_path):
    path = str(tmp_path / "thumbs.json")
    c = thumbs.ThumbCache(path, "http://a")
    c.put(22, thumbs.summary(_jpg("22.jpg")))
    c.save()
    assert thumbs.ThumbCache(path, "http://b").state(22) is None


@pytest.mark.parametrize("content", ["", "{", "[]", '{"server": "http://a", "d": 5}',
                                     '{"server": "http://a", "d": {"1": "x"}}',
                                     '{"server": "http://a", "d": {"1": [1]}}'])
def test_cache_corrupt_file_is_empty(tmp_path, content):
    path = tmp_path / "thumbs.json"
    path.write_text(content)
    c = thumbs.ThumbCache(str(path), "http://a")
    assert c.state(1) is None
    c.put(1, thumbs.summary(_jpg("11.jpg")))
    assert c.save()


def test_cache_threshold_applies_on_load(tmp_path, monkeypatch):
    """Raw [bytes, w, h] are stored, so a new threshold needs no refetch."""
    path = str(tmp_path / "thumbs.json")
    c = thumbs.ThumbCache(path, "http://a")
    c.put(11, thumbs.summary(_jpg("11.jpg")))  # a content placeholder, < 64800 B
    c.save()
    assert len(_jpg("11.jpg")) < 0.5 * 480 * 270
    monkeypatch.setattr(thumbs, "BLANK_BPP", 0.5)
    assert thumbs.ThumbCache(path, "http://a").state(11) is True


def test_cache_eviction_drops_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbs, "MAX_ENTRIES", 3)
    c = thumbs.ThumbCache(str(tmp_path / "t.json"), "http://a")
    for i in range(5):
        c.put(i, thumbs.summary(_jpg("11.jpg")))
    assert [c.state(i) for i in range(5)] == [None, None, False, False, False]
    assert thumbs.MAX_ENTRIES == 3 and len(c.d) == 3


def test_cache_save_only_when_dirty_and_failure(tmp_path):
    c = thumbs.ThumbCache(str(tmp_path / "missing-dir" / "t.json"), "http://a")
    assert c.save()  # nothing changed: no write
    c.put(1, thumbs.summary(_jpg("11.jpg")))
    assert c.save() is False  # cannot write: False, nothing left behind
    assert not (tmp_path / "missing-dir").exists()


# --- check() ------------------------------------------------------------------------------------------------------

class FakeApi:
    """api.thumbnails stand-in: answers from a dict (bytes; None = HTTP 404/5xx; missing id = timeout, left out)."""

    def __init__(self, data, delay=0.0):
        self.data, self.delay, self.calls = data, delay, []

    def thumbnails(self, ids, workers=4, timeout=3, deadline=1.0, reduce=None, limit=None):
        ids = list(ids)
        self.calls.append((ids, workers, deadline))
        if self.delay:
            time.sleep(self.delay)
        return {i: (None if self.data[i] is None else (reduce or bytes)(self.data[i])) for i in ids if i in self.data}


def test_check_fetches_unknown_once_and_returns_blank(tmp_path):
    cache = thumbs.ThumbCache(str(tmp_path / "t.json"), "http://a")
    api = FakeApi({22: _jpg("22.jpg"), 11: _jpg("11.jpg")})
    assert thumbs.check([22, 11, 99, 22, None], api, cache) == {22}
    assert [c[:2] for c in api.calls] == [([22, 11, 99], 4)] and 0.9 < api.calls[0][2] <= thumbs.DEADLINE
    assert cache.state(99) is None  # left out (timeout): not cached, shown as a thumbnail
    api.calls.clear()
    assert thumbs.check([22, 11], api, cache) == {22}
    assert api.calls == []  # warm: no request


def test_check_budget_max_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbs, "MAX_CHECKS", 3)
    cache = thumbs.ThumbCache(str(tmp_path / "t.json"), "http://a")
    api = FakeApi({i: _jpg("22.jpg") for i in range(10)})
    budget = thumbs.Budget()
    assert thumbs.check(range(10), api, cache, budget) == {0, 1, 2}
    assert api.calls[0][0] == [0, 1, 2]
    assert thumbs.check(range(10), api, cache, budget) == {0, 1, 2}  # budget spent: cached ones only
    assert len(api.calls) == 1
    assert thumbs.check(range(10), api, cache) == {0, 1, 2, 3, 4, 5}  # the next open checks the next ones


def test_check_budget_deadline(tmp_path):
    cache = thumbs.ThumbCache(str(tmp_path / "t.json"), "http://a")
    now = [100.0]
    budget = thumbs.Budget(checks=100, seconds=1.0, clock=lambda: now[0])
    api = FakeApi({1: _jpg("22.jpg"), 2: _jpg("22.jpg")})
    now[0] = 100.4
    assert thumbs.check([1], api, cache, budget) == {1}
    assert abs(api.calls[0][2] - 0.6) < 1e-9  # the rest of the 1.0 s
    now[0] = 101.0
    assert thumbs.check([2], api, cache, budget) == set()  # no time left: no request
    assert len(api.calls) == 1


def test_check_handles_api_errors(tmp_path):
    class Broken:
        def thumbnails(self, *a, **kw):
            raise ApiError("down")

    cache = thumbs.ThumbCache(str(tmp_path / "t.json"), "http://a")
    assert thumbs.check([1, 2], Broken(), cache) == set()
    assert cache.state(1) is None


def test_thumbs_module_is_stdlib_only():
    src = open(thumbs.__file__, encoding="utf-8").read()
    assert "xbmc" not in src and "import requests" not in src


# --- api.thumbnail / api.thumbnails against the fake server ----------------------------------------------------------

def test_api_thumbnail_get_raw_bytes(fake_server):
    import fake_epgstation

    api = EPGStation(fake_server)
    paths = sorted(glob.glob(os.path.join(fake_epgstation.HANDLERS[fake_server].root, "api", "thumbnails", "*.jpg")))
    tid = int(os.path.basename(paths[0])[:-4])
    with open(paths[0], "rb") as f:
        assert api.thumbnail(tid) == f.read()
    with pytest.raises(ApiError) as e:
        api.thumbnail(987654)
    assert e.value.status == 404
    got = api.thumbnails([tid, 987654])
    assert got == {tid: open(paths[0], "rb").read(), 987654: None}  # L2: HTTP 404 -> None (negative cache)
    assert api.thumbnails([tid], reduce=thumbs.summary) == {tid: thumbs.summary(open(paths[0], "rb").read())}
    req = [p for p, _ in fake_epgstation.REQUESTS[fake_server] if p.startswith("/api/thumbnails/")]
    assert f"/api/thumbnails/{tid}" in req


def test_api_thumbnails_sends_user_agent(monkeypatch):
    from resources.lib.epgstation import api as api_mod

    seen = []

    class Resp:
        def __init__(self, req):
            seen.append(req.get_header("User-agent"))

        def read(self):
            return b"\xff\xd8"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(api_mod.urllib.request, "urlopen", lambda req, timeout=None: Resp(req))
    assert EPGStation("http://x").thumbnails([1, 2]) == {1: b"\xff\xd8", 2: b"\xff\xd8"}
    assert seen == [api_mod.USER_AGENT] * 2 and api_mod.USER_AGENT.startswith("KODIstation/")


def test_api_thumbnails_deadline_and_trip(monkeypatch):
    """Returns within about the deadline; after a timeout no new requests start (same helper as durations)."""
    api = EPGStation("http://x")
    started, lock = [], threading.Lock()

    def slow(tid, timeout=None, limit=None):
        with lock:
            started.append(tid)
        time.sleep(0.5)
        return b"x"

    monkeypatch.setattr(api, "thumbnail", slow)
    t0 = time.monotonic()
    got = api.thumbnails(range(40), workers=4, deadline=0.2)
    assert time.monotonic() - t0 < 0.45
    assert got == {} and len(started) <= 8


def test_api_durations_unchanged_by_refactor(monkeypatch):
    api = EPGStation("http://x")

    def dur(vid, timeout=None):
        if vid == 3:
            raise ApiError("boom")
        return None if vid == 2 else float(vid)

    monkeypatch.setattr(api, "duration", dur)
    assert api.durations([1, 2, 3]) == {1: 1.0, 2: None}
    assert api.durations([]) == {}


# --- review round 1 ------------------------------------------------------------------------------------------------

BLACK = _jpg("22.jpg")
GOOD = _jpg("11.jpg")


def test_summary_caps_large_bodies():
    """L4: workers reduce bodies to (len, w, h); a body over MAX_BYTES counts as not blank."""
    assert thumbs.summary(BLACK) == (len(BLACK), 480, 270)
    big = BLACK + b"\0" * thumbs.MAX_BYTES
    assert thumbs.summary(big) == (len(big), 0, 0)
    assert thumbs.MAX_BYTES == 2 * 1024 * 1024


def test_negative_cache_for_failed_fetches(tmp_path):
    """L2: HTTP 404 / 5xx (None from api.thumbnails) are cached as {"t": time} for NEGATIVE_TTL: shown as the
    thumbnail, not asked again until the TTL ends; timeouts (left out) are not cached."""
    now = [1000.0]
    path = str(tmp_path / "t.json")
    cache = thumbs.ThumbCache(path, "http://a", now=lambda: now[0])
    api = FakeApi({1: None, 2: BLACK})
    assert thumbs.check([1, 2, 3], api, cache) == {2}
    assert cache.state(1) is False and cache.state(3) is None
    assert cache.save()
    data = json.load(open(path, encoding="utf-8"))
    assert data["d"]["1"] == {"t": 1000.0}
    api.calls.clear()
    again = thumbs.ThumbCache(path, "http://a", now=lambda: now[0])
    assert thumbs.check([1, 2], api, again) == {2} and api.calls == []
    now[0] += thumbs.NEGATIVE_TTL + 1
    expired = thumbs.ThumbCache(path, "http://a", now=lambda: now[0])
    assert expired.state(1) is None
    thumbs.check([1], api, expired)
    assert api.calls[0][0] == [1]
    assert thumbs.NEGATIVE_TTL == 24 * 3600


def test_save_merges_with_the_file(tmp_path):
    """L3: two runs that checked different ids both end up in thumbs.json."""
    path = str(tmp_path / "t.json")
    a = thumbs.ThumbCache(path, "http://a")
    b = thumbs.ThumbCache(path, "http://a")
    a.put(1, thumbs.summary(BLACK))
    b.put(2, thumbs.summary(GOOD))
    assert a.save() and b.save()
    c = thumbs.ThumbCache(path, "http://a")
    assert (c.state(1), c.state(2)) == (True, False)
    other = thumbs.ThumbCache(path, "http://b")  # another server: the file's entries are not merged in
    other.put(3, thumbs.summary(GOOD))
    assert other.save()
    assert thumbs.ThumbCache(path, "http://b").state(1) is None


def _show_entries(blacks, goods=()):
    """One show: episodes newest first; ids in blacks are blank thumbnails, goods good ones."""
    ids = list(blacks) + list(goods)
    return [{"id": t, "name": "S", "kind": "tv", "show": "S", "key": "s", "season": None, "episode": t, "version": "",
             "year": None, "startAt": (100 - n) * 100, "ruleId": None, "channelId": 1, "thumb": t, "rec": False}
            for n, t in enumerate(ids)]


def _settle(entries, api, cache, budget=None):
    from resources.lib.epgstation import items

    return thumbs.settle(lambda blank: items.show_thumb_candidates(entries, blank), api, cache,
                         budget or thumbs.Budget())


@pytest.mark.parametrize("n_black", [1, 3, 5])
def test_settle_advances_past_blank_runs(tmp_path, n_black):
    """M1: n blank episodes in a row, then a good one: cold and warm opens both end on the good one."""
    from resources.lib.epgstation import items

    blacks = list(range(90, 90 - n_black, -1))
    entries = _show_entries(blacks, [11])
    data = {**{t: BLACK for t in blacks}, 11: GOOD}
    path = str(tmp_path / "t.json")
    for run in ("cold", "warm", "warm2"):
        api, cache = FakeApi(data), thumbs.ThumbCache(path, "http://a")
        blank = _settle(entries, api, cache)
        cache.save()
        assert items.group_shows(entries, blank)[0]["thumb"]["thumb"] == 11, run
        if run != "cold":
            assert api.calls == [], run  # warm: known blanks marked from the cache, no request


def test_settle_all_blank_ends_on_logo_and_respects_budget(tmp_path):
    from resources.lib.epgstation import items

    blacks = [9, 8, 7, 6]
    entries = _show_entries(blacks)
    blank = _settle(entries, FakeApi({t: BLACK for t in blacks}), thumbs.ThumbCache(str(tmp_path / "t"), "x"))
    assert items.group_shows(entries, blank)[0]["thumb"] is None  # tvshow_folder then shows the logo
    budget = thumbs.Budget(checks=2)
    api = FakeApi({t: BLACK for t in blacks})
    _settle(entries, api, thumbs.ThumbCache(str(tmp_path / "u"), "x"), budget)
    assert sum(len(c[0]) for c in api.calls) == 2 and budget.checks == 0


def test_parallel_pools_are_joined(monkeypatch):
    """L5: pools left running by a deadline are kept, and join_workers() waits for them (Plugin.run calls it after
    the directory / resolved URL is handed to Kodi), so no worker outlives the plugin run."""
    import threading as th

    from resources.lib.epgstation import api as api_mod

    api_mod.join_workers()
    e = EPGStation("http://x")
    release = th.Event()

    def slow(tid, timeout=None, limit=None):
        release.wait(2)
        return b"x"

    monkeypatch.setattr(e, "thumbnail", slow)
    before = th.active_count()
    assert e.thumbnails([1, 2], deadline=0.05) == {}
    assert api_mod.PENDING and th.active_count() > before
    release.set()
    api_mod.join_workers()
    assert api_mod.PENDING == [] and th.active_count() == before


def test_parallel_requests_get_the_remaining_deadline_as_timeout(monkeypatch):
    """L5: a request that starts late gets at most the time left before the deadline as its socket timeout."""
    e = EPGStation("http://x")
    seen = []

    def fetch(tid, timeout=None, limit=None):
        seen.append(timeout)
        time.sleep(0.15)
        return b"x"

    monkeypatch.setattr(e, "thumbnail", fetch)
    e.thumbnails([1, 2], workers=1, timeout=3, deadline=0.5)
    from resources.lib.epgstation import api as api_mod

    api_mod.join_workers()
    assert seen[0] <= 0.5 and len(seen) == 2 and seen[1] <= 0.36
