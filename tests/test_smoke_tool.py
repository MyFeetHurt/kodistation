"""tools/kodi_smoke.py helpers that run without the VM."""
import os
import re
import sys

import kodi_smoke
import pytest


def test_retry_once_passes_on_second_try(monkeypatch):
    monkeypatch.setattr(kodi_smoke.time, "sleep", lambda s: None)
    results = [["recording 99 listed by Kodi but not by EPGStation"], []]
    calls = []

    def check():
        calls.append(1)
        return results[len(calls) - 1]

    assert kodi_smoke.retry_once("movies", check) == []
    assert len(calls) == 2


def test_retry_once_reports_a_persistent_mismatch(monkeypatch):
    monkeypatch.setattr(kodi_smoke.time, "sleep", lambda s: None)
    calls = []

    def check():
        calls.append(1)
        return [f"differs {len(calls)}"]

    assert kodi_smoke.retry_once("movies", check) == ["differs 2"]
    assert len(calls) == 2


def test_retry_once_runs_once_when_fine():
    calls = []
    assert kodi_smoke.retry_once("x", lambda: calls.append(1) or []) == []
    assert calls == [1]


def test_episode_label_failures():
    ok = [{"label": "6x8 傑作選 #9", "season": 6, "episode": 8},
          {"label": "[COLOR red]●[/COLOR] 1x1 転んだままで、終われるか!", "season": 1, "episode": 1},
          {"label": "2026-10-10 なぜ農業王国に?", "episode": -1, "premiered": "2026-10-10"}]
    assert kodi_smoke.episode_label_failures(ok) == []
    bad = [{"label": "孤独のグルメ 傑作選 Season6 #8", "season": 6, "episode": 8},
           {"label": "ブラタモリ", "episode": -1, "premiered": "2026-10-10"}]
    assert len(kodi_smoke.episode_label_failures(bad)) == 2


def test_expected_live_and_item_failures():
    """Backlog 012 smoke helpers: expected GR channels and the comparison with Kodi's listing."""
    import json
    import os

    from conftest import FIXTURES

    root = os.path.join(FIXTURES, "live")
    schedules = json.load(open(os.path.join(root, "schedules.json"), encoding="utf-8"))
    now = json.load(open(os.path.join(root, "live_meta.json")))["capturedAt"]
    gr = [s for s in schedules if s["channel"]["channelType"] == "GR"]
    want = kodi_smoke.expected_live(gr, now)
    assert want and all(isinstance(cid, int) for cid, _, _ in want)
    files = [{"file": f"plugin://plugin.video.epgstation/?action=live_play&channelId={cid}&name=x",
              "filetype": "file", "label": f"{name}  {prog}", "genre": ["g"],
              "art": {"thumb": f"image://http%3a%2f%2fh%2fapi%2fchannels%2f{cid}%2flogo/"}}
             for cid, name, prog in want]
    logos = {cid for cid, _, _ in want}
    assert kodi_smoke.live_item_failures(files, want, logos) == []
    assert kodi_smoke.live_item_failures(files[1:], want, logos)
    bad = [dict(files[0], label="other")] + files[1:]
    assert kodi_smoke.live_item_failures(bad, want, logos)
    noart = [dict(files[0], art={})] + files[1:]
    assert kodi_smoke.live_item_failures(noart, want, logos)


def _live_fake(monkeypatch, channel_types, lists=None, marked=()):
    """kodi_smoke against canned data: EPGStation GETs from the shared live fixtures, Kodi listings as do_live
    would make them (type folders, or the channels directly for a single type)."""
    import json
    import os

    from conftest import FIXTURES
    from resources.lib.epgstation import items, live

    root = os.path.join(FIXTURES, "live")
    schedules = [s for s in json.load(open(os.path.join(root, "schedules.json"), encoding="utf-8"))
                 if s["channel"]["channelType"] in channel_types]
    now = json.load(open(os.path.join(root, "live_meta.json")))["capturedAt"]
    channels = [dict(s["channel"], halfWidthName=items.to_half(s["channel"]["name"])) for s in schedules]
    monkeypatch.setattr(kodi_smoke.time, "time", lambda: now / 1000)
    requested = []

    def epgs_get(epgs, path, **params):
        requested.append(path)
        if path == "/channels":
            return channels
        if path == "/schedules":
            return [s for s in schedules if params.get(s["channel"]["channelType"]) == "true"]
        if path == "/reserves/lists":  # record from the guide (014)
            assert params["startAt"] == params["endAt"]
            return lists or {"normal": [], "conflicts": [], "skips": [], "overlaps": []}
        if path == "/recording":
            return {"records": []}
        raise AssertionError(path)

    def channel_files(t):
        return [{"file": f"{kodi_smoke.PLUGIN}?action=live_play&channelId={cid}&name=x", "filetype": "file",
                 "label": ("[COLOR red]○[/COLOR] " if cid in marked else "") + f"{name}  {prog}", "genre": ["g"],
                 "art": {"thumb": f"image://http%3a%2f%2fh%2fapi%2fchannels%2f{cid}%2flogo/"}}
                for cid, name, prog in kodi_smoke.expected_live(
                    [s for s in schedules if s["channel"]["channelType"] == t], now)]

    def listdir(host, url):
        types = live.live_types(channels)
        if url.endswith("?action=live"):
            top = [{"file": f"{kodi_smoke.PLUGIN}?action={a}", "filetype": "directory", "label": a}
                   for a in kodi_smoke.GUIDE_ACTIONS]  # program guide (013)
            if len(types) == 1:
                return top + channel_files(types[0])
            return top + [{"file": f"{kodi_smoke.PLUGIN}?action=live&type={t}", "filetype": "directory",
                           "label": t} for t in types]
        return channel_files(url.rsplit("type=", 1)[1])

    monkeypatch.setattr(kodi_smoke, "epgs_get", epgs_get)
    monkeypatch.setattr(kodi_smoke, "listdir", listdir)
    return requested


def test_check_live_single_type(monkeypatch):
    """Codex P2: with one broadcast type do_live lists the channels directly; the smoke must accept that."""
    _live_fake(monkeypatch, {"BS"})
    assert kodi_smoke.check_live("h", "http://e") == []
    url, headers = kodi_smoke.live_list_url(["BS"])
    assert url == kodi_smoke.PLUGIN + "?action=live" and "ライブTV" in headers


def test_check_live_type_folders(monkeypatch):
    _live_fake(monkeypatch, {"GR", "BS", "CS"})
    assert kodi_smoke.check_live("h", "http://e") == []
    url, headers = kodi_smoke.live_list_url(["GR", "BS", "CS"])
    assert url.endswith("?action=live&type=GR") and headers == {"地上波", "Terrestrial"}


def test_check_live_requires_guide_folders(monkeypatch):
    _live_fake(monkeypatch, {"GR", "BS", "CS"})
    listdir = kodi_smoke.listdir
    monkeypatch.setattr(kodi_smoke, "listdir", lambda h, u: listdir(h, u)[2:] if u.endswith("?action=live")
                        else listdir(h, u))
    assert any("guide folders" in f for f in kodi_smoke.check_live("h", "http://e"))


def test_check_live_reservation_markers(monkeypatch):
    """014: the channel whose current program is reserved carries ○; a missing or extra marker fails."""
    import json
    import os

    from conftest import FIXTURES
    from resources.lib.epgstation import live

    root = os.path.join(FIXTURES, "live")
    now = json.load(open(os.path.join(root, "live_meta.json")))["capturedAt"]
    gr = [s for s in json.load(open(os.path.join(root, "schedules.json"), encoding="utf-8"))
          if s["channel"]["channelType"] == "GR"]
    cid = gr[0]["channel"]["id"]
    cur, _ = live.now_next(gr[0]["programs"], now)
    lists = {"normal": [{"reserveId": 1, "programId": cur["id"], "ruleId": 2}], "conflicts": [], "skips": [],
             "overlaps": []}
    _live_fake(monkeypatch, {"GR", "BS"}, lists, marked={cid})
    assert kodi_smoke.check_live("h", "http://e") == []
    _live_fake(monkeypatch, {"GR", "BS"}, lists)
    assert any("lacks" in f for f in kodi_smoke.check_live("h", "http://e"))
    _live_fake(monkeypatch, {"GR", "BS"}, None, marked={cid})
    assert any("lacks" in f for f in kodi_smoke.check_live("h", "http://e"))


# --- program guide (013) ---------------------------------------------------------------------------------------

def _guide_files(dicts):
    """What Files.GetDirectory reports for guide item dicts (label, file, filetype, genre)."""
    return [{"label": d["label"], "file": d["path"], "filetype": "directory" if d["folder"] else "file",
             "genre": d["info"].get("genres", [])} for d in dicts]


def _guide_data():
    import json
    import os

    import fake_epgstation
    from conftest import FIXTURES

    now = json.load(open(os.path.join(FIXTURES, "live", "live_meta.json")))["capturedAt"]
    return fake_epgstation.merged_live("schedules"), now


def test_guide_smoke_helpers_accept_the_addon_lists():
    from resources.lib.epgstation import guide

    schedules, now = _guide_data()
    api, ja, en = kodi_smoke._LABEL_API, kodi_smoke.GUIDE_STRINGS[0], kodi_smoke.GUIDE_STRINGS[1]
    assert ja["whats_on"] == "放送中" and en["whats_on"] == "What's on"
    on_air = _guide_files(guide.program_items(guide.on_air_at(schedules, now), api, now, "ja", ja, "slot"))
    assert kodi_smoke.guide_now_failures(on_air, schedules, now) == []
    assert kodi_smoke.guide_now_failures(on_air[1:], schedules, now)
    for lang in ("ja", "en"):
        folders = _guide_files(guide.genre_folders(guide.genre_counts(schedules, now), lang))
        assert kodi_smoke.guide_genres_failures(folders, schedules, now) == []
    assert kodi_smoke.guide_genres_failures(folders[1:], schedules, now)
    for strings in (ja, en):
        news = _guide_files(guide.program_items(guide.genre_programs(schedules, 0, now), api, now, "ja", strings,
                                                "genre"))
        assert news and kodi_smoke.guide_genre_failures(news, schedules, 0, now) == []
    assert kodi_smoke.guide_genre_failures(news[::-1], schedules, 0, now)
    assert kodi_smoke.guide_genre_failures([dict(f, genre=[]) for f in news], schedules, 0, now)


def test_guide_smoke_day_helper():
    from resources.lib.epgstation import guide

    schedules, now = _guide_data()
    nhk = [s for s in schedules if s["channel"]["id"] == kodi_smoke.NHK]
    buckets = guide.day_buckets(nhk, now)
    for strings in kodi_smoke.GUIDE_STRINGS:
        days = _guide_files(guide.day_folders(nhk[0]["channel"], buckets, now, strings))
        today = _guide_files(guide.program_items([(nhk[0]["channel"], p) for p in buckets[0]], kodi_smoke._LABEL_API,
                                                 now, "ja", strings, "day"))
        assert kodi_smoke.guide_day_failures(days, today, nhk, now) == []
    assert kodi_smoke.guide_day_failures(days[1:], today, nhk, now)
    assert kodi_smoke.guide_day_failures(days, today[1:], nhk, now)


def test_guide_smoke_helpers_check_reservation_markers():
    """014: the genre and day comparisons include the markers drawn from the lists index."""
    from resources.lib.epgstation import guide, reserves

    schedules, now = _guide_data()
    rows = guide.genre_programs(schedules, 0, now)
    future = [p for _, p in rows if p["startAt"] > now]
    index = reserves.lists_index({"normal": [{"reserveId": 1, "programId": future[0]["id"], "ruleId": 2}],
                                  "overlaps": [{"reserveId": 2, "programId": future[1]["id"], "ruleId": 2}]})
    s = kodi_smoke.mark_strings(kodi_smoke.GUIDE_STRINGS[0])
    news = _guide_files(guide.program_items(rows, kodi_smoke._LABEL_API, now, "ja", s, "genre", index=index))
    assert kodi_smoke.marked_count(news) == 2
    assert kodi_smoke.guide_genre_failures(news, schedules, 0, now, (index, ())) == []
    assert kodi_smoke.guide_genre_failures(news, schedules, 0, now)  # markers not expected without the index
    plain = _guide_files(guide.program_items(rows, kodi_smoke._LABEL_API, now, "ja", s, "genre"))
    assert kodi_smoke.guide_genre_failures(plain, schedules, 0, now, (index, ()))
    assert any("[COLOR grey][重複][/COLOR] " in f["label"] for f in news)


def test_smoke_never_opens_play_or_info():
    import inspect

    src = inspect.getsource(kodi_smoke.check_guide)
    assert "guide_info" not in src and "live_play" not in src and "Player.Open" not in src
    code = "\n".join(line.split("#", 1)[0] for line in inspect.getsource(kodi_smoke).splitlines())
    assert not re.search(r"action=record(?![a-z_])", code)  # 014: the smoke never opens the 録画 entry


def test_clear_guide_cache_local_and_remote(monkeypatch, tmp_path):
    """M1: the smoke deletes the plugin's guide cache: a local rm on the VM itself (CI, host mode), else ssh."""
    calls = []
    monkeypatch.setenv("KODI_SSH_USER", "kodi")
    monkeypatch.setattr(kodi_smoke.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    cache = tmp_path / "guide-schedules.json"
    cache.write_text("{}")
    monkeypatch.setattr(kodi_smoke, "GUIDE_CACHE", str(cache))
    kodi_smoke.clear_guide_cache("127.0.0.1")
    assert not cache.exists() and calls == []
    kodi_smoke.clear_guide_cache("127.0.0.1")  # already gone: fine
    kodi_smoke.clear_guide_cache("192.0.2.10")  # TEST-NET: not a local address
    assert calls == [["ssh", "kodi@192.0.2.10", f"rm -f {cache}"]]
    monkeypatch.delenv("KODI_SSH_USER")  # 019: no user in local.env -> ssh's own default user
    kodi_smoke.clear_guide_cache("192.0.2.10")
    assert calls[-1] == ["ssh", "192.0.2.10", f"rm -f {cache}"]


def test_check_guide_clears_the_cache_first(monkeypatch):
    order = []
    monkeypatch.setattr(kodi_smoke, "clear_guide_cache", lambda host: order.append("clear"))

    def epgs_get(epgs, path, **params):
        order.append(path)
        return []
    monkeypatch.setattr(kodi_smoke, "epgs_get", epgs_get)
    assert kodi_smoke.check_guide("h", "http://e") == []
    assert order[0] == "clear"


# --- backlog 007: reservations ---------------------------------------------------------------------------------

def _reserve_data(fixture_set):
    import json
    import os

    from conftest import FIXTURES

    root = os.path.join(FIXTURES, fixture_set)

    def load(*path):
        with open(os.path.join(root, *path), encoding="utf-8") as f:
            return json.load(f)

    rules = {r["id"]: kodi_smoke.items.rule_name(r) for r in load("api", "rules.json")["rules"]}
    return (load("api", "reserves.json")["reserves"], load("api", "channels.json"), rules,
            load("reserves_meta.json")["capturedAt"], load("api", "reserves", "cnts.json"))


def _recording_fixture(fixture_set):
    import json
    import os

    from conftest import FIXTURES

    with open(os.path.join(FIXTURES, fixture_set, "api", "recording.json"), encoding="utf-8") as f:
        return json.load(f)["records"]


def _reserve_files(dicts):
    return [{"label": d["label"], "file": d["path"], "filetype": "directory" if d["folder"] else "file",
             "studio": d["info"].get("studios"), "plot": d["info"].get("plot"), "genre": d["info"].get("genres")}
            for d in dicts]


def test_reserve_strings_from_po():
    ja, en = kodi_smoke.RESERVE_STRINGS
    assert ja["overlap"] == "重複" and en["overlap"] == "Duplicates" and ja["weekdays"].startswith("日,")


@pytest.mark.parametrize("fixture_set", ["dev", "dev2"])
@pytest.mark.parametrize("lang", [0, 1])
def test_check_reserves_accepts_the_addon_lists(monkeypatch, fixture_set, lang):
    from resources.lib.epgstation import reserves

    all_reserves, channels, rules, now, cnts = _reserve_data(fixture_set)
    strings = kodi_smoke.RESERVE_STRINGS[lang]
    monkeypatch.setattr(kodi_smoke.time, "time", lambda: now / 1000)
    names = kodi_smoke.items.channel_names(channels)
    logos = {c["id"] for c in channels if c.get("hasLogoData")}

    records = _recording_fixture(fixture_set)

    def listing(state):
        return _reserve_files(reserves.listing(all_reserves, state, names, logos, rules, kodi_smoke._LABEL_API, now,
                                               "ja", strings, None, records))

    def listdir(host, url):
        state = kodi_smoke.urllib.parse.parse_qs(kodi_smoke.urllib.parse.urlparse(url).query).get("type")
        return listing(state[0] if state else None)

    rule_list = {"rules": [{"id": i, "reserveOption": {"name": n}} for i, n in rules.items()]}
    answers = {"/reserves/cnts": cnts, "/channels": channels, "/rules": rule_list,
               "/reserves": {"reserves": all_reserves, "total": len(all_reserves)}, "/recording": {"records": records}}
    monkeypatch.setattr(kodi_smoke, "epgs_get", lambda epgs, path, **params: answers[path])
    monkeypatch.setattr(kodi_smoke, "listdir", listdir)
    assert kodi_smoke.check_reserves("h", "http://e") == []
    monkeypatch.setattr(kodi_smoke, "listdir", lambda host, url: listing(None)[1:])
    assert kodi_smoke.check_reserves("h", "http://e")


def test_smoke_never_runs_reserve_actions():
    """AC 12: the smoke only lists; it never opens a reservation's info or action URL and never writes."""
    import inspect

    for fn in (kodi_smoke.check_reserves, kodi_smoke.reserve_list_failures, kodi_smoke.reserve_item_failures,
               kodi_smoke.expected_reserves, kodi_smoke.epgs_reserves, kodi_smoke.main):
        src = inspect.getsource(fn)
        assert "reserve_action" not in src and "Player.Open" not in src
        assert "listdir(host, f[\"file\"])" not in src or fn is not kodi_smoke.reserve_item_failures
    code = "\n".join(line.split("#", 1)[0] for line in inspect.getsource(kodi_smoke).splitlines())
    assert "action=reserve_action" not in code and "action=reserve_info" not in code
    assert "DELETE" not in code and "\"POST\"" not in code and "\"PUT\"" not in code


# --- review M1: direct write checks instead of count equality -----------------------------------------------------

KODI_UA = "KodifromDebian/21.3 (X11; Linux x86_64) Kodi_Fork_Kodi from Debian/1.0"
ADDON_UA = "KODIstation/0.1.0 (plugin.video.epgstation)"


def _access(ts, method, path, ua, ip="::ffff:127.0.0.1"):
    return f'[{ts}] [INFO] access - {ip} - - "{method} {path} HTTP/1.1" 200 2 "" "{ua}"'


def test_access_writes_window_and_sources():
    lines = [
        _access("2026-10-12T10:00:00.000", "DELETE", "/api/reserves/1", ADDON_UA),  # before the window
        _access("2026-10-12T10:00:05.000", "GET", "/api/reserves/lists", ADDON_UA),
        _access("2026-10-12T10:00:06.000", "HEAD", "/api/videos/1", KODI_UA, "::1"),  # Kodi's content lookup: a read
        _access("2026-10-12T10:00:07.000", "POST", "/api/reserves", ADDON_UA),
        _access("2026-10-12T10:00:08.000", "DELETE", "/api/recorded/3", KODI_UA, "::1"),
        _access("2026-10-12T10:00:09.000", "DELETE", "/api/reserves/9", "Python-urllib/3.14"),  # the guard: a note
        "garbage line",
        _access("2026-10-12T10:01:00.000", "PUT", "/api/reserves/1", ADDON_UA),  # after the window
    ]
    kodi, other = kodi_smoke.access_writes("\n".join(lines), "2026-10-12T10:00:01", "2026-10-12T10:00:59")
    assert [x.split('"')[1] for x in kodi] == ["POST /api/reserves HTTP/1.1", "DELETE /api/recorded/3 HTTP/1.1"]
    assert [x.split('"')[1] for x in other] == ["DELETE /api/reserves/9 HTTP/1.1"]
    assert kodi_smoke.access_writes("", "a", "b") == ([], [])


def test_kodi_write_lines():
    text = ("2026-10-12 10:00:00.000 T:1 info <general>: [epgstation] live: channel 1 mode 0\n"
            "2026-10-12 10:00:01.000 T:1 info <general>: [epgstation] reserve: add program 5 (P)\n"
            "2026-10-12 10:00:02.000 T:1 error <general>: [epgstation] reserve: cancel reserve 3 (x) failed\n")
    assert len(kodi_smoke.kodi_write_lines(text)) == 2
    assert kodi_smoke.kodi_write_lines("") == []


def test_write_check_failures(monkeypatch):
    access = "\n".join([_access("2026-10-12T10:00:05.000", "GET", "/api/reserves/lists", ADDON_UA),
                        _access("2026-10-12T10:00:07.000", "POST", "/api/reserves", ADDON_UA),
                        _access("2026-10-12T10:00:09.000", "DELETE", "/api/reserves/9", "Python-urllib/3.14")])
    kodi_log = "2026-10-12 10:00:07.000 T:1 info <general>: [epgstation] reserve: add program 5 (P)\n"
    reads = {}

    def read(host, path, offset=0):
        reads[path] = offset
        return {kodi_smoke.ACCESS_LOG: access, kodi_smoke.KODI_LOG: kodi_log}[path]

    monkeypatch.setattr(kodi_smoke, "read_vm_file", read)
    monkeypatch.setattr(kodi_smoke, "vm_file_size", lambda host, path: 5000)
    import time

    start, end = (time.mktime((2026, 10, 12, 10, 0, s, 0, 0, -1)) for s in (1, 59))
    failures = kodi_smoke.write_failures("h", start, end, 1234)
    assert len(failures) == 2 and any("POST /api/reserves" in f for f in failures)
    assert any("reserve: add program 5" in f for f in failures)
    assert reads[kodi_smoke.KODI_LOG] == 1234
    access, kodi_log = _access("2026-10-12T10:00:07.000", "GET", "/api/reserves/lists", ADDON_UA), ""
    assert kodi_smoke.write_failures("h", start, end, 0) == []


def test_write_check_skips_when_logs_cannot_be_read(monkeypatch, capsys):
    """Over ssh from the workstation an unreadable log stays a note."""
    monkeypatch.setattr(kodi_smoke, "read_vm_file", lambda host, path, offset=0: None)
    monkeypatch.setattr(kodi_smoke, "vm_file_size", lambda host, path: None)
    assert kodi_smoke.write_failures("192.0.2.10", 1.0, 2.0, 0) == []
    out = capsys.readouterr().out
    assert "access.log" in out and "skipped" in out and "kodi.log" in out


def test_read_vm_file_local_and_remote(monkeypatch, tmp_path):
    f = tmp_path / "x.log"
    f.write_text("abcdef")
    assert kodi_smoke.read_vm_file("127.0.0.1", str(f), 2) == "cdef"
    assert kodi_smoke.read_vm_file("127.0.0.1", str(tmp_path / "missing")) is None
    calls = []

    class Done:
        returncode, stdout = 0, "remote"

    monkeypatch.setenv("KODI_SSH_USER", "kodi")
    monkeypatch.setattr(kodi_smoke.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Done())
    assert kodi_smoke.read_vm_file("192.0.2.10", "~/a.log", 5) == "remote"
    assert calls == [["ssh", "kodi@192.0.2.10", "tail -c +6 ~/a.log"]]


def test_hosts_come_from_local_env(monkeypatch, tmp_path):
    """019: no address in the tool; --host defaults to $KODI_HOST (local.env), --screenshot needs the virsh target."""
    env = tmp_path / "local.env"
    env.write_text("# comment\nKODI_HOST=kodi-box.test\nKODI_SSH_USER=kodi\nexport SCREENSHOT_VM=vm1\n")
    monkeypatch.setenv("LOCAL_ENV", str(env))
    seen = {}
    monkeypatch.setattr(kodi_smoke, "listdir", lambda host, url: seen.setdefault("host", host) and [])
    monkeypatch.setattr(sys, "argv", ["kodi_smoke.py", "--show", "plugin://x/"])
    assert kodi_smoke.main() == 0
    assert seen["host"] == "kodi-box.test"
    assert os.environ["SCREENSHOT_VM"] == "vm1" and "SCREENSHOT_SSH" not in os.environ
    monkeypatch.setattr(sys, "argv", ["kodi_smoke.py", "--screenshot", str(tmp_path / "s.png")])
    with pytest.raises(SystemExit) as e:
        kodi_smoke.main()  # refused before any request: SCREENSHOT_SSH is unset
    assert "SCREENSHOT_SSH" in str(e.value)
    monkeypatch.setenv("KODI_HOST", "other.test")  # the environment wins over the file
    monkeypatch.setattr(sys, "argv", ["kodi_smoke.py", "--show", "plugin://x/"])
    seen.clear()
    kodi_smoke.main()
    assert seen["host"] == "other.test"


def test_no_host_without_local_env_is_refused(monkeypatch, capsys):
    """023: no local.env, no KODI_HOST -> one clear line and exit 1 (like deploy.sh), no request, no traceback."""
    monkeypatch.delenv("KODI_HOST", raising=False)
    monkeypatch.setattr(kodi_smoke, "listdir", lambda host, url: pytest.fail("no request without a host"))
    for argv in (["--show", "plugin://x/"], [], ["--host", ""]):
        monkeypatch.setattr(sys, "argv", ["kodi_smoke.py", *argv])
        with pytest.raises(SystemExit) as e:
            kodi_smoke.main()
        assert e.value.code not in (0, None)
        assert str(e.value).startswith("no Kodi host") and "\n" not in str(e.value)


def test_no_host_cli_prints_one_line(tmp_path):
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "KODI_HOST"}
    env["LOCAL_ENV"] = str(tmp_path / "missing.env")
    r = subprocess.run([sys.executable, kodi_smoke.__file__, "--show", "plugin://x/"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert r.stdout == "" and r.stderr.startswith("no Kodi host") and r.stderr.count("\n") == 1
    assert "Traceback" not in r.stderr


def test_counts_are_print_only(monkeypatch):
    """M1: reservation counts that change on their own (EPG update, guard, a finished recording) never fail."""
    calls = []

    def epgs_get(epgs, path, **params):
        if path == "/reserves/cnts":
            calls.append(1)
            return {"normal": len(calls)}
        return {"rules": [], "reserves": [], "total": 0, "records": []} if path != "/channels" else []

    monkeypatch.setattr(kodi_smoke, "epgs_get", epgs_get)
    monkeypatch.setattr(kodi_smoke, "listdir", lambda host, url: [])
    assert kodi_smoke.check_reserves("h", "http://e") == []


def test_guide_day_and_now_accept_markers_in_either_language():
    """State markers ([重複] / [Duplicates]) follow Kodi's language: the day and 現在 comparisons accept ja and en."""
    from resources.lib.epgstation import guide, reserves

    schedules, now = _guide_data()
    nhk = [s for s in schedules if s["channel"]["id"] == kodi_smoke.NHK]
    buckets = guide.day_buckets(nhk, now)
    ch = nhk[0]["channel"]
    index = reserves.lists_index({"overlaps": [{"reserveId": 2, "programId": p["id"], "ruleId": 2}
                                               for p in buckets[0]]})
    on_air = guide.on_air_at(schedules, now)
    now_index = reserves.lists_index({"overlaps": [{"reserveId": 3, "programId": p["id"], "ruleId": 2}
                                                   for _, p in on_air]})
    for strings in kodi_smoke.GUIDE_STRINGS:
        s = kodi_smoke.mark_strings(strings)
        days = _guide_files(guide.day_folders(ch, buckets, now, strings))
        today = _guide_files(guide.program_items([(ch, p) for p in buckets[0]], kodi_smoke._LABEL_API, now, "ja", s,
                                                 "day", index=index))
        assert kodi_smoke.guide_day_failures(days, today, nhk, now, (index, ())) == []
        cur = _guide_files(guide.program_items(on_air, kodi_smoke._LABEL_API, now, "ja", s, "slot", index=now_index))
        assert kodi_smoke.guide_now_failures(cur, schedules, now, (now_index, ())) == []
    assert any("[Duplicates]" in f["label"] for f in today)


def _window():
    import time

    return [time.mktime((2026, 10, 12, 10, 0, sec, 0, 0, -1)) for sec in (1, 59)]


def test_write_check_fails_on_the_vm_when_logs_cannot_be_read(monkeypatch):
    """N1: run locally on the VM (CI) the logs must be readable: a missing log is a failure, not a pass."""
    monkeypatch.setattr(kodi_smoke, "read_vm_file", lambda host, path, offset=0: None)
    monkeypatch.setattr(kodi_smoke, "vm_file_size", lambda host, path: None)
    failures = kodi_smoke.write_failures("127.0.0.1", *_window(), 0)
    assert any("access.log" in f for f in failures) and any("kodi.log" in f for f in failures)
    assert len(kodi_smoke.write_failures("127.0.0.1", *_window(), None)) == 2  # no start offset either


def test_write_check_self_check_needs_addon_reads(monkeypatch):
    """N1: the smoke makes hundreds of addon GETs; none in the window means the regex or the log path broke."""
    logs = {kodi_smoke.ACCESS_LOG: _access("2026-10-12T10:00:05.000", "GET", "/api/x", "Python-urllib/3.14"),
            kodi_smoke.KODI_LOG: ""}
    monkeypatch.setattr(kodi_smoke, "read_vm_file", lambda host, path, offset=0: logs[path])
    monkeypatch.setattr(kodi_smoke, "vm_file_size", lambda host, path: 0)
    for host in ("127.0.0.1", "192.0.2.10"):
        assert any("KODIstation/" in f for f in kodi_smoke.write_failures(host, *_window(), 0))
    logs[kodi_smoke.ACCESS_LOG] = _access("2026-10-12T11:00:05.000", "GET", "/api/x", ADDON_UA)  # outside
    assert any("KODIstation/" in f for f in kodi_smoke.write_failures("127.0.0.1", *_window(), 0))
    logs[kodi_smoke.ACCESS_LOG] = _access("2026-10-12T10:00:05.000", "GET", "/api/x", ADDON_UA)
    assert kodi_smoke.write_failures("127.0.0.1", *_window(), 0) == []
    assert kodi_smoke.addon_reads(logs[kodi_smoke.ACCESS_LOG], "2026-10-12T10:00:01", "2026-10-12T10:01:00") == 1


def test_write_check_reads_kodi_old_log_after_a_restart(monkeypatch):
    """N2: kodi.log smaller than the start offset: Kodi restarted; kodi.old.log from the offset + all of kodi.log."""
    reads = []
    texts = {kodi_smoke.KODI_OLD_LOG: "[epgstation] reserve: in old\n", kodi_smoke.KODI_LOG: "fresh\n",
             kodi_smoke.ACCESS_LOG: _access("2026-10-12T10:00:05.000", "GET", "/api/x", ADDON_UA)}

    def read(host, path, offset=0):
        reads.append((path, offset))
        return texts[path]

    monkeypatch.setattr(kodi_smoke, "read_vm_file", read)
    monkeypatch.setattr(kodi_smoke, "vm_file_size", lambda host, path: 10)
    failures = kodi_smoke.write_failures("127.0.0.1", *_window(), 500)
    assert (kodi_smoke.KODI_OLD_LOG, 500) in reads and (kodi_smoke.KODI_LOG, 0) in reads
    assert any("in old" in f for f in failures)
    reads.clear()
    monkeypatch.setattr(kodi_smoke, "vm_file_size", lambda host, path: 900)
    kodi_smoke.write_failures("127.0.0.1", *_window(), 500)
    assert (kodi_smoke.KODI_LOG, 500) in reads and not any(p == kodi_smoke.KODI_OLD_LOG for p, _ in reads)


# --- artwork (003) -----------------------------------------------------------------------------------------------

def _img(url):
    return "image://" + kodi_smoke.urllib.parse.quote(url, safe="").replace("%2F", "%2f").replace("%3A", "%3a") + "/"


E = "http://localhost:8888/api"


def test_art_ref_parses_kodi_image_urls():
    assert kodi_smoke.art_ref(_img(f"{E}/thumbnails/96")) == ("thumb", 96)
    assert kodi_smoke.art_ref(_img(f"{E}/channels/3272102056/logo")) == ("logo", 3272102056)
    assert kodi_smoke.art_ref(f"{E}/channels/5/logo|Authorization=Basic%20x") == ("logo", 5)
    assert kodi_smoke.art_ref("DefaultMovies.png") == ("file", "DefaultMovies.png")
    assert kodi_smoke.art_ref(_img("/opt/x/icon.png")) == ("file", "/opt/x/icon.png")


def _f(rid, **art):
    return {"filetype": "file", "label": f"r{rid}", "file": f"plugin://plugin.video.epgstation/?action=play&id={rid}",
            "art": {k: _img(v) for k, v in art.items()}}


def test_recording_art_failures():
    records = {1: {"id": 1, "channelId": 5, "thumbnails": [9, 11]}, 2: {"id": 2, "channelId": 6, "thumbnails": [12]},
               3: {"id": 3, "channelId": 7, "thumbnails": []}}
    logos = {5, 6}
    t11, logo5, logo6 = f"{E}/thumbnails/11", f"{E}/channels/5/logo", f"{E}/channels/6/logo"
    good = [_f(1, thumb=t11, poster=t11, fanart=t11, landscape=t11, icon=logo5),
            _f(2, thumb=logo6, icon=logo6), _f(3, thumb="/x/icon.png", icon="/x/icon.png")]
    assert kodi_smoke.recording_art_failures(good, records, logos, blank={12}) == []
    bad = kodi_smoke.recording_art_failures(good, records, logos, blank=set())  # 12 not blank: should be a thumbnail
    assert len(bad) == 1 and "r2" in bad[0]
    assert kodi_smoke.recording_art_failures([_f(1, thumb=f"{E}/thumbnails/9", icon=logo5)], records, logos, set())
    assert kodi_smoke.recording_art_failures([_f(1, thumb=f"{E}/thumbnails/9", icon=logo5)], records, logos, set(),
                                             strict=False) == []  # a library list may lag behind a regeneration
    assert kodi_smoke.recording_art_failures([_f(3, thumb=f"{E}/channels/7/logo")], records, logos, set())
    assert kodi_smoke.recording_art_failures([_f(1, thumb=t11, fanart=logo5)], records, logos, set())
    assert kodi_smoke.recording_art_failures([_f(1, icon=logo5)], records, logos, set())  # no thumb


def test_show_art_and_folder_icon_failures():
    t11, logo5 = f"{E}/thumbnails/11", f"{E}/channels/5/logo"
    show = {"filetype": "directory", "label": "S", "file": "plugin://x/?action=show&key=s"}
    assert kodi_smoke.show_art_failures([dict(show, art={"thumb": _img(t11), "fanart": _img(t11),
                                                         "icon": _img(logo5)})], {5}, set()) == []
    assert kodi_smoke.show_art_failures([dict(show, art={"thumb": _img(t11)})], {5}, {11})
    assert kodi_smoke.show_art_failures([dict(show, art={"thumb": _img(logo5)})], set(), set())
    assert kodi_smoke.show_art_failures([dict(show, art={"thumb": _img(logo5), "fanart": _img(logo5)})], {5}, set())
    assert kodi_smoke.show_art_failures([dict(show, art={})], {5}, set())
    folders = [{"label": "a", "file": "plugin://x/?action=movies", "art": {"icon": "DefaultMovies.png",
                                                                           "thumb": "DefaultMovies.png"}},
               {"label": "b", "file": "plugin://x/?action=browse", "art": {"icon": "DefaultFolder.png"}}]
    want = {"movies": "DefaultMovies.png", "browse": "DefaultFolder.png"}
    assert kodi_smoke.menu_icon_failures(folders[:1], want, "root") == []
    assert kodi_smoke.menu_icon_failures(folders, want, "root") == ["root item 'b' art {'icon': 'DefaultFolder.png'}, "
                                                                    "expected icon = thumb = 'DefaultFolder.png'"]
    chans = [{"label": "N (3)", "file": "plugin://x/?action=list&channelId=5", "art": {"icon": _img(logo5),
                                                                                         "thumb": _img(logo5)}},
             {"label": "M (1)", "file": "plugin://x/?action=list&channelId=6",
              "art": {"icon": "DefaultStudios.png", "thumb": "DefaultStudios.png"}}]
    assert kodi_smoke.channel_folder_failures(chans, {5}) == []
    assert kodi_smoke.channel_folder_failures(chans, {5, 6})
    assert kodi_smoke.channel_folder_failures(chans, set())


# --- full detail view (002) ------------------------------------------------------------------------------------------

def _detail_files(records, channels, lang, strings):
    """What Kodi's Files.GetDirectory reports for these recordings (the fields check_details reads)."""
    from resources.lib.epgstation import items

    out = []
    for rec in records:
        d = items.recorded_item(rec, kodi_smoke._LABEL_API, channels, lang, strings)
        sd = {"audio": [dict(d["audio"], language="")] if d["audio"] else [],
              "video": [{"aspect": d["stream"]["aspect"]}] if d["stream"] else []}
        out.append({"label": d["label"], "file": d["path"], "filetype": "file", "plot": d["info"]["plot"],
                    "streamdetails": sd})
    return out


@pytest.mark.parametrize("fixture_set", ["dev", "dev2"])
@pytest.mark.parametrize("ui,lang", [("ja", "ja"), ("en", "ja"), ("ja", "en"), ("en", "en")])
def test_check_details_accepts_the_addon_items(monkeypatch, fixture_set, ui, lang):
    import json
    import os

    from conftest import FIXTURES

    def load(*parts):
        with open(os.path.join(FIXTURES, fixture_set, "api", *parts), encoding="utf-8") as f:
            return json.load(f)

    records = load("recorded.json")["records"]
    channels = load("channels.json")
    names = kodi_smoke.items.channel_names(channels)
    files = _detail_files(records, names, lang, kodi_smoke.DETAIL_STRINGS[0 if ui == "ja" else 1])
    settings = f'<settings>\n<setting id="genre_language">{1 if lang == "en" else 0}</setting>\n</settings>'
    log = {"text": ""}

    def read_vm_file(host, path, offset=0):
        return settings if path == kodi_smoke.ADDON_SETTINGS else log["text"]

    locale = "resource.language.ja_jp" if ui == "ja" else "resource.language.en_gb"
    monkeypatch.setattr(kodi_smoke, "rpc", lambda host, method, **params: {"value": locale})
    monkeypatch.setattr(kodi_smoke, "listdir", lambda host, url: files)
    monkeypatch.setattr(kodi_smoke, "epgs_records", lambda epgs: {r["id"]: r for r in records})
    monkeypatch.setattr(kodi_smoke, "epgs_get", lambda epgs, path, **params: channels)
    monkeypatch.setattr(kodi_smoke, "read_vm_file", read_vm_file)
    assert kodi_smoke.check_details("h", "http://e") == []
    # only the one combination: Kodi's other language is a failure
    other = "resource.language.en_gb" if ui == "ja" else "resource.language.ja_jp"
    monkeypatch.setattr(kodi_smoke, "rpc", lambda host, method, **params: {"value": other})
    assert any(" block " in f for f in kodi_smoke.check_details("h", "http://e"))
    monkeypatch.setattr(kodi_smoke, "rpc", lambda host, method, **params: {"value": locale})
    # a wrong audio stream, a missing block line and a drop-log request during the listing are caught
    bad = [dict(f, streamdetails={"audio": [], "video": []}) for f in files[:1]] + \
        [dict(f, plot=f["plot"].replace("\n", "\nX", 1) if "\n\n" not in f["plot"]
              else f["plot"][:f["plot"].rindex("\n\n") + 2] + "X" + f["plot"][f["plot"].rindex("\n\n") + 2:])
         for f in files[1:2]]
    monkeypatch.setattr(kodi_smoke, "listdir", lambda host, url: bad)
    log["text"] = _access(kodi_smoke._iso(kodi_smoke.time.time()) + ".000", "GET", "/api/dropLogs/3", ADDON_UA)
    failures = kodi_smoke.check_details("h", "http://e")
    assert any("streamdetails audio" in f for f in failures)
    assert any(f.startswith(f"recording {files[1]['file'].rsplit('=', 1)[1]} block ") for f in failures)
    assert any("listing requested a drop log" in f for f in failures)


def test_genre_lang_from_settings():
    assert kodi_smoke.genre_lang('<setting id="genre_language" default="true">0</setting>') == "ja"
    assert kodi_smoke.genre_lang('<setting id="genre_language">1</setting>') == "en"
    assert kodi_smoke.genre_lang("<settings/>") == "ja"


def test_detail_strings_from_po():
    ja, en = kodi_smoke.DETAIL_STRINGS
    assert ja["detail_header"] == "【録画情報】" and en["detail_header"] == "[Recording]"
    assert ja["files"] == "ファイル: {0}" and en["weekdays"].startswith("Sun,")


def test_genre_smoke_helpers():  # subgenres (009)
    from resources.lib.epgstation import items

    records = {1: {"id": 1, "startAt": 10, "genre1": 6, "subGenre1": 0},
               2: {"id": 2, "startAt": 20, "genre1": None, "genre2": 6, "subGenre2": 1},
               3: {"id": 3, "startAt": 30, "genre1": 6, "subGenre1": 0, "genre2": 11, "subGenre2": 5}}
    entries = kodi_smoke.genre_entries(records)

    def files(dicts):
        return [{"label": d["label"], "file": d["path"]} for d in dicts]
    for lang in ("ja", "en"):
        assert kodi_smoke.genres_failures(files(items.genre_folders(entries, lang)), entries) == []
        for ui in ("ja", "en"):  # dev Kodi default: English UI ("All") with Japanese genre names
            subs = files(items.subgenre_folders(6, entries, lang, kodi_smoke._po(ui)[30270]))
            assert kodi_smoke.subgenre_folder_failures(subs, 6, entries) == []
    assert subs[0]["label"] == "All (3)" and kodi_smoke._po("ja")[30270] == "すべて ({0})"
    assert kodi_smoke.genres_failures(files(items.genre_folders(entries, "ja"))[1:], entries)
    assert kodi_smoke.subgenre_folder_failures(subs[1:], 6, entries)
    listed = [{"label": "x", "file": items.plugin_url(action="play", id=i)} for i in (3, 1)]
    assert kodi_smoke.subgenre_list_failures(listed, 6, 0, entries) == []
    assert kodi_smoke.subgenre_list_failures(listed[::-1], 6, 0, entries)


def test_make_deploy_and_smoke_pass_only_a_given_host(tmp_path):
    """023 review: without local.env / KODI_HOST, make deploy and make smoke hand an empty host to the tools (which
    refuse), not the Makefile's localhost default; a host on the command line (Gitea CI: 127.0.0.1) passes through."""
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items() if k not in ("KODI_HOST", "MAKEFLAGS", "MFLAGS")}
    env["LOCAL_ENV"] = str(tmp_path / "missing.env")
    out = subprocess.run(["make", "-n", "-C", root, "deploy", "smoke"], env=env, capture_output=True, text=True,
                         check=True).stdout
    assert 'KODI_HOST="" tools/deploy.sh ""' in out and 'kodi_smoke.py --host ""' in out
    out = subprocess.run(["make", "-n", "-C", root, "deploy", "smoke", "KODI_HOST=127.0.0.1"], env=env,
                         capture_output=True, text=True, check=True).stdout
    assert 'tools/deploy.sh "127.0.0.1"' in out and 'kodi_smoke.py --host "127.0.0.1"' in out
    r = subprocess.run(["bash", os.path.join(root, "tools", "deploy.sh"), ""], env=dict(env, KODI_HOST=""),
                       capture_output=True, text=True, cwd=tmp_path, timeout=60)
    assert r.returncode == 1 and r.stderr.startswith("no Kodi host") and r.stderr.count("\n") == 1
