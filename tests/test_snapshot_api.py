"""tools/snapshot_api.py (backlog 020): free-channel rule, refusals, --prune-free, drop-log pick, fingerprints,
GET only."""
import io
import json
import os
import urllib.parse
import urllib.request

import pytest
import snapshot_api as snap
from conftest import ROOT

PROD = "http://192.0.2.118:9984"
DEV = "http://192.0.2.202:8888"


def gr(cid=3211240960, sid=40960):
    return {"id": cid, "serviceId": sid, "channelType": "GR", "type": 1, "name": "ＧＲ"}


def bs(sid):
    return {"id": 400000 + sid, "serviceId": sid, "channelType": "BS", "type": 1, "name": f"BS{sid}"}


def cs(cid=700297, sid=297):
    return {"id": cid, "serviceId": sid, "channelType": "CS", "type": 1, "name": "ＣＳ"}


# --- free-channel rule -------------------------------------------------------------------------------------------

def test_is_free_channel():
    assert snap.is_free_channel(gr()) and snap.is_free_channel(gr(3272102056, 2056))
    for sid in snap.FREE_BS_SERVICES:
        assert snap.is_free_channel(bs(sid)), sid
    for sid in (201, 800, 191, 192, 193, 234, 236, 252, 255, 256, 531, 700):  # BS10プレミアム, WOWOW, ...: paid
        assert not snap.is_free_channel(bs(sid)), sid
    assert not snap.is_free_channel(cs()) and not snap.is_free_channel(cs(600055, 55))  # also shopping channels
    assert not snap.is_free_channel({"id": 1, "channelType": "SKY", "serviceId": 101})
    assert snap.free_channel_ids([gr(), bs(141), bs(201), cs()]) == {3211240960, 400141}


# --- refusals: no request is sent --------------------------------------------------------------------------------

@pytest.fixture
def no_request(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("a request was sent")

    monkeypatch.setattr(snap, "get", fail)
    monkeypatch.setattr(urllib.request, "urlopen", fail)


def test_refuses_the_tree_without_dev(no_request, capsys):
    out = os.path.join(ROOT, "tests", "fixtures", "dev2")
    assert snap.main([PROD, out]) == 2
    assert "inside a git work tree" in capsys.readouterr().err
    assert snap.main([DEV, os.path.join(ROOT, "somewhere")]) == 2


def test_refuses_dev_with_the_prod_url(no_request, capsys, tmp_path):
    out = str(tmp_path / "x")
    assert snap.main(["--dev", "--prod-url", PROD, PROD, out]) == 2
    assert "production" in capsys.readouterr().err
    assert snap.main(["--dev", "--prod-url", PROD, "http://192.0.2.118:9984/", out]) == 2
    assert snap.main(["--dev", "--prod-url", PROD + "/", "http://192.0.2.118:9984/api", out]) == 2  # host:port
    assert snap.main(["--dev", "--prod-url", "", DEV, out]) == 2  # no prod URL to guard against
    assert not os.path.exists(out)


def test_check_capture_allows_dev_into_the_tree_and_private_outside(tmp_path, monkeypatch):
    snap.check_capture(DEV, os.path.join(ROOT, "tests", "fixtures", "dev2"), True, PROD)
    snap.check_capture(PROD, str(tmp_path / "prod-snapshot"), False, "")
    (tmp_path / "repo" / ".git").mkdir(parents=True)  # any other git work tree is refused too
    with pytest.raises(snap.RefusedError):
        snap.check_capture(PROD, str(tmp_path / "repo" / "fixtures"), False, "")
    with pytest.raises(snap.RefusedError):
        snap.check_capture("http://192.0.2.118:9984", str(tmp_path / "x"), True, "http://192.0.2.118:9984/")


def test_dev_capture_requests_get_only_and_drops_paid_channels(monkeypatch, tmp_path):
    """--dev capture against a fake urlopen: every request is a GET; paid channels and their recordings, recording
    and reservation items are left out; storages are not captured; options are recounted for the subset."""
    channels = [gr(), bs(141), bs(201), cs()]
    rec = {"id": 5, "channelId": 3211240960, "startAt": 2, "endAt": 3, "name": "ｆｒｅｅ", "genre1": 7,
           "videoFiles": [{"id": 50}], "thumbnails": [], "dropLogFile": {"id": 9, "errorCnt": 1, "dropCnt": 0}}
    paid = dict(rec, id=6, channelId=700297, videoFiles=[{"id": 60}], dropLogFile={"id": 8, "dropCnt": 3})
    answers = {
        "/api/version": {"version": "2.10.0"}, "/api/config": {}, "/api/channels": channels, "/api/encode": [],
        "/api/rules": {"rules": [], "total": 0}, "/api/recorded/options": {"channels": [], "genres": []},
        "/api/recording": {"records": [dict(rec, id=7), dict(paid, id=8)], "total": 2},
        "/api/recorded": {"records": [rec, paid], "total": 2},
        "/api/recorded/5": rec, "/api/videos/50/duration": {"duration": 1.0},
        "/api/reserves": {"reserves": [{"id": 1, "channelId": 3211240960, "startAt": 1, "endAt": 2},
                                       {"id": 2, "channelId": 400201, "startAt": 1, "endAt": 2, "isSkip": True}],
                          "total": 2},
        "/api/reserves/cnts": {"normal": 1, "conflicts": 0, "skips": 1, "overlaps": 0},
    }
    methods, paths = [], []

    class Resp(io.BytesIO):
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        methods.append(req.get_method())
        path = urllib.parse.urlsplit(req.full_url).path
        paths.append(path)
        if path.startswith("/api/dropLogs/"):
            return Resp(b"log")
        return Resp(json.dumps(answers[path], ensure_ascii=False).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    out = tmp_path / "set"
    assert snap.main(["--dev", "--prod-url", PROD, DEV, str(out), "--reserves-too"]) == 0
    assert set(methods) == {"GET"}
    assert "/api/storages" not in paths and "/api/recorded/6" not in paths and "/api/dropLogs/8" not in paths

    def load(rel):
        return json.load(open(out / rel, encoding="utf-8"))

    assert [c["id"] for c in load("api/channels.json")] == [3211240960, 400141]
    assert [r["id"] for r in load("api/recorded.json")["records"]] == [5]
    assert [r["id"] for r in load("api/recording.json")["records"]] == [7]
    assert [r["id"] for r in load("api/reserves.json")["reserves"]] == [1]
    assert load("api/reserves/cnts.json") == {"normal": 1, "conflicts": 0, "skips": 0, "overlaps": 0}
    assert load("api/recorded/options.json") == {"channels": [{"cnt": 2, "channelId": 3211240960}],
                                                  "genres": [{"cnt": 2, "genre": 7}]}
    assert (out / "api" / "dropLogs" / "9.txt").read_bytes() == b"log"
    assert not (out / "api" / "storages.json").exists() and not (out / "reserves_all.json").exists()


def test_private_capture_keeps_everything(monkeypatch, tmp_path):
    """Without --dev (outside every work tree) nothing is filtered and storages are captured (fingerprints)."""
    seen = []

    def get(base, path, params=None):
        seen.append(path)
        if path == "/api/recorded":
            return json.dumps({"records": [], "total": 0}).encode(), ""
        if path == "/api/recording":
            return json.dumps({"records": [], "total": 0}).encode(), ""
        return b"{}", ""

    monkeypatch.setattr(snap, "get", get)
    monkeypatch.setattr(snap, "snapshot_halfwidth", lambda *a, **k: 0)
    assert snap.main([PROD, str(tmp_path / "p")]) == 0
    assert "/api/storages" in seen and "/api/channels" in seen


# --- --prune-free ------------------------------------------------------------------------------------------------

def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def test_prune_free_set(tmp_path):
    d = str(tmp_path)
    free = {"id": 2, "channelId": 3211240960, "genre1": 7, "videoFiles": [{"id": 20}], "thumbnails": [20]}
    paid = {"id": 1, "channelId": 700297, "genre1": 3, "videoFiles": [{"id": 10}], "thumbnails": [10],
            "dropLogFile": {"id": 4}}
    _write(f"{d}/api/channels.json", [gr(), cs(), bs(201)])
    _write(f"{d}/api/recorded.json", {"records": [free, paid], "total": 2})
    for rid in (1, 2):
        _write(f"{d}/api/recorded/{rid}.json", {})
    for vid in (10, 20):
        _write(f"{d}/api/videos/{vid}/duration.json", {"duration": 1})
    os.makedirs(f"{d}/api/thumbnails")
    os.makedirs(f"{d}/api/dropLogs")
    for name in ("10.jpg", "20.jpg"):
        open(f"{d}/api/thumbnails/{name}", "wb").close()
    open(f"{d}/api/dropLogs/4.txt", "w").close()
    _write(f"{d}/api/recorded/options.json", {"channels": [{"cnt": 1, "channelId": 700297},
                                                           {"cnt": 2, "channelId": 3211240960}],
                                              "genres": [{"cnt": 1, "genre": 3}, {"cnt": 2, "genre": 7}]})
    _write(f"{d}/halfwidth.json", {"1": {"name": "p"}, "2": {"name": "f"}})
    _write(f"{d}/api/recording.json", {"records": [dict(free, id=3), dict(paid, id=5)], "total": 2})
    with open(f"{d}/api/reserves.json", "wb") as f:
        f.write(snap._reserves_body([{"id": 1, "channelId": 3211240960, "startAt": 0},
                                     {"id": 2, "channelId": 400201, "startAt": 0, "isOverlap": True}]))
    _write(f"{d}/api/reserves/cnts.json", {"normal": 10, "conflicts": 0, "skips": 0, "overlaps": 3})
    _write(f"{d}/api/storages.json", {"items": [{"total": 1}]})

    summary = snap.prune_free(d)
    assert summary == {"channels": 2, "recorded": 1, "recording": 1, "reserves": 1, "storages": 1}

    def load(rel):
        return json.load(open(os.path.join(d, rel), encoding="utf-8"))

    assert [c["id"] for c in load("api/channels.json")] == [3211240960]
    assert load("api/recorded.json") == {"records": [free], "total": 1}
    assert not os.path.exists(f"{d}/api/recorded/1.json") and os.path.exists(f"{d}/api/recorded/2.json")
    assert not os.path.exists(f"{d}/api/videos/10") and os.path.exists(f"{d}/api/videos/20/duration.json")
    assert sorted(os.listdir(f"{d}/api/thumbnails")) == ["20.jpg"] and not os.listdir(f"{d}/api/dropLogs")
    assert load("api/recorded/options.json") == {"channels": [{"cnt": 2, "channelId": 3211240960}],
                                                  "genres": [{"cnt": 2, "genre": 7}]}
    assert load("halfwidth.json") == {"2": {"name": "f"}}
    assert [r["id"] for r in load("api/recording.json")["records"]] == [3]
    assert [r["id"] for r in load("api/reserves.json")["reserves"]] == [1]
    assert load("api/reserves/cnts.json") == {"normal": 10, "conflicts": 0, "skips": 0, "overlaps": 2}
    assert not os.path.exists(f"{d}/api/storages.json")
    assert snap.prune_free(d)["recorded"] == 0  # idempotent


def test_prune_free_live(tmp_path):
    d = str(tmp_path)
    sched = [{"channel": gr(), "programs": [{"id": 1}]}, {"channel": cs(), "programs": [{"id": 2}]},
             {"channel": bs(201), "programs": [{"id": 3}]}]
    with open(f"{d}/schedules.json", "w", encoding="utf-8") as f:
        json.dump(sched, f)
    _write(f"{d}/broadcasting.json", [{"channel": {"id": s["channel"]["id"]}, "programs": s["programs"]}
                                      for s in sched])
    _write(f"{d}/guide.json", [{"channel": gr(), "programs": []}, {"channel": cs(), "programs": []}])
    assert snap.prune_free(d) == {"schedules": 2, "guide": 1, "broadcasting": 2}
    for name in ("schedules", "broadcasting", "guide"):
        assert [s["channel"]["id"] for s in json.load(open(f"{d}/{name}.json"))] == [3211240960]


# --- drop logs ---------------------------------------------------------------------------------------------------

def test_pick_droplogs_free_channels_counts_first():
    def r(rid, cid, **counts):
        return {"id": rid, "channelId": cid, "dropLogFile": dict({"id": rid * 10}, **counts)}

    records = [r(1, 3211240960), r(2, 700297, dropCnt=5), r(3, 3211240960, errorCnt=1), {"id": 4, "channelId": 1},
               r(5, 3211240960, scramblingCnt=2), r(6, 3211240960)]
    free = {3211240960}
    assert snap.pick_droplogs(records, free) == [50, 30, 10]  # drops/scrambling, errors, then any (newest first)
    assert snap.pick_droplogs(records, free, n=5) == [50, 30, 10, 60]
    assert snap.pick_droplogs(records)[:2] == [20, 50]  # private: every channel


# --- fingerprints ------------------------------------------------------------------------------------------------

def test_fingerprints_build_and_check(tmp_path, capsys):
    snapshot = tmp_path / "snap"
    _write(str(snapshot / "api" / "recorded.json"), {"records": [
        {"id": 1, "programId": 123456789012, "name": "[映]ヒミツノタイトル（吹替版）",
         "extended": "◇出演者\n【出演】ナゾノ・ハイユウ（コエノヒト）", "genre1": 6, "subGenre1": 0}]})
    _write(str(snapshot / "api" / "storages.json"), {"items": [{"total": 23000000000000}]})
    _write(str(snapshot / "api" / "rules.json"), {"rules": [{"id": 1, "saveOption": {"directory": "/himitsu/"}}]})
    fixtures = tmp_path / "fixtures"
    _write(str(fixtures / "dev" / "a.json"), {"name": "ナゾノ・ハイユウ ヒミツノタイトル"})  # already "leaked"
    allow = tmp_path / "allow.txt"
    allow.write_text("# reviewed\nナゾノ・ハイユウ\t# public name, also on free channels\n", encoding="utf-8")
    # being in the fixtures excludes nothing: only the frozen allow list does, and says why
    prints, excluded, in_fixtures = snap.build_fingerprints(str(snapshot), (str(allow), ""), str(fixtures))
    assert prints == sorted(["123456789012", "23000000000000", "himitsu", "ヒミツノタイトル"])
    assert excluded == {"ナゾノ・ハイユウ": "public name, also on free channels (allow.txt)"}
    assert in_fixtures == ["ヒミツノタイトル"]  # the leak is reported, not hidden
    prints2, _, _ = snap.build_fingerprints(str(snapshot), (), str(fixtures))
    assert "ナゾノ・ハイユウ" in prints2
    fp = tmp_path / "fp.txt"
    fp.write_text("old", encoding="utf-8")
    os.chmod(fp, 0o644)
    snap.write_fingerprints(str(fp), prints)
    assert oct(os.stat(fp).st_mode & 0o777) == "0o600"
    tree = tmp_path / "tree"
    _write(str(tree / "ok.json"), {"x": "nothing here"})
    assert snap.check_fingerprints(str(fp), str(tree)) == []
    _write(str(tree / "sub" / "bad.json"), {"x": "ﾋﾐﾂﾉﾀｲﾄﾙ"})  # half-width: found through NFKC
    assert snap.check_fingerprints(str(fp), str(tree)) == [(os.path.join("sub", "bad.json"), "ヒミツノタイトル")]
    assert snap.main(["--check-fingerprints", str(fp), str(tree)]) == 1  # 019 L1: place and count only
    out = capsys.readouterr().out
    assert os.path.join("sub", "bad.json") + ": 1 fingerprint(s)" in out and "ヒミツノタイトル" not in out
    assert snap.main(["--check-fingerprints", str(fp), str(tree), "--show-tokens"]) == 1
    assert "ヒミツノタイトル" in capsys.readouterr().out
    assert snap.main(["--check-fingerprints", str(fp), str(tree)]) == 1


def test_committed_allow_list_is_generic_and_reasoned():
    allow = snap.load_allow(snap.FINGERPRINT_ALLOW)
    assert allow and all(reason != "no reason given" for reason in allow.values())
    assert "天気予報" in allow and len(allow) <= 20  # generic words only; names go into the private list


def test_fingerprints_path_inside_the_tree_is_refused(no_request, capsys, tmp_path):
    assert snap.main([PROD, str(tmp_path / "p"), "--fingerprints", os.path.join(ROOT, "fp.txt")]) == 2
    assert "private" in capsys.readouterr().err
    assert not os.path.exists(os.path.join(ROOT, "fp.txt")) and not (tmp_path / "p").exists()


def test_dev_refuses_the_prod_host_on_any_port_or_alias(no_request, monkeypatch, tmp_path):
    out = str(tmp_path / "x")
    assert snap.main(["--dev", "--prod-url", PROD, "http://192.0.2.118:8888", out]) == 2  # another port

    def getaddrinfo(host, port, *a, **kw):
        addr = {"epgs-prod.lan": "192.0.2.118", "epgs-dev.lan": "192.0.2.202"}.get(host, host)
        return [(2, 1, 6, "", (addr, 0))]

    monkeypatch.setattr(snap.socket, "getaddrinfo", getaddrinfo)
    assert snap.main(["--dev", "--prod-url", PROD, "http://epgs-prod.lan:8888", out]) == 2  # an alias
    snap.check_capture("http://epgs-dev.lan:8888", out, True, PROD)  # the dev host is fine


def test_private_capture_is_0700_and_0600(monkeypatch, tmp_path):
    def get(base, path, params=None):
        if path in ("/api/recorded", "/api/recording"):
            return json.dumps({"records": [], "total": 0}).encode(), ""
        return b"{}", ""

    monkeypatch.setattr(snap, "get", get)
    monkeypatch.setattr(snap, "snapshot_halfwidth", lambda *a, **k: 0)
    before = os.umask(0o022)
    try:
        assert snap.main([PROD, str(tmp_path / "p")]) == 0
        assert os.umask(0o022) == 0o022  # restored
    finally:
        os.umask(before)
    out = tmp_path / "p"
    assert oct(out.stat().st_mode & 0o777) == "0o700"
    assert oct((out / "api").stat().st_mode & 0o777) == "0o700"
    assert oct((out / "api" / "version.json").stat().st_mode & 0o777) == "0o600"


def test_options_recounted_when_only_an_in_progress_recording_is_paid(monkeypatch, tmp_path):
    """--dev: the server's /recorded/options also counts a paid recording in progress; the fixture recounts."""
    channels = [gr(), cs()]
    rec = {"id": 5, "channelId": 3211240960, "startAt": 2, "endAt": 3, "name": "f", "genre1": 7, "videoFiles": [],
           "thumbnails": []}
    answers = {"/api/channels": channels, "/api/recorded": {"records": [rec], "total": 1}, "/api/recorded/5": rec,
               "/api/recording": {"records": [dict(rec, id=6, channelId=700297, genre1=3)], "total": 1},
               "/api/recorded/options": {"channels": [{"cnt": 1, "channelId": 700297}], "genres": []}}

    def get(base, path, params=None):
        return json.dumps(answers.get(path, {})).encode(), ""

    monkeypatch.setattr(snap, "get", get)
    monkeypatch.setattr(snap, "snapshot_halfwidth", lambda *a, **k: 0)
    assert snap.main(["--dev", "--prod-url", PROD, DEV, str(tmp_path / "s")]) == 0
    opts = json.load(open(tmp_path / "s" / "api" / "recorded" / "options.json"))
    assert opts == {"channels": [{"cnt": 1, "channelId": 3211240960}], "genres": [{"cnt": 1, "genre": 7}]}


def test_only_ids_of_refreshes_in_place_and_removes_stale_files(monkeypatch, tmp_path):
    out = tmp_path / "dev"
    old = [{"id": 2, "channelId": 3211240960, "videoFiles": [{"id": 20}], "thumbnails": [20]},
           {"id": 3, "channelId": 3211240960, "videoFiles": [{"id": 30}], "thumbnails": [30],
            "dropLogFile": {"id": 4}}]
    _write(str(out / "api" / "recorded.json"), {"records": old, "total": 2})
    for rel in ("api/recorded/2.json", "api/recorded/3.json", "api/videos/20/duration.json",
                "api/videos/30/duration.json"):
        _write(str(out / rel), {})
    os.makedirs(out / "api" / "thumbnails")
    os.makedirs(out / "api" / "dropLogs")
    for rel in ("api/thumbnails/20.jpg", "api/thumbnails/30.jpg", "api/dropLogs/4.txt"):
        (out / rel).write_bytes(b"x")
    server = [dict(old[0], startAt=1, endAt=2, name="n"), {"id": 9, "channelId": 3211240960, "startAt": 1,
                                                           "endAt": 2, "name": "new", "videoFiles": [],
                                                           "thumbnails": []}]

    def get(base, path, params=None):
        answers = {"/api/channels": [gr()], "/api/recorded": {"records": server, "total": 2},
                   "/api/recording": {"records": [], "total": 0}, "/api/recorded/2": server[0],
                   "/api/videos/20/duration": {"duration": 1}}
        return json.dumps(answers.get(path, {})).encode(), ""

    monkeypatch.setattr(snap, "get", get)
    monkeypatch.setattr(snap, "snapshot_halfwidth", lambda *a, **k: 0)
    assert snap.main(["--dev", "--prod-url", PROD, DEV, str(out), "--only-ids-of", str(out), "--thumbs", "0"]) == 0
    assert [r["id"] for r in json.load(open(out / "api" / "recorded.json"))["records"]] == [2]  # 9 is not ours
    left = sorted(os.path.relpath(os.path.join(d, f), out) for d, _, fs in os.walk(out) for f in fs)
    assert left == sorted(["api/channels.json", "api/recorded.json", "api/recorded/2.json",
                           "api/recorded/options.json", "api/recording.json", "api/thumbnails/20.jpg",
                           "api/videos/20/duration.json"] + [f"api/{n}.json" for n in
                                                             ("version", "config", "encode", "rules")])
