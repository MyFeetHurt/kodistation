"""Backlog 012: live TV logic (resources/lib/epgstation/live.py), against the captured schedules."""
import json
import os

import fake_epgstation
import pytest
from conftest import FIXTURES, ROOT
from resources.lib.epgstation import api as api_mod
from resources.lib.epgstation import epgmeta, items, live
from resources.lib.epgstation.genres import genre_name

STRINGS = {"GR": "Terrestrial", "SKY": "SKY", "next": "Next", "left": "{0} min left",
           "no_tuner": "No free tuner", "failed": "Could not start live stream"}
MIN = 60 * 1000
ERRORS = json.load(open(os.path.join(FIXTURES, "live_errors.json"), encoding="utf-8"))


LIVE = os.path.join(FIXTURES, "live")  # one shared copy: dev and dev2 come from the same server (L4)


def _load(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(params=["dev", "dev2"])
def fx(request):
    """The captured free channels plus the two synthetic paid ones (020), merged like the fake server does."""
    paid = fake_epgstation.synthetic_paid()
    return {"schedules": _load(LIVE, "schedules.json") + paid["schedules"],
            "broadcasting": fake_epgstation.merged_live("broadcasting"),
            "channels": _load(FIXTURES, request.param, "api", "channels.json") + paid["channels"],
            "now": _load(LIVE, "live_meta.json")["capturedAt"]}


def test_live_fixtures_are_small_and_shared():
    """L4: one copy, now + next per channel, broadcasting trimmed to the ids the consistency test needs."""
    for name in ("dev", "dev2"):
        assert not os.path.exists(os.path.join(FIXTURES, name, "api", "schedules.json"))
        assert not os.path.exists(os.path.join(FIXTURES, name, "api", "schedules"))
    assert all(len(s["programs"]) <= 2 for s in _load(LIVE, "schedules.json"))
    for s in _load(LIVE, "broadcasting.json"):
        assert set(s["channel"]) == {"id"} and all(set(p) == {"id"} for p in s["programs"])


def api(username=""):
    return api_mod.EPGStation("http://epgs:8888", username, "pw")


def prog(pid, start, end, name="P", **kw):
    return dict({"id": pid, "channelId": 1, "startAt": start, "endAt": end, "isFree": True, "name": name}, **kw)


# --- watchable -------------------------------------------------------------------------------------------------

def test_watchable_tv_services_only():
    for t in (0x01, 0xA1, 0xA5, 0xAD):
        assert live.watchable({"type": t}), t
    for t in (0x02, 0xA2, 0xA6, 0xC0, 0xA4):  # radio (user decision: TV only), data, Dpa
        assert not live.watchable({"type": t}), t
    assert live.watchable({})  # no type: shown


def test_fixture_drops_radio_and_data(fx):
    out = live.channel_items(fx["schedules"], api(), fx["now"], "ja", STRINGS)
    names = [d["props"]["epgstation.channel"] for d in out]
    assert not [n for n in names if "携帯" in n or "Gガイド" in n or "ラジオ" in n]
    kept = [s for s in fx["schedules"] if s["channel"].get("type") in (None, 1)
            and live.now_next(s["programs"], fx["now"])[0]]
    assert len(out) == len(kept)


# --- now / next ------------------------------------------------------------------------------------------------

def test_now_next_boundaries():
    a, b, c = prog(1, 0, 10 * MIN), prog(2, 10 * MIN, 20 * MIN), prog(3, 25 * MIN, 30 * MIN)
    assert live.now_next([a, b, c], 0) == (a, b)  # startAt == now is current
    assert live.now_next([a, b, c], 10 * MIN) == (b, c)  # endAt == now is not
    assert live.now_next([a, b, c], 26 * MIN) == (c, None)
    assert live.now_next([a, b, c], 22 * MIN) == (None, None)
    assert live.now_next([], 0) == (None, None)
    overlap = prog(4, 5 * MIN, 15 * MIN)  # starts before the current one ends: not "next"
    assert live.now_next([a, overlap, b], 1 * MIN) == (a, b)


def test_now_next_matches_broadcasting(fx):
    on_air = {s["channel"]["id"]: s["programs"][0]["id"] for s in fx["broadcasting"] if s["programs"]}
    checked = 0
    for s in fx["schedules"]:
        cur, _ = live.now_next(s["programs"], fx["now"])
        if cur is not None and s["channel"]["id"] in on_air:
            assert cur["id"] == on_air[s["channel"]["id"]], s["channel"]["name"]
            checked += not s["channel"]["name"].startswith("（合成）")
    assert checked >= 24  # 020: real (free) channels on air; the synthetic paid ones are not counted


def test_channel_without_current_program_is_skipped():
    sched = [{"channel": {"id": 1, "name": "A", "type": 1, "hasLogoData": False, "channelType": "GR"},
              "programs": [prog(1, 10 * MIN, 20 * MIN)]}]
    assert live.channel_items(sched, api(), 0, "ja", STRINGS) == []


# --- channel items ---------------------------------------------------------------------------------------------

def _sample(fx, channel_type="GR"):
    s = next(s for s in fx["schedules"] if s["channel"]["channelType"] == channel_type and s["channel"]["hasLogoData"])
    return s, live.now_next(s["programs"], fx["now"])


def test_channel_items_order_and_paid(fx):
    out = live.channel_items(fx["schedules"], api(), fx["now"], "ja", STRINGS)
    want = [s["channel"]["id"] for s in fx["schedules"]
            if live.watchable(s["channel"]) and live.now_next(s["programs"], fx["now"])[0]]
    assert [int(d["props"]["epgstation.channelId"]) for d in out] == want  # server order
    assert any(d["props"]["epgstation.isFree"] == "false" for d in out)  # paid channels stay


def test_channel_item_fields(fx):
    s, (cur, nxt) = _sample(fx)
    d, = live.channel_items([s], api(), fx["now"], "ja", STRINGS)
    name, title = items.to_half(s["channel"]["name"]), items.to_half(cur["name"])
    assert d["label"] == f"{name}  {title}"
    assert d["playable"] and not d["folder"] and d["context"] == []
    assert d["path"] == items.plugin_url(action="live_play", channelId=s["channel"]["id"], name=name)
    info = d["info"]
    assert info["mediatype"] == "video" and info["title"] == title
    assert info["studios"] == [name]
    assert info["duration"] == (cur["endAt"] - cur["startAt"]) // 1000
    want_genres = []
    for n in "123":
        g = genre_name(cur.get("genre" + n), cur.get("subGenre" + n), "ja")
        if g and g not in want_genres:
            want_genres.append(g)
    assert info["genres"] == want_genres
    rng = live.time_range(cur, fx["now"], STRINGS)
    assert rng.startswith(items.fmt_time(cur["startAt"], "%H:%M") + "-" + items.fmt_time(cur["endAt"], "%H:%M"))
    assert "min left" in rng
    assert info["tagline"] == f"{name}  {rng}"
    if nxt is not None:
        nline = f"Next: {items.fmt_time(nxt['startAt'], '%H:%M')}-{items.fmt_time(nxt['endAt'], '%H:%M')} " \
                f"{items.to_half(nxt['name'])}"
        # tester AC 7: at the top (Estuary cuts long plots off), right under the time range
        assert info["plot"].startswith(f"{rng}\n{nline}\n\n")
        if nxt.get("description"):
            assert info["plot"].rstrip().endswith(items.to_half(nxt["description"]).rstrip())
    else:
        assert info["plot"].startswith(rng + "\n\n")
        assert info["plotoutline"] == f"{rng}  Next: {items.fmt_time(nxt['startAt'], '%H:%M')} " \
                                      f"{items.to_half(nxt['name'])}"
        assert d["props"]["epgstation.next.title"] == items.to_half(nxt["name"])
        assert d["props"]["epgstation.next.startAt"] == items.fmt_time(nxt["startAt"], "%H:%M")
    if cur.get("description"):
        assert items.to_half(cur["description"]).splitlines()[0] in info["plot"]
    props = d["props"]
    assert props["epgstation.channelId"] == str(s["channel"]["id"])
    assert props["epgstation.channelType"] == "GR"
    assert props["epgstation.programId"] == str(cur["id"])
    assert 0 <= int(props["epgstation.progress"]) <= 100
    logo = f"http://epgs:8888/api/channels/{s['channel']['id']}/logo"
    assert d["art"] == {"icon": logo, "thumb": logo}


def test_channel_item_blocks_kodis_db_overlay(fx):
    """Tester: Kodi stores playCount and stream details for a live_play URL after a play and overlays them on
    the list (a watched mark, "00:19 MPEG-2"). An explicit playcount and own stream details stop that
    (measured on Kodi 21.3)."""
    s, (cur, _) = _sample(fx)
    d, = live.channel_items([s], api(), fx["now"], "ja", STRINGS)
    assert d["info"]["playcount"] == 0
    assert d["stream"] == {"codec": "", "width": 0, "height": 0, "duration": (cur["endAt"] - cur["startAt"]) // 1000}


def test_channel_item_english_genres_and_metadata(fx):
    s, (cur, _) = _sample(fx)
    d, = live.channel_items([s], api(), fx["now"], "en", STRINGS)
    assert d["info"]["genres"] == [g for g in dict.fromkeys(
        genre_name(cur.get("genre" + n), cur.get("subGenre" + n), "en") for n in "123") if g]
    meta = epgmeta.metadata(cur)
    if meta and meta["cast"]:
        assert d["info"]["cast"]


def test_logo_only_with_logo_data_and_auth_headers():
    ch = {"id": 7, "name": "Ｘ", "type": 1, "channelType": "BS", "hasLogoData": False}
    sched = [{"channel": ch, "programs": [prog(1, 0, 10 * MIN)]}]
    d, = live.channel_items(sched, api(), MIN, "ja", STRINGS)
    assert d["art"] == {}
    ch["hasLogoData"] = True
    d, = live.channel_items(sched, api("user"), MIN, "ja", STRINGS)
    assert d["art"]["thumb"].startswith("http://epgs:8888/api/channels/7/logo|Authorization=")
    assert d["label"].startswith("X  ")


def test_metadata_failure_falls_back_to_raw_text(monkeypatch):
    def boom(item, on_error=None):
        on_error(ValueError("x"))
        return None

    monkeypatch.setattr(epgmeta, "metadata", boom)
    seen = []
    sched = [{"channel": {"id": 1, "name": "A", "type": 1, "hasLogoData": False, "channelType": "GR"},
              "programs": [prog(1, 0, 10 * MIN, "Ｔｉｔｌｅ", description="ＤＥＳＣ", extended="◇出演\nＡ")]}]
    d, = live.channel_items(sched, api(), MIN, "ja", STRINGS, on_error=lambda p, e: seen.append((p["id"], e)))
    assert "DESC" in d["info"]["plot"] and "出演" in d["info"]["plot"]
    assert d["info"]["title"] == "Title"
    assert seen and seen[0][0] == 1


def test_progress_and_remaining():
    p = prog(1, 0, 10 * MIN)
    assert live.progress(p, 0) == 0
    assert live.progress(p, 5 * MIN) == 50
    assert live.progress(p, 20 * MIN) == 100
    assert live.progress(p, -MIN) == 0
    assert live.progress(prog(1, 0, 0), 0) == 0
    assert live.remaining_minutes(p, 5 * MIN) == 5
    assert live.remaining_minutes(p, 9 * MIN + 1000) == 1  # never "0 min left" while on air
    assert "5 min left" in live.time_range(p, 5 * MIN, STRINGS)


# --- type folders ------------------------------------------------------------------------------------------------

def test_type_folders(fx):
    types = live.live_types(fx["channels"])
    assert types == ["GR", "BS", "CS"]
    folders = live.type_folders(types, STRINGS)
    assert [f["label"] for f in folders] == ["Terrestrial", "BS", "CS"]
    assert all(f["folder"] for f in folders)
    assert folders[0]["path"] == items.plugin_url(action="live", type="GR")


def test_live_types_single_and_filtered():
    assert live.live_types([{"channelType": "BS", "type": 1}, {"channelType": "GR", "type": 0xC0}]) == ["BS"]
    assert live.live_types([{"channelType": "SKY", "type": 1}, {"channelType": "GR", "type": 1}]) == ["GR", "SKY"]
    assert live.type_label("SKY", STRINGS) == "SKY" and live.type_label("CS", STRINGS) == "CS"


# --- stream mode and errors --------------------------------------------------------------------------------------

M3 = [{"name": "720p", "isUnconverted": False}, {"name": "480p", "isUnconverted": False},
      {"name": "無変換", "isUnconverted": True}]


def test_mode_index():
    assert [live.mode_index(M3, c) for c in (0, 1, 2)] == [2, 0, 1]
    assert live.mode_index(M3[:2], 0) == 0  # no unconverted mode
    assert [live.mode_index(M3[2:], c) for c in (0, 1, 2)] == [0, 0, 0]
    only = [{"name": "a", "isUnconverted": False}, {"name": "b", "isUnconverted": False}, M3[2]]
    assert live.mode_index(only[2:], 2) == 0
    assert live.mode_index([], 0) == 0 and live.mode_index([], 2) == 0
    assert live.mode_index(M3, 9) == 2  # unknown choice: unconverted


def test_mode_index_unconverted_is_its_index():
    modes = [M3[2], M3[0]]
    assert [live.mode_index(modes, c) for c in (0, 1, 2)] == [0, 1, 1]


def test_live_enabled_both_config_keys():
    """L3: real 2.10.0 sends isEnableTSLiveStream; docs/api/openapi.json names isEnableLiveStream."""
    assert live.live_enabled({"isEnableTSLiveStream": True})
    assert live.live_enabled({"isEnableLiveStream": True})
    assert not live.live_enabled({"isEnableTSLiveStream": False})
    assert not live.live_enabled({"isEnableLiveStream": False})
    assert not live.live_enabled({})


def test_m2ts_modes_openapi_shape():
    """L3: openapi.json has streamConfig.live.m2ts; real 2.10.0 has streamConfig.live.ts.m2ts."""
    assert live.m2ts_modes({"streamConfig": {"live": {"m2ts": M3}}}) == M3
    assert live.m2ts_modes({"streamConfig": {"live": {"ts": {"m2ts": M3}, "m2ts": []}}}) == M3


def test_m2ts_modes_from_config():
    cfg = {"streamConfig": {"live": {"ts": {"m2ts": M3}}}}
    assert live.m2ts_modes(cfg) == M3
    assert live.m2ts_modes({}) == []
    assert live.mode_name(M3, 2) == "無変換" and live.mode_name([], 0) == ""


def test_error_message():
    """Tester: the notification cut the server text off ("...: Bad st"): short text, the HTTP status from the inner
    "Bad status respond (NNN ...)" when present; the full text only goes to the log."""
    assert live.error_message(500, ERRORS["no_tuner"], STRINGS) == "No free tuner"
    assert live.error_message(500, ERRORS["no_such_service"], STRINGS) == "Could not start live stream (HTTP 404)"
    assert live.error_message(500, {"code": 500, "errors": "something else"}, STRINGS) == \
        "Could not start live stream (HTTP 500)"
    assert live.error_message(502, None, STRINGS) == "Could not start live stream (HTTP 502)"
    assert live.error_message(None, None, STRINGS) == "Could not start live stream"
    assert live.error_message(200, None, STRINGS) is None
    assert live.error_message(500, {"errors": "Mirakurun 503 Service Unavailable"}, STRINGS) == "No free tuner"


def test_play_info(fx):
    s, (cur, nxt) = _sample(fx)
    name = items.to_half(s["channel"]["name"])
    info = live.play_info(cur, nxt, name, fx["now"], "ja", STRINGS)
    assert info["title"] == items.to_half(cur["name"]) and info["studios"] == [name]
    assert info["mediatype"] == "video" and info["plot"].startswith(live.time_range(cur, fx["now"], STRINGS))
    assert "duration" not in info  # L1: no length, so Kodi never stores a resume point for the live_play URL
    assert info["tagline"] == f"{name}  {live.time_range(cur, fx['now'], STRINGS)}"
    assert live.play_info(None, None, "NHK", fx["now"], "ja", STRINGS) == \
        {"title": "NHK", "studios": ["NHK"], "mediatype": "video"}


# --- api additions ----------------------------------------------------------------------------------------------

def test_api_urls():
    a = api()
    assert a.live_url(3211240960, 2) == "http://epgs:8888/api/streams/live/3211240960/m2ts?mode=2"


# --- start watch (M1): what to do after setResolvedUrl, from Kodi player callbacks -----------------------------

URL = "http://epgs:8888/api/streams/live/3211240960/m2ts?mode=2"
PLUGIN_URL = "plugin://plugin.video.epgstation/?action=live_play&channelId=3211240960&name=NHK"


def test_start_watch_is_ours():
    w = live.StartWatch(3211240960)
    assert w.is_ours(URL) and w.is_ours(PLUGIN_URL) and w.is_ours(URL + "|Authorization=Basic%20x")
    assert not w.is_ours("http://epgs:8888/api/streams/live/32112409601/m2ts?mode=2")
    assert not w.is_ours("plugin://plugin.video.epgstation/?action=live_play&channelId=1")
    assert not w.is_ours("http://epgs:8888/api/videos/5") and not w.is_ours("")


def test_start_watch_av_started_is_done():
    w = live.StartWatch(3211240960)
    w.on_av_started(URL)
    assert w.result == live.STARTED and not w.probe_now()
    w.on_error()  # a later error of the playing stream is not a start failure
    w.on_stopped()
    assert w.result == live.STARTED
    assert not w.probe_at_backstop(player_active=False)


def test_start_watch_playback_started_is_only_an_attempt():
    """Kodi 21 sends onPlayBackStarted before it opens the stream, also for a start that then fails."""
    w = live.StartWatch(3211240960)
    w.on_playback_started(URL)
    assert w.result is None and w.tried


def test_start_watch_started_other_file_is_ignored():
    w = live.StartWatch(3211240960)
    w.on_av_started("http://epgs:8888/api/videos/5")
    assert w.result is None


def test_start_watch_error_probes_now():
    w = live.StartWatch(3211240960)
    w.on_error()
    assert w.result == live.FAILED and w.probe_now()
    assert not w.probe_now()  # once
    assert not w.probe_at_backstop(player_active=False)  # probed once already


def test_start_watch_stop_before_start_waits_for_kodis_verdict():
    """Kodi 21 on a failed start: onPlayBackStarted, onPlayBackStopped, and its "Playback failed" dialog."""
    for event in ("on_stopped", "on_ended"):
        w = live.StartWatch(3211240960)
        w.on_playback_started(URL)
        getattr(w, event)()
        assert w.result == live.STOPPED and not w.probe_now()
        w.decide_stop(kodi_failed=True)
        assert w.result == live.FAILED and w.probe_now()
        w = live.StartWatch(3211240960)
        getattr(w, event)()
        w.decide_stop(kodi_failed=False)  # no failure dialog: the user cancelled
        assert w.result == live.CANCELLED and not w.probe_now()
        assert not w.probe_at_backstop(player_active=False)


def test_start_watch_decide_stop_only_after_a_stop():
    w = live.StartWatch(3211240960)
    w.decide_stop(kodi_failed=True)
    assert w.result is None


def test_start_watch_zapping_ignores_previous_file():
    prev = "http://epgs:8888/api/streams/live/3272102056/m2ts?mode=2"  # channel A still playing
    w = live.StartWatch(3211240960, previous_file=prev)
    w.poll(prev)
    assert w.result is None  # A is not "started"
    w.on_stopped()  # Kodi stopping A for B
    assert w.result is None
    w.poll("")
    assert w.probe_at_backstop(player_active=False)  # Kodi tried B, nothing plays: ask why
    assert not w.probe_at_backstop(player_active=True)  # never while a player runs (second tuner)


def test_start_watch_zapping_then_error_and_then_cancel():
    prev = "http://epgs:8888/api/videos/5"
    w = live.StartWatch(3211240960, previous_file=prev)
    w.on_ended()
    w.on_error()
    assert w.probe_now()
    w = live.StartWatch(3211240960, previous_file=prev)
    w.on_stopped()
    w.on_stopped()  # the second stop is B's: switching, so a failure (Kodi shows no dialog then)
    assert w.result == live.FAILED


def test_start_watch_poll():
    w = live.StartWatch(3211240960)
    w.poll(URL)  # Kodi reports our file for a moment also on a start that fails: only onAVStarted counts
    assert w.result is None and w.tried
    w = live.StartWatch(3211240960)
    w.poll("http://epgs:8888/api/videos/5")  # the user went on to something else
    assert w.result == live.OTHER and not w.probe_at_backstop(player_active=False)


def test_start_watch_no_player_start_never_probes():
    """Files.GetDirectory on a live_play URL: no callbacks, nothing playing."""
    w = live.StartWatch(3211240960)
    w.poll("")
    assert w.result is None and not w.probe_now()
    assert not w.probe_at_backstop(player_active=False)


def _marked(text, needle):
    """True if the line containing needle, or the line before it, has the live TV (012) marker."""
    lines = text.splitlines()
    return any(needle in line and ("live TV (012)" in line or (n and "live TV (012)" in lines[n - 1]))
               for n, line in enumerate(lines))


def test_removal_checklist_lists_every_hook():
    """L5: live.py's docstring names every file with a live TV (012) hook; the root entries carry the marker."""
    files = [os.path.join(ROOT, "Makefile")]
    for top in ("plugin.video.epgstation", "tools", "tests"):
        files += [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(ROOT, top)) for f in fs
                  if f.endswith((".py", ".xml", ".po")) and "fixtures" not in d and "__pycache__" not in d]
    live_py = os.path.abspath(live.__file__)
    hooks = {os.path.basename(f) for f in files
             if os.path.abspath(f) != live_py and "live TV (012)" in open(f, encoding="utf-8").read()}
    hooks |= {"test_live.py", "test_smoke_tool.py", "test_plugin.py", "fake_epgstation.py", "snapshot_api.py"}
    missing = sorted(h for h in hooks if h not in live.__doc__)
    assert not missing, missing
    plugin = os.path.join(ROOT, "plugin.video.epgstation", "resources", "lib", "epgstation", "plugin.py")
    assert _marked(open(plugin, encoding="utf-8").read(), '("live", 30015)')
    assert _marked(open(os.path.join(ROOT, "tools", "kodi_smoke.py"), encoding="utf-8").read(), "ROOT_ACTIONS = ")


def test_start_watch_switch_from_recording_tester_sequence():
    """Tester L-b (Kodi 21.3): a recording plays, then live_play: onPlayBackStarted (ours), one onPlayBackStopped,
    no separate stop for the recording and no failure dialog. That stop is ours and a failure: probe at once."""
    rec = "http://epgs:8888/api/videos/57"
    for started_path in (URL, PLUGIN_URL, ""):
        w = live.StartWatch(3211240960, previous_file=rec)
        w.on_playback_started(started_path)
        w.on_stopped()
        assert w.result == live.FAILED, started_path
        assert w.probe_now()


def test_start_watch_switch_with_stop_of_the_previous_file_first():
    w = live.StartWatch(3211240960, previous_file="http://epgs:8888/api/videos/57")
    w.on_stopped()  # the recording
    assert w.result is None
    w.on_playback_started(URL)
    w.on_ended()
    assert w.result == live.FAILED and w.probe_now()


def test_start_watch_switch_started_other_file_keeps_previous_stop_pending():
    prev = "http://epgs:8888/api/videos/57"
    w = live.StartWatch(3211240960, previous_file=prev)
    w.on_playback_started("http://epgs:8888/api/videos/99")  # some other file: not our attempt
    w.on_stopped()
    assert w.result is None


def test_live_mode_setting_explains_the_options():
    """Tester: the 3 fixed options (string IDs 30232-30234 kept) say what they mean on the server, with help text."""
    import re
    import xml.etree.ElementTree as ET

    res = os.path.join(ROOT, "plugin.video.epgstation", "resources")
    setting = ET.parse(os.path.join(res, "settings.xml")).find(".//setting[@id='live_mode']")
    assert [o.get("label") for o in setting.iter("option")] == ["30232", "30233", "30234"]
    help_id = setting.get("help")
    assert help_id
    for lang in ("en_gb", "ja_jp"):
        po = open(os.path.join(res, "language", f"resource.language.{lang}", "strings.po"), encoding="utf-8").read()
        entries = re.findall(r'msgctxt "#(\d+)"\nmsgid "([^"]*)"\nmsgstr "([^"]*)"', po)
        texts = {i: (translated or english) for i, english, translated in entries}
        assert texts.get(help_id), lang
        assert "720p" in texts["30233"] and "480p" in texts["30234"], lang
