"""Backlog 005: the cached library index (library.py) against the fake EPGStation."""
import json
import urllib.request

import fake_epgstation
import pytest
from resources.lib.epgstation import epgmeta, items, library
from resources.lib.epgstation.api import ApiError, EPGStation

WEEK_MS = 7 * 24 * 3600 * 1000


def _get(url, path):
    with urllib.request.urlopen(url + path) as resp:
        return json.loads(resp.read())


# --- fake server --multiply ----------------------------------------------------------------------------------------

def test_fake_multiply_copies_every_recording(fake_server, handler):
    base = _get(fake_server, "/api/recorded?limit=10000")
    handler.multiply = 3
    many = _get(fake_server, "/api/recorded?limit=10000")
    assert many["total"] == 3 * base["total"] == len(many["records"])
    by_id = {r["id"]: r for r in many["records"]}
    for rec in base["records"]:
        for k in (1, 2):
            copy = by_id[rec["id"] + k * 100000]
            assert copy["name"] == rec["name"] and copy["videoFiles"] == rec["videoFiles"]
            assert copy["startAt"] == rec["startAt"] - k * WEEK_MS
            assert copy["endAt"] == rec["endAt"] - k * WEEK_MS
    # still newest first, and the single-record endpoint knows the copies
    starts = [r["startAt"] for r in many["records"]]
    assert starts == sorted(starts, reverse=True)
    rec = base["records"][0]
    one = _get(fake_server, f"/api/recorded/{rec['id'] + 200000}")
    assert one["id"] == rec["id"] + 200000 and one["startAt"] == rec["startAt"] - 2 * WEEK_MS
    # filters and paging work on the copies
    page = _get(fake_server, "/api/recorded?offset=1&limit=2")
    assert page["total"] == many["total"] and [r["id"] for r in page["records"]] == \
        [r["id"] for r in many["records"][1:3]]
    # /recording is live: not multiplied
    assert len(_get(fake_server, "/api/recording")["records"]) == \
        len(json.load(open(handler.root + "/api/recording.json"))["records"])


def test_fake_multiply_scales_option_counts(fake_server, handler):
    base = _get(fake_server, "/api/recorded/options")
    handler.multiply = 2
    opts = _get(fake_server, "/api/recorded/options")
    assert [c["cnt"] for c in opts["channels"]] == [2 * c["cnt"] for c in base["channels"]]
    assert [g["cnt"] for g in opts["genres"]] == [2 * g["cnt"] for g in base["genres"]]


def test_fake_mutate_hook_changes_the_list(fake_server, handler):
    total = _get(fake_server, "/api/recorded?limit=1")["total"]
    handler.mutate = lambda recs: recs[1:]
    assert _get(fake_server, "/api/recorded?limit=1")["total"] == total - 1


def test_fake_serve_multiply_argument():
    import os

    from conftest import FIXTURES

    srv, url = fake_epgstation.serve(os.path.join(FIXTURES, "dev"), multiply=33)
    try:
        base = len(json.load(open(os.path.join(FIXTURES, "dev", "api", "recorded.json")))["records"])
        assert _get(url, "/api/recorded?limit=1")["total"] == 33 * base
    finally:
        srv.shutdown()


# --- library.py ------------------------------------------------------------------------------------------------------


def _log(url):
    return fake_epgstation.REQUESTS[url]


def _paths(url, since=0):
    """Requests since index `since`: probe / page / recording / channels / other."""
    out = []
    for path, qs in _log(url)[since:]:
        if path == "/api/recorded":
            if qs.get("limit") == ["1"]:
                out.append("probe-oldest" if qs.get("isReverse") == ["true"] else "probe")
            else:
                out.append(f"page:{qs['offset'][0]}+{qs['limit'][0]}")
        else:
            out.append(path)
    return out


def _count_parses(monkeypatch):
    calls, real = [], epgmeta.metadata

    def spy(item, on_error=None):
        calls.append(item["id"])
        return real(item, on_error)

    monkeypatch.setattr(epgmeta, "metadata", spy)
    return calls


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _lib(tmp_path, url, clock=None):
    return library.Library(str(tmp_path), url, now=clock or Clock())


def _live_ids(url):
    return {r["id"] for r in _get(url, "/api/recording")["records"]}


def test_slim_entry_fields():
    rec = {"id": 8, "channelId": 5, "startAt": 100, "endAt": 200, "ruleId": 3, "name": "孤独のグルメ　Season6 #8",
           "genre1": 3, "thumbnails": [41, 42], "videoFiles": [], "isRecording": False}
    e = library.slim(rec, epgmeta.metadata(rec))
    assert e == {"id": 8, "name": rec["name"], "kind": "tv", "show": "孤独のグルメ", "key": "孤独のグルメ",
                 "season": 6, "episode": 8, "version": "", "year": None, "startAt": 100, "ruleId": 3,
                 "channelId": 5, "thumb": 42, "rec": False,  # artwork (003): the newest thumbnail (max id)
                 "genres": [[3, None]]}  # subgenres (009)
    e = library.slim(dict(rec, name="", thumbnails=[], ruleId=None, isRecording=True), None)
    assert e["kind"] == "tv" and e["show"] == "" and e["thumb"] is None and e["rec"] is True


def test_cold_load_fetches_all_pages_and_writes_both_files(tmp_path, fake_server, handler):
    api = EPGStation(fake_server)
    total = _get(fake_server, "/api/recorded?limit=1")["total"]
    n = len(_log(fake_server))
    snap = _lib(tmp_path, fake_server).current(api)
    assert _paths(fake_server, n)[:3] == ["probe", "probe-oldest", "/api/recording"]
    assert sorted(_paths(fake_server, n)[3:]) == sorted(["page:0+500", "/api/channels"])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["library-index.json", "library-records.json"]
    live = _live_ids(fake_server)
    assert len(snap.entries) == total + len(live) == len(snap.records)
    assert {e["id"] for e in snap.entries} == set(snap.records)
    assert {e["id"] for e in snap.entries if e["rec"]} == live
    starts = [e["startAt"] for e in snap.entries]
    assert starts == sorted(starts, reverse=True)
    assert snap.channels and not snap.stale
    index = json.load(open(tmp_path / "library-index.json", encoding="utf-8"))
    recs = json.load(open(tmp_path / "library-records.json", encoding="utf-8"))
    assert index["h"] == recs["h"]
    assert index["h"]["server"] == fake_server and index["h"]["code"] == library.CODE_SHA1
    assert len(index["entries"]) == len(recs["records"]) == total  # in-progress ones are never cached


def test_warm_load_is_two_probes_and_recording(tmp_path, fake_server, handler, monkeypatch):
    api = EPGStation(fake_server)
    cold = _lib(tmp_path, fake_server).current(api)
    live = _live_ids(fake_server)
    parses = _count_parses(monkeypatch)
    n = len(_log(fake_server))
    for with_records in (True, False):
        snap = _lib(tmp_path, fake_server).current(api, with_records=with_records)
        assert snap.entries == cold.entries and snap.channels == cold.channels
        assert (snap.records == cold.records) if with_records else set(snap.records) == live
    assert _paths(fake_server, n) == ["probe", "probe-oldest", "/api/recording"] * 2
    assert set(parses) <= live  # finished recordings are not parsed again


def test_signature_change_refreshes_and_parses_only_new(tmp_path, fake_server, handler, monkeypatch):
    api = EPGStation(fake_server)
    handler.mutate = lambda recs: recs[1:]  # newest recording "not finished yet"
    _lib(tmp_path, fake_server).current(api)
    handler.mutate = None
    parses = _count_parses(monkeypatch)
    n = len(_log(fake_server))
    snap = _lib(tmp_path, fake_server).current(api)
    assert "page:0+500" in _paths(fake_server, n)
    newest = _get(fake_server, "/api/recorded?limit=1")["records"][0]
    assert newest["id"] in snap.records
    assert [i for i in parses if i not in _live_ids(fake_server)] == [newest["id"]]


def test_new_video_file_or_thumbnail_refreshes_without_parsing(tmp_path, fake_server, handler, monkeypatch):
    api = EPGStation(fake_server)
    _lib(tmp_path, fake_server).current(api)
    newest = _get(fake_server, "/api/recorded?limit=1")["records"][0]
    for change in ({"videoFiles": newest["videoFiles"] + [{"id": 999999, "name": "x", "type": "encoded",
                                                           "size": 1}]},
                   {"thumbnails": (newest.get("thumbnails") or []) + [999999]}):
        handler.mutate = lambda recs, change=change: [dict(recs[0], **change)] + recs[1:]
        parses = _count_parses(monkeypatch)
        n = len(_log(fake_server))
        snap = _lib(tmp_path, fake_server).current(api)
        assert "page:0+500" in _paths(fake_server, n)
        assert [i for i in parses if i not in _live_ids(fake_server)] == []
        for key, value in change.items():
            assert snap.records[newest["id"]][key] == value
        if "thumbnails" in change:
            assert next(e for e in snap.entries if e["id"] == newest["id"])["thumb"] == 999999  # (003): max id


def test_ttl_expiry_refreshes(tmp_path, fake_server, handler, monkeypatch):
    api = EPGStation(fake_server)
    clock = Clock()
    _lib(tmp_path, fake_server, clock).current(api)
    clock.t += library.TTL - 1
    n = len(_log(fake_server))
    _lib(tmp_path, fake_server, clock).current(api)
    assert _paths(fake_server, n) == ["probe", "probe-oldest", "/api/recording"]
    clock.t += 2
    parses = _count_parses(monkeypatch)
    n = len(_log(fake_server))
    _lib(tmp_path, fake_server, clock).current(api)
    assert "page:0+500" in _paths(fake_server, n)
    assert [i for i in parses if i not in _live_ids(fake_server)] == []


def _is_cold(tmp_path, url, lib=None):
    n = len(_log(url))
    (lib or _lib(tmp_path, url)).current(EPGStation(url))
    return "page:0+500" in _paths(url, n)


def test_header_mismatch_or_broken_files_mean_cold(tmp_path, fake_server, handler, monkeypatch):
    api = EPGStation(fake_server)
    _lib(tmp_path, fake_server).current(api)
    assert not _is_cold(tmp_path, fake_server)
    # another server: cold (and the api still points at the fake one)
    assert _is_cold(tmp_path, fake_server, library.Library(str(tmp_path), fake_server + "/other", now=Clock()))
    assert _is_cold(tmp_path, fake_server)  # back to the right server: written for /other, cold again
    monkeypatch.setattr(library, "CODE_SHA1", "0" * 40)
    assert _is_cold(tmp_path, fake_server)
    assert not _is_cold(tmp_path, fake_server)
    monkeypatch.setattr(library, "FORMAT", library.FORMAT + 1)
    assert _is_cold(tmp_path, fake_server)
    assert not _is_cold(tmp_path, fake_server)
    for name, content in (("library-index.json", "{not json"), ("library-records.json", "[]"),
                          ("library-index.json", None), ("library-records.json", None)):
        path = tmp_path / name
        if content is None:
            path.unlink()
        else:
            path.write_text(content)
        assert _is_cold(tmp_path, fake_server), name
        assert not _is_cold(tmp_path, fake_server)
    # records written by a later refresh than the index (crash between the two writes): cold
    recs = json.load(open(tmp_path / "library-records.json", encoding="utf-8"))
    recs["h"]["sig"] = [0, 0, 0, 0]
    (tmp_path / "library-records.json").write_text(json.dumps(recs))
    assert _is_cold(tmp_path, fake_server)


def test_slow_server_returns_stale_cache_within_deadline(tmp_path, fake_server, handler):
    import time

    api = EPGStation(fake_server)
    clock = Clock()
    warm = _lib(tmp_path, fake_server, clock).current(api)
    clock.t += library.TTL + 1
    handler.delay = 4.0
    lib = _lib(tmp_path, fake_server, clock)
    lib.deadline = 30.0  # with a cache to fall back on, the shorter stale_deadline applies
    lib.stale_deadline = 1.0
    t0 = time.monotonic()
    snap = lib.current(api)
    assert time.monotonic() - t0 < lib.stale_deadline + 1
    assert snap.stale and snap.entries == warm.entries and snap.records == warm.records
    assert lib.warnings


def test_slow_server_without_cache_raises_within_deadline(tmp_path, fake_server, handler):
    import time

    handler.delay = 4.0
    lib = _lib(tmp_path, fake_server)
    lib.deadline = 1.0
    t0 = time.monotonic()
    with pytest.raises(ApiError):
        lib.current(EPGStation(fake_server))
    assert time.monotonic() - t0 < lib.deadline + 1
    assert not (tmp_path / "library-index.json").exists()


def test_unreachable_server_uses_stale_cache_or_raises(tmp_path, fake_server, handler):
    warm = _lib(tmp_path, fake_server).current(EPGStation(fake_server))
    down = EPGStation("http://127.0.0.1:9")
    snap = _lib(tmp_path, fake_server).current(down)
    finished = [e for e in warm.entries if not e["rec"]]
    assert snap.stale and snap.entries == finished
    assert set(snap.records) == {e["id"] for e in finished}
    with pytest.raises(ApiError):
        _lib(tmp_path / "x", fake_server).current(down)


def test_total_changing_between_pages_retries_once(tmp_path, fake_server, handler):
    calls = []

    def flaky(recs):
        calls.append(1)
        return recs[1:] if len(calls) == 1 else recs  # the probe sees one recording less than the pages

    handler.mutate = flaky
    n = len(_log(fake_server))
    snap = _lib(tmp_path, fake_server).current(EPGStation(fake_server))
    assert _paths(fake_server, n).count("page:0+500") == 2
    total = _get(fake_server, "/api/recorded?limit=1")["total"]
    assert len([e for e in snap.entries if not e["rec"]]) == total
    assert not _is_cold(tmp_path, fake_server)


def test_still_inconsistent_after_retry_keeps_result_and_refreshes_next_time(tmp_path, fake_server, handler):
    calls = []

    def unstable(recs):
        calls.append(1)
        return recs[len(calls) % 3:]  # requests see changing totals

    handler.mutate = unstable
    n = len(_log(fake_server))
    snap = _lib(tmp_path, fake_server).current(EPGStation(fake_server))
    assert _paths(fake_server, n).count("page:0+500") == 2
    assert snap.entries and not snap.stale
    handler.mutate = None
    assert _is_cold(tmp_path, fake_server)


def test_pages_of_500_up_to_4_in_parallel(tmp_path, fake_server, handler):
    handler.multiply = 33
    total = _get(fake_server, "/api/recorded?limit=1")["total"]
    n = len(_log(fake_server))
    snap = _lib(tmp_path, fake_server).current(EPGStation(fake_server))
    pages = sorted(p for p in _paths(fake_server, n) if p.startswith("page:"))
    assert pages == sorted(f"page:{off}+500" for off in range(0, total, 500))
    assert len({e["id"] for e in snap.entries if not e["rec"]}) == total


def test_3000_recordings_warm_load_parses_nothing(tmp_path, fake_server, handler, monkeypatch):
    """No wall-clock limit here (flaky on a busy runner): parsing nothing proves the warm path."""
    handler.multiply = 33
    api = EPGStation(fake_server)
    _lib(tmp_path, fake_server).current(api)
    parses = _count_parses(monkeypatch)
    snap = _lib(tmp_path, fake_server).current(api, with_records=False)
    assert set(parses) <= _live_ids(fake_server)
    assert len(snap.entries) == _get(fake_server, "/api/recorded?limit=1")["total"] + len(_live_ids(fake_server))


def test_live_recording_replaces_a_finished_copy(tmp_path, fake_server, handler):
    """A recording listed by both /recorded and /recording (race) appears once, as in progress."""
    live = _get(fake_server, "/api/recording")["records"]
    if not live:
        pytest.skip("nothing recording in this fixture set")
    handler.mutate = lambda recs: [dict(live[0], isRecording=False)] + recs
    snap = _lib(tmp_path, fake_server).current(EPGStation(fake_server))
    assert [e["id"] for e in snap.entries].count(live[0]["id"]) == 1
    assert next(e for e in snap.entries if e["id"] == live[0]["id"])["rec"] is True
    assert snap.records[live[0]["id"]]["isRecording"] is True


def test_show_key():
    def key(name, genre=7):
        rec = {"name": name, "genre1": genre}
        return items.show_key(epgmeta.metadata(rec), rec)

    assert key("午後LIVE ニュースーン", 0) == key("午後ＬＩＶＥニュースーン", 0)
    assert key("[新]魔法学園クロニクル #1「入学の日」") == key("魔法学園クロニクル　＃２［終］") == "魔法学園クロニクル"
    assert items.show_key(None, {"name": "ＡＢＣ　ｄｅｆ"}) == "ABC def"
    assert items.show_key({"show": ""}, {"name": "ｘ"}) == "x"


def test_code_hash_covers_parser_grouping_and_library():
    """A change to epgmeta.py, items.py (show_key, to_half), library.py (_parsed) or genres.py invalidates the cache."""
    import hashlib
    import os

    from resources.lib.epgstation import items as items_mod

    files = [epgmeta.__file__, items_mod.__file__, library.__file__,
             os.path.join(os.path.dirname(library.__file__), "genres.py")]
    assert sorted(map(os.path.abspath, library.CODE_FILES)) == sorted(map(os.path.abspath, files))
    digest = hashlib.sha1()
    for path in library.CODE_FILES:
        with open(path, "rb") as f:
            digest.update(f.read())
    assert library.CODE_SHA1 == digest.hexdigest()


def test_code_hash_changes_with_any_file(tmp_path, monkeypatch):
    paths = []
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text(name)
        paths.append(str(tmp_path / name))
    before = library.code_sha1(paths)
    (tmp_path / "c.py").write_text("changed")
    assert library.code_sha1(paths) != before
    assert library.code_sha1(paths + [str(tmp_path / "missing.py")]) != library.code_sha1(paths)


# --- review fix: a recording that finishes while the oldest is deleted ------------------------------------------

class ListApi:
    """In-memory EPGStation: /recorded (newest first, isReverse), /recording, pages."""
    timeout = 15

    def __init__(self, recorded, live):
        self.recorded_l, self.live, self.calls = recorded, live, []

    def recorded(self, offset=0, limit=50, reverse=False, timeout=None, **kw):
        self.calls.append(("recorded", limit, reverse))
        recs = sorted(self.recorded_l, key=lambda r: r["startAt"], reverse=not reverse)
        return {"records": recs[offset:offset + limit], "total": len(recs)}

    def recording(self, timeout=None):
        self.calls.append(("recording",))
        return {"records": self.live}

    def channels(self, timeout=None):
        return []

    def recorded_pages(self, total, page, workers, deadline):
        self.calls.append(("pages",))
        return [self.recorded(0, 100000)]


def _r(i, start, name, **kw):
    return dict({"id": i, "name": name, "startAt": start, "endAt": start + 10, "channelId": 1, "genre1": 7,
                 "videoFiles": [{"id": i}], "thumbnails": [i], "ruleId": None}, **kw)


def test_finished_recording_and_deleted_oldest_within_ttl(tmp_path):
    """Total and newest id stay the same: A (started before the newest B) finishes while the oldest is deleted."""
    old, newest = _r(1, 100, "古い番組 #1"), _r(3, 300, "短い番組 #1")
    long_ = _r(2, 200, "長い番組 #1", isRecording=True)
    lib = lambda: library.Library(str(tmp_path), "x", now=lambda: 1000.0)  # noqa: E731
    snap = lib().current(ListApi([old, newest], [long_]))
    assert [e["id"] for e in snap.entries] == [3, 2, 1]
    api = ListApi([dict(long_, isRecording=False), newest], [])
    snap = lib().current(api)
    assert [e["id"] for e in snap.entries] == [3, 2]
    assert ("pages",) in api.calls
    assert snap.records[2]["isRecording"] is False


def test_live_recording_that_left_recording_refreshes(tmp_path):
    """A recording in progress at refresh time that is no longer in /recording: refresh (it finished or was
    cancelled), even when the probe signature is unchanged."""
    old = _r(1, 100, "古い番組 #1")
    live = _r(2, 200, "長い番組 #1", isRecording=True)
    lib = lambda: library.Library(str(tmp_path), "x", now=lambda: 1000.0)  # noqa: E731
    lib().current(ListApi([old], [live]))
    header = json.load(open(tmp_path / "library-index.json", encoding="utf-8"))["h"]
    assert header["live"] == [2]
    api = ListApi([old], [live])
    lib().current(api)
    assert ("pages",) not in api.calls  # still recording: warm
    api = ListApi([old], [])
    lib().current(api)
    assert ("pages",) in api.calls


def test_deleted_oldest_changes_the_signature(tmp_path):
    """A deletion of the oldest plus a new finished one keeps total and newest; the oldest id catches it."""
    a, b, c = _r(1, 100, "A #1"), _r(2, 200, "B #1"), _r(3, 300, "C #1")
    lib = lambda: library.Library(str(tmp_path), "x", now=lambda: 1000.0)  # noqa: E731
    lib().current(ListApi([a, c], []))
    api = ListApi([b, c], [])  # 1 deleted, 2 finished (an older start than the newest)
    snap = lib().current(api)
    assert ("pages",) in api.calls
    assert [e["id"] for e in snap.entries] == [3, 2]


def test_warm_open_probes_newest_oldest_and_recording(tmp_path):
    lib = lambda: library.Library(str(tmp_path), "x", now=lambda: 1000.0)  # noqa: E731
    recs = [_r(1, 100, "A #1"), _r(2, 200, "B #1")]
    lib().current(ListApi(recs, []))
    api = ListApi(recs, [])
    lib().current(api)
    assert api.calls == [("recorded", 1, False), ("recorded", 1, True), ("recording",)]


# --- review fix: a cache file with a valid header shape but missing fields is no cache -------------------------

def _edit(path, change):
    data = json.load(open(path, encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("name,change", [
    ("library-index.json", lambda d: d["h"].pop("sig")),
    ("library-index.json", lambda d: d["h"].pop("t")),
    ("library-index.json", lambda d: d["h"].pop("live")),
    ("library-index.json", lambda d: d["h"].update(t="soon")),
    ("library-index.json", lambda d: d["h"].update(sig=[1, 2])),
    ("library-index.json", lambda d: d["entries"][0].pop("startAt")),
    ("library-index.json", lambda d: d["entries"][0].pop("kind")),
    ("library-index.json", lambda d: d["entries"][0].pop("key")),
    ("library-index.json", lambda d: d["entries"][0].update(id="7")),
    ("library-index.json", lambda d: d["entries"].append(None)),
    ("library-index.json", lambda d: d.update(channels=[1])),
    ("library-index.json", lambda d: d.pop("logos")),  # artwork (003)
    ("library-index.json", lambda d: d.update(logos={"1": True})),
    ("library-index.json", lambda d: d.update(logos=["1"])),
    ("library-records.json", lambda d: d["records"][0].pop("endAt")),
    ("library-records.json", lambda d: d["records"].append("x")),
])
def test_cache_with_missing_fields_is_cold(tmp_path, fake_server, handler, name, change):
    _lib(tmp_path, fake_server).current(EPGStation(fake_server))
    _edit(tmp_path / name, change)
    assert _is_cold(tmp_path, fake_server)
    assert not _is_cold(tmp_path, fake_server)


def test_stale_cache_with_missing_fields_raises_api_error(tmp_path, fake_server, handler):
    _lib(tmp_path, fake_server).current(EPGStation(fake_server))
    _edit(tmp_path / "library-index.json", lambda d: d["entries"][0].pop("startAt"))
    with pytest.raises(ApiError):
        _lib(tmp_path, fake_server).current(EPGStation("http://127.0.0.1:9"))


def test_old_cache_gets_the_full_deadline(tmp_path, fake_server, handler):
    """A cache over STALE_MAX_AGE old waits the full deadline, so a slow server can still refresh it."""
    api = EPGStation(fake_server)
    clock = Clock()
    _lib(tmp_path, fake_server, clock).current(api)
    clock.t += library.STALE_MAX_AGE + 1
    handler.delay = 1.5
    lib = _lib(tmp_path, fake_server, clock)
    lib.deadline = 30.0
    lib.stale_deadline = 0.5
    snap = lib.current(api)
    assert not snap.stale


def test_stale_deadline_is_5_seconds():
    assert library.STALE_DEADLINE == 5.0 and library.Library("x", "y").stale_deadline == 5.0
    assert library.DEADLINE == 15.0


# --- artwork (003): logos in the index, FORMAT 4 ----------------------------------------------------------------

def test_index_keeps_logo_channel_ids(tmp_path, fake_server, handler):
    api = EPGStation(fake_server)
    channels = _get(fake_server, "/api/channels")
    want = {c["id"] for c in channels if c.get("hasLogoData")}
    assert want and len(want) < len(channels)  # the fixtures have channels without a logo
    snap = _lib(tmp_path, fake_server).current(api)
    assert snap.logos == want
    index = json.load(open(tmp_path / "library-index.json", encoding="utf-8"))
    assert index["h"]["v"] == library.FORMAT == 5 and sorted(index["logos"]) == sorted(want)
    warm = _lib(tmp_path, fake_server).current(api)
    assert warm.logos == want
    stale = _lib(tmp_path, fake_server).current(EPGStation("http://127.0.0.1:9"))
    assert stale.stale and stale.logos == want


def test_format_3_cache_is_discarded(tmp_path, fake_server, handler):
    _lib(tmp_path, fake_server).current(EPGStation(fake_server))
    for name in ("library-index.json", "library-records.json"):
        _edit(tmp_path / name, lambda d: d["h"].update(v=3))
    _edit(tmp_path / "library-index.json", lambda d: d.pop("logos"))
    assert _is_cold(tmp_path, fake_server)


def test_logos_keep_old_value_when_channels_fail(tmp_path):
    rec = _r(1, 100, "A #1")

    class NoChannels(ListApi):
        def channels(self, timeout=None):
            raise ApiError("down")

    class WithChannels(ListApi):
        def channels(self, timeout=None):
            return [{"id": 1, "name": "N", "hasLogoData": True}, {"id": 2, "name": "M", "hasLogoData": False}]

    lib = lambda: library.Library(str(tmp_path), "x", now=lambda: 1000.0)  # noqa: E731
    assert lib().current(WithChannels([rec], [])).logos == {1}
    assert lib().current(NoChannels([rec, _r(2, 200, "B #1")], [])).logos == {1}
    assert library.Library(str(tmp_path / "other"), "x").current(NoChannels([rec], [])).logos == set()


# --- subgenres (009): slim "genres", FORMAT 5 -----------------------------------------------------------------------

def test_entries_carry_genre_pairs(tmp_path, fake_server, handler):
    from resources.lib.epgstation.genres import genre_pairs

    api = EPGStation(fake_server)
    recs = {r["id"]: r for r in _get(fake_server, "/api/recorded?limit=10000")["records"]}
    recs.update({r["id"]: r for r in _get(fake_server, "/api/recording")["records"]})
    for snap in (_lib(tmp_path, fake_server).current(api), _lib(tmp_path, fake_server).current(api)):  # cold, warm
        assert snap.entries and all(e["genres"] == genre_pairs(recs[e["id"]]) for e in snap.entries)
    # the fixtures have recordings with a genre only in genre2/3 (what /recorded/options leaves out: genre1 only)
    assert any({r.get("genre2"), r.get("genre3")} - {None, r.get("genre1")} for r in recs.values())


def test_format_4_cache_is_rebuilt(tmp_path, fake_server, handler):
    api = EPGStation(fake_server)
    _lib(tmp_path, fake_server).current(api)
    for name in ("library-index.json", "library-records.json"):
        _edit(tmp_path / name, lambda d: d["h"].update(v=4))
    _edit(tmp_path / "library-index.json", lambda d: [e.pop("genres") for e in d["entries"]])
    assert _is_cold(tmp_path, fake_server)
    assert not _is_cold(tmp_path, fake_server)
    assert all("genres" in e for e in _lib(tmp_path, fake_server).current(api).entries)


@pytest.mark.parametrize("bad", [None, "6", [[6]], [[6, "0"]], [["6", 0]], [[True, 0]], [(6, 0, 1)], [[6, 1.5]]])
def test_invalid_genres_in_the_index_mean_cold(tmp_path, fake_server, handler, bad):
    _lib(tmp_path, fake_server).current(EPGStation(fake_server))

    def spoil(d):
        if bad is None:
            d["entries"][0].pop("genres")
        else:
            d["entries"][0]["genres"] = bad
    _edit(tmp_path / "library-index.json", spoil)
    assert _is_cold(tmp_path, fake_server)


def test_valid_pairs_accepts_missing_subgenre():
    assert library._valid_pairs([[3, None], [6, 0]]) and library._valid_pairs([])


# --- invalidate (after a write) --------------------------------------------------------------------------------------

def test_invalidate_refreshes_next_open_and_reuses_parsed_metadata(tmp_path, fake_server, handler, monkeypatch):
    api = EPGStation(fake_server)
    _lib(tmp_path, fake_server).current(api)
    assert library.invalidate(str(tmp_path), fake_server)
    assert not (tmp_path / library.INDEX_FILE).exists() and (tmp_path / library.RECORDS_FILE).exists()
    parses = _count_parses(monkeypatch)
    since = len(_log(fake_server))
    snap = _lib(tmp_path, fake_server).current(api)
    assert "page:0+500" in _paths(fake_server, since)  # a full fetch, not the warm cache
    assert set(parses) <= _live_ids(fake_server)  # finished recordings are not parsed again
    assert (tmp_path / library.INDEX_FILE).exists() and snap.entries


def test_invalidate_leaves_another_servers_cache(tmp_path, fake_server, handler):
    _lib(tmp_path, fake_server).current(EPGStation(fake_server))
    assert library.invalidate(str(tmp_path), "http://other:8888")
    assert (tmp_path / library.INDEX_FILE).exists()


def test_invalidate_without_or_with_a_broken_index(tmp_path):
    assert library.invalidate(str(tmp_path), "http://x")  # nothing there
    (tmp_path / library.INDEX_FILE).write_text("{broken", encoding="utf-8")
    assert library.invalidate(str(tmp_path), "http://x") and not (tmp_path / library.INDEX_FILE).exists()
