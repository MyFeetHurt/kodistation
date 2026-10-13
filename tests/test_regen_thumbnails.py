"""Backlog 003 D2: tools/dev/regen_thumbnails.py (DEV-only: replaces the thumbnails of finished dev recordings)."""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tool():
    spec = importlib.util.spec_from_file_location("regen_thumbnails", os.path.join(ROOT, "tools", "dev",
                                                                                   "regen_thumbnails.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rec(rid, thumbs, files=(("ts", 10),), recording=False):
    return {"id": rid, "name": f"R{rid}", "thumbnails": list(thumbs), "isRecording": recording,
            "videoFiles": [{"id": rid * 10 + i, "type": t, "size": s, "name": t.upper()}
                           for i, (t, s) in enumerate(files)]}


DEV = "http://epgs-dev.test:8888"
PROD = "http://epgs-prod.test:9984"


ADDRS = {"epgs-dev.test": "192.0.2.20", "epgs-prod.test": "192.0.2.18", "prod-alias.test": "192.0.2.18"}


def fake_getaddrinfo(host, port, *a, **kw):
    """DNS for the example names only (no real lookup in tests)."""
    if host not in ADDRS:
        raise OSError("unknown host")
    return [(2, 1, 6, "", (ADDRS[host], 0))]


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture
def dev_env(monkeypatch):
    monkeypatch.setenv("EPGS_DEV_URL", DEV)
    monkeypatch.setenv("EPGS_PROD_URL", PROD)


@pytest.mark.parametrize("base", [PROD, "http://epgs-prod.test:8888", "http://localhost:8888",
                                  "http://127.0.0.1:8888", "http://epgs-dev.test:8889", "https://epgs-dev.test:8888",
                                  "http://epgs-dev.test:8888/api", "http://epgs-dev.test2:8888", ""])
def test_refuses_every_other_server(base, dev_env):
    tool = _tool()
    with pytest.raises(SystemExit):
        tool.check_base(base)
    with pytest.raises(SystemExit):
        tool.Dev(base)


def test_accepts_only_the_dev_url(dev_env):
    tool = _tool()
    assert tool.check_base(DEV) == DEV
    assert tool.check_base(DEV + "/") == DEV


def test_refuses_everything_without_a_dev_url(monkeypatch):
    """019: no address in the tool; EPGS_DEV_URL unset -> nothing is allowed (no request is sent)."""
    tool = _tool()
    monkeypatch.setattr(tool.urllib.request, "urlopen", lambda *a, **k: pytest.fail("request sent"))
    for base in (DEV, "http://localhost:8888", ""):
        with pytest.raises(SystemExit):
            tool.Dev(base)


def test_refuses_without_a_prod_url_and_by_dns_address(monkeypatch):
    """019 L2: both URLs must be set; a dev URL whose name resolves to the production address is refused."""
    tool = _tool()
    monkeypatch.setenv("EPGS_DEV_URL", DEV)
    with pytest.raises(SystemExit):
        tool.check_base(DEV)  # EPGS_PROD_URL unset
    monkeypatch.setenv("EPGS_PROD_URL", PROD)
    assert tool.check_base(DEV) == DEV
    monkeypatch.setenv("EPGS_DEV_URL", "http://prod-alias.test:8888")
    with pytest.raises(SystemExit):
        tool.check_base("http://prod-alias.test:8888")


def test_refuses_a_dev_url_on_the_production_host(monkeypatch):
    tool = _tool()
    monkeypatch.setenv("EPGS_PROD_URL", PROD)
    monkeypatch.setenv("EPGS_DEV_URL", "http://epgs-prod.test:8888")  # misconfigured: prod host, other port
    with pytest.raises(SystemExit):
        tool.check_base("http://epgs-prod.test:8888")


def test_plan_finished_recordings_only_ts_first():
    tool = _tool()
    records = [rec(1, [5]), rec(2, [7, 9], files=(("encoded", 50), ("ts", 10))), rec(3, []),
               rec(4, [11], recording=True), rec(5, [12], files=()), rec(6, [13])]
    plan, skipped = tool.make_plan(records, live_ids={6}, durations={})
    assert [(p["id"], p["delete"], p["video"]) for p in plan] == [(1, [5], 10), (2, [7, 9], 21), (3, [], 30)]
    assert skipped == {4: "in progress", 5: "no video file", 6: "in progress"}
    largest = rec(7, [1], files=(("encoded", 50), ("encoded", 60)))
    assert tool.make_plan([largest], set(), {})[0][0]["video"] == 71  # no TS: the largest file


def test_plan_skips_files_shorter_than_the_position():
    """L6: a file shorter than thumbnailPosition gets no frame from ffmpeg (exit 234): skipped. The real length
    (GET /videos/{id}/duration) wins over the EPG slot; without one the EPG slot counts."""
    tool = _tool()
    short = dict(rec(1, [5]), startAt=0, endAt=86400 * 1000)  # EPG slot 24 h, real file 40 s (recording 10 on dev)
    epg_short = dict(rec(2, [6]), startAt=0, endAt=30 * 1000)
    ok = dict(rec(3, [7]), startAt=0, endAt=600 * 1000)
    plan, skipped = tool.make_plan([short, epg_short, ok], set(), {10: 40.0, 20: None, 30: 600.0})
    assert [p["id"] for p in plan] == [3] and skipped == {1: "40 s < position 60 s", 2: "30 s < position 60 s"}
    plan, _ = tool.make_plan([short, epg_short, ok], set(), {10: 40.0, 20: None, 30: 600.0}, position=30)
    assert [p["id"] for p in plan] == [1, 2, 3]  # 40 s and 30 s files reach a 30 s position


def test_plan_only_missing():
    tool = _tool()
    plan, skipped = tool.make_plan([rec(1, [5]), rec(2, [])], set(), {}, only_missing=True)
    assert [(p["id"], p["delete"]) for p in plan] == [(2, [])] and skipped == {1: "has a thumbnail"}


class FakeDev:
    """Dev stand-in: /recorded pages from a list, every call recorded; POST adds a new thumbnail id."""

    def __init__(self, records, live=(), fail=(), durations=None):
        self.records = {r["id"]: r for r in records}
        self.live, self.fail, self.sent, self.next_id = list(live), set(fail), [], 1000
        self.durations = durations or {}

    def call(self, method, path, **params):
        self.sent.append((method, path))
        if (method, path) in self.fail:
            raise OSError("boom")
        if method == "GET" and path == "/recorded":
            recs = sorted(self.records.values(), key=lambda r: -r["id"])
            off, lim = params.get("offset", 0), params.get("limit", 1000)
            return {"records": recs[off:off + lim], "total": len(recs)}
        if method == "GET" and path == "/recording":
            return {"records": self.live, "total": len(self.live)}
        if method == "GET" and path.endswith("/duration"):
            return {"duration": self.durations.get(int(path.split("/")[2]), 3600.0)}
        if method == "DELETE":
            tid = int(path.rsplit("/", 1)[1])
            for r in self.records.values():
                if tid in r["thumbnails"]:
                    r["thumbnails"].remove(tid)
            return None
        if method == "POST":
            vid = int(path.rsplit("/", 1)[1])
            r = next(r for r in self.records.values() if any(v["id"] == vid for v in r["videoFiles"]))
            self.next_id += 1
            r["thumbnails"].append(self.next_id)
            return None
        raise AssertionError((method, path))


def test_dry_run_sends_no_writes(capsys):
    tool = _tool()
    dev = FakeDev([rec(1, [5]), rec(2, [])])
    assert tool.run(dev, apply=False) == 0
    assert all(m == "GET" for m, _ in dev.sent)
    out = capsys.readouterr().out
    assert "dry run" in out and "POST /thumbnails/videos/10" in out and "then DELETE /thumbnails/5" in out


def test_apply_posts_first_and_deletes_old_ids_after_the_new_one_appears(capsys):
    tool = _tool()
    dev = FakeDev([rec(1, [5]), rec(2, [7, 9]), rec(3, []), rec(4, [11], recording=True)])
    assert tool.run(dev, apply=True, wait=1, poll=0) == 0
    writes = [(m, p) for m, p in dev.sent if m != "GET"]
    assert writes[:3] == [("POST", "/thumbnails/videos/30"), ("POST", "/thumbnails/videos/20"),
                          ("POST", "/thumbnails/videos/10")]  # every POST before any DELETE
    assert sorted(writes[3:]) == [("DELETE", "/thumbnails/5"), ("DELETE", "/thumbnails/7"), ("DELETE", "/thumbnails/9")]
    assert dev.records[4]["thumbnails"] == [11]  # in progress: untouched
    assert all(len(dev.records[i]["thumbnails"]) == 1 and dev.records[i]["thumbnails"][0] > 1000 for i in (1, 2, 3))
    out = capsys.readouterr().out
    assert "before:" in out and "after:" in out and "every planned recording has exactly one new thumbnail" in out


def test_apply_keeps_the_old_thumbnail_when_no_new_one_comes(capsys):
    tool = _tool()
    dev = FakeDev([rec(1, [5]), rec(2, [6])], fail={("POST", "/thumbnails/videos/10")})
    assert tool.run(dev, apply=True, wait=0, poll=0) == 1
    assert dev.records[1]["thumbnails"] == [5]  # never left without a thumbnail
    assert ("DELETE", "/thumbnails/5") not in dev.sent and ("DELETE", "/thumbnails/6") in dev.sent
    assert "FAIL" in capsys.readouterr().out


def test_apply_skips_short_files_and_only_missing(capsys):
    tool = _tool()
    dev = FakeDev([rec(1, [5]), rec(2, []), rec(3, [])], durations={30: 20.0})
    assert tool.run(dev, apply=True, wait=1, poll=0, only_missing=True) == 0
    assert [(m, p) for m, p in dev.sent if m != "GET"] == [("POST", "/thumbnails/videos/20")]
    assert dev.records[1]["thumbnails"] == [5] and dev.records[3]["thumbnails"] == []


def test_tool_writes_only_thumbnails():
    """The only write calls: DELETE /thumbnails/{id} and POST /thumbnails/videos/{videoFileId}."""
    import re

    src = open(os.path.join(ROOT, "tools", "dev", "regen_thumbnails.py"), encoding="utf-8").read()
    writes = re.findall(r'\.call\("(DELETE|POST|PUT|PATCH)", f?"([^"]*)"', src)
    assert sorted(writes) == [("DELETE", "/thumbnails/{tid}"), ("POST", "/thumbnails/videos/{p['video']}")]
