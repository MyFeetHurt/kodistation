import json

from resources.lib.epgstation import durations
from resources.lib.epgstation.api import EPGStation

V1 = {"id": 1, "type": "ts", "size": 100}
V2 = {"id": 2, "type": "ts", "size": 200}


class FakeApi:
    def __init__(self, values):
        self.values, self.calls = values, []

    def durations(self, ids):
        self.calls.append(sorted(ids))
        return {i: self.values[i] for i in ids if i in self.values}


def _rec(rid, video, recording=False):
    return {"id": rid, "videoFiles": [video], "isRecording": recording}


def test_cache_round_trip(tmp_path):
    path = str(tmp_path / "d.json")
    c = durations.DurationCache(path, "http://a")
    assert c.get(V1) is None
    c.put(V1, 290.1)
    assert c.save()
    assert durations.DurationCache(path, "http://a").get(V1) == 290.1


def test_save_only_when_dirty(tmp_path):
    path = tmp_path / "d.json"
    durations.DurationCache(str(path), "http://a").save()
    assert not path.exists()


def test_server_change_discards(tmp_path):
    path = str(tmp_path / "d.json")
    c = durations.DurationCache(path, "http://a")
    c.put(V1, 1.0)
    c.save()
    assert durations.DurationCache(path, "http://b").get(V1) is None


def test_corrupt_file_is_empty(tmp_path):
    for content in ("not json", "[1, 2]", '{"server": "http://a", "d": {"1:100": "x"}}'):
        path = tmp_path / "d.json"
        path.write_text(content)
        c = durations.DurationCache(str(path), "http://a")
        assert c.get(V1) is None
        c.put(V1, 3.0)
        assert c.save()
        assert json.loads(path.read_text())["d"] == {"1:100": 3.0}


def test_size_change_is_a_miss(tmp_path):
    c = durations.DurationCache(str(tmp_path / "d.json"), "http://a")
    c.put(V1, 5.0)
    assert c.get(dict(V1, size=101)) is None


def test_cap_drops_oldest(tmp_path):
    path = str(tmp_path / "d.json")
    c = durations.DurationCache(path, "http://a")
    for i in range(durations.MAX_ENTRIES + 10):
        c.put({"id": i, "size": 1}, float(i))
    c.save()
    c = durations.DurationCache(path, "http://a")
    assert len(c.d) == durations.MAX_ENTRIES
    assert c.get({"id": 9, "size": 1}) is None
    assert c.get({"id": 10, "size": 1}) == 10.0


def test_fill_fetches_only_misses(tmp_path):
    c = durations.DurationCache(str(tmp_path / "d.json"), "http://a")
    c.put(V1, 290.1)
    api = FakeApi({2: 750.9, 3: 1.0})
    recs = [_rec(10, V1), _rec(20, V2), _rec(30, {"id": 3, "type": "ts", "size": 1}, recording=True)]
    assert durations.fill(recs, api, c, "encoded") == {10: 290.1, 20: 750.9}
    assert api.calls == [[2]]
    assert c.get(V2) == 750.9
    # second time: nothing to fetch
    assert durations.fill(recs, api, c, "encoded") == {10: 290.1, 20: 750.9}
    assert api.calls == [[2]]


def test_fill_uses_preferred_file(tmp_path):
    c = durations.DurationCache(str(tmp_path / "d.json"), "http://a")
    enc = {"id": 6, "type": "encoded", "size": 10}
    api = FakeApi({1: 1.0, 6: 6.0})
    rec = {"id": 4, "videoFiles": [V1, enc]}
    assert durations.fill([rec], api, c, "encoded") == {4: 6.0}
    assert durations.fill([rec], api, c, "ts") == {4: 1.0}


def test_fetch_failures_not_cached(tmp_path):
    c = durations.DurationCache(str(tmp_path / "d.json"), "http://a")
    api = FakeApi({})
    assert durations.fill([_rec(10, V1)], api, c, "ts") == {}
    assert c.get(V1) is None and not c.dirty
    durations.fill([_rec(10, V1)], api, c, "ts")
    assert api.calls == [[1], [1]]


def test_unknown_length_is_cached(tmp_path):
    path = str(tmp_path / "d.json")
    c = durations.DurationCache(path, "http://a")
    api = FakeApi({1: None})
    assert durations.fill([_rec(10, V1)], api, c, "ts") == {}
    c.save()
    c = durations.DurationCache(path, "http://a")
    assert V1 in c and c.get(V1) is None
    assert durations.fill([_rec(10, V1)], api, c, "ts") == {}
    assert api.calls == [[1]]


def test_api_durations_against_fake_server(fake_server):
    api = EPGStation(fake_server)
    recs = api.recorded(limit=5)["records"]
    ids = [v["id"] for r in recs for v in r["videoFiles"]]
    got = api.durations(ids + [987654])
    assert set(got) == set(ids)
    for vid in ids:
        assert got[vid] == api.get(f"/videos/{vid}/duration")["duration"]
        assert got[vid] is None or got[vid] > 0


def _silent_server():
    """A TCP server that accepts connections and never answers."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    held = []

    def accept():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            held.append(conn)

    threading.Thread(target=accept, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.getsockname()[1]}"


def test_api_durations_has_overall_deadline():
    import time

    srv, url = _silent_server()
    try:
        t = time.monotonic()
        got = EPGStation(url).durations(range(50), deadline=1.0)
        took = time.monotonic() - t
    finally:
        srv.close()
    assert got == {}
    assert took < 2.0


def test_unknown_length_is_asked_again_after_ttl(tmp_path):
    path = str(tmp_path / "d.json")
    clock = [1000000.0]
    c = durations.DurationCache(path, "http://a", now=lambda: clock[0])
    api = FakeApi({1: None})
    durations.fill([_rec(10, V1)], api, c, "ts")
    c.save()
    clock[0] += durations.UNKNOWN_TTL - 60
    c = durations.DurationCache(path, "http://a", now=lambda: clock[0])
    durations.fill([_rec(10, V1)], api, c, "ts")
    assert api.calls == [[1]]
    clock[0] += 120
    api.values[1] = 42.0
    assert durations.fill([_rec(10, V1)], api, c, "ts") == {10: 42.0}
    assert api.calls == [[1], [1]]


def test_old_null_entries_are_refetched(tmp_path):
    path = tmp_path / "d.json"
    path.write_text('{"server": "http://a", "d": {"1:100": null}}')
    assert V1 not in durations.DurationCache(str(path), "http://a")


def test_save_leaves_no_temp_files(tmp_path):
    c = durations.DurationCache(str(tmp_path / "d.json"), "http://a")
    c.put(V1, 1.0)
    assert c.save()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["d.json"]


def test_save_failure_cleans_up(tmp_path):
    c = durations.DurationCache(str(tmp_path / "missing" / "d.json"), "http://a")
    c.put(V1, 1.0)
    assert c.save() is False
