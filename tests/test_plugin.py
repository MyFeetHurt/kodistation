"""Run the real plugin code against the fake EPGStation through the Kodi stubs."""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
import xbmc
import xbmcaddon
import xbmcgui
from conftest import FIXTURES, run_plugin


def test_root_menu(kodi):
    run_plugin()
    assert kodi.ended is True
    labels = [li.label for _, li, _ in kodi.items]
    assert labels == ["Movies", "TV shows", "Recent recordings", "Recording now", "Scheduled recordings", "Live TV",
                      "Search", "Browse"]
    # protect / delete (006): 検索 is a click action (asks, then Container.Update to a URL with the keyword)
    assert [li.label for _, li, folder in kodi.items if not folder] == ["Search"]


def test_browse_menu(kodi):
    run_plugin("action=browse")
    assert kodi.ended is True
    assert [li.label for _, li, _ in kodi.items] == ["By channel", "By genre", "By rule"]
    for url, _, folder in list(kodi.items):
        assert folder
        kodi.reset()  # same stub state object as xbmcplugin.state
        run_plugin(url.split("?", 1)[1])
        assert kodi.ended is True and kodi.items


def test_recent_list_pages(kodi):
    xbmcaddon.settings["page_size"] = "10"
    run_plugin("action=list")
    assert kodi.ended is True
    recs = [li for _, li, folder in kodi.items if not folder]
    assert 0 < len(recs) <= 10
    first = recs[0]
    assert first.tag.data["title"]
    assert first.props["IsPlayable"] == "true"
    # newest first
    dates = [li.tag.data["dateadded"] for li in recs]
    assert dates == sorted(dates, reverse=True)


def test_sort_methods_show_our_label(kodi):
    """Without labelMask %L Kodi shows the info-tag title and drops [Partial] / the red dot."""
    run_plugin("action=list")
    assert kodi.sort_methods
    assert all(label == "%L" and label2 for _, label, label2 in kodi.sort_methods)


def test_channel_and_genre_folders_filter(kodi):
    run_plugin("action=channels")
    url, li, folder = kodi.items[0]
    assert folder and "channelId=" in url
    count = int(li.label.rsplit("(", 1)[1].rstrip(")"))
    kodi.reset()  # same stub state object as xbmcplugin.state
    xbmcaddon.settings["page_size"] = "500"
    run_plugin(url.split("?", 1)[1])
    assert len([1 for *_, f in kodi.items if not f]) == count


def test_play_resolves_to_video_url(kodi, fake_server):
    run_plugin("action=list")
    rec_id = kodi.items[0][1].props["epgstation.recordedId"]
    kodi.reset()  # same stub state object as xbmcplugin.state
    run_plugin(f"action=play&id={rec_id}")
    ok, li = kodi.resolved
    assert ok and li.path.startswith(fake_server + "/api/videos/")


def test_search_uses_dialog_keyword(kodi):
    xbmcgui.input_answers.append("zzz-no-such-program")
    run_plugin("action=search")  # an old folder link (protect / delete (006): the root uses search_ask)
    assert kodi.ended is True
    assert kodi.items == []


def test_unreachable_server_notifies(kodi):
    xbmcaddon.settings["server_url"] = "http://127.0.0.1:9"
    run_plugin("action=list")
    assert kodi.ended is False
    assert xbmcgui.notifications and xbmcgui.notifications[0][2] == "error"


def _duration_requests(monkeypatch):
    """Record every /videos/<id>/duration GET the addon makes."""
    from resources.lib.epgstation import api

    seen, real_get = [], api.EPGStation.get

    def spy(self, path, *a, **kw):
        if path.endswith("/duration"):
            seen.append(path)
        return real_get(self, path, *a, **kw)

    monkeypatch.setattr(api.EPGStation, "get", spy)
    return seen


def _fixture_duration(fake_server, rec, prefer="encoded"):
    from resources.lib.epgstation import items

    video = items.pick_video(rec["videoFiles"], prefer)
    with urllib.request.urlopen(f"{fake_server}/api/videos/{video['id']}/duration") as resp:
        return json.loads(resp.read())["duration"]


def test_list_shows_real_durations_and_caches_them(kodi, fake_server, monkeypatch):
    seen = _duration_requests(monkeypatch)
    run_plugin("action=list")
    recs = [li for _, li, folder in kodi.items if not folder]
    assert seen and len(seen) <= len(recs)
    by_id = {r["id"]: r for r in _get(fake_server, "/api/recorded?limit=1000")["records"]}
    for li in recs:
        rec = by_id[int(li.props["epgstation.recordedId"])]
        if rec.get("isRecording") or not rec["videoFiles"]:
            continue
        real = _fixture_duration(fake_server, rec)
        scheduled = (rec["endAt"] - rec["startAt"]) // 1000
        if real is None:  # server could not measure it: scheduled length, no flag
            assert li.tag.data["duration"] == scheduled
            assert "epgstation.partial" not in li.props
            continue
        assert li.tag.data["duration"] == int(round(real))
        assert (li.props.get("epgstation.partial") == "true") == (real < 0.9 * scheduled)
    # second open: everything comes from the cache
    del seen[:]
    kodi.reset()  # same stub state object as xbmcplugin.state
    run_plugin("action=list")
    assert seen == []
    assert [li.tag.data["duration"] for _, li, f in kodi.items if not f] == \
        [li.tag.data["duration"] for li in recs]


def test_real_duration_setting_off(kodi, fake_server, monkeypatch):
    seen = _duration_requests(monkeypatch)
    xbmcaddon.settings["real_duration"] = "false"
    run_plugin("action=list")
    by_id = {r["id"]: r for r in _get(fake_server, "/api/recorded?limit=1000")["records"]}
    for _, li, folder in kodi.items:
        if not folder:
            rec = by_id[int(li.props["epgstation.recordedId"])]
            assert li.tag.data["duration"] == (rec["endAt"] - rec["startAt"]) // 1000
            assert "epgstation.partial" not in li.props
    assert seen == []


def test_play_resolves_with_metadata(kodi, fake_server):
    run_plugin("action=list")
    first = kodi.items[0][1]
    rec_id = first.props["epgstation.recordedId"]
    kodi.reset()  # same stub state object as xbmcplugin.state
    run_plugin(f"action=play&id={rec_id}")
    ok, li = kodi.resolved
    assert ok and li.getPath().startswith(fake_server + "/api/videos/")
    assert li.tag.data["title"] == first.tag.data["title"]
    assert li.tag.data["plot"] == first.tag.data["plot"]
    assert li.tag.data["duration"] == first.tag.data["duration"]
    assert li.art == first.art and li.art.get("icon")


def _multi_file_recording(fake_server):
    for rec in _get(fake_server, "/api/recorded?limit=1000")["records"]:
        if len(rec["videoFiles"]) > 1 and not rec.get("isRecording"):
            # the addon reads /recorded/{id}, whose videoFiles can be in another order than the list's
            return _get(fake_server, f"/api/recorded/{rec['id']}")
    pytest.skip("no recording with several video files in this fixture set")


def test_play_follows_prefer_setting(kodi, fake_server):
    rec = _multi_file_recording(fake_server)
    from resources.lib.epgstation import items

    for setting, prefer in (("0", "encoded"), ("1", "ts")):
        kodi.reset()  # same stub state object as xbmcplugin.state
        xbmcaddon.settings["prefer_video"] = setting
        run_plugin(f"action=play&id={rec['id']}")
        want = items.pick_video(rec["videoFiles"], prefer)["id"]
        assert kodi.resolved[1].getPath() == f"{fake_server}/api/videos/{want}"


def test_choose_plays_chosen_file_with_metadata(kodi, fake_server):
    from resources.lib.epgstation import items

    rec = _multi_file_recording(fake_server)
    ts = [i for i, v in enumerate(rec["videoFiles"]) if v["type"] == "ts"][0]
    xbmcgui.select_answers.append(ts)
    run_plugin(f"action=choose&id={rec['id']}")
    heading, labels = xbmcgui.selects[0]
    assert heading == "Choose video file"
    assert len(labels) == len(rec["videoFiles"])
    assert all(re.search(r"B  \d+:\d\d$", label) for label in labels)
    (url, li), = xbmc.played
    assert url == f"{fake_server}/api/videos/{rec['videoFiles'][ts]['id']}" == li.getPath()
    assert li.tag.data["originaltitle"] == items.to_half(rec["name"])
    assert li.tag.data["plot"]


def test_choose_cancelled_plays_nothing(kodi, fake_server):
    rec = _multi_file_recording(fake_server)
    xbmcgui.select_answers.append(-1)
    run_plugin(f"action=choose&id={rec['id']}")
    assert xbmc.played == []


def _get(fake_server, path):
    with urllib.request.urlopen(fake_server + path) as resp:
        return json.loads(resp.read())


def test_play_survives_channels_failure(kodi, fake_server, monkeypatch):
    from resources.lib.epgstation import api

    def boom(self):
        raise api.ApiError("channels down")

    monkeypatch.setattr(api.EPGStation, "channels", boom)
    rec = _get(fake_server, "/api/recorded?limit=1")["records"][0]
    run_plugin(f"action=play&id={rec['id']}")
    ok, li = kodi.resolved
    assert ok and li.getPath().startswith(fake_server + "/api/videos/")
    from resources.lib.epgstation import items

    assert li.tag.data["originaltitle"] == items.to_half(rec["name"])


def test_filtered_list_includes_in_progress_recordings(kodi, fake_server):
    """/recorded leaves out recordings in progress; channel folders count them, so show them."""
    now = _get(fake_server, "/api/recording")["records"]
    if not now:
        pytest.skip("nothing recording in this fixture set")
    xbmcaddon.settings["page_size"] = "5"
    rec = now[0]
    run_plugin(f"action=list&channelId={rec['channelId']}")
    labels = [li.label for _, li, folder in kodi.items if not folder]
    assert labels and "\u25cf" in labels[0]
    kodi.reset()  # same stub state object as xbmcplugin.state
    run_plugin(f"action=list&channelId={rec['channelId']}&offset=5")
    assert not any("\u25cf" in li.label for _, li, folder in kodi.items if not folder)


# --- backlog 015b: full-width texts and InfoTagVideo metadata ---------------------------------------------------

def _requests(fake_server, path):
    import fake_epgstation

    return [q for p, q in fake_epgstation.REQUESTS[fake_server] if p == path]


def test_requests_full_width_texts(kodi, fake_server):
    """Backlog 015 AC 9: /recorded, /recorded/{id} and /recording are asked for with isHalfWidth=false."""
    import fake_epgstation

    rec = _get(fake_server, "/api/recorded?limit=1")["records"][0]
    fake_epgstation.REQUESTS[fake_server].clear()
    run_plugin(f"action=list&channelId={rec['channelId']}")
    run_plugin("action=recording")
    run_plugin(f"action=play&id={rec['id']}")
    for path in ("/api/recorded", "/api/recording", f"/api/recorded/{rec['id']}"):
        queries = _requests(fake_server, path)
        assert queries, path
        assert all(q.get("isHalfWidth") == ["false"] for q in queries), (path, queries)


def test_fake_server_half_width_like_epgstation(fake_server):
    """isHalfWidth=true: to_half'd texts plus rawExtended; false: full-width, no rawExtended."""
    from resources.lib.epgstation import items

    full = _get(fake_server, "/api/recorded?limit=1000&isHalfWidth=false")["records"]
    half = _get(fake_server, "/api/recorded?limit=1000&isHalfWidth=true")["records"]
    assert [r["id"] for r in full] == [r["id"] for r in half]
    for f, h in zip(full, half):
        assert "rawExtended" not in f
        assert h["name"] == items.to_half(f["name"])
        if f.get("extended"):
            assert h["rawExtended"]
    one = _get(fake_server, f"/api/recorded/{full[0]['id']}?isHalfWidth=true")
    assert one["name"] == items.to_half(full[0]["name"])


def test_fake_server_keyword_matches_half_width(fake_server):
    """EPGStation searches halfWidthName / halfWidthDescription with to_half(keyword)."""
    from resources.lib.epgstation import items

    recs = _get(fake_server, "/api/recorded?limit=1000")["records"]
    rec = next(r for r in recs if r["name"] != items.to_half(r["name"]))
    word = items.to_half(rec["name"])[:4]
    for kw in (word, rec["name"][:4], word.swapcase(), word[:2] + "\u3000 " + word[2:]):
        found = _get(fake_server, "/api/recorded?limit=1000&keyword=" + urllib.parse.quote(kw))["records"]
        assert rec["id"] in [r["id"] for r in found], kw


def _list_items(kodi):
    xbmcaddon.settings["page_size"] = "500"
    run_plugin("action=list")
    return [li for _, li, folder in kodi.items if not folder]


def test_list_items_carry_metadata(kodi, fake_server):
    from resources.lib.epgstation import epgmeta, items

    full = {r["id"]: r for r in _get(fake_server, "/api/recorded?limit=1000")["records"]}
    lis = _list_items(kodi)
    assert lis
    kinds = set()
    for li in lis:
        rec = full[int(li.props["epgstation.recordedId"])]
        meta = epgmeta.metadata(rec)
        data = li.tag.data
        assert li.label.endswith(items.to_half(rec["name"])) or items.to_half(rec["name"]) in li.label
        assert data["originaltitle"] == items.to_half(rec["name"])
        if meta["kind"] == "movie":
            assert data["mediatype"] == "movie"
            if meta["year"]:
                assert data["year"] == meta["year"]
            # Kodi takes the year from premiered, else firstaired: either would show the air year
            assert "premiered" not in data and "firstaired" not in data
            assert data["dateadded"]
        else:
            assert data["mediatype"] == "episode"
            assert data["tvshowtitle"] == meta["show"]
            if meta["episode"] is not None:
                assert (data["season"], data["episode"]) == (meta["season"] or 1, meta["episode"])
        kinds.add(meta["kind"])
        if meta["cast"]:
            assert all(isinstance(a, xbmc.Actor) for a in data["cast"])
            assert [(a.name, a.order) for a in data["cast"]] == [(n, i) for i, (n, _) in enumerate(meta["cast"])]
        if meta["directors"]:
            assert data["directors"] == meta["directors"]
        if meta["writers"]:
            assert data["writers"] == meta["writers"]
        if meta["tags"]:
            assert data["tags"] == meta["tags"]
    assert kinds == {"movie", "tv"}
    seasons = {li.tag.data.get("season") for li in lis}
    if any(r["name"].startswith("孤独のグルメ　傑作選　Ｓｅａｓｏｎ６") for r in full.values()):
        assert 6 in seasons  # dev 8 Season6 #8 #9: setSeason(6)


def test_play_and_choose_carry_metadata(kodi, fake_server):
    rec = _multi_file_recording(fake_server)
    lis = {li.props["epgstation.recordedId"]: li for li in _list_items(kodi)}
    want = lis[str(rec["id"])].tag.data
    kodi.reset()  # same stub state object as xbmcplugin.state
    run_plugin(f"action=play&id={rec['id']}")
    got = kodi.resolved[1].tag.data
    xbmcgui.select_answers.append(0)
    run_plugin(f"action=choose&id={rec['id']}")
    chosen = xbmc.played[-1][1].tag.data
    for data in (got, chosen):
        for key in ("mediatype", "title", "originaltitle", "tvshowtitle", "episode", "year", "tags"):
            assert data.get(key) == want.get(key), key
        assert data["plot"].split("\n\n")[0] == want["plot"].split("\n\n")[0]  # file length lines may differ
        assert [(a.name, a.role) for a in data.get("cast", [])] == [(a.name, a.role) for a in want.get("cast", [])]


def test_english_tags_follow_genre_language(kodi, fake_server):
    xbmcaddon.settings["genre_language"] = "1"
    tags = {t for li in _list_items(kodi) for t in li.tag.data.get("tags", [])}
    assert "Subtitles" in tags and "字幕" not in tags


# --- backlog 005: movies and TV shows -----------------------------------------------------------------------------

def _all_records(fake_server):
    recs = _get(fake_server, "/api/recorded?limit=100000")["records"] + _get(fake_server, "/api/recording")["records"]
    return {r["id"]: r for r in recs}


def _keys_by_kind(fake_server):
    from resources.lib.epgstation import epgmeta, items

    movies, shows = [], {}
    for r in _all_records(fake_server).values():
        meta = epgmeta.metadata(r)
        if meta is not None and meta["kind"] == "movie":
            movies.append(r["id"])
        else:
            shows.setdefault(items.show_key(meta, r), []).append(r["id"])
    return movies, shows


def test_movies_lists_every_movie(kodi, fake_server):
    from resources.lib.epgstation import epgmeta, items

    movies, _ = _keys_by_kind(fake_server)
    records = _all_records(fake_server)
    run_plugin("action=movies")
    assert kodi.ended is True and kodi.content == "movies"
    got = [int(li.props["epgstation.recordedId"]) for _, li, folder in kodi.items if not folder]
    assert sorted(got) == sorted(movies) and len(got) == len(kodi.items)
    starts = [records[i]["startAt"] for i in got]
    assert starts == sorted(starts, reverse=True)
    for _, li, _ in kodi.items:
        rec = records[int(li.props["epgstation.recordedId"])]
        meta = epgmeta.metadata(rec)
        assert li.tag.data["mediatype"] == "movie"
        title = items.to_half(meta["show"]) + (f" [{meta['version']}]" if meta["version"] else "")
        assert li.label.replace("[COLOR red]●[/COLOR] ", "").startswith(title)
        if meta["year"]:
            assert li.tag.data["year"] == meta["year"]
        assert "premiered" not in li.tag.data
    import xbmcplugin

    methods = [m for m, *_ in kodi.sort_methods]
    assert methods == [xbmcplugin.SORT_METHOD_UNSORTED, xbmcplugin.SORT_METHOD_LABEL,
                       xbmcplugin.SORT_METHOD_DATEADDED, xbmcplugin.SORT_METHOD_VIDEO_YEAR,
                       xbmcplugin.SORT_METHOD_DURATION]
    assert all(label == "%L" for _, label, _ in kodi.sort_methods)


def test_movies_english_version_label(kodi, fake_server):
    xbmcaddon.settings["genre_language"] = "1"
    run_plugin("action=movies")
    assert not any("[字幕版]" in li.label or "[吹替版]" in li.label for _, li, _ in kodi.items)


def test_tvshows_one_folder_per_show(kodi, fake_server):
    import xbmcplugin

    _, shows = _keys_by_kind(fake_server)
    records = _all_records(fake_server)
    run_plugin("action=tvshows")
    assert kodi.ended is True and kodi.content == "tvshows"
    assert len(kodi.items) == len(shows)
    newest = []
    for url, li, folder in kodi.items:
        assert folder and "action=show" in url
        key = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["key"][0]
        ids = shows[key]
        assert li.tag.data["mediatype"] == "tvshow"
        assert li.props["TotalEpisodes"] == str(len(ids))
        assert li.label == f"{li.tag.data['title']} ({len(ids)})"
        with_thumb = sorted((records[i] for i in ids if records[i].get("thumbnails")), key=lambda r: r["startAt"])
        if with_thumb:
            assert li.art["thumb"] == f"{fake_server}/api/thumbnails/{with_thumb[-1]['thumbnails'][0]}"
        assert li.art["icon"]
        newest.append(max(records[i]["startAt"] for i in ids))
    assert newest == sorted(newest, reverse=True)
    methods = [m for m, *_ in kodi.sort_methods]
    assert methods == [xbmcplugin.SORT_METHOD_UNSORTED, xbmcplugin.SORT_METHOD_LABEL,
                       xbmcplugin.SORT_METHOD_DATEADDED]


def test_show_folder_lists_its_episodes(kodi, fake_server, handler):
    import xbmcplugin

    handler.multiply = 3
    _, shows = _keys_by_kind(fake_server)
    run_plugin("action=tvshows")
    folders = list(kodi.items)
    records = _all_records(fake_server)
    for url, li, _ in folders[:5]:
        key = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["key"][0]
        kodi.reset()  # same stub state object as xbmcplugin.state
        run_plugin(url.split("?", 1)[1])
        assert kodi.ended is True and kodi.content == "episodes"
        got = [int(e.props["epgstation.recordedId"]) for _, e, f in kodi.items if not f]
        assert sorted(got) == sorted(shows[key])
        assert len(got) >= 3 or all(records[i].get("isRecording") for i in got)  # /recording is not multiplied
        starts = [records[i]["startAt"] for i in got]
        assert starts == sorted(starts, reverse=True)
        assert all(e.tag.data["mediatype"] in ("episode", "video") for _, e, _ in kodi.items)
        assert {e.tag.data.get("tvshowtitle") for _, e, _ in kodi.items} <= {li.tag.data["title"], None}
        methods = [m for m, *_ in kodi.sort_methods]
        assert methods == [xbmcplugin.SORT_METHOD_UNSORTED, xbmcplugin.SORT_METHOD_EPISODE,
                           xbmcplugin.SORT_METHOD_DATEADDED, xbmcplugin.SORT_METHOD_LABEL]
        assert all(label == "%L" for _, label, _ in kodi.sort_methods)


def test_show_folder_multiply_3_red_mask(kodi, fake_server, handler):
    """Backlog 005 AC 6: with --multiply 3 (dev fixtures) 朱色の仮面 has 3 episodes, 1x1 仮面屋ペル."""
    handler.multiply = 3
    run_plugin("action=tvshows")
    match = [(u, li) for u, li, _ in kodi.items if li.tag.data["title"] == "朱色の仮面"]
    if not match:
        pytest.skip("no 朱色の仮面 in this fixture set")
    (url, li), = match
    assert li.label == "朱色の仮面 (3)"
    kodi.reset()  # same stub state object as xbmcplugin.state
    run_plugin(url.split("?", 1)[1])
    eps = [e.tag.data for _, e, _ in kodi.items]
    assert len(eps) == 3
    for data in eps:
        assert (data["mediatype"], data["tvshowtitle"], data["season"], data["episode"], data["title"]) == \
            ("episode", "朱色の仮面", 1, 1, "仮面屋ペル")


def test_season_stays_inside_its_show(kodi, fake_server):
    """Backlog 005 AC 7: dev 8 孤独のグルメ Season6 #8 is 6x8 in folder 孤独のグルメ."""
    if os.path.basename(_fixture_root(fake_server)) != "dev":
        pytest.skip("dev fixtures only (recording 8)")
    run_plugin("action=tvshows")
    match = [u for u, li, _ in kodi.items if li.tag.data["title"] == "孤独のグルメ"]
    if not match:
        pytest.skip("no 孤独のグルメ in this fixture set")
    kodi.reset()  # same stub state object as xbmcplugin.state
    run_plugin(match[0].split("?", 1)[1])
    assert (kodi.items[0][1].tag.data["season"], kodi.items[0][1].tag.data["episode"]) == (6, 8)


def test_unknown_show_key_is_empty(kodi):
    run_plugin("action=show&key=zzz-no-such-show")
    assert kodi.ended is True and kodi.items == []


def test_in_progress_recording_appears_in_its_show(kodi, fake_server):
    from resources.lib.epgstation import epgmeta, items

    live = _get(fake_server, "/api/recording")["records"]
    if not live:
        pytest.skip("nothing recording in this fixture set")
    rec = live[0]
    meta = epgmeta.metadata(rec)
    view = "movies" if meta and meta["kind"] == "movie" else f"show&key={urllib.parse.quote(items.show_key(meta, rec))}"
    run_plugin("action=" + view)
    lis = {li.props["epgstation.recordedId"]: li for _, li, f in kodi.items if not f}
    assert lis[str(rec["id"])].label.startswith("[COLOR red]\u25cf[/COLOR] ")
    if view != "movies":
        kodi.reset()  # same stub state object as xbmcplugin.state
        run_plugin("action=tvshows")
        folder = next(li for u, li, _ in kodi.items if urllib.parse.quote(items.show_key(meta, rec)) in u
                      or items.show_key(meta, rec) in urllib.parse.unquote_plus(u))
        assert int(folder.props["TotalEpisodes"]) >= 1


def test_second_tvshows_open_is_two_probes_and_recording_only(kodi, fake_server):
    import fake_epgstation

    run_plugin("action=tvshows")
    first = [li.label for _, li, _ in kodi.items]
    n = len(fake_epgstation.REQUESTS[fake_server])
    kodi.reset()  # same stub state object as xbmcplugin.state
    run_plugin("action=tvshows")
    seen = fake_epgstation.REQUESTS[fake_server][n:]
    assert [(p, q.get("limit"), q.get("isReverse")) for p, q in seen] == \
        [("/api/recorded", ["1"], None), ("/api/recorded", ["1"], ["true"]), ("/api/recording", ["100"], None)]
    assert [li.label for _, li, _ in kodi.items] == first


def test_rules_hide_empty_and_count(kodi, fake_server):
    run_plugin("action=rules")
    assert kodi.ended is True and kodi.items
    for url, li, _ in list(kodi.items):
        n = int(li.label.rsplit("(", 1)[1].rstrip(")"))
        assert n > 0
        rid = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["ruleId"][0]
        live = [r for r in _get(fake_server, "/api/recording")["records"] if str(r.get("ruleId")) == rid]
        assert n == _get(fake_server, f"/api/recorded?ruleId={rid}&limit=1")["total"] + len(live)
        kodi.reset()  # same stub state object as xbmcplugin.state
        xbmcaddon.settings["page_size"] = "500"
        run_plugin(url.split("?", 1)[1])
        assert len([1 for *_, f in kodi.items if not f]) == n


def test_new_views_only_get(kodi, fake_server, monkeypatch):
    """Backlog 005 AC 1: the new code paths never send anything but GET."""
    real = urllib.request.urlopen
    methods = []

    def spy(req, *a, **kw):
        methods.append(req.get_method() if hasattr(req, "get_method") else "GET")
        return real(req, *a, **kw)

    monkeypatch.setattr(urllib.request, "urlopen", spy)
    for q in ("", "action=browse", "action=movies", "action=tvshows", "action=rules", "action=show&key=x"):
        run_plugin(q)
    assert methods and set(methods) == {"GET"}


def test_new_views_notify_when_unreachable(kodi):
    xbmcaddon.settings["server_url"] = "http://127.0.0.1:9"
    for q in ("action=movies", "action=tvshows", "action=rules", "action=show&key=x"):
        xbmcgui.notifications.clear()
        run_plugin(q)
        assert kodi.ended is False
        assert xbmcgui.notifications and xbmcgui.notifications[0][2] == "error"


def test_tvshows_from_stale_cache_when_server_down(kodi, fake_server):
    run_plugin("action=tvshows")
    first = [li.label for _, li, _ in kodi.items]
    live = len(_get(fake_server, "/api/recording")["records"])
    kodi.reset()  # same stub state object as xbmcplugin.state
    xbmcaddon.settings["server_url"] = "http://127.0.0.1:9"
    # another server URL is another cache; point the cache at the same server name instead
    from resources.lib.epgstation import plugin

    real = plugin._server_url
    plugin._server_url = lambda: fake_server
    try:
        plugin_obj = plugin.Plugin(["plugin://plugin.video.epgstation/", "1", "?action=tvshows"])
        from resources.lib.epgstation.api import EPGStation

        plugin_obj.api = EPGStation("http://127.0.0.1:9")
        plugin_obj.run()
    finally:
        plugin._server_url = real
    assert kodi.ended is True
    if not live:
        assert [li.label for _, li, _ in kodi.items] == first
    assert any("cached library" in msg for _, msg in xbmc.logs)


def test_broken_library_cache_still_lists(kodi, fake_server):
    """Review fix 3: a cache entry without startAt is no cache, not a KeyError past the error handling."""
    import xbmcvfs

    run_plugin("action=tvshows")
    first = [li.label for _, li, _ in kodi.items]
    path = xbmcvfs.translatePath("special://profile/addon_data/plugin.video.epgstation/library-index.json")
    data = json.load(open(path, encoding="utf-8"))
    for e in data["entries"]:
        e.pop("startAt")
    data["h"].pop("sig")
    open(path, "w", encoding="utf-8").write(json.dumps(data))
    kodi.reset()  # same stub state object as xbmcplugin.state
    run_plugin("action=tvshows")
    assert kodi.ended is True and [li.label for _, li, _ in kodi.items] == first


def test_blank_show_name_folder_label(kodi, fake_server, handler):
    """Review fix 5: recordings with a blank name group into a folder named after the channel."""
    recs = _get(fake_server, "/api/recorded?limit=1")["records"]
    handler.mutate = lambda rs: [dict(rs[0], name="", description="")] + rs[1:]
    run_plugin("action=tvshows")
    labels = [li.label for _, li, _ in kodi.items]
    assert not any(label.startswith(" (") for label in labels)
    channel = {c["id"]: c.get("halfWidthName") or c["name"] for c in _get(fake_server, "/api/channels")}
    assert f"{channel[recs[0]['channelId']]} (1)" in labels


def _parse_spy(monkeypatch):
    from resources.lib.epgstation import epgmeta

    calls, real = [], epgmeta.metadata

    def spy(item, on_error=None):
        calls.append(item["id"])
        return real(item, on_error)

    monkeypatch.setattr(epgmeta, "metadata", spy)
    return calls


def _plain(li):
    data = dict(li.tag.data, cast=[(a.name, a.role, a.order) for a in li.tag.data.get("cast", [])],
                streams=[(v.width, v.height, v.codec, v.aspect) for v in li.tag.data["streams"]],
                audio_streams=[(a.channels, a.codec) for a in li.tag.data["audio_streams"]])  # 002
    return li.label, data, li.props, li.art


def test_warm_movies_and_show_reuse_cached_metadata(kodi, fake_server, monkeypatch):
    """Review fix 6: finished recordings are parsed once (at the refresh), not again for every list."""
    run_plugin("action=movies")
    cold = [_plain(li) for _, li, _ in kodi.items]
    run_plugin("action=tvshows")
    show_url = kodi.items[-1][0]
    live = {r["id"] for r in _get(fake_server, "/api/recording")["records"]}
    calls = _parse_spy(monkeypatch)
    kodi.reset()  # same stub state object as xbmcplugin.state
    run_plugin("action=movies")
    assert [_plain(li) for _, li, _ in kodi.items] == cold
    run_plugin(show_url.split("?", 1)[1])
    assert set(calls) <= live


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_cached_metadata_items_equal_fresh_ones(kodi, fake_server, lang):
    """Items built from the cached (JSON round-tripped) metadata equal items parsed on the spot."""
    from resources.lib.epgstation import items

    xbmcaddon.settings["real_duration"] = "false"
    xbmcaddon.settings["genre_language"] = "0" if lang == "ja" else "1"
    run_plugin("action=movies")
    run_plugin("action=movies")
    records = _all_records(fake_server)
    from resources.lib.epgstation.api import EPGStation

    api = EPGStation(fake_server)
    channels = items.channel_names(_get(fake_server, "/api/channels"))
    for _, li, _ in kodi.items:
        want = items.recorded_item(records[int(li.props["epgstation.recordedId"])], api, channels, lang)
        data = li.tag.data
        assert data["title"] == want["info"]["title"] and data["plot"] == want["info"]["plot"]
        assert [(a.name, a.role, a.order) for a in data.get("cast", [])] == \
            [tuple(c) for c in want["info"].get("cast", [])]
        assert data.get("tags", []) == want["info"].get("tags", [])
        assert {k: v for k, v in li.props.items() if k.startswith("epgstation.")} == want["props"]


def test_real_durations_skipped_above_300_items(kodi, fake_server, handler, monkeypatch):
    """Review fix 6: a list of more than 300 recordings shows EPG lengths instead of probing every file."""
    from resources.lib.epgstation import items

    assert items.REAL_DURATION_MAX == 300
    movies, _ = _keys_by_kind(fake_server)
    handler.multiply = items.REAL_DURATION_MAX // len(movies) + 1
    seen = _duration_requests(monkeypatch)
    run_plugin("action=movies")
    assert len(kodi.items) > items.REAL_DURATION_MAX
    assert seen == []
    records = _all_records(fake_server)
    for _, li, _ in kodi.items[:20]:
        rec = records[int(li.props["epgstation.recordedId"])]
        assert li.tag.data["duration"] == (rec["endAt"] - rec["startAt"]) // 1000



# --- episode labels and window headers (user request before the 005 merge) ----------------------------------------

def _open_show(kodi, title):
    run_plugin("action=tvshows")
    match = [u for u, li, _ in kodi.items if li.tag.data["title"] == title]
    if not match:
        pytest.skip(f"no {title} in this fixture set")
    kodi.reset()  # same stub state object as xbmcplugin.state
    run_plugin(match[0].split("?", 1)[1])


def test_show_folder_episode_labels(kodi, fake_server, handler):
    handler.multiply = 3
    _open_show(kodi, "朱色の仮面")
    assert [li.label for _, li, _ in kodi.items] == ["1x1 仮面屋ペル"] * 3
    assert kodi.category == "朱色の仮面"
    assert all(li.tag.data["title"] == "仮面屋ペル" for _, li, _ in kodi.items)  # info tags unchanged


def test_show_folder_season_label(kodi, fake_server):
    if os.path.basename(_fixture_root(fake_server)) != "dev":
        pytest.skip("dev fixtures only (recording 8)")
    _open_show(kodi, "孤独のグルメ")
    assert kodi.items[0][1].label in ("6x8 傑作選 #9", "6x8 傑作選 #9 [COLOR yellow][Partial][/COLOR]")


def test_show_folder_in_progress_label(kodi, fake_server):
    """An in-progress episode keeps the red dot inside its show folder (007 refreshed the dev recordings: generic)."""
    from resources.lib.epgstation import epgmeta, items

    metas = [epgmeta.metadata(r) for r in _get(fake_server, "/api/recording")["records"]]
    shows = [items.to_half(m["show"]) for m in metas if m and m["kind"] != "movie" and m["show"]]
    if not shows:
        pytest.skip("nothing recording in this fixture set")
    _open_show(kodi, shows[0])
    assert kodi.items[0][1].label.startswith("[COLOR red]\u25cf[/COLOR] ")


def test_other_lists_keep_full_names(kodi, fake_server):
    from resources.lib.epgstation import items

    records = _all_records(fake_server)
    for q in ("action=list", "action=recording"):
        kodi.reset()  # same stub state object as xbmcplugin.state
        run_plugin(q)
        for _, li, folder in kodi.items:
            if not folder:
                name = items.to_half(records[int(li.props["epgstation.recordedId"])]["name"])
                assert name in li.label


@pytest.mark.parametrize("query,want", [
    ("action=movies", "Movies"), ("action=tvshows", "TV shows"), ("action=list", "Recent recordings"),
    ("action=recording", "Recording now"), ("action=browse", "Browse"), ("action=channels", "By channel"),
    ("action=genres", "By genre"), ("action=rules", "By rule"), ("action=search&keyword=zzz", "Search"),
])
def test_window_header_per_view(kodi, query, want):
    run_plugin(query)
    assert kodi.category == want


def test_window_header_of_filtered_lists(kodi, fake_server):
    for view in ("channels", "genres", "rules"):
        kodi.reset()  # same stub state object as xbmcplugin.state
        run_plugin("action=" + view)
        url, li, _ = kodi.items[0]
        name = li.label.rsplit(" (", 1)[0]
        import fake_epgstation

        n = len(fake_epgstation.REQUESTS[fake_server])
        kodi.reset()  # same stub state object as xbmcplugin.state
        run_plugin(url.split("?", 1)[1])
        assert kodi.category == name, view
        assert not any(p == "/api/rules" for p, _ in fake_epgstation.REQUESTS[fake_server][n:])


# --- backlog 012: live TV ----------------------------------------------------------------------------------------

def _live_requests(fake_server):
    import fake_epgstation

    return [(p, q) for p, q in fake_epgstation.REQUESTS[fake_server] if p.startswith("/api/streams")]


def test_live_type_folders(kodi, fake_server):
    run_plugin("action=live")
    assert kodi.ended is True and kodi.category == "Live TV"
    types = kodi.items[2:]  # program guide (013): What's on, By genre first
    assert [li.label for _, li, _ in types] == ["Terrestrial", "BS", "CS"]
    assert [url.rsplit("type=", 1)[1] for url, _, _ in types] == ["GR", "BS", "CS"]
    assert all(folder for *_, folder in kodi.items)


def test_live_gr_channels(kodi, fake_server):
    import fake_epgstation
    from resources.lib.epgstation import items, live

    fake_epgstation.REQUESTS[fake_server].clear()
    run_plugin("action=live&type=GR")
    assert kodi.ended is True and kodi.category == "Terrestrial" and kodi.cache is False
    queries = _requests(fake_server, "/api/schedules")
    assert len(queries) == 1
    q = queries[0]
    assert (q["GR"], q["BS"], q["CS"], q["SKY"], q["isHalfWidth"]) == (["true"], ["false"], ["false"], ["false"],
                                                                       ["false"])
    assert int(q["endAt"][0]) - int(q["startAt"][0]) == live.WINDOW_MS
    assert _live_requests(fake_server) == []
    sched = _get(fake_server, "/api/schedules?GR=true&BS=false&CS=false&SKY=false&startAt=0&endAt=99999999999999")
    # hide terrestrial sub-channels (025): on by default
    want = [items.to_half(s["channel"]["name"]) for s in live.drop_subchannels(sched) if live.watchable(s["channel"])]
    assert len(want) < sum(1 for s in sched if live.watchable(s["channel"]))
    lis = [li for _, li, folder in kodi.items]
    assert [li.props["epgstation.channel"] for li in lis] == want
    assert all(li.props["IsPlayable"] == "true" for li in lis)
    assert all(li.tag.data["mediatype"] == "video" for li in lis)
    # never Kodi's DB playcount / cached stream details of the live_play URL (watched mark, "00:19 MPEG-2")
    assert all(li.tag.data["playcount"] == 0 for li in lis)
    assert all(len(li.tag.data["streams"]) == 1 and li.tag.data["streams"][0].codec == "" for li in lis)
    assert sum(1 for li in lis if li.tag.data.get("genres")) >= len(lis) // 2


def test_live_bs_lists_paid_channels(kodi, fake_server):
    run_plugin("action=live&type=BS")
    lis = [li for _, li, _ in kodi.items]
    assert lis and any(li.props["epgstation.isFree"] == "false" for li in lis)
    assert not any("ラジオ" in li.props["epgstation.channel"] for li in lis)


def _live_play(kodi, fake_server, query="", playing=True):

    from resources.lib.epgstation import items

    sched = _get(fake_server, "/api/schedules?GR=true&BS=false&CS=false&SKY=false&startAt=0&endAt=99999999999999")
    ch = sched[0]["channel"]
    if playing:
        xbmc.player_file = f"{fake_server}/api/streams/live/{ch['id']}/m2ts?mode=2"
    run_plugin(f"action=live_play&channelId={ch['id']}&name={urllib.parse.quote(items.to_half(ch['name']))}" + query)
    return ch, sched[0]


def test_live_play_resolves_stream(kodi, fake_server):
    import fake_epgstation
    from resources.lib.epgstation import items, live

    fake_epgstation.REQUESTS[fake_server].clear()
    ch, sched = _live_play(kodi, fake_server)
    ok, li = kodi.resolved
    assert ok
    assert li.getPath() == f"{fake_server}/api/streams/live/{ch['id']}/m2ts?mode=2"
    assert li.mimetype == "video/mp2t" and li.content_lookup is False
    cur, _ = live.now_next(sched["programs"], time.time() * 1000)
    assert li.tag.data["title"] == items.to_half(cur["name"])
    assert li.tag.data["studios"] == [items.to_half(ch["name"])]
    # L1: nothing that makes Kodi bookmark the fixed live_play URL
    assert "duration" not in li.tag.data and "resumepoint" not in li.tag.data
    assert li.tag.data["tagline"].startswith(items.to_half(ch["name"]) + "  ")
    assert any(m == f"[epgstation] live: channel {ch['id']} mode 2 (無変換)" for _, m in xbmc.logs)
    assert _live_requests(fake_server) == []  # Kodi opens the stream, the plugin never does
    assert _requests(fake_server, f"/api/schedules/{ch['id']}")
    assert xbmcgui.notifications == []


def test_live_play_with_auth_and_mode_setting(kodi, fake_server):
    xbmcaddon.settings.update(username="u", live_mode="1")
    ch, _ = _live_play(kodi, fake_server)
    path = kodi.resolved[1].getPath()
    assert path.startswith(f"{fake_server}/api/streams/live/{ch['id']}/m2ts?mode=0|Authorization=Basic%20")
    xbmcaddon.settings["live_mode"] = "2"
    kodi.reset()
    _live_play(kodi, fake_server)
    assert "m2ts?mode=1|" in kodi.resolved[1].getPath()


def test_live_play_survives_schedule_failure(kodi, fake_server, monkeypatch):
    from resources.lib.epgstation import api

    def boom(self, *a, **kw):
        raise api.ApiError("timed out")

    monkeypatch.setattr(api.EPGStation, "channel_schedule", boom)
    ch, _ = _live_play(kodi, fake_server)
    ok, li = kodi.resolved
    assert ok and li.tag.data["title"] == li.label and "/streams/live/" in li.getPath()
    assert li.mimetype == "video/mp2t" and li.content_lookup is False


def test_live_play_openapi_config_keys(kodi, fake_server, handler):
    """L3: a server answering with the openapi.json keys (isEnableLiveStream, streamConfig.live.m2ts) works too."""
    cfg = _get(fake_server, "/api/config")
    handler.config_patch = {"isEnableTSLiveStream": None, "isEnableLiveStream": True,
                            "streamConfig": {"live": {"m2ts": cfg["streamConfig"]["live"]["ts"]["m2ts"]}}}
    ch, _ = _live_play(kodi, fake_server)
    assert kodi.resolved[0] is True
    assert kodi.resolved[1].getPath().endswith(f"/api/streams/live/{ch['id']}/m2ts?mode=2")


def test_live_play_disabled_on_server(kodi, fake_server, handler):
    import fake_epgstation

    fake_epgstation.REQUESTS[fake_server].clear()
    handler.config_patch = {"isEnableTSLiveStream": False}
    _live_play(kodi, fake_server)
    assert kodi.resolved[0] is False
    assert xbmcgui.notifications[0][1] == "Live streaming is disabled on the server"
    assert _live_requests(fake_server) == []


def _errors():
    return json.load(open(os.path.join(FIXTURES, "live_errors.json"), encoding="utf-8"))


def _on_wait(at, event):
    """At `at` seconds into the start watch, fire `event` on the addon's player (the newest stub Player)."""
    def hook(waited):
        if abs(waited - at) < 1e-9:
            event(xbmc.players[-1])
    xbmc.wait_hook = hook


def test_live_play_error_probes_at_once(kodi, fake_server, handler):
    import fake_epgstation

    handler.live_error = _errors()["no_tuner"]
    fake_epgstation.REQUESTS[fake_server].clear()
    _on_wait(1.0, lambda p: p.onPlayBackError())
    ch, _ = _live_play(kodi, fake_server, playing=False)
    assert kodi.resolved[0] is True
    assert [p for p, _ in _live_requests(fake_server)] == [f"/api/streams/live/{ch['id']}/m2ts"]  # one probe
    assert xbmcgui.notifications[-1][1] == "No free tuner"
    assert xbmc.waited < 2  # right after Kodi's error, not after the 20 s backstop


def test_live_play_other_error_message(kodi, fake_server, handler):
    handler.live_error = _errors()["no_such_service"]
    _on_wait(0.5, lambda p: p.onPlayBackError())
    _live_play(kodi, fake_server, playing=False)
    assert xbmcgui.notifications[-1][1] == "Could not start live stream (HTTP 404)"
    assert any("Bad status respond (404 Not Found)." in m for _, m in xbmc.logs)  # full server text in the log


def test_live_play_av_started_never_probes(kodi, fake_server, handler):
    import fake_epgstation

    handler.live_error = _errors()["no_tuner"]
    sched = _get(fake_server, "/api/schedules?GR=true&BS=false&CS=false&SKY=false&startAt=0&endAt=99999999999999")
    url = f"{fake_server}/api/streams/live/{sched[0]['channel']['id']}/m2ts?mode=2"

    def start(p):
        xbmc.player_file = url
        p.onPlayBackStarted()
        p.onAVStarted()
    _on_wait(1.0, start)
    fake_epgstation.REQUESTS[fake_server].clear()
    _live_play(kodi, fake_server, playing=False)
    assert _live_requests(fake_server) == [] and xbmcgui.notifications == []
    assert xbmc.waited < 2


def _failed_start(p):
    """What Kodi 21 does when the stream answers an HTTP error: started, stopped, its Playback failed dialog."""
    p.onPlayBackStarted()
    p.onPlayBackStopped()
    xbmc.visible.add("Window.IsActive(okdialog)")


def test_live_play_kodi_failure_probes_at_once(kodi, fake_server, handler):
    import fake_epgstation

    handler.live_error = _errors()["no_tuner"]
    _on_wait(0.5, _failed_start)
    fake_epgstation.REQUESTS[fake_server].clear()
    ch, _ = _live_play(kodi, fake_server, playing=False)
    assert [p for p, _ in _live_requests(fake_server)] == [f"/api/streams/live/{ch['id']}/m2ts"]
    assert xbmcgui.notifications[-1][1] == "No free tuner"
    assert xbmc.waited < 3  # together with Kodi's dialog, not at the 20 s backstop


def test_live_play_cancel_never_probes(kodi, fake_server, handler):
    """Stopped before AV started without Kodi's failure dialog: the user cancelled."""
    import fake_epgstation

    handler.live_error = _errors()["no_tuner"]
    for event in ("onPlayBackStopped", "onPlayBackEnded"):
        fake_epgstation.REQUESTS[fake_server].clear()
        xbmcgui.notifications.clear()
        xbmc.waited = 0.0
        _on_wait(1.0, lambda p, event=event: (p.onPlayBackStarted(), getattr(p, event)()))
        _live_play(kodi, fake_server, playing=False)
        assert _live_requests(fake_server) == [] and xbmcgui.notifications == []
        assert xbmc.waited < 4


def test_live_play_without_player_start_never_probes(kodi, fake_server, handler):
    """Files.GetDirectory on a live_play URL: Kodi resolves it but never plays it."""
    import fake_epgstation

    handler.live_error = _errors()["no_tuner"]
    fake_epgstation.REQUESTS[fake_server].clear()
    _live_play(kodi, fake_server, playing=False)
    assert xbmc.waited >= 20  # waited for the backstop
    assert _live_requests(fake_server) == [] and xbmcgui.notifications == []


def test_live_play_zapping_reports_new_channel_failure(kodi, fake_server, handler):
    """Channel A plays, the user picks B: A's file is not B's start; A's stop is not a cancel of B."""
    import fake_epgstation

    handler.live_error = _errors()["no_tuner"]
    xbmc.player_file = f"{fake_server}/api/streams/live/3272102056/m2ts?mode=2"

    def zap(p):
        xbmc.player_file = ""
        p.onPlayBackStopped()  # A
        _failed_start(p)  # B
    _on_wait(1.0, zap)
    fake_epgstation.REQUESTS[fake_server].clear()
    ch, _ = _live_play(kodi, fake_server, playing=False)
    assert [p for p, _ in _live_requests(fake_server)] == [f"/api/streams/live/{ch['id']}/m2ts"]
    assert xbmcgui.notifications[-1][1] == "No free tuner"


def test_live_play_switch_from_recording_reports_at_once(kodi, fake_server, handler):
    """Tester L-b: recording playing, live_play fails: started (ours), one stop, no Kodi dialog -> probe at once."""
    import fake_epgstation

    handler.live_error = _errors()["no_tuner"]
    xbmc.player_file = f"{fake_server}/api/videos/57"

    def switch(p):
        xbmc.player_file = ""
        p.onPlayBackStarted()
        p.onPlayBackStopped()
    _on_wait(0.5, switch)
    fake_epgstation.REQUESTS[fake_server].clear()
    ch, _ = _live_play(kodi, fake_server, playing=False)
    assert [p for p, _ in _live_requests(fake_server)] == [f"/api/streams/live/{ch['id']}/m2ts"]
    assert xbmcgui.notifications[-1][1] == "No free tuner"
    assert xbmc.waited < 2  # was the 20 s backstop


def test_live_play_switch_cancel_costs_one_silent_probe(kodi, fake_server):
    """A user cancel while switching looks the same: one probe after the stop (Kodi closed its connection
    already), and silent when the server answers 200."""
    import fake_epgstation

    xbmc.player_file = f"{fake_server}/api/videos/57"

    def switch(p):
        xbmc.player_file = ""
        p.onPlayBackStarted()
        p.onPlayBackStopped()
    _on_wait(0.5, switch)
    fake_epgstation.REQUESTS[fake_server].clear()
    _live_play(kodi, fake_server, playing=False)
    assert len(_live_requests(fake_server)) == 1 and xbmcgui.notifications == []


def test_live_play_switch_never_probes_while_something_plays(kodi, fake_server, handler):
    import fake_epgstation

    handler.live_error = _errors()["no_tuner"]
    xbmc.player_file = f"{fake_server}/api/videos/57"

    def switch(p):
        p.onPlayBackStarted()
        p.onPlayBackStopped()  # but a player is still active (e.g. the user started something else)
        xbmc.player_file = f"{fake_server}/api/videos/58"
    _on_wait(0.5, switch)
    fake_epgstation.REQUESTS[fake_server].clear()
    _live_play(kodi, fake_server, playing=False)
    assert _live_requests(fake_server) == [] and xbmcgui.notifications == []


def test_live_play_never_probes_while_a_player_runs(kodi, fake_server, handler):
    import fake_epgstation

    handler.live_error = _errors()["no_tuner"]
    xbmc.player_file = f"{fake_server}/api/videos/1"  # something else keeps playing (stop event comes, still active)
    _on_wait(1.0, lambda p: p.onPlayBackStopped())
    fake_epgstation.REQUESTS[fake_server].clear()
    _live_play(kodi, fake_server, playing=False)
    assert _live_requests(fake_server) == [] and xbmcgui.notifications == []


def test_fake_server_live_stream_and_schedule_shift(fake_server, handler):
    from resources.lib.epgstation import live

    now = time.time() * 1000
    sched = _get(fake_server, f"/api/schedules?GR=true&BS=true&CS=true&SKY=false&startAt={int(now)}"
                              f"&endAt={int(now + live.WINDOW_MS)}")
    # every channel on air at capture (a subchannel may only have a later program in the window)
    on_air = {s["channel"]["id"] for s in _get(fake_server, "/api/schedules/broadcasting")}
    assert sched and all(live.now_next(s["programs"], now)[0] for s in sched if s["channel"]["id"] in on_air)
    one = _get(fake_server, f"/api/schedules/{sched[0]['channel']['id']}?startAt={int(now)}&days=1")
    assert one[0]["programs"][0]["id"] == sched[0]["programs"][0]["id"]
    with urllib.request.urlopen(f"{fake_server}/api/streams/live/{sched[0]['channel']['id']}/m2ts?mode=2") as r:
        assert r.headers["Content-Type"] == "video/mp2t"
    handler.live_error = {"code": 500, "message": "x", "errors": "y"}
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{fake_server}/api/streams/live/1/m2ts?mode=2")
    assert e.value.code == 500


@pytest.mark.parametrize("query", ["action=play&id=1", "action=live_play&channelId=1&name=x", "action=choose&id=1"])
def test_play_actions_resolve_false_on_api_error(kodi, query):
    """L2: a play action that fails tells Kodi through setResolvedUrl, not endOfDirectory."""
    xbmcaddon.settings["server_url"] = "http://127.0.0.1:9"
    run_plugin(query)
    assert kodi.resolved is not None and kodi.resolved[0] is False
    assert kodi.ended is None
    assert xbmcgui.notifications and xbmcgui.notifications[0][2] == "error"


def test_live_play_without_channel_id(kodi, fake_server):
    import fake_epgstation

    fake_epgstation.REQUESTS[fake_server].clear()
    run_plugin("action=live_play&name=x")
    assert kodi.resolved[0] is False
    assert xbmcgui.notifications[-1][1] == "Could not start live stream"
    assert _live_requests(fake_server) == []


def test_live_player_log_has_no_credentials(kodi, fake_server):
    """The player-event log line shows the playing file without its |Authorization= header suffix."""
    xbmcaddon.settings["username"] = "u"

    def start(p):
        xbmc.player_file = f"{fake_server}/api/streams/live/1/m2ts?mode=2|Authorization=Basic%20dTpw"
        p.onPlayBackStarted()
    _on_wait(0.5, start)
    _live_play(kodi, fake_server, playing=False)
    events = [m for _, m in xbmc.logs if "live: player" in m]
    assert events and not any("Authorization" in m or "Basic" in m for m in events)


@pytest.mark.parametrize("query", ["", "action=browse", "action=channels", "action=genres", "action=rules",
                                   "action=live"])
def test_menus_are_unsorted(kodi, query):
    """Tester: folder menus showed "ソート: 日付"; they register only the unsorted method (no date sort)."""
    import xbmcplugin

    run_plugin(query)
    assert kodi.ended is True
    assert kodi.sort_methods == [(xbmcplugin.SORT_METHOD_UNSORTED, "%L", "")]


# --- backlog 013: program guide ----------------------------------------------------------------------------------

def _captured():
    with open(os.path.join(FIXTURES, "live", "live_meta.json"), encoding="utf-8") as f:
        return json.load(f)["capturedAt"]


@pytest.fixture
def guide_clock(handler, monkeypatch):
    """Fake server and plugin agree on now = capturedAt (local days line up with the fixtures)."""
    from resources.lib.epgstation import plugin

    now = _captured()
    handler.now = now
    monkeypatch.setattr(plugin, "_now_ms", lambda: now)
    return now


def _guide_schedules(fake_server, now):
    """What the guide lists with the default settings: the next 24 h without GR sub-channels (025)."""
    from resources.lib.epgstation import guide, live

    return live.drop_subchannels(_get(fake_server, f"/api/schedules?GR=true&BS=true&CS=true&SKY=false&startAt={now}"
                                                   f"&endAt={now + guide.WINDOW_MS}"))


def test_live_lists_guide_folders_first(kodi, fake_server):
    run_plugin("action=live")
    assert [li.label for _, li, _ in kodi.items] == ["What's on", "By genre", "Terrestrial", "BS", "CS"]
    assert kodi.items[0][0].endswith("action=guide_now") and kodi.items[1][0].endswith("action=guide_genres")
    assert kodi.category == "Live TV"


def test_live_channel_items_have_schedule_context(kodi, fake_server):
    run_plugin("action=live&type=GR")
    for _, li, _ in kodi.items:
        label, builtin = li.context[0]  # 014: the reservation entry follows it
        assert label == "Schedule" and builtin.startswith("Container.Update(plugin://")
        assert "action=guide_channel" in builtin and f"channelId={li.props['epgstation.channelId']}" in builtin


def test_guide_now_slots_make_no_request(kodi, fake_server, guide_clock):
    import fake_epgstation

    fake_epgstation.REQUESTS[fake_server].clear()
    run_plugin("action=guide_now")
    assert fake_epgstation.REQUESTS[fake_server] == []
    labels = [li.label for _, li, _ in kodi.items]
    assert len(labels) == 25 and labels[0].startswith("Now (") and kodi.category == "What's on"
    assert all(folder for *_, folder in kodi.items)


def test_guide_now_on_air_items_play(kodi, fake_server, guide_clock):
    from resources.lib.epgstation import guide

    run_plugin("action=guide_now&t=now")
    want = [c["id"] for c, _ in guide.on_air_at(_guide_schedules(fake_server, guide_clock), guide_clock)]
    lis = [(url, li) for url, li, _ in kodi.items]
    assert [int(li.props["epgstation.channelId"]) for _, li in lis] == want
    assert all(li.props["IsPlayable"] == "true" and "action=live_play" in url for url, li in lis)
    assert all(li.label.startswith("[COLOR yellow]▶[/COLOR] ") for _, li in lis)
    assert kodi.cache is False and _live_requests(fake_server) == []


def test_guide_now_hour_items_are_info(kodi, fake_server, guide_clock):
    from resources.lib.epgstation import guide

    t = guide.time_slots(guide_clock)[3]
    run_plugin(f"action=guide_now&t={t}")
    lis = [(url, li) for url, li, _ in kodi.items]
    assert lis
    assert all("IsPlayable" not in li.props and "action=guide_info" in url for url, li in lis
               if li.props["epgstation.onAir"] == "false")
    assert kodi.category.startswith("What's on ")


def test_guide_genres_counts(kodi, fake_server, guide_clock):
    from resources.lib.epgstation import guide

    run_plugin("action=guide_genres")
    counts = guide.genre_counts(_guide_schedules(fake_server, guide_clock), guide_clock)
    assert [li.label for _, li, _ in kodi.items] == [f["label"] for f in guide.genre_folders(counts, "ja")]
    assert kodi.category == "By genre"


def test_guide_genre_list_and_cache(kodi, fake_server, guide_clock):
    import fake_epgstation
    import xbmcplugin
    from resources.lib.epgstation import guide

    fake_epgstation.REQUESTS[fake_server].clear()
    run_plugin("action=guide_genre&genre=3")
    want = guide.genre_programs(_guide_schedules(fake_server, guide_clock), 3, guide_clock)
    lis = [li for _, li, _ in kodi.items]
    assert [li.props["epgstation.programId"] for li in lis] == [str(p["id"]) for _, p in want]
    assert all(any(g.startswith("ドラマ") for g in li.tag.data["genres"]) for li in lis)
    assert kodi.category == "ドラマ" and kodi.cache is False
    assert [m for m, *_ in kodi.sort_methods] == [xbmcplugin.SORT_METHOD_UNSORTED, xbmcplugin.SORT_METHOD_GENRE,
                                                  xbmcplugin.SORT_METHOD_STUDIO, xbmcplugin.SORT_METHOD_TITLE]
    assert len(_requests(fake_server, "/api/schedules")) == 2  # this test's reference GET + the plugin's
    fake_epgstation.REQUESTS[fake_server].clear()
    kodi.reset()
    run_plugin("action=guide_genres")
    kodi.reset()
    run_plugin("action=guide_genre&genre=3")
    assert _requests(fake_server, "/api/schedules") == []  # cache hit
    assert _live_requests(fake_server) == []


def test_guide_channel_days_and_day(kodi, fake_server, guide_clock):
    import fake_epgstation
    from resources.lib.epgstation import guide

    fake_epgstation.REQUESTS[fake_server].clear()
    run_plugin("action=guide_channel&channelId=3211240960&name=NHK総合1・大阪")
    queries = _requests(fake_server, "/api/schedules/3211240960")
    assert len(queries) == 1 and queries[0]["days"] == ["8"]
    assert int(queries[0]["startAt"][0]) == guide.local_midnight(guide_clock)
    labels = [li.label for _, li, _ in kodi.items]
    assert labels[0].startswith("Today ") and labels[1].startswith("Tomorrow ") and len(labels) >= 7
    assert kodi.category == "NHK総合1・大阪 schedule"
    day1 = kodi.items[1][0]
    fake_epgstation.REQUESTS[fake_server].clear()
    kodi.reset()
    run_plugin(day1.split("?", 1)[1])
    queries = _requests(fake_server, "/api/schedules/3211240960")
    assert len(queries) == 1 and queries[0]["days"] == ["1"]
    lis = [li for _, li, _ in kodi.items]
    n = int(labels[1].rsplit("(", 1)[1].rstrip(")"))
    assert len(lis) == n and all("Schedule" not in _ctx(li) for li in lis)  # its own channel: no 番組表 entry
    assert all(li.props["epgstation.onAir"] == "false" for li in lis)
    assert kodi.category.startswith("NHK総合1・大阪 ")
    kodi.reset()
    today = guide.local_midnight(guide_clock)
    run_plugin(f"action=guide_channel&channelId=3211240960&name=NHK総合1・大阪&day={today}")
    assert kodi.items[0][1].label.startswith("[COLOR yellow]▶[/COLOR] ")


def test_guide_info_opens_dialog(kodi, fake_server, guide_clock):
    import fake_epgstation
    import xbmcgui
    from resources.lib.epgstation import guide

    tomorrow = guide.day_start(guide_clock, 1)
    run_plugin(f"action=guide_channel&channelId=3211240960&name=NHK総合1・大阪&day={tomorrow}")
    url, li, _ = kodi.items[0]
    fake_epgstation.REQUESTS[fake_server].clear()
    run_plugin(url.split("?", 1)[1])
    reqs = [p for p, _ in fake_epgstation.REQUESTS[fake_server]]
    assert reqs == [f"/api/schedules/detail/{li.props['epgstation.programId']}"]
    assert xbmcgui.info_shown and xbmcgui.info_shown[-1].tag.data["title"] == li.tag.data["title"]
    assert _live_requests(fake_server) == []


def test_guide_info_missing_program(kodi, fake_server, guide_clock):
    import xbmcgui

    run_plugin("action=guide_info&programId=1&channelId=3211240960&name=x")
    assert not xbmcgui.info_shown
    assert xbmcgui.notifications[-1][1] == "Program no longer in the guide"


def test_fake_server_channel_schedule_days(fake_server, handler):
    from resources.lib.epgstation import guide

    now = _captured()
    handler.now = now
    mid = guide.local_midnight(now)
    out = _get(fake_server, f"/api/schedules/3211240960?startAt={mid}&days=3")
    assert len(out) == 3 and all(s["channel"]["id"] == 3211240960 for s in out)
    assert all(s["programs"] for s in out)
    p = out[1]["programs"][0]
    assert _get(fake_server, f"/api/schedules/detail/{p['id']}")["name"] == p["name"]
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{fake_server}/api/schedules/detail/1")
    assert e.value.code == 404


def test_guide_actions_never_stream_or_post(kodi, fake_server, guide_clock):
    import fake_epgstation

    fake_epgstation.REQUESTS[fake_server].clear()
    for q in ("action=live", "action=guide_now", "action=guide_now&t=now", "action=guide_genres",
              "action=guide_genre&genre=0", "action=guide_channel&channelId=3211240960&name=x",
              f"action=guide_channel&channelId=3211240960&name=x&day={guide_clock}"):
        kodi.reset()
        run_plugin(q)
        assert kodi.ended is True, q
    assert _live_requests(fake_server) == []


@pytest.mark.parametrize("query", ["action=guide_now", "action=guide_now&t=now", "action=guide_genres",
                                   "action=guide_genre&genre=0", "action=guide_channel&channelId=3211240960&name=x"])
def test_clock_dependent_guide_lists_are_not_cached(kodi, fake_server, guide_clock, query):
    """M2: labels depend on the clock ("現在 (15:28)", hour folders, 今日/明日, ▶): Kodi must not keep a copy."""
    run_plugin(query)
    assert kodi.ended is True and kodi.cache is False


def test_guide_info_paths_of_the_dialog_item(kodi, fake_server, guide_clock):
    """L2: started since the list was made -> 再生 plays it through live_play; future -> no path (Kodi's own
    "Playback failed" on 再生: no request, no stream). Never the channel's live_play or guide_info for a future one."""
    import xbmcgui
    from resources.lib.epgstation import guide

    run_plugin("action=guide_now&t=now")
    on_air = kodi.items[0][1]
    run_plugin(f"action=guide_info&programId={on_air.props['epgstation.programId']}"
               f"&channelId={on_air.props['epgstation.channelId']}&name=x")
    li = xbmcgui.info_shown[-1]
    assert "action=live_play" in li.path and f"channelId={on_air.props['epgstation.channelId']}" in li.path
    assert li.props.get("IsPlayable") == "true"
    kodi.reset()
    tomorrow = guide.day_start(guide_clock, 1)
    run_plugin(f"action=guide_channel&channelId=3211240960&name=x&day={tomorrow}")
    fut = kodi.items[0][1]
    run_plugin(f"action=guide_info&programId={fut.props['epgstation.programId']}&channelId=3211240960&name=x")
    li = xbmcgui.info_shown[-1]
    assert li.path == "" and "IsPlayable" not in li.props


@pytest.mark.parametrize("query", ["action=guide_now&t=abc", "action=guide_genre&genre=x", "action=guide_genre",
                                   "action=guide_channel&channelId=3211240960&name=x&day=x",
                                   "action=guide_channel&name=x", "action=guide_channel&channelId=1;x&name=x"])
def test_guide_bad_params_end_the_directory(kodi, fake_server, guide_clock, query):
    """L3: a bad parameter ends the directory with succeeded=False and a notification, never a raw exception."""
    import xbmcgui

    run_plugin(query)
    assert kodi.ended is False
    assert xbmcgui.notifications and xbmcgui.notifications[-1][1] == "Invalid link"


def test_guide_info_bad_params_notify_without_dialog(kodi, fake_server, guide_clock):
    import xbmcgui

    run_plugin("action=guide_info&channelId=3211240960&name=x")
    assert not xbmcgui.info_shown and xbmcgui.notifications[-1][1] == "Invalid link"
    assert kodi.ended is None  # run by a click: no directory


def test_guide_malformed_cache_is_refetched(kodi, fake_server, guide_clock):
    """L3: valid JSON of the wrong shape ("schedules": [{}]) counts as no cache."""
    import fake_epgstation
    import xbmcvfs
    from resources.lib.epgstation import guide

    path = os.path.join(xbmcvfs.profile_dir, "addon_data", "plugin.video.epgstation", guide.CACHE_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"v": 1, "server": fake_server, "types": ["GR", "BS", "CS"], "t": guide_clock,
                   "end": guide_clock + 2 * guide.WINDOW_MS, "schedules": [{}]}, f)
    fake_epgstation.REQUESTS[fake_server].clear()
    run_plugin("action=guide_genres")
    assert kodi.ended is True and len(_requests(fake_server, "/api/schedules")) == 1


# --- backlog 007: scheduled recordings ---------------------------------------------------------------------------

def _set(handler):
    return os.path.basename(handler.root)


@pytest.fixture
def reserve_clock(handler, monkeypatch):
    """The plugin's clock = the fixture set's reserves_meta capturedAt ("recording now" lines up with the capture)."""
    from resources.lib.epgstation import plugin

    path = os.path.join(handler.root, "reserves_meta.json")
    if not os.path.isfile(path):
        pytest.skip("no reservation fixtures")
    with open(path, encoding="utf-8") as f:
        now = json.load(f)["capturedAt"]
    monkeypatch.setattr(plugin, "_now_ms", lambda: now)
    return now


def _set_clock(monkeypatch, now):
    from resources.lib.epgstation import plugin

    monkeypatch.setattr(plugin, "_now_ms", lambda: now)


def _all_reserves(fake_server):
    return _get(fake_server, "/api/reserves?type=all&limit=1000&isHalfWidth=false")["reserves"]


def _reserve_paths(fake_server):
    import fake_epgstation

    return [p for p, _ in fake_epgstation.REQUESTS[fake_server] if p.startswith("/api/reserves")]


def _future(handler, now, state="normal", rule=True):
    """A captured reservation of this state starting after now (rule / manual derived by dropping ruleId)."""
    from resources.lib.epgstation import reserves

    for r in handler.reserves_state:
        if reserves.classify(r) == state and r["startAt"] > now:
            if not rule:
                r.pop("ruleId", None)
            return r
    pytest.skip(f"no future {state} reservation in {_set(handler)}")


def _make(handler, r, state):
    """Derive a state on a captured reservation (none of these exist on both servers)."""
    r.update(isConflict=state == "conflict", isSkip=state == "skip", isOverlap=state == "overlap")
    return r


def _action(op, rid):
    return f"action=reserve_action&op={op}&reserveId={rid}"


def test_reserves_list_folders_and_items(kodi, fake_server, handler, reserve_clock):
    import fake_epgstation
    import xbmcplugin
    from resources.lib.epgstation import api, items, reserves

    want_reserves = _all_reserves(fake_server)
    records = _get(fake_server, "/api/recording")["records"]
    channels = _get(fake_server, "/api/channels")
    rules = {r["id"]: items.rule_name(r) for r in _get(fake_server, "/api/rules?type=all&limit=1000")["rules"]}
    fake_epgstation.REQUESTS[fake_server].clear()
    run_plugin("action=reserves")
    assert kodi.ended is True and kodi.cache is False and kodi.content == "videos"
    assert kodi.category == "Scheduled recordings"
    want = reserves.listing(want_reserves, None, items.channel_names(channels),
                            {c["id"] for c in channels if c.get("hasLogoData")}, rules, api.EPGStation(fake_server),
                            reserve_clock, "ja", {"reserves": "Scheduled recordings"}, None, records)
    assert [li.label for _, li, _ in kodi.items] == [d["label"] for d in want]
    assert [folder for *_, folder in kodi.items] == [d["folder"] for d in want]
    up = reserves.upcoming(want_reserves, reserve_clock)
    n_folders = len(reserves.type_folders(reserves.counts(up)))
    assert len(kodi.items) == n_folders + len(reserves.split(up, "normal"))
    reqs = fake_epgstation.REQUESTS[fake_server]
    assert [q for p, q in reqs if p == "/api/reserves"] == [{"type": ["all"], "isHalfWidth": ["false"], "offset": ["0"],
                                                             "limit": ["1000"]}]
    assert sorted(p for p, _ in reqs) == ["/api/channels", "/api/recording", "/api/reserves", "/api/rules"]
    assert [m for m, *_ in kodi.sort_methods] == [xbmcplugin.SORT_METHOD_UNSORTED, xbmcplugin.SORT_METHOD_GENRE,
                                                  xbmcplugin.SORT_METHOD_STUDIO, xbmcplugin.SORT_METHOD_TITLE]
    items_ = [li for _, li, folder in kodi.items if not folder]
    assert items_ and all(li.props["epgstation.state"] == "normal" for li in items_)
    assert all("IsPlayable" not in li.props for li in items_)
    assert all(len(li.context) == 1 and li.context[0][1].startswith("RunPlugin(") for li in items_)
    assert handler.writes == []


def test_reserves_recording_now_is_marked(kodi, fake_server, handler, reserve_clock):
    run_plugin("action=reserves")
    rec = [li for _, li, _ in kodi.items if li.props.get("epgstation.recording") == "true"]
    if _set(handler) == "dev":
        assert rec  # captured while recording
    for li in rec:
        assert li.label.startswith("[COLOR red]●[/COLOR] ")
        assert li.context[0][0] == "Stop recording" and "op=stop" in li.context[0][1]


def test_reserves_in_window_but_not_recording_gets_cancel(kodi, fake_server, handler, reserve_clock, monkeypatch):
    """L3: inside its time window but not in /recording (failed recording, PREP_TIME): no ●, normal cancel/skip."""
    r = _future(handler, reserve_clock)
    _set_clock(monkeypatch, r["startAt"] + 1000)
    run_plugin("action=reserves")
    li = next(li for _, li, _ in kodi.items if li.props.get("epgstation.reserveId") == str(r["id"]))
    assert not li.label.startswith("[COLOR red]") and li.props["epgstation.recording"] == "false"
    assert "op=cancel" in li.context[0][1]


def test_reserves_type_folders(kodi, fake_server, handler, reserve_clock):
    from resources.lib.epgstation import reserves

    _make(handler, _future(handler, reserve_clock), "conflict")
    up = reserves.upcoming(_all_reserves(fake_server), reserve_clock)
    run_plugin("action=reserves")
    folders = [(url, li.label) for url, li, folder in kodi.items if folder]
    assert folders and folders[0][1].startswith("Conflicts (")
    for state, title in (("conflict", "Conflicts"), ("overlap", "Duplicates"), ("skip", "Skipped")):
        kodi.reset()
        run_plugin(f"action=reserves&type={state}")
        assert kodi.ended is True and kodi.category == title
        want = reserves.split(up, state)
        assert [li.props["epgstation.reserveId"] for _, li, _ in kodi.items] == [str(r["id"]) for r in want]
        assert not any(li.label.startswith("[COLOR") for _, li, _ in kodi.items)
        if want:
            assert any(label == f"{title} ({len(want)})" for _, label in folders)
        else:
            assert not any(label.startswith(title + " (") for _, label in folders)


def test_reserves_bad_type(kodi, fake_server, reserve_clock):
    import xbmcgui

    run_plugin("action=reserves&type=bogus")
    assert kodi.ended is False and xbmcgui.notifications[-1][1] == "Invalid link"


def test_reserves_pages_beyond_page_size(kodi, fake_server, handler, reserve_clock, monkeypatch):
    from resources.lib.epgstation import reserves

    monkeypatch.setattr(reserves, "PAGE", 5)
    run_plugin("action=reserves")
    want = reserves.listing(_all_reserves(fake_server), None, {}, set(), {}, None, reserve_clock)
    assert len(kodi.items) == len(want)
    assert len(_requests(fake_server, "/api/reserves")) >= len(handler.reserves_state) // 5


def test_reserve_info_opens_dialog(kodi, fake_server, handler, reserve_clock):
    import fake_epgstation
    import xbmcgui

    run_plugin("action=reserves")
    url, li, _ = next(x for x in kodi.items if not x[2])
    fake_epgstation.REQUESTS[fake_server].clear()
    kodi.reset()
    run_plugin(url.split("?", 1)[1])
    rid = li.props["epgstation.reserveId"]
    assert [p for p, _ in fake_epgstation.REQUESTS[fake_server]] == [f"/api/reserves/{rid}"]
    shown = xbmcgui.info_shown[-1]
    assert shown.tag.data["title"] == li.tag.data["title"] and shown.tag.data["plot"] == li.tag.data["plot"]
    assert shown.tag.data["studios"] == li.tag.data["studios"] and shown.art == li.art
    assert shown.path == "" and kodi.ended is None and handler.writes == []


def test_reserve_info_missing(kodi, fake_server, reserve_clock):
    import xbmc
    import xbmcgui

    run_plugin("action=reserve_info&reserveId=999999&name=x")
    assert not xbmcgui.info_shown
    assert xbmcgui.notifications[-1][1] == "Reservation is gone"
    assert xbmc.builtins == ["Container.Refresh"] and kodi.ended is None


@pytest.mark.parametrize("op,state,rule", [("cancel", "normal", True), ("cancel", "normal", False),
                                           ("cancel", "conflict", True), ("unskip", "skip", True),
                                           ("unoverlap", "overlap", True)])
def test_reserve_action_no_writes_nothing(kodi, fake_server, handler, reserve_clock, op, state, rule):
    import xbmcgui

    r = _future(handler, reserve_clock, "normal", rule)
    _make(handler, r, state)
    before = json.dumps(handler.reserves_state, sort_keys=True)
    xbmcgui.yesno_answers.append(False)
    run_plugin(_action(op, r["id"]))
    assert len(xbmcgui.yesnos) == 1
    assert handler.writes == []
    assert json.dumps(handler.reserves_state, sort_keys=True) == before
    assert not xbmcgui.notifications and kodi.ended is None


@pytest.mark.parametrize("op,state,rule,path,label,done", [
    ("cancel", "normal", True, "", "Skip this airing", "Airing will not record"),
    ("cancel", "normal", False, "", "Delete reservation", "Reservation deleted"),
    ("cancel", "conflict", True, "", "Skip this airing", "Airing will not record"),
    ("cancel", "conflict", False, "", "Delete reservation", "Reservation deleted"),
    ("unskip", "skip", True, "/skip", "Undo skip", "Skip undone"),
    ("unoverlap", "overlap", True, "/overlap", "Record anyway", "Will record"),
])
def test_reserve_action_yes_writes_once(kodi, fake_server, handler, reserve_clock, op, state, rule, path, label,
                                        done):
    import xbmc
    import xbmcgui
    from resources.lib.epgstation import reserves

    r = _future(handler, reserve_clock, "normal", rule)
    _make(handler, r, state)
    rid = r["id"]
    xbmcgui.yesno_answers.append(True)
    run_plugin(_action(op, rid))
    y = xbmcgui.yesnos[-1]
    assert y["heading"] == label and y["yeslabel"] == label and y["nolabel"] == "Cancel"
    assert y["defaultbutton"] == xbmcgui.DLG_YESNO_NO_BTN
    lines = y["message"].split("\n")
    assert lines[0] == reserves.to_half(r["name"]) and reserves.time_line(r) in lines[1]
    assert handler.writes == [("DELETE", f"/api/reserves/{rid}{path}", "")]
    assert xbmcgui.notifications[-1][1] == done
    assert xbmc.builtins == ["Container.Refresh"]
    after = next((x for x in handler.reserves_state if x["id"] == rid), None)
    if op == "cancel" and not rule:
        assert after is None
    elif op == "cancel":
        assert reserves.classify(after) == "skip"
    else:
        assert reserves.classify(after) == "normal"
    assert any("reserve: " + op in m for _, m in xbmc.logs)


def test_reserve_action_state_changed_no_write(kodi, fake_server, handler, reserve_clock):
    import xbmc
    import xbmcgui

    r = _future(handler, reserve_clock)
    _make(handler, r, "skip")  # skipped elsewhere since the list was made
    xbmcgui.yesno_answers.append(True)
    run_plugin(_action("cancel", r["id"]))
    assert not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "Reservation has changed"
    assert xbmc.builtins == ["Container.Refresh"]


def test_reserve_action_state_changes_while_the_dialog_is_open(kodi, fake_server, handler, reserve_clock):
    """M1: the reservation is re-read after the yes; skipped elsewhere meanwhile -> 30052, refresh, no write."""
    import xbmc
    import xbmcgui

    r = _future(handler, reserve_clock)
    xbmcgui.yesno_answers.append(True)
    xbmcgui.yesno_hook = lambda: _make(handler, r, "skip")
    run_plugin(_action("cancel", r["id"]))
    assert len(xbmcgui.yesnos) == 1 and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "Reservation has changed"
    assert xbmc.builtins == ["Container.Refresh"]


def test_reserve_action_gone_while_the_dialog_is_open(kodi, fake_server, handler, reserve_clock):
    import xbmcgui

    r = _future(handler, reserve_clock)
    xbmcgui.yesno_answers.append(True)
    xbmcgui.yesno_hook = lambda: handler.reserves_state.remove(r)
    run_plugin(_action("cancel", r["id"]))
    assert handler.writes == [] and xbmcgui.notifications[-1][1] == "Reservation is gone"


def test_reserve_action_double_confirm_writes_once(kodi, fake_server, handler, reserve_clock):
    """M1: a double click opens two dialogs; both answered yes -> only the first one writes."""
    import xbmcgui

    r = _future(handler, reserve_clock)
    xbmcgui.yesno_answers.extend([True, True])

    def second_click():
        xbmcgui.yesno_hook = None
        run_plugin(_action("cancel", r["id"]))  # the second dialog, answered while the first is still open

    xbmcgui.yesno_hook = second_click
    run_plugin(_action("cancel", r["id"]))
    assert len(xbmcgui.yesnos) == 2
    assert handler.writes == [("DELETE", f"/api/reserves/{r['id']}", "")]
    assert xbmcgui.notifications[-1][1] == "Reservation has changed"


def test_reserve_action_missing_no_write(kodi, fake_server, handler, reserve_clock):
    import xbmc
    import xbmcgui

    xbmcgui.yesno_answers.append(True)
    run_plugin(_action("cancel", 999999))
    assert not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "Reservation is gone"
    assert xbmc.builtins == ["Container.Refresh"]


@pytest.mark.parametrize("query", ["action=reserve_action&op=cancel", "action=reserve_action&op=x&reserveId=1",
                                   "action=reserve_action&op=cancel&reserveId=x",
                                   "action=reserve_action&op=cancel&recordedId=1"])
def test_reserve_action_bad_params(kodi, fake_server, handler, reserve_clock, query):
    import xbmcgui

    run_plugin(query)
    assert handler.writes == [] and not xbmcgui.yesnos
    assert xbmcgui.notifications[-1][1] == "Invalid link" and kodi.ended is None


@pytest.mark.parametrize("status,body,want", [
    (500, {"code": 500, "message": "Internal Server Error", "errors": "ReservationIsNotFound"}, "Reservation is gone"),
    (500, {"code": 500, "message": "Internal Server Error", "errors": "SomethingElse"}, "Failed: SomethingElse"),
    (500, {"code": 500, "message": "Internal Server Error"}, "Failed: Internal Server Error"),
    (503, "", "Failed: HTTP 503"),
])
def test_reserve_action_server_error(kodi, fake_server, handler, reserve_clock, status, body, want):
    """L1: readable texts; the full error stays in the log."""
    import xbmc
    import xbmcgui

    r = _future(handler, reserve_clock)
    handler.write_error = (status, body)
    xbmcgui.yesno_answers.append(True)
    run_plugin(_action("cancel", r["id"]))
    assert handler.writes == [("DELETE", f"/api/reserves/{r['id']}", "")]
    assert xbmcgui.notifications[-1][1] == want
    assert xbmcgui.notifications[-1][2] == xbmcgui.NOTIFICATION_ERROR
    assert xbmc.builtins == ["Container.Refresh"]
    assert any(f"HTTP {status} for /reserves/{r['id']}" in m and "failed" in m for _, m in xbmc.logs)


def test_reserve_action_write_timeout(kodi, fake_server, handler, reserve_clock, monkeypatch):
    """L1: no answer (timeout, connection reset) -> a short generic text; the log says it may have been applied."""
    import xbmc
    import xbmcgui
    from resources.lib.epgstation import api

    r = _future(handler, reserve_clock)

    def timeout(self, reserve_id):
        raise api.ApiError(f"/reserves/{reserve_id}: timed out")

    monkeypatch.setattr(api.EPGStation, "cancel_reserve", timeout)
    xbmcgui.yesno_answers.append(True)
    run_plugin(_action("cancel", r["id"]))
    assert xbmcgui.notifications[-1][1] == "Failed (connection error)"
    assert any("timed out" in m and "may still have applied" in m for _, m in xbmc.logs)


def test_reserve_action_unreachable_server(kodi, fake_server, handler, reserve_clock):
    """AC 10: a dead server -> error notification, no dialog, nothing written."""
    import xbmcaddon
    import xbmcgui

    xbmcaddon.settings["server_url"] = "http://127.0.0.1:1"
    xbmcgui.yesno_answers.append(True)
    run_plugin(_action("cancel", 1))
    assert not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][2] == xbmcgui.NOTIFICATION_ERROR and kodi.ended is None
    assert xbmcgui.notifications[-1][1] == "Failed (connection error)"  # short, no URL or urllib text


def _recording_reserve(handler, monkeypatch, reserve_clock):
    """A reservation recording now: the captured one (dev), else a future normal one with the clock moved into it
    and a /recording item derived from it (for a set captured with nothing recording)."""
    from resources.lib.epgstation import reserves

    for rec in handler.recording_state:
        r = reserves.match_recording(handler.reserves_state, rec)
        if r is not None and reserves.is_recording(r, handler.recording_state):
            return r, rec, reserve_clock
    r = _future(handler, reserve_clock)
    now = r["startAt"] + 60 * 1000
    _set_clock(monkeypatch, now)
    rec = {k: r[k] for k in ("channelId", "programId", "startAt", "endAt", "name", "ruleId") if k in r}
    # id below fake_epgstation.COPY_ID (larger ids are --multiply copies)
    rec.update(id=99901, isRecording=True, isEncoding=False, isProtected=False, videoFiles=[], thumbnails=[])
    handler.recording_state.append(rec)
    return r, rec, now


@pytest.mark.parametrize("via", ["reserve", "recorded"])
def test_reserve_action_stop(kodi, fake_server, handler, reserve_clock, monkeypatch, via):
    import fake_epgstation
    import xbmc
    import xbmcgui

    r, rec, now = _recording_reserve(handler, monkeypatch, reserve_clock)
    fake_epgstation.REQUESTS[fake_server].clear()
    xbmcgui.yesno_answers.append(True)
    by_recorded = f"action=reserve_action&op=stop&recordedId={rec['id']}"
    run_plugin(_action("stop", r["id"]) if via == "reserve" else by_recorded)
    y = xbmcgui.yesnos[-1]
    assert y["heading"] == "Stop recording" and y["defaultbutton"] == xbmcgui.DLG_YESNO_NO_BTN
    assert "What has been recorded so far is kept." in y["message"]
    if r.get("ruleId") is not None:
        assert y["message"].endswith("The rest of this airing will not be recorded.")
    assert handler.writes == [("DELETE", f"/api/reserves/{r['id']}", "")]
    assert xbmcgui.notifications[-1][1] == "Recording stopped"
    assert xbmc.builtins == ["Container.Refresh"]
    paths = [p for p, _ in fake_epgstation.REQUESTS[fake_server]]
    if via == "recorded":
        assert paths[0] == f"/api/recorded/{rec['id']}" and "/api/reserves/lists" in paths
    assert "/api/recording" in paths  # waited until the recording left /recording
    assert all(x["id"] != rec["id"] for x in handler.recording_state)


def test_reserve_action_stop_waits_at_most_stop_wait(kodi, fake_server, handler, reserve_clock, monkeypatch):
    import xbmc
    import xbmcgui
    from resources.lib.epgstation import plugin, reserves

    r, rec, now = _recording_reserve(handler, monkeypatch, reserve_clock)
    handler.stop_drops_recording = False  # the server is slow to stop it
    monkeypatch.setattr(plugin, "_clock", lambda: xbmc.waited)  # time passes only in the waits (stub: no sleep)
    xbmcgui.yesno_answers.append(True)
    run_plugin(_action("stop", r["id"]))
    assert handler.writes == [("DELETE", f"/api/reserves/{r['id']}", "")]
    assert reserves.STOP_WAIT - reserves.STOP_STEP <= xbmc.waited <= reserves.STOP_WAIT
    assert xbmc.builtins == ["Container.Refresh"]


@pytest.mark.parametrize("delay", [0.3, 2.0])
def test_reserve_action_stop_wait_is_a_wall_clock_deadline(kodi, fake_server, handler, reserve_clock, monkeypatch,
                                                           delay):
    """Codex P2: slow /recording answers count against the 5 s; each request's timeout is capped to what is left."""
    import time

    import xbmc
    import xbmcgui
    from resources.lib.epgstation import reserves

    r, rec, now = _recording_reserve(handler, monkeypatch, reserve_clock)
    monkeypatch.setattr(reserves, "STOP_WAIT", 1.0)
    handler.stop_drops_recording = False
    handler.recording_delay = delay  # only after the DELETE
    xbmcgui.yesno_answers.append(True)
    t = time.monotonic()
    run_plugin(_action("stop", r["id"]))
    elapsed = time.monotonic() - t
    assert handler.writes == [("DELETE", f"/api/reserves/{r['id']}", "")]
    assert elapsed < reserves.STOP_WAIT + 0.5, elapsed
    assert xbmc.builtins == ["Container.Refresh"]


def test_reserve_action_stop_of_finished_recording(kodi, fake_server, handler, reserve_clock):
    """recordedId of a recording that is no longer in progress: nothing to stop, no write."""
    import xbmcgui

    rec = _get(fake_server, "/api/recorded?limit=1")["records"][0]
    xbmcgui.yesno_answers.append(True)
    run_plugin(f"action=reserve_action&op=stop&recordedId={rec['id']}")
    assert not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "Reservation is gone"


def test_recording_now_items_offer_stop(kodi, fake_server, handler, reserve_clock, monkeypatch):
    _recording_reserve(handler, monkeypatch, reserve_clock)
    run_plugin("action=recording")
    assert kodi.items
    for _, li, _ in kodi.items:
        assert ("Stop recording", "RunPlugin(plugin://plugin.video.epgstation/?action=reserve_action&op=stop"
                f"&recordedId={li.props['epgstation.recordedId']})") in li.context


def test_api_delete_and_errors(fake_server, handler, monkeypatch):
    import urllib.request as ur

    from resources.lib.epgstation import api

    r = handler.reserves_state[0]
    sent = []
    real = ur.urlopen

    def spy(req, timeout=None):
        sent.append((req.get_method(), req.full_url, req.get_header("Authorization")))
        return real(req, timeout=timeout)

    monkeypatch.setattr(ur, "urlopen", spy)
    client = api.EPGStation(fake_server, "user", "pw")
    assert client.cancel_reserve(r["id"]) is None  # empty 200 body
    assert sent[-1][0] == "DELETE" and sent[-1][1].endswith(f"/api/reserves/{r['id']}")
    assert sent[-1][2].startswith("Basic ")
    with pytest.raises(api.ApiError) as e:
        client.remove_skip(999999)
    assert e.value.status == 500 and e.value.detail == "ReservationIsNotFound"
    with pytest.raises(api.ApiError) as e:
        client.reserve(999999)
    assert e.value.status == 404 and e.value.detail == "reserve is not found"
    client.reserves(limit=7)
    assert "limit=7" in sent[-1][1] and "type=all" in sent[-1][1]
    assert [w[:2] for w in handler.writes] == [("DELETE", f"/api/reserves/{r['id']}"),
                                               ("DELETE", "/api/reserves/999999/skip")]


def test_fake_server_reserve_routes(fake_server, handler):
    cnts = _get(fake_server, "/api/reserves/cnts")
    assert sum(cnts.values()) == len(handler.reserves_state)
    assert len(_get(fake_server, "/api/reserves?type=all")["reserves"]) == min(24, len(handler.reserves_state))
    r = handler.reserves_state[0]
    lists = _get(fake_server, f"/api/reserves/lists?startAt={r['startAt']}&endAt={r['startAt']}")
    assert any(x["reserveId"] == r["id"] for v in lists.values() for x in v)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{fake_server}/api/reserves/{r['id']}")  # isHalfWidth is required
    assert e.value.code == 400
    req = urllib.request.Request(f"{fake_server}/api/reserves/1", data=b"{}", method="PUT")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 405 and handler.writes == [("PUT", "/api/reserves/1", "{}")]


ALL_READ_ACTIONS = ["", "action=browse", "action=list", "action=recording", "action=movies", "action=tvshows",
                    "action=channels", "action=genres", "action=rules", "action=live", "action=live&type=GR",
                    "action=guide_now", "action=guide_genres", "action=reserves", "action=reserves&type=overlap",
                    "action=reserves&type=skip", "action=reserves&type=conflict",
                    "action=guide_now&t=now", "action=guide_genre&genre=3", "action=live&type=BS",
                    "action=guide_channel&channelId=3211240960&name=x"]
GUIDE_READ_ACTIONS = ["action=live", "action=live&type=GR", "action=live&type=BS", "action=guide_now",
                      "action=guide_now&t=now", "action=guide_genres", "action=guide_genre&genre=0",
                      "action=guide_genre&genre=3", "action=guide_genre&genre=7",
                      "action=guide_channel&channelId=3211240960&name=x"]


def test_no_action_but_reserve_action_and_record_writes(kodi, fake_server, handler, reserve_clock):
    import xbmcgui

    for q in ALL_READ_ACTIONS:
        kodi.reset()
        xbmcgui.yesno_answers[:] = [True] * 3  # would say yes to anything
        run_plugin(q)
    run_plugin("action=reserves")
    url = next(u for u, _, folder in kodi.items if not folder)
    run_plugin(url.split("?", 1)[1])  # reserve_info
    assert handler.writes == [] and not xbmcgui.yesnos


# --- backlog 014: record from the guide --------------------------------------------------------------------------

def _reserve_on(handler, program, state="normal", rule=True):
    """Copy a captured reservation onto a guide program (programId, channelId, times, name): never hand-written JSON."""
    base = handler._load("/api/reserves")["reserves"][0]
    r = json.loads(json.dumps(base))
    r.update(id=max([x["id"] for x in handler.reserves_state] + [0]) + 1, programId=program["id"],
             channelId=program["channelId"], startAt=program["startAt"], endAt=program["endAt"],
             name=program["name"], isConflict=state == "conflict", isSkip=state == "skip",
             isOverlap=state == "overlap", isTimeSpecified=False)
    if not rule:
        r.pop("ruleId", None)
    elif r.get("ruleId") is None:
        r["ruleId"] = 1
    handler.reserves_state.append(r)
    return r


def _detail(fake_server, pid):
    return _get(fake_server, f"/api/schedules/detail/{pid}?isHalfWidth=false")


def _lists_queries(fake_server):
    return _requests(fake_server, "/api/reserves/lists")


def _ctx(li):
    return [c[0] for c in li.context]


def _record_url(li):
    url = next(b for label, b in li.context if label == "Record")
    return url[len("RunPlugin(plugin://plugin.video.epgstation/?"):-1]


def _tomorrow_items(kodi, guide_clock):
    from resources.lib.epgstation import guide

    kodi.reset()
    run_plugin(f"action=guide_channel&channelId=3211240960&name=NHK総合1・大阪&day={guide.day_start(guide_clock, 1)}")
    return [li for _, li, _ in kodi.items]


def _free_item(kodi, guide_clock):
    """A future NHK総合1 program of tomorrow that is not reserved (its entry is 録画)."""
    li = next((li for li in _tomorrow_items(kodi, guide_clock) if _ctx(li) == ["Record"]), None)
    if li is None:
        pytest.skip("no unreserved program tomorrow")
    kodi.reset()
    return li


def test_guide_genre_marks_reserved_programs(kodi, fake_server, handler, guide_clock):
    import fake_epgstation
    from resources.lib.epgstation import guide

    rows = guide.genre_programs(_guide_schedules(fake_server, guide_clock), 3, guide_clock)
    future = [p for _, p in rows if p["startAt"] > guide_clock and p["id"] not in
              {r.get("programId") for r in handler.reserves_state}]
    if len(future) < 3:
        pytest.skip("not enough future drama programs")
    rule = _reserve_on(handler, _detail(fake_server, future[0]["id"]))
    manual = _reserve_on(handler, _detail(fake_server, future[1]["id"]), rule=False)
    over = _reserve_on(handler, _detail(fake_server, future[2]["id"]), state="overlap")
    fake_epgstation.REQUESTS[fake_server].clear()
    run_plugin("action=guide_genre&genre=3")
    by_id = {li.props["epgstation.programId"]: li for _, li, _ in kodi.items}
    li = by_id[str(rule["programId"])]
    assert li.label.startswith("[COLOR red]○[/COLOR] ") and _ctx(li) == ["Schedule", "Skip this airing"]
    assert f"reserveId={rule['id']}" in li.context[1][1] and "Scheduled: rule" in li.tag.data["plot"].split("\n")[1]
    li = by_id[str(manual["programId"])]
    assert li.label.startswith("[COLOR red]○[/COLOR] ") and _ctx(li) == ["Schedule", "Delete reservation"]
    assert li.tag.data["plot"].split("\n")[1] == "Scheduled: manual"
    li = by_id[str(over["programId"])]
    assert li.label.startswith("[COLOR grey][Duplicates][/COLOR] ") and _ctx(li)[1] == "Record anyway"
    queries = _lists_queries(fake_server)
    assert len(queries) == 1
    assert int(queries[0]["startAt"][0]) == min(p["startAt"] for _, p in rows)
    assert int(queries[0]["endAt"][0]) == max(p["endAt"] for _, p in rows)
    assert _requests(fake_server, "/api/reserves") == [] and handler.writes == []
    lists = _get(fake_server, f"/api/reserves/lists?startAt={queries[0]['startAt'][0]}&endAt={queries[0]['endAt'][0]}")
    marked = {str(x["programId"]) for v in lists.values() for x in v if "programId" in x}
    for pid, li in by_id.items():
        label = li.label[len("[COLOR yellow]▶[/COLOR] "):] if li.props["epgstation.onAir"] == "true" else li.label
        assert label.startswith(("[COLOR red]", "[COLOR grey]")) == (pid in marked), li.label
        if pid not in marked:
            assert _ctx(li) == ["Schedule", "Record"], li.label


def test_guide_lists_fetch_recording_only_for_on_air_reservations(kodi, fake_server, handler, guide_clock):
    import fake_epgstation

    run_plugin("action=guide_now&t=now")
    on_air = [li for _, li, _ in kodi.items]
    reserved = {r.get("programId") for r in handler.reserves_state}
    normal_on_air = [li for li in on_air if int(li.props["epgstation.programId"]) in reserved]
    fake_epgstation.REQUESTS[fake_server].clear()
    kodi.reset()
    run_plugin("action=guide_now&t=now")
    assert len(_lists_queries(fake_server)) == 1
    q = _lists_queries(fake_server)[0]
    assert int(q["startAt"][0]) <= guide_clock <= int(q["endAt"][0])
    if not normal_on_air:
        assert _requests(fake_server, "/api/recording") == []
        li = on_air[0]
        _reserve_on(handler, _detail(fake_server, int(li.props["epgstation.programId"])))
        fake_epgstation.REQUESTS[fake_server].clear()
        kodi.reset()
        run_plugin("action=guide_now&t=now")
    assert len(_requests(fake_server, "/api/recording")) == 1
    marked = [li for _, li, _ in kodi.items if li.label.startswith("[COLOR yellow]▶[/COLOR] [COLOR red]")]
    assert marked


def test_guide_lists_failure_shows_listing_without_marks(kodi, fake_server, handler, guide_clock, monkeypatch):
    import xbmc
    from resources.lib.epgstation import api

    def boom(self, start, end, timeout=None):
        raise api.ApiError("HTTP 500 for /reserves/lists", 500, "x")

    monkeypatch.setattr(api.EPGStation, "reserve_lists", boom)
    for li in _tomorrow_items(kodi, guide_clock):
        assert li.context == [] and not li.label.startswith("[COLOR red]") and not li.label.startswith("[COLOR grey]")
    assert kodi.ended is True
    assert any("reserve lists" in m for lvl, m in xbmc.logs if lvl == xbmc.LOGWARNING)
    kodi.reset()
    run_plugin("action=live&type=GR")
    assert kodi.items and all(_ctx(li) == ["Schedule"] for _, li, _ in kodi.items)


def test_guide_day_list_offers_record(kodi, fake_server, handler, guide_clock):
    import fake_epgstation

    fake_epgstation.REQUESTS[fake_server].clear()
    items_ = _tomorrow_items(kodi, guide_clock)
    assert items_ and all(len(li.context) == 1 for li in items_)  # one reservation entry, no 番組表
    assert len(_lists_queries(fake_server)) == 1


def test_live_channels_marked_and_record_current_program(kodi, fake_server, handler, guide_clock):
    import fake_epgstation

    run_plugin("action=live&type=GR")
    first = kodi.items[0][1]
    pid = int(first.props["epgstation.programId"])
    if pid not in {r.get("programId") for r in handler.reserves_state}:
        _reserve_on(handler, _detail(fake_server, pid))
    fake_epgstation.REQUESTS[fake_server].clear()
    kodi.reset()
    run_plugin("action=live&type=GR")
    lis = [li for _, li, _ in kodi.items]
    assert lis[0].label.startswith("[COLOR red]") and _ctx(lis[0])[0] == "Schedule" and len(lis[0].context) == 2
    free = [li for li in lis if _ctx(li) == ["Schedule", "Record"]]
    assert free and all(f"programId={li.props['epgstation.programId']}" in li.context[1][1] for li in free)
    q = _lists_queries(fake_server)
    assert len(q) == 1 and q[0]["startAt"] == q[0]["endAt"] == [str(int(guide_clock))]


def test_record_no_writes_nothing(kodi, fake_server, handler, guide_clock):
    import xbmcgui

    li = _free_item(kodi, guide_clock)
    before = json.dumps(handler.reserves_state, sort_keys=True)
    xbmcgui.yesno_answers.append(False)
    run_plugin(_record_url(li))
    assert len(xbmcgui.yesnos) == 1 and handler.writes == []
    assert json.dumps(handler.reserves_state, sort_keys=True) == before
    assert not xbmcgui.notifications and kodi.ended is None


def test_record_yes_posts_once(kodi, fake_server, handler, guide_clock):
    import xbmc
    import xbmcgui
    from resources.lib.epgstation import reserves

    li = _free_item(kodi, guide_clock)
    pid = int(li.props["epgstation.programId"])
    p = _detail(fake_server, pid)
    xbmcgui.yesno_answers.append(True)
    run_plugin(_record_url(li))
    y = xbmcgui.yesnos[-1]
    assert y["heading"] == "Record" and y["yeslabel"] == "Record" and y["nolabel"] == "Cancel"
    assert y["defaultbutton"] == xbmcgui.DLG_YESNO_NO_BTN
    assert y["message"].split("\n") == [reserves.to_half(p["name"]),
                                        f"{reserves.time_line(p)} NHK総合1・大阪", "Record this program?"]
    assert len(handler.writes) == 1
    method, path, body = handler.writes[0]
    assert (method, path, json.loads(body)) == ("POST", "/api/reserves", {"programId": pid, "allowEndLack": True})
    assert xbmcgui.notifications[-1][1] == "Recording scheduled"
    assert xbmc.builtins == ["Container.Refresh"]
    new = [r for r in handler.reserves_state if r.get("programId") == pid]
    assert len(new) == 1 and "ruleId" not in new[0] and not new[0].get("encodeOption")
    assert any(f"reserve: add program {pid}" in m for _, m in xbmc.logs)
    assert any(f"done: reserve {new[0]['id']}" in m for _, m in xbmc.logs)
    after = next(x for x in _tomorrow_items(kodi, guide_clock) if x.props["epgstation.programId"] == str(pid))
    assert after.label.startswith("[COLOR red]○[/COLOR] ") and _ctx(after) == ["Delete reservation"]


def test_record_already_reserved_before_the_dialog(kodi, fake_server, handler, guide_clock):
    import xbmc
    import xbmcgui

    li = _free_item(kodi, guide_clock)
    _reserve_on(handler, _detail(fake_server, int(li.props["epgstation.programId"])))
    xbmcgui.yesno_answers.append(True)
    run_plugin(_record_url(li))
    assert not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "Already scheduled" and xbmc.builtins == ["Container.Refresh"]


def test_record_reserved_while_the_dialog_is_open(kodi, fake_server, handler, guide_clock):
    import xbmcgui

    li = _free_item(kodi, guide_clock)
    p = _detail(fake_server, int(li.props["epgstation.programId"]))
    xbmcgui.yesno_answers.append(True)
    xbmcgui.yesno_hook = lambda: _reserve_on(handler, p)
    run_plugin(_record_url(li))
    assert len(xbmcgui.yesnos) == 1 and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "Already scheduled"


def test_record_double_confirm_posts_once(kodi, fake_server, handler, guide_clock):
    import xbmcgui

    li = _free_item(kodi, guide_clock)
    url = _record_url(li)
    xbmcgui.yesno_answers.extend([True, True])

    def second_click():
        xbmcgui.yesno_hook = None
        run_plugin(url)  # the second dialog, answered while the first is still open

    xbmcgui.yesno_hook = second_click
    run_plugin(url)
    assert len(xbmcgui.yesnos) == 2
    assert [w[:2] for w in handler.writes] == [("POST", "/api/reserves")]
    assert [n[1] for n in xbmcgui.notifications] == ["Recording scheduled", "Already scheduled"]
    assert len([r for r in handler.reserves_state if r.get("programId") == int(li.props["epgstation.programId"])]) == 1


def test_record_server_refuses_a_duplicate(kodi, fake_server, handler, guide_clock, monkeypatch):
    """Both re-checks raced: the server's own duplicate check refuses the second POST -> 30067."""
    import xbmcgui
    from resources.lib.epgstation import api

    li = _free_item(kodi, guide_clock)
    p = _detail(fake_server, int(li.props["epgstation.programId"]))
    real = api.EPGStation.create_reserve

    def raced(self, program_id, encode_mode=None):
        _reserve_on(handler, p)  # another Kodi got there first
        return real(self, program_id, encode_mode)

    monkeypatch.setattr(api.EPGStation, "create_reserve", raced)
    xbmcgui.yesno_answers.append(True)
    run_plugin(_record_url(li))
    assert [w[:2] for w in handler.writes] == [("POST", "/api/reserves")]
    assert xbmcgui.notifications[-1][1] == "Already scheduled"
    assert len([r for r in handler.reserves_state if r.get("programId") == p["id"]]) == 1


def test_record_program_gone_or_ended(kodi, fake_server, handler, guide_clock, monkeypatch):
    import xbmc
    import xbmcgui

    xbmcgui.yesno_answers.append(True)
    run_plugin("action=record&programId=1&channelId=3211240960&name=x")
    assert not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "Program no longer in the guide" and xbmc.builtins == ["Container.Refresh"]
    li = _free_item(kodi, guide_clock)
    p = _detail(fake_server, int(li.props["epgstation.programId"]))
    _set_clock(monkeypatch, p["endAt"])  # the list was stale: it has ended
    xbmc.builtins.clear()
    run_plugin(_record_url(li))
    assert not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "This program has ended" and xbmc.builtins == ["Container.Refresh"]


def test_record_gone_while_the_dialog_is_open(kodi, fake_server, handler, guide_clock, monkeypatch):
    import xbmcgui
    from resources.lib.epgstation import plugin

    li = _free_item(kodi, guide_clock)
    p = _detail(fake_server, int(li.props["epgstation.programId"]))
    xbmcgui.yesno_answers.append(True)
    xbmcgui.yesno_hook = lambda: monkeypatch.setattr(plugin, "_now_ms", lambda: p["endAt"] + 1)
    run_plugin(_record_url(li))
    assert len(xbmcgui.yesnos) == 1 and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "This program has ended"


@pytest.mark.parametrize("knob,want", [
    ("conflict", "Not scheduled: no free tuner (conflict)"),
    ((500, {"code": 500, "message": "Internal Server Error", "errors": "Boom"}), "Failed: Boom"),
    ((500, {"code": 500, "message": "Internal Server Error", "errors": "ProgramIsNotFound"}),
     "Program no longer in the guide"),
    ((503, ""), "Failed: HTTP 503"),
])
def test_record_server_errors(kodi, fake_server, handler, guide_clock, knob, want):
    import xbmc
    import xbmcgui

    li = _free_item(kodi, guide_clock)
    n = len(handler.reserves_state)
    if knob == "conflict":
        handler.add_conflict = True
    else:
        handler.write_error = knob
    xbmcgui.yesno_answers.append(True)
    run_plugin(_record_url(li))
    assert [w[:2] for w in handler.writes] == [("POST", "/api/reserves")]
    assert xbmcgui.notifications[-1][1] == want and xbmcgui.notifications[-1][2] != xbmcgui.NOTIFICATION_INFO
    assert xbmc.builtins == ["Container.Refresh"] and len(handler.reserves_state) == n
    assert any("reserve: add program" in m and "failed" in m for _, m in xbmc.logs)


def test_record_no_answer(kodi, fake_server, handler, guide_clock, monkeypatch):
    import xbmc
    import xbmcgui
    from resources.lib.epgstation import api

    li = _free_item(kodi, guide_clock)

    def timeout(self, program_id, encode_mode=None):
        raise api.ApiError("/reserves: timed out")

    monkeypatch.setattr(api.EPGStation, "create_reserve", timeout)
    xbmcgui.yesno_answers.append(True)
    run_plugin(_record_url(li))
    assert xbmcgui.notifications[-1][1] == "Failed (connection error)" and xbmc.builtins == ["Container.Refresh"]
    assert any("may still have applied" in m for _, m in xbmc.logs)


def test_record_unreachable_server(kodi, fake_server, handler, guide_clock):
    """AC 10: a dead server -> short 30059 toast, no dialog, nothing written."""
    import xbmcaddon
    import xbmcgui

    li = _free_item(kodi, guide_clock)
    xbmcaddon.settings["server_url"] = "http://127.0.0.1:1"
    xbmcgui.yesno_answers.append(True)
    run_plugin(_record_url(li))
    assert not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "Failed (connection error)" and kodi.ended is None


def test_record_bad_params(kodi, fake_server, handler, guide_clock):
    import xbmcgui

    run_plugin("action=record&programId=x&channelId=1&name=x")
    assert handler.writes == [] and not xbmcgui.yesnos
    assert xbmcgui.notifications[-1][1] == "Invalid link" and kodi.ended is None


def test_record_with_encode_setting(kodi, fake_server, handler, guide_clock):
    import xbmcaddon
    import xbmcgui

    li = _free_item(kodi, guide_clock)
    xbmcaddon.settings["record_encode"] = "1"
    mode = _get(fake_server, "/api/config")["encode"][0]
    xbmcgui.yesno_answers.append(True)
    run_plugin(_record_url(li))
    assert xbmcgui.yesnos[-1]["message"].split("\n")[-1] == f"Encode: {mode}"
    body = json.loads(handler.writes[0][2])
    assert body["encodeOption"] == {"mode1": mode, "isDeleteOriginalAfterEncode": False}
    new = next(r for r in handler.reserves_state if r.get("programId") == body["programId"])
    assert new["encodeOption"] == body["encodeOption"]


def test_record_encode_setting_without_server_modes(kodi, fake_server, handler, guide_clock):
    import xbmcaddon
    import xbmcgui

    li = _free_item(kodi, guide_clock)
    xbmcaddon.settings["record_encode"] = "1"
    handler.config_patch = {"encode": []}
    xbmcgui.yesno_answers.append(True)
    run_plugin(_record_url(li))
    assert "Encode" not in xbmcgui.yesnos[-1]["message"]
    assert "encodeOption" not in json.loads(handler.writes[0][2])


def test_record_default_encode_setting_never_reads_config(kodi, fake_server, handler, guide_clock):
    import fake_epgstation
    import xbmcgui

    li = _free_item(kodi, guide_clock)
    fake_epgstation.REQUESTS[fake_server].clear()
    xbmcgui.yesno_answers.append(False)
    run_plugin(_record_url(li))
    assert _requests(fake_server, "/api/config") == []


def test_record_airing_program(kodi, fake_server, handler, guide_clock):
    import xbmcgui

    run_plugin("action=guide_now&t=now")
    li = next((li for _, li, _ in kodi.items if _ctx(li) == ["Schedule", "Record"]), None)
    if li is None:
        pytest.skip("every program on air is reserved")
    xbmcgui.yesno_answers.append(True)
    run_plugin(_record_url(li))
    assert xbmcgui.yesnos[-1]["message"].split("\n")[-1] == "On air: recording starts now, partway in"
    assert [w[:2] for w in handler.writes] == [("POST", "/api/reserves")]
    assert xbmcgui.notifications[-1][1] == "Recording started"
    pid = li.props["epgstation.programId"]
    assert any(str(r.get("programId")) == pid for r in handler.recording_state)
    kodi.reset()
    run_plugin("action=guide_now&t=now")
    after = next(x for _, x, _ in kodi.items if x.props["epgstation.programId"] == pid)
    assert after.label.startswith("[COLOR yellow]▶[/COLOR] [COLOR red]●[/COLOR] ")
    assert _ctx(after) == ["Schedule", "Stop recording"]


def test_api_post_and_create_reserve(fake_server, handler, monkeypatch, guide_clock):
    import urllib.request as ur

    from resources.lib.epgstation import api

    handler.now = guide_clock
    sched = _get(fake_server, f"/api/schedules/3211240960?startAt={guide_clock + 86400000}&days=1")
    p = sched[0]["programs"][0]
    sent = []
    real = ur.urlopen

    def spy(req, timeout=None):
        sent.append((req.get_method(), req.full_url, req.get_header("Authorization"),
                     req.get_header("Content-type"), req.data))
        return real(req, timeout=timeout)

    monkeypatch.setattr(ur, "urlopen", spy)
    client = api.EPGStation(fake_server, "user", "pw")
    rid = client.create_reserve(p["id"], "H.264")
    method, url, auth, ctype, data = sent[-1]
    assert (method, url, ctype) == ("POST", f"{fake_server}/api/reserves", "application/json")
    assert auth.startswith("Basic ")
    assert json.loads(data) == {"programId": p["id"], "allowEndLack": True,
                                "encodeOption": {"mode1": "H.264", "isDeleteOriginalAfterEncode": False}}
    assert isinstance(rid, int) and any(r["id"] == rid for r in handler.reserves_state)
    with pytest.raises(api.ApiError) as e:
        client.create_reserve(p["id"])
    assert e.value.status == 500 and e.value.detail == "ReservationManageModelCheckReservedProgramError"
    assert json.loads(sent[-1][4]) == {"programId": p["id"], "allowEndLack": True}
    with pytest.raises(api.ApiError) as e:
        client.create_reserve(1)
    assert e.value.detail == "ProgramIsNotFound"


def test_fake_server_post_reserves(fake_server, handler, guide_clock):
    handler.now = guide_clock

    def post(body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(f"{fake_server}/api/reserves", data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    sched = _get(fake_server, f"/api/schedules/3211240960?startAt={guide_clock + 86400000}&days=1")
    p = sched[0]["programs"][0]
    assert post(b"{}")[0] == 400 and post({"programId": p["id"]})[0] == 400
    assert post({"programId": str(p["id"]), "allowEndLack": True})[0] == 400
    handler.add_conflict = True
    status, err = post({"programId": p["id"], "allowEndLack": True})
    assert status == 500 and err["errors"] == "ReservationManageModelAddReserveConflict"
    handler.add_conflict = False
    status, out = post({"programId": p["id"], "allowEndLack": True})
    assert status == 201 and set(out) == {"reserveId"}
    r = _get(fake_server, f"/api/reserves/{out['reserveId']}?isHalfWidth=false")
    assert r["programId"] == p["id"] and r["channelId"] == 3211240960 and "ruleId" not in r
    assert (r["startAt"], r["endAt"], r["name"]) == (p["startAt"], p["endAt"], p["name"])
    assert r["isConflict"] is r["isSkip"] is r["isOverlap"] is False and r["allowEndLack"] is True
    status, err = post({"programId": p["id"], "allowEndLack": True})
    assert status == 500 and err["errors"] == "ReservationManageModelCheckReservedProgramError"
    status, err = post({"programId": 1, "allowEndLack": True})
    assert status == 500 and err["errors"] == "ProgramIsNotFound"
    lists = _get(fake_server, f"/api/reserves/lists?startAt={p['startAt']}&endAt={p['endAt']}")
    assert {"reserveId": out["reserveId"], "programId": p["id"]} in lists["normal"]
    assert [w[0] for w in handler.writes] == ["POST"] * 7
    handler.reset_reserves()
    assert handler.add_conflict is False and handler.writes == []


def test_record_ipc_timeout_counts_as_no_answer(kodi, fake_server, handler, guide_clock):
    """L1: 500 IPCTimeout -> the short connection toast and the "may still have applied" log line."""
    import xbmc
    import xbmcgui

    li = _free_item(kodi, guide_clock)
    handler.write_error = (500, {"code": 500, "message": "Internal Server Error", "errors": "IPCTimeout"})
    xbmcgui.yesno_answers.append(True)
    run_plugin(_record_url(li))
    assert [w[:2] for w in handler.writes] == [("POST", "/api/reserves")]
    assert xbmcgui.notifications[-1][1] == "Failed (connection error)" and xbmc.builtins == ["Container.Refresh"]
    assert any("IPCTimeout" in m and "may still have applied" in m for _, m in xbmc.logs)


@pytest.mark.parametrize("answer", [None, {}, {"reserveId": "x"}, [], {"reserves": []}])
def test_create_reserve_unexpected_answer_is_no_answer(answer, monkeypatch):
    """L2: a redirected or odd 2xx answer: ApiError without a status (never "HTTP 201"), short detail."""
    from resources.lib.epgstation import api

    monkeypatch.setattr(api.EPGStation, "post", lambda self, path, body, timeout=None: answer)
    with pytest.raises(api.ApiError) as e:
        api.EPGStation("http://x").create_reserve(1)
    assert e.value.status is None and e.value.detail == "unexpected answer"


def test_record_unexpected_answer_toast(kodi, fake_server, handler, guide_clock, monkeypatch):
    import xbmc
    import xbmcgui
    from resources.lib.epgstation import api

    li = _free_item(kodi, guide_clock)
    monkeypatch.setattr(api.EPGStation, "post", lambda self, path, body, timeout=None: {})
    xbmcgui.yesno_answers.append(True)
    run_plugin(_record_url(li))
    assert xbmcgui.notifications[-1][1] == "Failed (connection error)"
    assert "201" not in xbmcgui.notifications[-1][1]
    assert any("unexpected answer" in m and "may still have applied" in m for _, m in xbmc.logs)


MALFORMED_LISTS = [
    {"normal": {"reserveId": 1, "programId": 1}, "conflicts": [], "skips": [], "overlaps": []},  # openapi's shape
    {"normal": [{"programId": 1}], "conflicts": [], "skips": [], "overlaps": []},  # no reserveId
    {"normal": [1, 2]},
    ["normal"],
]


@pytest.mark.parametrize("answer", MALFORMED_LISTS)
def test_guide_malformed_lists_answer_degrades(kodi, fake_server, handler, guide_clock, monkeypatch, answer):
    """L4: an object-shaped or item-missing-reserveId lists answer -> no markers and no entries, never a crash."""
    import xbmc
    from resources.lib.epgstation import api

    monkeypatch.setattr(api.EPGStation, "reserve_lists", lambda self, s, e, timeout=None: answer)
    lis = _tomorrow_items(kodi, guide_clock)
    assert kodi.ended is True and lis
    assert all(li.context == [] and not li.label.startswith(("[COLOR red]", "[COLOR grey]")) for li in lis)
    assert any("reserve lists" in m for lvl, m in xbmc.logs if lvl == xbmc.LOGWARNING)
    kodi.reset()
    run_plugin("action=live&type=GR")
    assert kodi.ended is True and all(_ctx(li) == ["Schedule"] for _, li, _ in kodi.items)


@pytest.mark.parametrize("answer", MALFORMED_LISTS)
def test_record_malformed_lists_answer_writes_nothing(kodi, fake_server, handler, guide_clock, monkeypatch, answer):
    import xbmcgui
    from resources.lib.epgstation import api

    li = _free_item(kodi, guide_clock)
    monkeypatch.setattr(api.EPGStation, "reserve_lists", lambda self, s, e, timeout=None: answer)
    xbmcgui.yesno_answers.append(True)
    run_plugin(_record_url(li))
    assert not xbmcgui.yesnos and handler.writes == []
    assert xbmcgui.notifications[-1][1] == "Failed: unexpected answer" and kodi.ended is None


def _start_during_dialog(monkeypatch, p):
    from resources.lib.epgstation import plugin

    def hook():
        if len(xbmcgui.yesnos) == 1:  # only while the first dialog is open
            monkeypatch.setattr(plugin, "_now_ms", lambda: p["startAt"] + 1000)
    return hook


@pytest.mark.parametrize("second,writes,toast", [(False, 0, None), (True, 1, "Recording started")])
def test_record_started_during_the_dialog_asks_again(kodi, fake_server, handler, guide_clock, monkeypatch,
                                                     second, writes, toast):
    """L5: not on air when the dialog opened, on air after the yes: ask once more with the on-air warning."""
    from resources.lib.epgstation import reserves

    monkeypatch.setattr(reserves, "RECORD_WAIT", 0.2)  # only the plugin's clock moves: the fake records nothing
    li = _free_item(kodi, guide_clock)
    p = _detail(fake_server, int(li.props["epgstation.programId"]))
    xbmcgui.yesno_answers.extend([True, second])
    xbmcgui.yesno_hook = _start_during_dialog(monkeypatch, p)
    run_plugin(_record_url(li))
    assert len(xbmcgui.yesnos) == 2
    first, again = xbmcgui.yesnos
    assert "On air" not in first["message"]
    assert again["message"].split("\n")[-1] == "On air: recording starts now, partway in"
    assert again["defaultbutton"] == xbmcgui.DLG_YESNO_NO_BTN and again["heading"] == "Record"
    assert len(handler.writes) == writes
    if toast:
        assert xbmcgui.notifications[-1][1] == toast
    else:
        assert not xbmcgui.notifications


def test_record_airing_from_the_start_asks_once(kodi, fake_server, handler, guide_clock):
    run_plugin("action=guide_now&t=now")
    li = next((li for _, li, _ in kodi.items if _ctx(li) == ["Schedule", "Record"]), None)
    if li is None:
        pytest.skip("every program on air is reserved")
    kodi.reset()
    xbmcgui.yesno_answers.append(True)
    run_plugin(_record_url(li))
    assert len(xbmcgui.yesnos) == 1 and len(handler.writes) == 1


def test_guide_read_actions_never_write(kodi, fake_server, handler, guide_clock):
    """L6: every guide listing (with 014's markers and entries built), a 番組表 day list and guide_info under the
    fixtures' clock: no write, no dialog, even with yes answers queued."""
    from resources.lib.epgstation import guide

    tomorrow = guide.day_start(guide_clock, 1)
    day = f"action=guide_channel&channelId=3211240960&name=NHK総合1・大阪&day={tomorrow}"
    entries = []
    for q in GUIDE_READ_ACTIONS + [day]:
        kodi.reset()
        xbmcgui.yesno_answers[:] = [True] * 3
        run_plugin(q)
        assert kodi.ended is True, q
        entries += [c[0] for _, li, _ in kodi.items for c in li.context]
    assert "Record" in entries  # 014's entry building ran
    future = next(u for u, li, _ in kodi.items if "action=guide_info" in u)
    xbmcgui.yesno_answers[:] = [True] * 3
    run_plugin(future.split("?", 1)[1])  # guide_info
    assert xbmcgui.info_shown
    assert handler.writes == [] and not xbmcgui.yesnos


def test_api_sends_the_addon_user_agent(fake_server, monkeypatch):
    """M1: the smoke tells the addon's requests apart in EPGStation's access log by this User-Agent."""
    import urllib.request as ur

    from resources.lib.epgstation import api

    seen = []
    real = ur.urlopen
    monkeypatch.setattr(ur, "urlopen", lambda req, timeout=None: seen.append(req.get_header("User-agent")) or
                        real(req, timeout=timeout))
    client = api.EPGStation(fake_server)
    client.version()
    client.live_probe(3211240960, 0, timeout=5)
    assert seen and all(ua == api.USER_AGENT for ua in seen)
    assert api.USER_AGENT.startswith("KODIstation/")


def _on_air_free(kodi):
    run_plugin("action=guide_now&t=now")
    li = next((li for _, li, _ in kodi.items if _ctx(li) == ["Schedule", "Record"]), None)
    if li is None:
        pytest.skip("every program on air is reserved")
    kodi.reset()
    return li


def test_record_on_air_waits_for_the_recording_then_refreshes_again(kodi, fake_server, handler, guide_clock):
    """Tester GUI issue 1: EPGStation starts the recording a moment after the 201; refresh again once /recording
    lists it, so the item shows ● and 録画を停止 instead of ○ and 予約を削除."""
    import fake_epgstation

    li = _on_air_free(kodi)
    handler.recording_start_delay = 0.5
    xbmc.wait_hook = lambda waited: time.sleep(0.1)  # the stub's waitForAbort does not sleep
    fake_epgstation.REQUESTS[fake_server].clear()
    xbmcgui.yesno_answers.append(True)
    t = time.monotonic()
    run_plugin(_record_url(li))
    elapsed = time.monotonic() - t
    assert xbmcgui.notifications[-1][1] == "Recording started"
    assert xbmc.builtins == ["Container.Refresh", "Container.Refresh"]
    assert len(_requests(fake_server, "/api/recording")) >= 2 and 0.4 <= elapsed < 3, elapsed
    kodi.reset()
    run_plugin("action=guide_now&t=now")
    after = next(x for _, x, _ in kodi.items if x.props["epgstation.programId"] == li.props["epgstation.programId"])
    assert after.label.startswith("[COLOR yellow]▶[/COLOR] [COLOR red]●[/COLOR] ")


def test_record_on_air_wait_is_bounded(kodi, fake_server, handler, guide_clock, monkeypatch):
    from resources.lib.epgstation import reserves

    li = _on_air_free(kodi)
    monkeypatch.setattr(reserves, "RECORD_WAIT", 1.0)
    handler.recording_start_delay = 60  # never within the wait
    xbmc.wait_hook = lambda waited: time.sleep(0.1)
    xbmcgui.yesno_answers.append(True)
    t = time.monotonic()
    run_plugin(_record_url(li))
    elapsed = time.monotonic() - t
    assert 0.9 <= elapsed < 1.6, elapsed
    assert xbmc.builtins == ["Container.Refresh"] and len(handler.writes) == 1


def test_record_future_program_does_not_wait(kodi, fake_server, handler, guide_clock):
    import fake_epgstation

    li = _free_item(kodi, guide_clock)
    fake_epgstation.REQUESTS[fake_server].clear()
    xbmcgui.yesno_answers.append(True)
    run_plugin(_record_url(li))
    assert _requests(fake_server, "/api/recording") == [] and xbmc.builtins == ["Container.Refresh"]


# --- artwork (003) -----------------------------------------------------------------------------------------------

def _thumb_requests(fake_server, since):
    import fake_epgstation

    return [p for p, _ in fake_epgstation.REQUESTS[fake_server][since:] if p.startswith("/api/thumbnails/")]


def _req_count(fake_server):
    import fake_epgstation

    return len(fake_epgstation.REQUESTS[fake_server])


def _fixture_thumbs(fake_server):
    import fake_epgstation

    d = os.path.join(fake_epgstation.HANDLERS[fake_server].root, "api", "thumbnails")
    return {int(n[:-4]) for n in os.listdir(d) if n.endswith(".jpg")}


def _logos(fake_server):
    return {c["id"] for c in _get(fake_server, "/api/channels") if c.get("hasLogoData")}


def _expected_art(fake_server, rec, logos, blank):
    from resources.lib.epgstation import items
    from resources.lib.epgstation.api import EPGStation

    return items.recording_art(items.newest_thumb(rec), rec["channelId"], EPGStation(fake_server), logos, blank,
                               xbmcaddon.ICON)


def test_root_and_browse_menu_icons(kodi):
    run_plugin()
    want = ["DefaultMovies.png", "DefaultTVShows.png", "DefaultRecentlyAddedEpisodes.png", "DefaultPVRRecordings.png",
            "DefaultPVRTimers.png", "DefaultPVRChannels.png", "DefaultAddonsSearch.png", "DefaultFolder.png"]
    assert [li.art for _, li, _ in kodi.items] == [{"icon": w, "thumb": w} for w in want]
    kodi.reset()
    run_plugin("action=browse")
    assert [li.art["icon"] for _, li, _ in kodi.items] == ["DefaultStudios.png", "DefaultGenre.png",
                                                          "DefaultPVRTimerRules.png"]


def test_recent_list_art_blank_detection_and_cache(kodi, fake_server):
    """dev: thumbnail 22 (recording 21, ジョン・ウィック) is a real 986 B black frame -> logo art; captured thumbnails
    -> thumbnail art; thumbnails that 404 keep the thumbnail URL and are negative-cached; a second open: no requests."""
    xbmcaddon.settings["page_size"] = "500"
    records = _all_records(fake_server)
    logos, captured = _logos(fake_server), _fixture_thumbs(fake_server)
    n = _req_count(fake_server)
    run_plugin("action=list")
    shown = [li for _, li, folder in kodi.items if not folder]
    from resources.lib.epgstation import items

    wanted = {items.newest_thumb(records[int(li.props["epgstation.recordedId"])]) for li in shown} - {None}
    fetched = _thumb_requests(fake_server, n)
    assert sorted(fetched) == sorted(f"/api/thumbnails/{t}" for t in wanted)  # one GET per shown thumbnail
    blank = {22} if 22 in captured and os.path.basename(_fixture_root(fake_server)) == "dev" else set()
    for li in shown:
        rec = records[int(li.props["epgstation.recordedId"])]
        assert li.art == _expected_art(fake_server, rec, logos, blank), rec["id"]
        assert "fanart" not in li.art or "/logo" not in li.art["fanart"]
    if blank:
        john = next(li for li in shown if li.props["epgstation.recordedId"] == "21")
        logo = f"{fake_server}/api/channels/{records[21]['channelId']}/logo"
        assert john.art == {"thumb": logo, "icon": logo}
    cache = json.load(open(os.path.join(xbmc_profile(), "thumbs.json"), encoding="utf-8"))
    assert {int(k) for k, v in cache["d"].items() if isinstance(v, list)} == wanted & captured
    assert {int(k) for k, v in cache["d"].items() if isinstance(v, dict)} == wanted - captured  # L2: 404 -> {"t"}
    # second open: nothing is asked again (the 404 ones not before NEGATIVE_TTL)
    n = _req_count(fake_server)
    kodi.reset()
    run_plugin("action=list")
    assert _thumb_requests(fake_server, n) == []
    assert [li.art for _, li, f in kodi.items if not f] == [li.art for li in shown]


def _fixture_root(fake_server):
    import fake_epgstation

    return fake_epgstation.HANDLERS[fake_server].root


def xbmc_profile():
    import xbmcvfs

    return os.path.join(xbmcvfs.profile_dir, "addon_data", "plugin.video.epgstation")


def test_warm_lists_make_no_thumbnail_requests(kodi, fake_server, handler):
    """Every thumbnail captured: after the first open of 映画, TV番組 and 最近の録画, the second makes none."""
    captured = _fixture_thumbs(fake_server)
    handler.mutate = lambda recs: [dict(r, thumbnails=[t for t in r.get("thumbnails") or [] if t in captured])
                                   for r in recs]
    for action in ("movies", "tvshows", "list"):
        kodi.reset()
        run_plugin(f"action={action}")
    n = _req_count(fake_server)
    for action in ("movies", "tvshows", "list"):
        kodi.reset()
        run_plugin(f"action={action}")
        assert kodi.ended is True
    assert _thumb_requests(fake_server, n) == []


def test_no_logo_channel_never_gets_a_logo_url(kodi, fake_server, handler):
    """A recording moved to a fixture channel without hasLogoData: no logo URL in any list, the addon icon when it
    has no thumbnail."""
    channels = _get(fake_server, "/api/channels")
    no_logo = next(c["id"] for c in channels if not c.get("hasLogoData"))
    newest = _get(fake_server, "/api/recorded?limit=1")["records"][0]
    handler.mutate = lambda recs: [dict(r, channelId=no_logo, thumbnails=[]) if r["id"] == newest["id"] else r
                                   for r in recs]
    xbmcaddon.settings["page_size"] = "500"
    for action in ("list", "movies", "tvshows", "channels"):
        kodi.reset()
        run_plugin(f"action={action}")
        assert kodi.ended is True
        for _, li, _ in kodi.items:
            assert f"/channels/{no_logo}/logo" not in json.dumps(li.art), (action, li.label)
            for url in li.art.values():
                if "/logo" in url:
                    cid = int(url.split("/channels/", 1)[1].split("/", 1)[0])
                    assert cid in _logos(fake_server)
    kodi.reset()
    run_plugin("action=list")
    li = next(li for _, li, f in kodi.items if not f and li.props["epgstation.recordedId"] == str(newest["id"]))
    assert li.art == {"thumb": xbmcaddon.ICON, "icon": xbmcaddon.ICON}


def test_channel_folders_carry_logos(kodi, fake_server):
    logos = _logos(fake_server)
    run_plugin("action=channels")
    assert kodi.items
    for url, li, _ in kodi.items:
        cid = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["channelId"][0])
        want = f"{fake_server}/api/channels/{cid}/logo" if cid in logos else "DefaultStudios.png"
        assert li.art == {"icon": want, "thumb": want}


def test_tvshows_skip_blank_thumbnails(kodi, fake_server, handler):
    """A show whose newest episode's newest thumbnail is blank (the black 22.jpg) uses the next episode's thumbnail,
    and with none left the channel logo (no poster / fanart)."""
    if 22 not in _fixture_thumbs(fake_server) or os.path.basename(_fixture_root(fake_server)) != "dev":
        pytest.skip("dev fixtures only (22.jpg is the black one)")
    logos = _logos(fake_server)
    run_plugin("action=tvshows")
    for url, _, _ in list(kodi.items):
        key = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["key"][0]
        kodi.reset()
        run_plugin("action=show&key=" + urllib.parse.quote(key))
        eps = [int(li.props["epgstation.recordedId"]) for _, li, f in kodi.items if not f]
        newest = eps[0]
        handler.mutate = lambda recs, newest=newest: [dict(r, thumbnails=[1, 22]) if r["id"] == newest else r
                                                      for r in recs]
        records = _all_records(fake_server)
        kodi.reset()
        run_plugin("action=tvshows")
        li = next(li for u, li, _ in kodi.items if u == url)
        others = [max(records[i]["thumbnails"]) for i in eps[1:] if records[i].get("thumbnails")]
        if others:
            t = f"{fake_server}/api/thumbnails/{others[0]}"
            assert li.art["thumb"] == li.art["poster"] == li.art["fanart"] == li.art["landscape"] == t
        else:
            cid = records[newest]["channelId"]
            want = f"{fake_server}/api/channels/{cid}/logo" if cid in logos else xbmcaddon.ICON
            assert li.art == {"thumb": want, "icon": want}
        handler.mutate = None
        return  # one show is enough (each run re-lists every show)
    pytest.skip("no shows")


def test_play_makes_no_thumbnail_request(kodi, fake_server):
    run_plugin("action=list")
    rec_id = kodi.items[0][1].props["epgstation.recordedId"]
    n = _req_count(fake_server)
    kodi.reset()
    run_plugin(f"action=play&id={rec_id}")
    ok, li = kodi.resolved
    assert ok and _thumb_requests(fake_server, n) == []
    assert li.art.get("icon")


def test_day_folders_and_guide_menu_icons(kodi, fake_server, handler):
    run_plugin("action=live")
    arts = {li.label: li.art for _, li, _ in kodi.items}
    assert any(a == {"icon": "DefaultPVRGuide.png", "thumb": "DefaultPVRGuide.png"} for a in arts.values())
    assert any(a == {"icon": "DefaultGenre.png", "thumb": "DefaultGenre.png"} for a in arts.values())


def test_tvshows_advance_past_three_blank_episodes(kodi, fake_server, handler, monkeypatch):
    """Review M1: a show whose 3 newest episodes have blank thumbnails (distinct ids) and a good 4th: cold and warm
    opens both show the 4th; with all 4 blank the folder shows the logo."""
    from resources.lib.epgstation import api as api_mod

    black = open(os.path.join(FIXTURES, "dev", "api", "thumbnails", "22.jpg"), "rb").read()
    good = open(os.path.join(FIXTURES, "dev", "api", "thumbnails", "11.jpg"), "rb").read()
    real = api_mod.EPGStation.thumbnail

    def thumbnail(self, tid, timeout=None, limit=None):
        if int(tid) >= 900000:
            return good if int(tid) == 900010 else black
        return real(self, tid, timeout, limit)

    monkeypatch.setattr(api_mod.EPGStation, "thumbnail", thumbnail)
    from resources.lib.epgstation import epgmeta

    newest = next(r for r in _get(fake_server, "/api/recorded?limit=1000")["records"]
                  if (epgmeta.metadata(r) or {}).get("kind") != "movie")
    handler.multiply = 4  # copies: same name (one show), one week apart
    logos = _logos(fake_server)

    def folders(last):
        handler.mutate = lambda recs: [dict(r, thumbnails=[900000 + (r["id"] // 100000 if r["id"] // 100000 < 3
                                                                    else last)])
                                       if r["id"] % 100000 == newest["id"] else r for r in recs]
        for name in ("library-index.json", "library-records.json"):  # the probe signature does not see the change
            path = os.path.join(xbmc_profile(), name)
            if os.path.exists(path):
                os.remove(path)
        kodi.reset()
        run_plugin("action=tvshows")
        return {url: li.art for url, li, _ in kodi.items}

    good_url = f"{fake_server}/api/thumbnails/900010"
    url = next(u for u, a in folders(10).items() if a.get("thumb") == good_url)  # cold
    assert folders(10)[url]["poster"] == good_url  # warm
    cid = newest["channelId"]
    want = f"{fake_server}/api/channels/{cid}/logo" if cid in logos else xbmcaddon.ICON
    assert folders(3)[url] == {"thumb": want, "icon": want}  # all four blank (900000..900003)


def test_run_joins_worker_pools_after_the_directory(kodi, monkeypatch):
    """Review L5: Plugin.run waits for every worker pool after the handler (endOfDirectory / setResolvedUrl done), also
    when the handler fails, so no worker thread outlives the plugin run into Kodi's interpreter teardown."""
    import xbmcplugin
    from resources.lib.epgstation import api as api_mod

    order = []
    real_end = xbmcplugin.endOfDirectory
    monkeypatch.setattr(xbmcplugin, "endOfDirectory", lambda *a, **kw: (order.append("end"), real_end(*a, **kw)))
    monkeypatch.setattr(api_mod, "join_workers", lambda: order.append("join"))
    run_plugin("action=list")
    assert order == ["end", "join"]
    order.clear()
    xbmcaddon.settings["server_url"] = "http://127.0.0.1:9"
    run_plugin("action=list")  # ApiError path
    assert order[-1] == "join"


@pytest.mark.parametrize("action", ["list", "tvshows"])
def test_thumbnail_check_failure_never_skips_the_directory(kodi, monkeypatch, action):
    """Review L1: an unexpected error in the blank check (e.g. RuntimeError from the thread pool) is logged and the list
    is shown without blank detection."""
    from resources.lib.epgstation import thumbs

    def boom(*a, **kw):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(thumbs, "check", boom)
    monkeypatch.setattr(thumbs, "settle", boom)
    run_plugin(f"action={action}")
    assert kodi.ended is True and kodi.items
    assert any("thumbnail check failed" in str(entry) for entry in xbmc.logs)


# --- full detail view (002) ------------------------------------------------------------------------------------------

def _views_without_droplog_requests(kodi, fake_server):
    import fake_epgstation

    log = fake_epgstation.REQUESTS[fake_server]
    n = len(log)
    for query in ("action=list", "action=movies", "action=tvshows"):
        kodi.reset()
        run_plugin(query)
        assert kodi.ended is True
    return [p for p, _ in log[n:]]


def test_list_has_audio_and_aspect_and_no_droplog_request(kodi, fake_server):
    xbmcaddon.settings["page_size"] = "500"
    paths = _views_without_droplog_requests(kodi, fake_server)
    assert not any(p.startswith("/api/dropLogs") for p in paths)
    kodi.reset()
    run_plugin("action=list")
    recs = {r["id"]: r for r in _get(fake_server, "/api/recorded?limit=500")["records"]}
    checked = 0
    for _, li, folder in kodi.items:
        rec = recs.get(int(li.props.get("epgstation.recordedId", 0)))
        if folder or rec is None:
            continue
        audio = [(a.channels, a.codec) for a in li.tag.data["audio_streams"]]
        want = {1: [(1, "aac")], 3: [(2, "aac")], 9: [(6, "aac")], None: []}[rec.get("audioComponentType")]
        assert audio == want, rec["id"]
        if rec.get("videoComponentType") == 179:
            assert li.tag.data["streams"][0].aspect == 1.78
        assert li.tag.data["plot"].split("\n\n")[-1].startswith("[Recording]\nBroadcast: ")
        checked += 1
    assert checked


def test_droplog_opens_text_viewer_summary_first(kodi, fake_server):
    import fake_epgstation

    if not _has_dev2_droplog(fake_server):
        pytest.skip("dev2 fixtures only (dev has no drop logs)")
    log = fake_epgstation.REQUESTS[fake_server]
    n = len(log)
    run_plugin("action=droplog&id=125&log=2")
    assert [p for p, _ in log[n:]] == ["/api/dropLogs/2"] and log[n][1]["maxsize"] == ["512"]
    (heading, text, mono), = xbmcgui.textviewers
    assert heading == "Drop log" and mono is True
    lines = text.split("\n")
    assert lines[0] == "pid: 0x0000, error: 0, drop: 0, scrambling: 0, packet: 34255, name: PAT"
    assert lines[lines.index("") + 1].startswith("error: (pid: 0x16FC")
    assert kodi.items == [] and not xbmcgui.notifications


def _has_dev2_droplog(fake_server):
    """The dev2 set: recording 125 (errors only) with dropLogFile 2 and its captured log (1920 bytes)."""
    try:
        rec = _get(fake_server, "/api/recorded/125")
    except urllib.error.HTTPError:
        return False
    return (rec.get("dropLogFile") or {}).get("id") == 2 and os.path.isfile(
        os.path.join(FIXTURES, "dev2", "api", "dropLogs", "2.txt"))


def test_droplog_not_found_and_too_large_toast(kodi, fake_server, monkeypatch):
    from resources.lib.epgstation import plugin

    run_plugin("action=droplog&id=1&log=999999")  # not captured: 404
    assert xbmcgui.textviewers == [] and xbmcgui.notifications[-1][1] == "No drop log"
    if _has_dev2_droplog(fake_server):
        monkeypatch.setattr(plugin, "DROPLOG_MAX_KB", 1)  # 2.txt is 1.9 KB: 416
        run_plugin("action=droplog&id=125&log=2")
        assert xbmcgui.textviewers == [] and xbmcgui.notifications[-1][1] == "Drop log too large"
    run_plugin("action=droplog&id=1&log=x")  # bad parameter: the usual toast, no request
    assert xbmcgui.textviewers == [] and kodi.items == []


def test_droplog_empty_log_toast(kodi, fake_server, handler):
    """An empty 200 drop log: the 'No drop log' toast, no text viewer, no connection-error text (one GET)."""
    import fake_epgstation

    handler.droplog_body = b""
    log = fake_epgstation.REQUESTS[fake_server]
    n = len(log)
    run_plugin("action=droplog&id=1&log=7")
    assert [p for p, _ in log[n:]] == ["/api/dropLogs/7"]
    assert xbmcgui.textviewers == [] and xbmcgui.notifications[-1][1] == "No drop log"
    handler.droplog_body = b"\n\n"
    run_plugin("action=droplog&id=1&log=7")
    assert xbmcgui.textviewers == [] and xbmcgui.notifications[-1][1] == "No drop log"


# --- backlog 009: subgenres ---------------------------------------------------------------------------------------

def _slot_pairs(rec):
    return {(rec.get("genre" + n), rec.get("subGenre" + n)) for n in "123" if rec.get("genre" + n) is not None}


def _expected_genres(records):
    """{genre: {recording ids}}, {(genre, subGenre): {ids}} over genre1-3, computed independently of items.py."""
    by_genre, by_pair = {}, {}
    for r in records.values():
        for g, s in _slot_pairs(r):
            by_genre.setdefault(g, set()).add(r["id"])
            by_pair.setdefault((g, s), set()).add(r["id"])
    return by_genre, by_pair


def _query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_genres_count_genre1_to_3(kodi, fake_server, lang):
    from resources.lib.epgstation.genres import GENRE_EN, GENRE_JA

    xbmcaddon.settings["genre_language"] = "0" if lang == "ja" else "1"
    by_genre, by_pair = _expected_genres(_all_records(fake_server))
    names = GENRE_JA if lang == "ja" else GENRE_EN
    run_plugin("action=genres")
    assert [li.label for _, li, _ in kodi.items] == [f"{names[g]} ({len(by_genre[g])})" for g in sorted(by_genre)]
    options = {g["genre"] for g in _get(fake_server, "/api/recorded/options")["genres"]}
    assert set(by_genre) - options  # /recorded/options (genre1 only) misses: dev 趣味・教育/福祉, dev2 also ニュース
    for url, _, folder in kodi.items:
        q = _query(url)
        g = int(q["genre"])
        subs = {s for (pg, s) in by_pair if pg == g and s is not None}
        assert folder and q["action"] == ("list" if len(subs) <= 1 else "genre")  # D3


def test_genre_folder_opens_what_it_counts(kodi, fake_server):
    """AC 2: a genre's すべて (N) entry lists N recordings (/recorded?genre=G matches genre1-3, plus in progress)."""
    by_genre, _ = _expected_genres(_all_records(fake_server))
    xbmcaddon.settings["page_size"] = "500"
    for g, ids in by_genre.items():
        kodi.reset()  # same stub state object as xbmcplugin.state
        run_plugin(f"action=list&genre={g}")
        got = {int(li.props["epgstation.recordedId"]) for _, li, f in kodi.items if not f}
        assert got == ids, g


def test_genre_subgenre_folders(kodi, fake_server):
    from resources.lib.epgstation.genres import GENRE_JA, SUBGENRE_JA

    by_genre, by_pair = _expected_genres(_all_records(fake_server))
    g = max(by_genre, key=lambda k: len({s for (pg, s) in by_pair if pg == k}))  # the genre with most subgenres
    run_plugin(f"action=genre&genre={g}")
    subs = sorted(s for (pg, s) in by_pair if pg == g and s is not None)
    # the stubs return the en_gb strings: "All (N)"
    want = [f"All ({len(by_genre[g])})"] + [f"{SUBGENRE_JA[g].get(s, f'#{s}')} ({len(by_pair[(g, s)])})"
                                           for s in subs]
    assert [li.label for _, li, _ in kodi.items] == want
    assert [_query(u) for u, _, _ in kodi.items] == [{"action": "list", "genre": str(g)}] + [
        {"action": "list", "genre": str(g), "subGenre": str(s)} for s in subs]
    assert all(f for *_, f in kodi.items) and kodi.category == GENRE_JA[g]


def test_genre_subgenre_folders_english(kodi, fake_server):
    from resources.lib.epgstation.genres import GENRE_EN, SUBGENRE_EN

    xbmcaddon.settings["genre_language"] = "1"
    by_genre, by_pair = _expected_genres(_all_records(fake_server))
    g = max(by_genre, key=lambda k: len({s for (pg, s) in by_pair if pg == k}))
    run_plugin(f"action=genre&genre={g}")
    labels = [li.label for _, li, _ in kodi.items]
    assert labels[0] == f"All ({len(by_genre[g])})"
    assert labels[1:] == [f"{SUBGENRE_EN[g][s]} ({len(by_pair[(g, s)])})"
                          for s in sorted(s for (pg, s) in by_pair if pg == g)]
    assert kodi.category == GENRE_EN[g]


def test_subgenre_list_from_the_library(kodi, fake_server):
    """AC 4: ?action=list&genre=G&subGenre=S lists the recordings with (G, S) in some slot, newest first, from the
    library snapshot (no /recorded?genre= request), header "映画 / 洋画"; items carry "genre / subgenre"."""
    import fake_epgstation
    from resources.lib.epgstation.genres import genre_name

    records = _all_records(fake_server)
    _, by_pair = _expected_genres(records)
    xbmcaddon.settings["page_size"] = "5"  # unpaged anyway
    for (g, s), ids in sorted(by_pair.items(), key=lambda kv: (kv[0][0], kv[0][1] if kv[0][1] is not None else -1)):
        if s is None:
            continue
        kodi.reset()  # same stub state object as xbmcplugin.state
        n = len(fake_epgstation.REQUESTS[fake_server])
        run_plugin(f"action=list&genre={g}&subGenre={s}")
        got = [int(li.props["epgstation.recordedId"]) for _, li, f in kodi.items if not f]
        assert got == sorted(ids, key=lambda i: (records[i]["startAt"], i), reverse=True), (g, s)
        assert all(not f for *_, f in kodi.items)  # no 次へ
        assert kodi.category == genre_name(g, s)
        assert not any(p == "/api/recorded" and "genre" in q for p, q in fake_epgstation.REQUESTS[fake_server][n:])
        lis = [li for _, li, f in kodi.items if not f]
        assert all(genre_name(g, s) in li.tag.data["genres"] for li in lis)


def test_subgenre_list_english_header_and_genres(kodi, fake_server):
    from resources.lib.epgstation.genres import genre_name

    xbmcaddon.settings["genre_language"] = "1"
    _, by_pair = _expected_genres(_all_records(fake_server))
    g, s = next(p for p in sorted(by_pair, key=str) if p[1] is not None)
    run_plugin(f"action=list&genre={g}&subGenre={s}")
    want = genre_name(g, s, "en")
    assert " / " in want and kodi.category == want
    assert all(want in li.tag.data["genres"] for _, li, f in kodi.items if not f)


def test_genre_views_multiply(kodi, fake_server, handler):
    """--multiply 3: the counts follow the copies (finished recordings x3, keeping their genres; live ones once)."""
    by_genre, by_pair = _expected_genres(_all_records(fake_server))
    handler.multiply = 3
    run_plugin("action=genres")
    counts = {int(_query(u)["genre"]): int(li.label.rsplit("(", 1)[1].rstrip(")")) for u, li, _ in kodi.items}
    recs = _all_records(fake_server)
    want = {}
    for r in recs.values():
        for g in {g for g, _ in _slot_pairs(r)}:
            want[g] = want.get(g, 0) + 1
    assert counts == want and any(counts[g] > len(by_genre[g]) for g in counts)
    g, s = max(by_pair, key=lambda p: len(by_pair[p]))
    kodi.reset()  # same stub state object as xbmcplugin.state
    run_plugin(f"action=list&genre={g}&subGenre={s}")
    assert len([1 for *_, f in kodi.items if not f]) == len([r for r in recs.values() if (g, s) in _slot_pairs(r)])


def test_old_genre_list_link_still_uses_the_api(kodi, fake_server):
    """AC 9: an old favourite ?action=list&genre=6 still lists through GET /recorded?genre=6 (paged)."""
    import fake_epgstation

    n = len(fake_epgstation.REQUESTS[fake_server])
    run_plugin("action=list&genre=6")
    assert any(p == "/api/recorded" and q.get("genre") == ["6"] for p, q in fake_epgstation.REQUESTS[fake_server][n:])
    assert kodi.category == "映画"


def test_second_genres_open_is_probes_only(kodi, fake_server):
    """AC 8: a warm library: ジャンル別 and a genre folder make only the probe requests."""
    import fake_epgstation

    run_plugin("action=genres")
    first = [li.label for _, li, _ in kodi.items]
    probes = [("/api/recorded", ["1"], None), ("/api/recorded", ["1"], ["true"]), ("/api/recording", ["100"], None)]
    for q in ("action=genres", "action=genre&genre=6"):
        n = len(fake_epgstation.REQUESTS[fake_server])
        kodi.reset()  # same stub state object as xbmcplugin.state
        run_plugin(q)
        seen = fake_epgstation.REQUESTS[fake_server][n:]
        assert [(p, q.get("limit"), q.get("isReverse")) for p, q in seen] == probes
    kodi.reset()
    run_plugin("action=genres")
    assert [li.label for _, li, _ in kodi.items] == first


@pytest.mark.parametrize("query", ["action=genre", "action=genre&genre=x", "action=list&genre=6&subGenre=x",
                                   "action=list&subGenre=0"])
def test_genre_views_bad_params(kodi, query):
    run_plugin(query)
    assert kodi.ended is False and xbmcgui.notifications


# --- transcoded playback (011) ----------------------------------------------------------------------------------

ISA_ON = "System.AddonIsEnabled(inputstream.adaptive)"
KEEP = ("PUT", "/api/streams/0/keep")
DELETE = ("DELETE", "/api/streams/0")


def _run_stream(rec_id, handle="-1"):
    """Like Kodi's RunPlugin of the context entry: no handle."""
    from resources.lib.epgstation.plugin import Plugin

    Plugin(["plugin://plugin.video.epgstation/", handle, f"?action=stream&id={rec_id}"]).run()


@pytest.fixture
def stream_rec(kodi, fake_server, handler, monkeypatch):
    """A finished recording with a file; the plugin clock and the fake server's 15 s stream timer (011 review L4d)
    follow the stub monitor's waits."""
    from resources.lib.epgstation import items, plugin

    monkeypatch.setattr(plugin, "_clock", lambda: xbmc.waited)
    handler.stream_timer, handler.stream_clock = 15, lambda: xbmc.waited
    rec = next(r for r in _get(fake_server, "/api/recorded?limit=50")["records"] if r.get("videoFiles"))
    rec = _get(fake_server, f"/api/recorded/{rec['id']}")
    video = items.pick_video(rec["videoFiles"], items.default_prefer(rec, "encoded"))
    return dict(rec, _video=video)


def _kodi_plays(start_at=1.0, stop_at=35.0, other=None, tag=False, stall_at=None):
    """Kodi starts the played item `start_at` s after play() and plays it (time from 0; frozen from `stall_at`: paused);
    at `stop_at` it stops it (onPlayBackStopped), or (other=URL) switches to another file. tag: Kodi reports the
    item's nonce property (Player.getPlayingItem, Kodi 20+). Returns the hook (to chain)."""
    state = {"t0": None, "done": False}

    def hook(waited):
        if not xbmc.played or state["done"]:
            return
        if state["t0"] is None:
            state["t0"] = waited
        t = waited - state["t0"]
        if t >= stop_at:
            state["done"] = True
            if other:
                xbmc.player_file, xbmc.player_tag = other, "" if tag else None
            else:
                xbmc.player_file = ""
                xbmc.players[-1].onPlayBackStopped()
        elif t >= start_at:
            url, li = xbmc.played[-1]
            xbmc.player_file = url
            xbmc.player_tag = li.props["epgstation.stream"] if tag else None
            xbmc.player_time = (min(t, stall_at) if stall_at is not None else t) - start_at
    xbmc.wait_hook = hook
    return hook


def _chain(first, second):
    def hook(waited):
        first(waited)
        second(waited)
    xbmc.wait_hook = hook


def _stream_writes(handler):
    return [(m, p) for m, p, _ in handler.writes]


def _toasts():
    return [n[1] for n in xbmcgui.notifications]


def _positions_file():
    import xbmcvfs

    return os.path.join(xbmcvfs.profile_dir, "addon_data", "plugin.video.epgstation", "stream_positions.json")


def _saved(rec_id):
    try:
        return json.load(open(_positions_file(), encoding="utf-8"))["positions"].get(str(rec_id))
    except OSError:
        return None


@pytest.mark.parametrize("tag", [False, True], ids=["path", "item-property"])
def test_stream_happy_path(stream_rec, handler, tag):
    """AC 2-4: start GET, keeps every 5 s (the fake server's 15 s timer never fires), DELETE at the end. 011 review
    L4a: also with Kodi reporting the item's nonce property."""
    vid = stream_rec["_video"]["id"]
    xbmcgui.select_answers.append(0)  # 最初から
    _kodi_plays(stop_at=35, tag=tag)
    _run_stream(stream_rec["id"])
    writes = _stream_writes(handler)
    assert writes[0] == ("GET", f"/api/streams/recorded/{vid}/hls?ss=0&mode=0")
    assert writes[1:-1] == [KEEP] * 7 and writes[-1] == DELETE  # keeps at 5, 10, ... 35 s
    assert handler.streams == {}
    url, li = xbmc.played[0]
    assert re.search(r"/streamfiles/stream0\.m3u8\?k=[0-9a-f]{12}$", url) and "/api/" not in url
    assert li.mimetype == "application/vnd.apple.mpegurl" and li.content_lookup is False
    assert "duration" not in li.tag.data and li.tag.data["playcount"] == 0
    assert li.tag.data["title"] and li.props["epgstation.stream"] == url.rsplit("k=", 1)[1]
    assert "inputstream" not in li.props
    assert xbmcgui.selects[0][1] == ["From the beginning", "From a position…"]  # no saved position yet
    assert xbmcgui.progress_log[0][0] == "create" and xbmcgui.progress_log[-1] == ("close",)
    assert not xbmcgui.notifications and xbmc.stop_calls == []
    assert any("stream: 0 ends (stopped)" in m for _, m in xbmc.logs)


def test_stream_keeps_alive_past_the_server_timer(stream_rec, handler):
    """AC 3: the stream is still there after 46 s, kept every 5 s."""
    xbmcgui.select_answers.append(0)
    seen = {}

    def peek(waited):
        if abs(waited - 46) < 1e-9:
            seen.update(streams={k: v["keeps"] for k, v in handler.streams.items()})
    _chain(_kodi_plays(stop_at=50), peek)
    _run_stream(stream_rec["id"])
    assert seen["streams"] == {0: 9}
    assert _stream_writes(handler)[-1] == DELETE


def test_stream_survives_one_timed_out_keep(stream_rec, handler, monkeypatch):
    """011 review M1: a keep that times out (3 s) is retried 2 s later and the server's 15 s timer never fires."""
    from resources.lib.epgstation.api import ApiError, EPGStation

    real, calls = EPGStation.keep_stream, []

    def keep(self, stream_id, timeout=3):
        calls.append((xbmc.waited, timeout))
        if len(calls) == 2:  # the keep at 10 s hangs for its whole timeout
            xbmc.waited += timeout
            raise ApiError("/streams/0/keep: timed out")
        return real(self, stream_id, timeout)
    monkeypatch.setattr(EPGStation, "keep_stream", keep)
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=30)
    _run_stream(stream_rec["id"])
    times = [t for t, _ in calls]
    assert times[:3] == [5, 10, 15] and all(t == 3 for _, t in calls)  # 10 + 3 s timeout + 2 s retry = 15
    assert _stream_writes(handler)[-1] == DELETE and not any("ends (gone)" in m for _, m in xbmc.logs)
    assert not xbmcgui.notifications


def test_stream_repeated_timed_out_keeps_end_it(stream_rec, handler, monkeypatch):
    """Keeps that keep timing out exceed the server's timer: the next keep that gets through finds the stream gone."""
    from resources.lib.epgstation.api import ApiError, EPGStation

    real, calls = EPGStation.keep_stream, []

    def keep(self, stream_id, timeout=3):
        calls.append(xbmc.waited)
        if len(calls) in (2, 3, 4):
            xbmc.waited += timeout
            raise ApiError("/streams/0/keep: timed out")
        return real(self, stream_id, timeout)
    monkeypatch.setattr(EPGStation, "keep_stream", keep)
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=60)
    _run_stream(stream_rec["id"])
    assert any("ends (gone)" in m for _, m in xbmc.logs) and DELETE not in _stream_writes(handler)
    assert _toasts() == ["Transcoded stream ended"] and len(xbmc.stop_calls) == 1


def test_stream_slow_channels_before_the_start(stream_rec, handler, monkeypatch):
    """011 review M2 / L4c: a slow GET /channels comes before the start GET (no request between ready and play)."""
    from resources.lib.epgstation.api import EPGStation

    real, timeouts = EPGStation.channels, []

    def slow(self, timeout=None):
        timeouts.append(timeout)
        xbmc.waited += 12
        return real(self, timeout=timeout)
    monkeypatch.setattr(EPGStation, "channels", slow)
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=20)
    _run_stream(stream_rec["id"])
    assert timeouts == [3]
    assert _stream_writes(handler)[-1] == DELETE and not xbmcgui.notifications


def test_stream_channels_failing(stream_rec, handler, monkeypatch):
    from resources.lib.epgstation.api import ApiError, EPGStation

    def fail(self, timeout=None):
        raise ApiError("/channels: timed out")
    monkeypatch.setattr(EPGStation, "channels", fail)
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=8)
    _run_stream(stream_rec["id"])
    assert xbmc.played and xbmc.played[0][1].tag.data["title"] and _stream_writes(handler)[-1] == DELETE


def test_stream_keep_right_before_play(stream_rec, handler):
    """011 review M2: ready 5 s after the start -> a keep before play() (the server's timer runs from the start)."""
    handler.stream_ready_after = 11  # polls at 0, 0.5, ... 5.0 s
    xbmcgui.select_answers.append(0)
    order = []

    def peek(waited):
        if xbmc.played and not order:
            order.append(_stream_writes(handler))
    _chain(_kodi_plays(stop_at=3), peek)
    _run_stream(stream_rec["id"])
    assert order[0][1:] == [KEEP]  # sent before Kodi got the item


def test_stream_resume_position(stream_rec, handler):
    """AC 8 (D2 B): stopped at ~1:13 absolute -> the next start offers "1:13" and starts there."""
    xbmcgui.select_answers.append(0)
    _kodi_plays(start_at=1, stop_at=75)
    _run_stream(stream_rec["id"])
    handler.reset_state()
    handler.stream_timer, handler.stream_clock = 15, lambda: xbmc.waited
    xbmc.played.clear()
    xbmc.player_file, xbmc.waited = "", 0.0
    xbmcgui.select_answers.append(1)
    _kodi_plays(stop_at=5)
    _run_stream(stream_rec["id"])
    assert xbmcgui.selects[-1][1] == ["From the beginning", "From 1:13", "From a position…"]
    assert _stream_writes(handler)[0][1].endswith("?ss=73&mode=0")


def test_stream_position_store_merges(stream_rec, handler):
    """011 review C2: another transcoded play saved a position while this one ran: both stay."""
    from resources.lib.epgstation import transcode

    xbmcgui.select_answers.append(1)
    xbmcgui.numeric_answers.append("00:05")

    def other_play_saves(waited):
        if abs(waited - 10) < 1e-9:
            store = transcode.PositionStore(_positions_file(), handler_url[0])
            store.put(424242, 900, None)
            assert store.save()
    handler_url = [None]
    import xbmcaddon as addon_stub
    handler_url[0] = addon_stub.settings["server_url"]
    _chain(_kodi_plays(stop_at=20), other_play_saves)
    _run_stream(stream_rec["id"])
    assert _saved(424242) == 900 and _saved(stream_rec["id"]) == 300 + 18


@pytest.mark.parametrize("how", ["other", "abort"])
def test_stream_position_saved_when_another_file_plays_or_kodi_exits(stream_rec, handler, how):
    """011 review L4f."""
    xbmcgui.select_answers.append(1)
    xbmcgui.numeric_answers.append("00:05")  # ss = 300
    _kodi_plays(stop_at=12, other="http://x/api/videos/1" if how == "other" else None)
    if how == "abort":
        xbmc.abort_after = 10
    _run_stream(stream_rec["id"])
    assert _saved(stream_rec["id"]) == (300 + 10 if how == "other" else 300 + 7)  # the last time seen playing
    assert _stream_writes(handler)[-1] == DELETE


def test_stream_from_a_position(stream_rec, handler):
    """AC 7: 位置を指定… with the time dialog's 00:05 (hours:minutes) starts at ss=300."""
    xbmcgui.select_answers.append(1)
    xbmcgui.numeric_answers.append("00:05")
    _kodi_plays(stop_at=3)
    _run_stream(stream_rec["id"])
    assert xbmcgui.numerics[0][0] == 2
    assert _stream_writes(handler)[0][1].endswith("?ss=300&mode=0")


def test_stream_position_dialog_cancelled(stream_rec, handler):
    xbmcgui.select_answers.append(1)
    _run_stream(stream_rec["id"])
    assert handler.writes == [] and not xbmc.played


def test_stream_cancel_at_the_start_dialog(stream_rec, handler):
    _run_stream(stream_rec["id"])
    assert handler.writes == [] and not xbmc.played and xbmcgui.progress_log == []


def test_stream_cancel_in_the_progress_dialog(stream_rec, handler):
    """AC 5c: cancelling while it starts -> DELETE, no playback."""
    handler.stream_ready_after = None
    xbmcgui.progress_cancel_after = 3
    xbmcgui.select_answers.append(0)
    _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[1:] == [DELETE] and not xbmc.played
    assert not xbmcgui.notifications and xbmcgui.progress_log[-1] == ("close",)


def test_stream_never_ready(stream_rec, handler):
    """AC 10: never ready in 20 s -> DELETE + toast (kept alive meanwhile: keeps at 5, 10, 15, 20 s)."""
    handler.stream_ready_after = None
    xbmcgui.select_answers.append(0)
    _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[1:] == [KEEP] * 4 + [DELETE] and not xbmc.played
    assert 20 <= xbmc.waited <= 21
    assert _toasts() == ["Could not start transcoded stream"]


def test_stream_disappears_while_starting(stream_rec, handler):
    handler.stream_ready_after = None
    xbmcgui.select_answers.append(0)
    xbmc.wait_hook = lambda waited: handler.streams.clear() if waited >= 2 else None
    _run_stream(stream_rec["id"])
    assert [m for m, _ in _stream_writes(handler)] == ["GET"] and xbmc.waited < 5  # nothing of ours to DELETE
    assert _toasts() == ["Could not start transcoded stream"]


def test_stream_id_taken_by_another_client_while_starting(stream_rec, handler):
    """011 review L2: our id shows another file (the server reused it): never kept or DELETEd again."""
    handler.stream_ready_after = None
    xbmcgui.select_answers.append(0)

    def reuse(waited):
        if waited >= 2 and handler.streams:
            handler.streams[0]["item"]["viodeFileId"] = 999999
    xbmc.wait_hook = reuse
    _run_stream(stream_rec["id"])
    assert [m for m, _ in _stream_writes(handler)] == ["GET"] and 0 in handler.streams


def test_stream_id_taken_by_another_client_while_playing(stream_rec, handler):
    """011 review L2: after a keep, GET /streams shows our id with another mode: gone, no DELETE of theirs."""
    xbmcgui.select_answers.append(0)

    def reuse(waited):
        if abs(waited - 12) < 1e-9:
            handler.streams[0]["item"]["mode"] = 1
    _chain(_kodi_plays(stop_at=60), reuse)
    _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[-1] == KEEP and 0 in handler.streams  # theirs survives
    assert any("ends (gone)" in m for _, m in xbmc.logs) and _toasts() == ["Transcoded stream ended"]
    assert len(xbmc.stop_calls) == 1


def test_stream_not_deleted_when_the_server_cannot_be_asked(stream_rec, handler, monkeypatch):
    """L2 / round 2 L-2: without a good keep in the last 12 s the DELETE needs GET /streams to show our stream; the
    server unreachable -> no DELETE (its own 15 s timer stops the stream)."""
    from resources.lib.epgstation.api import ApiError, EPGStation

    real_keep, real_streams = EPGStation.keep_stream, EPGStation.streams

    def keep(self, stream_id, timeout=3):
        if xbmc.waited >= 7:  # the server becomes unreachable after the first keep
            raise ApiError("connection refused")
        return real_keep(self, stream_id, timeout)

    def streams(self, timeout=None):
        if xbmc.waited >= 7:
            raise ApiError("connection refused")
        return real_streams(self, timeout=timeout)
    monkeypatch.setattr(EPGStation, "keep_stream", keep)
    monkeypatch.setattr(EPGStation, "streams", streams)
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=18)
    _run_stream(stream_rec["id"])
    assert DELETE not in _stream_writes(handler)
    assert any("not stopped: server not reachable" in m for _, m in xbmc.logs)


def test_stream_delete_checks_ownership_first(stream_rec, fake_server, handler):
    """Codex round 3: the DELETE always follows a GET /streams that shows our stream."""
    import fake_epgstation

    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=12)
    fake_epgstation.REQUESTS[fake_server].clear()
    _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[-1] == DELETE
    assert fake_epgstation.REQUESTS[fake_server][-1][0] == "/api/streams"  # the last GET: the check before the DELETE


def test_stream_id_reused_within_the_trust_window_is_not_deleted(stream_rec, handler):
    """Codex round 3: another client (the web UI) stopped ours and got the id 2 s after our last good keep: GET
    /streams shows their file, so no DELETE, although the keep was recent."""
    xbmcgui.select_answers.append(0)
    play = _kodi_plays(stop_at=12)

    def reuse(waited):
        play(waited)
        if abs(waited - 12) < 1e-9:  # the keep at 10 s was good
            handler.streams[0]["item"]["viodeFileId"] = 999999
    xbmc.wait_hook = reuse
    _run_stream(stream_rec["id"])
    assert DELETE not in _stream_writes(handler) and 0 in handler.streams
    assert any("not stopped: other" in m for _, m in xbmc.logs)


def test_stream_trusted_delete_when_the_check_fails(stream_rec, handler, monkeypatch):
    """Codex round 3: GET /streams fails at the end but the last good keep is < 12 s old -> DELETE anyway."""
    from resources.lib.epgstation.api import ApiError, EPGStation

    real = EPGStation.streams

    def streams(self, timeout=None):
        if xbmc.played and not xbmc.player_file and xbmc.waited > 3:
            raise ApiError("/streams: timed out")
        return real(self, timeout=timeout)
    monkeypatch.setattr(EPGStation, "streams", streams)
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=12)
    _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[-1] == DELETE
    assert any("stopped (trusted)" in m for _, m in xbmc.logs)


def test_stream_verify_every_third_keep(stream_rec, fake_server, handler):
    """Round 2 L-3: the ownership GET follows keeps 1, 4, 7 (5, 20, 35 s), not every keep."""
    import fake_epgstation

    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=36)
    fake_epgstation.REQUESTS[fake_server].clear()
    _run_stream(stream_rec["id"])
    # the readiness poll, the checks after keeps 1, 4, 7 and the one before the DELETE (Codex round 3)
    assert sum(1 for p, _ in fake_epgstation.REQUESTS[fake_server] if p == "/api/streams") == 1 + 3 + 1


def test_stream_gone_before_play_is_a_failed_start(stream_rec, handler, monkeypatch):
    """Round 2 nit: the server lost the stream before play() (the keep right before it: StreamIsUndefined) -> "Could
    not start", not "ended"; nothing to DELETE."""
    from resources.lib.epgstation.api import ApiError, EPGStation

    def gone(self, stream_id, timeout=3):
        raise ApiError("HTTP 500", 500, "StreamIsUndefined")
    monkeypatch.setattr(EPGStation, "keep_stream", gone)
    handler.stream_ready_after = 11  # ready at 5.0 s: the keep before play() is due
    xbmcgui.select_answers.append(0)
    _run_stream(stream_rec["id"])
    assert not xbmc.played and _toasts() == ["Could not start transcoded stream"]
    assert DELETE not in _stream_writes(handler)


@pytest.mark.parametrize("patch", [
    {"isEnableTSRecordedStream": False, "isEnableEncodedRecordedStream": False},
    {"streamConfig": {"recorded": {"ts": {"hls": []}, "encoded": {"hls": []}}}},
    {"streamConfig": {}}])
def test_stream_off_on_the_server(stream_rec, handler, patch):
    """AC 10: streaming off or no mode -> toast, no start GET."""
    handler.config_patch = patch
    xbmcgui.select_answers.append(0)
    _run_stream(stream_rec["id"])
    assert handler.writes == [] and xbmcgui.selects == []
    assert _toasts() == ["Server: streaming off"]


@pytest.mark.parametrize("reason,toast", [
    ("EncodeProcessManageModelCreateError", "No free encoder on the server"),
    ("OutOfRange", "Start position is past the end"),
    ("VideoFileIsNotFound", "Could not start transcoded stream (HTTP 500)")])
def test_stream_start_errors(stream_rec, handler, reason, toast):
    import fake_epgstation

    handler.stream_error = (500, fake_epgstation.error_500(reason))
    xbmcgui.select_answers.append(0)
    _run_stream(stream_rec["id"])
    assert [m for m, _ in _stream_writes(handler)] == ["GET"] and not xbmc.played
    assert _toasts() == [toast]


def test_stream_gone_on_the_server(stream_rec, handler):
    """The server stopped it (keep -> StreamIsUndefined): the loop ends, Kodi's player is stopped, a toast; nothing
    left to DELETE (nit 3)."""
    xbmcgui.select_answers.append(0)

    def gone(waited):
        if abs(waited - 12) < 1e-9:
            handler.streams.clear()
    _chain(_kodi_plays(stop_at=100), gone)
    _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[-1] == KEEP and DELETE not in _stream_writes(handler)
    assert len(xbmc.stop_calls) == 1 and "/streamfiles/stream0.m3u8" in xbmc.stop_calls[0]
    assert 15 <= xbmc.waited < 16 and _toasts() == ["Transcoded stream ended"]


def test_stream_keep_connection_error_is_retried(stream_rec, handler, monkeypatch):
    from resources.lib.epgstation.api import ApiError, EPGStation

    real, calls = EPGStation.keep_stream, []

    def flaky(self, stream_id, timeout=3):
        calls.append(xbmc.waited)
        if len(calls) == 1:
            raise ApiError("connection refused")
        return real(self, stream_id, timeout)
    monkeypatch.setattr(EPGStation, "keep_stream", flaky)
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=14)
    _run_stream(stream_rec["id"])
    assert calls[:3] == [5, 7, 12] and _stream_writes(handler)[-1] == DELETE


def test_stream_stops_when_another_file_plays(stream_rec, handler):
    """AC 5a: playing another recording while the stream plays -> DELETE."""
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=12, other="http://x/api/videos/1")
    _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[-1] == DELETE and xbmc.waited <= 14
    assert xbmc.stop_calls == [] and not xbmcgui.notifications


def test_stream_never_played(stream_rec, handler):
    """Kodi never plays it and sends nothing: "timeout" after 20 s, toast (nit 3)."""
    xbmcgui.select_answers.append(0)
    _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[-1] == DELETE and 20 <= xbmc.waited <= 22
    assert _toasts() == ["Could not start transcoded stream"]


def test_stream_failed_start_is_seen_at_once(stream_rec, handler):
    """011 review L1 / L4b: Kodi's failed start (onPlayBackStopped, nothing of ours played) -> stop after ~2.5 s."""
    xbmcgui.select_answers.append(0)

    def fail(waited):
        if xbmc.played and abs(waited - 1) < 1e-9:
            xbmc.players[-1].onPlayBackStarted()
            xbmc.players[-1].onPlayBackStopped()
    xbmc.wait_hook = fail
    _run_stream(stream_rec["id"])
    assert any("ends (failed)" in m for _, m in xbmc.logs) and xbmc.waited <= 5
    assert _stream_writes(handler)[-1] == DELETE and _toasts() == ["Could not start transcoded stream"]


def test_stream_switch_from_a_recording(stream_rec, handler):
    """The previous file's stop (Kodi 21.3: one stop before our start) is not a failed start."""
    xbmc.player_file = "http://x/api/videos/7"
    xbmcgui.select_answers.append(0)
    play = _kodi_plays(start_at=3, stop_at=10)

    def switch(waited):
        if abs(waited - 1) < 1e-9:
            xbmc.player_file = ""
            xbmc.players[-1].onPlayBackStopped()  # the recording's stop
        play(waited)
    xbmc.wait_hook = switch
    _run_stream(stream_rec["id"])
    assert any("ends (stopped)" in m for _, m in xbmc.logs) and not xbmcgui.notifications


def test_stream_paused_too_long(stream_rec, handler, monkeypatch):
    """011 review L3: paused (time stands still) for PAUSE_LIMIT -> stream and player stopped, position kept, toast."""
    from resources.lib.epgstation import transcode

    monkeypatch.setattr(transcode, "PAUSE_LIMIT", 20)
    xbmcgui.select_answers.append(1)
    xbmcgui.numeric_answers.append("00:05")
    _kodi_plays(stop_at=100, stall_at=6)
    _run_stream(stream_rec["id"])
    assert any("ends (paused)" in m for _, m in xbmc.logs) and len(xbmc.stop_calls) == 1
    assert _stream_writes(handler)[-1] == DELETE and _toasts() == ["Transcoded stream ended"]
    assert _saved(stream_rec["id"]) == 305


def test_stream_abort(stream_rec, handler):
    """AC 5b: Kodi exits (waitForAbort True) -> DELETE."""
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=100)
    xbmc.abort_after = 12
    _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[-1] == DELETE and xbmc.waited == 12


def test_stream_exception_in_the_loop_still_deletes(stream_rec, handler, monkeypatch):
    from resources.lib.epgstation import transcode

    def boom(self, *a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(transcode.KeepAlive, "tick", boom)
    xbmcgui.select_answers.append(0)
    with pytest.raises(RuntimeError):
        _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[-1] == DELETE


@pytest.mark.parametrize("setting,answers,mode", [("1", [0], 1), ("0", [0], 0), ("2", [1, 0], 1), ("2", [0, 0], 0)])
def test_stream_mode_setting(stream_rec, handler, setting, answers, mode):
    """AC 6: last mode -> mode=1; ask -> a select of the server's mode names."""
    xbmcaddon.settings["stream_mode"] = setting
    xbmcgui.select_answers.extend(answers)
    _kodi_plays(stop_at=3)
    _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[0][1].endswith(f"&mode={mode}")
    assert _stream_writes(handler)[-1] == DELETE  # the ownership check matched our mode
    if setting == "2":
        assert xbmcgui.selects[0][1] == ["720p", "480p"]


def test_stream_mode_dialog_cancelled(stream_rec, handler):
    xbmcaddon.settings["stream_mode"] = "2"
    _run_stream(stream_rec["id"])
    assert handler.writes == [] and len(xbmcgui.selects) == 1


def test_stream_with_isa_and_auth(stream_rec, handler):
    """D6: inputstream.adaptive with play_timeshift_buffer when enabled; auth goes into its header properties."""
    xbmc.visible.add(ISA_ON)
    xbmcaddon.settings.update(username="u", password="p")
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=3)
    _run_stream(stream_rec["id"])
    url, li = xbmc.played[0]
    assert "|" not in url and li.props["inputstream"] == "inputstream.adaptive"
    assert li.props["inputstream.adaptive.play_timeshift_buffer"] == "true"
    assert li.props["inputstream.adaptive.manifest_headers"].startswith("Authorization=Basic%20")
    assert li.props["inputstream.adaptive.stream_headers"] == li.props["inputstream.adaptive.manifest_headers"]


def test_stream_plain_kodi_with_auth(stream_rec, handler):
    xbmcaddon.settings.update(username="u", password="p")
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=3)
    _run_stream(stream_rec["id"])
    url, li = xbmc.played[0]
    assert "?k=" in url and url.split("|", 1)[1].startswith("Authorization=Basic%20") and "inputstream" not in li.props


def test_stream_isa_item_without_query_counts_as_ours(stream_rec, handler):
    """inputstream.adaptive reports the playlist path without ?k= (seen on the dev Kodi): still ours."""
    xbmcgui.select_answers.append(0)
    play = _kodi_plays(stop_at=12)

    def strip(waited):
        play(waited)
        if xbmc.player_file:
            xbmc.player_file = xbmc.player_file.split("?", 1)[0]
    xbmc.wait_hook = strip
    _run_stream(stream_rec["id"])
    assert _stream_writes(handler).count(KEEP) == 2 and any("ends (stopped)" in m for _, m in xbmc.logs)


def test_stream_play_request_resolves_false(stream_rec, handler):
    """A play request of the URL (a handle) is answered with a failed resolve first, never a directory."""
    import xbmcplugin

    _run_stream(stream_rec["id"], handle="1")
    assert xbmcplugin.state.resolved is not None and xbmcplugin.state.resolved[0] is False
    assert xbmcplugin.state.ended is None


def test_stream_unknown_recording(kodi, fake_server, handler):
    _run_stream(999999)
    assert handler.writes == [] and xbmcgui.notifications  # the ApiError toast


def _active_dir():
    import xbmcvfs

    return os.path.join(xbmcvfs.profile_dir, "addon_data", "plugin.video.epgstation", "stream_active")


def _active():
    """{(server_url, streamId): entry} of every file in stream_active/."""
    out = {}
    for name in os.listdir(_active_dir()) if os.path.isdir(_active_dir()) else []:
        entry = json.load(open(os.path.join(_active_dir(), name), encoding="utf-8"))
        out[(entry["server_url"], entry["streamId"])] = entry
    return out


def _leave_stale(fake_server, vid, mode=0, server=None, zombie=False, started=None):
    """What a killed Kodi leaves: a stream on the (fake) server (zombie: EPGStation 2.10.0's dead listing, files gone)
    and its entry in stream_active/."""
    import xbmcaddon as addon_stub
    from resources.lib.epgstation import transcode

    sid = json.loads(_http(fake_server, "GET", f"/api/streams/recorded/{vid}/hls?ss=0&mode={mode}")[2])["streamId"]
    fake_epgstation_handler(fake_server).streams[sid]["zombie"] = zombie
    started = int(time.time() * 1000) if started is None else started
    assert transcode.ActiveStreams(_active_dir()).add(server or addon_stub.settings["server_url"], sid, vid, mode,
                                                      started)
    return sid


def fake_epgstation_handler(fake_server):
    import fake_epgstation

    return fake_epgstation.HANDLERS[fake_server]


def test_stream_active_entry_while_playing_and_cleared(stream_rec, handler):
    """Tester F1: the stream is in stream_active/ while it runs and gone from it after the DELETE."""
    xbmcgui.select_answers.append(0)
    seen = {}

    def peek(waited):
        if abs(waited - 5) < 1e-9:
            seen["active"] = _active()
    _chain(_kodi_plays(stop_at=8), peek)
    _run_stream(stream_rec["id"])
    (entry,) = seen["active"].values()
    assert entry["streamId"] == 0 and entry["videoFileId"] == stream_rec["_video"]["id"] and entry["mode"] == 0
    assert _stream_writes(handler)[-1] == DELETE and _active() == {}


def test_stream_zombie_deleted_before_the_next_start(stream_rec, fake_server, handler):
    """Tester F1 / AC 5b: Kodi was killed during playback, EPGStation 2.10.0 keeps the dead stream listed without its
    files: the next transcoded start DELETEs it (listed, enabled, same file and mode, playlist 404)."""
    import fake_epgstation

    vid = stream_rec["_video"]["id"]
    _leave_stale(fake_server, vid, zombie=True)
    handler.writes.clear()
    fake_epgstation.REQUESTS[fake_server].clear()
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=3)
    _run_stream(stream_rec["id"])
    writes = _stream_writes(handler)
    assert writes[0] == DELETE and writes[1] == ("GET", f"/api/streams/recorded/{vid}/hls?ss=0&mode=0")
    assert writes[-1] == DELETE and _active() == {}
    assert ("HEAD /streamfiles/stream0.m3u8", {}) in fake_epgstation.REQUESTS[fake_server]
    assert any("dead listing of an earlier run: stopped" in m for _, m in xbmc.logs)


def test_stream_live_stream_with_the_same_id_file_and_mode_is_not_deleted(stream_rec, fake_server, handler):
    """Final review round: a live stream (playlist there) with our recorded id, file and mode, e.g. another client's
    after an id reuse, or this Kodi's own running one: never DELETEd; the entry stays."""
    vid = stream_rec["_video"]["id"]
    _leave_stale(fake_server, vid)
    handler.writes.clear()
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=3)
    _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[0][0] == "GET" and 0 in handler.streams  # ours was id 1
    assert ("DELETE", "/api/streams/1") in _stream_writes(handler) and DELETE not in _stream_writes(handler)
    assert list(_active()) == [(fake_server, 0)]
    assert any("of an earlier run is live (playlist 200): kept" in m for _, m in xbmc.logs)


def test_stream_running_stream_survives_a_failed_second_start(stream_rec, fake_server, handler):
    """Final review round: this Kodi plays stream A (live, recorded); a second transcoded start fails at the start GET:
    A is not DELETEd and its entry stays."""
    import fake_epgstation

    vid = stream_rec["_video"]["id"]
    _leave_stale(fake_server, vid)  # A, running
    handler.writes.clear()
    handler.stream_error = (500, fake_epgstation.error_500("EncodeProcessManageModelCreateError"))
    xbmcgui.select_answers.append(0)
    _run_stream(stream_rec["id"])
    assert [m for m, _ in _stream_writes(handler)] == ["GET"] and 0 in handler.streams
    assert list(_active()) == [(fake_server, 0)] and _toasts() == ["No free encoder on the server"]


def test_stream_second_start_while_playing(stream_rec, fake_server, handler):
    """Final review round: a second transcoded start while ours plays: Kodi plays the new stream instead, our loop
    sees another file and DELETEs its own stream through the normal stop path (GET /streams shows it as ours)."""
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=10, other=f"{fake_server}/streamfiles/stream1.m3u8?k=0123456789ab")
    _run_stream(stream_rec["id"])
    assert any("ends (other)" in m for _, m in xbmc.logs) and any("stopped (ready)" in m for _, m in xbmc.logs)
    assert _stream_writes(handler)[-1] == DELETE and _active() == {}


def test_stream_stale_entry_of_another_client_is_forgotten(stream_rec, fake_server, handler):
    """The id now shows another file (someone else's): no DELETE, the entry is dropped."""
    vid = stream_rec["_video"]["id"]
    sid = _leave_stale(fake_server, vid, zombie=True)
    handler.streams[sid]["item"]["viodeFileId"] = 999999
    handler.writes.clear()
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=3)
    _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[0][0] == "GET" and sid in handler.streams
    assert any("of an earlier run: forget, forgotten" in m for _, m in xbmc.logs) and _active() == {}


def test_stream_old_stale_entries_are_dropped(stream_rec, fake_server, handler):
    """Entries older than 7 days are dropped without a check or a DELETE (even a dead listing)."""
    from resources.lib.epgstation import transcode

    vid = stream_rec["_video"]["id"]
    _leave_stale(fake_server, vid, zombie=True, started=int(time.time() * 1000) - transcode.STALE_MAX_AGE_MS - 60000)
    handler.writes.clear()
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=3)
    _run_stream(stream_rec["id"])
    assert DELETE not in _stream_writes(handler) and _active() == {}
    assert any("of an earlier run: old, forgotten" in m for _, m in xbmc.logs)


def test_stream_stale_entry_kept_when_the_server_cannot_be_asked(stream_rec, fake_server, handler, monkeypatch):
    from resources.lib.epgstation.api import ApiError, EPGStation

    vid = stream_rec["_video"]["id"]
    _leave_stale(fake_server, vid, zombie=True)
    handler.writes.clear()
    real, calls = EPGStation.streams, []

    def streams(self, timeout=None):
        calls.append(1)
        if len(calls) == 1:  # the stale check
            raise ApiError("/streams: timed out")
        return real(self, timeout=timeout)
    monkeypatch.setattr(EPGStation, "streams", streams)
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=3)
    _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[0][0] == "GET" and list(_active()) == [(fake_server, 0)]


def test_stream_stale_entry_kept_when_the_playlist_cannot_be_asked(stream_rec, fake_server, handler, monkeypatch):
    from resources.lib.epgstation.api import EPGStation

    vid = stream_rec["_video"]["id"]
    _leave_stale(fake_server, vid, zombie=True)
    handler.writes.clear()
    monkeypatch.setattr(EPGStation, "stream_playlist_status", lambda self, sid, timeout=3: None)
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=3)
    _run_stream(stream_rec["id"])
    assert DELETE not in _stream_writes(handler) and list(_active()) == [(fake_server, 0)]


def test_stream_stale_entry_of_another_server_is_left_alone(stream_rec, fake_server, handler):
    vid = stream_rec["_video"]["id"]
    _leave_stale(fake_server, vid, server="http://other:8888", zombie=True)
    handler.writes.clear()
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=3)
    _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[0][0] == "GET" and 0 in handler.streams  # the new one is id 1
    assert list(_active()) == [("http://other:8888", 0)]


def test_stream_entry_kept_when_the_stop_cannot_be_checked(stream_rec, handler, monkeypatch):
    """No DELETE (server unreachable, last good keep too old): the entry stays for the next start."""
    from resources.lib.epgstation.api import ApiError, EPGStation

    real_keep, real_streams = EPGStation.keep_stream, EPGStation.streams

    def keep(self, stream_id, timeout=3):
        if xbmc.waited >= 7:
            raise ApiError("connection refused")
        return real_keep(self, stream_id, timeout)

    def streams(self, timeout=None):
        if xbmc.waited >= 7:
            raise ApiError("connection refused")
        return real_streams(self, timeout=timeout)
    monkeypatch.setattr(EPGStation, "keep_stream", keep)
    monkeypatch.setattr(EPGStation, "streams", streams)
    xbmcgui.select_answers.append(0)
    _kodi_plays(stop_at=18)
    _run_stream(stream_rec["id"])
    assert DELETE not in _stream_writes(handler) and len(_active()) == 1


def test_stream_entry_written_inside_the_try(stream_rec, handler, monkeypatch):
    """Final review nit 1: a failure writing stream_active/ cannot skip the DELETE."""
    from resources.lib.epgstation import transcode

    def boom(self, *a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(transcode.ActiveStreams, "add", boom)
    xbmcgui.select_answers.append(0)
    with pytest.raises(OSError):
        _run_stream(stream_rec["id"])
    assert _stream_writes(handler)[-1] == DELETE


def test_fake_server_head_and_zombie(stream_rec, fake_server, handler):
    vid = stream_rec["_video"]["id"]
    _http(fake_server, "GET", f"/api/streams/recorded/{vid}/hls?ss=0&mode=0")
    assert _http(fake_server, "HEAD", "/streamfiles/stream0.m3u8")[0] == 200
    handler.streams[0]["zombie"] = True
    assert _http(fake_server, "HEAD", "/streamfiles/stream0.m3u8")[0] == 404
    assert _http(fake_server, "GET", "/streamfiles/stream0.m3u8")[0] == 404
    assert json.loads(_http(fake_server, "GET", "/api/streams?isHalfWidth=true")[2])["items"][0]["streamId"] == 0


def test_fake_server_stream_timer(stream_rec, fake_server, handler):
    """011 review L4d: the fake server's stop timer (15 s after the start or the last keep, on the test clock)."""
    vid = stream_rec["_video"]["id"]
    _http(fake_server, "GET", f"/api/streams/recorded/{vid}/hls?ss=0&mode=0")
    xbmc.waited = 14
    assert _http(fake_server, "PUT", "/api/streams/0/keep")[0] == 200
    xbmc.waited = 29
    assert json.loads(_http(fake_server, "GET", "/api/streams?isHalfWidth=true")[2])["items"]
    xbmc.waited = 29.5
    assert json.loads(_http(fake_server, "GET", "/api/streams?isHalfWidth=true")[2]) == {"items": []}
    assert _http(fake_server, "GET", "/streamfiles/stream0.m3u8")[0] == 404


def _http(fake_server, method, path):
    req = urllib.request.Request(fake_server + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        e.close()
        return e.code, e.headers.get("Content-Type"), body


def test_fake_server_stream_routes(stream_rec, fake_server, handler):
    """The fake EPGStation's recorded HLS routes behave like 2.10.0 (backlog 011 Notes)."""
    vid = stream_rec["_video"]["id"]
    base = f"/api/streams/recorded/{vid}/hls"
    assert _http(fake_server, "GET", base + "?ss=0")[0] == 400
    status, _, body = _http(fake_server, "GET", base + "?ss=0&mode=5")
    assert status == 500 and json.loads(body)["errors"] == "ConfigIsUndefined"
    status, _, body = _http(fake_server, "GET", "/api/streams/recorded/999999/hls?ss=0&mode=0")
    assert status == 500 and json.loads(body)["errors"] == "VideoFileIsNotFound"
    assert json.loads(_http(fake_server, "GET", base + "?ss=0&mode=0")[2]) == {"streamId": 0}
    assert json.loads(_http(fake_server, "GET", base + "?ss=0&mode=1")[2]) == {"streamId": 1}
    assert _http(fake_server, "GET", "/api/streams")[0] == 400
    items = json.loads(_http(fake_server, "GET", "/api/streams?isHalfWidth=true")[2])["items"]
    assert [(i["streamId"], i["mode"], i["isEnable"], i["viodeFileId"], i["recordedId"], i["type"]) for i in items] == \
        [(0, 0, True, vid, stream_rec["id"], "RecordedHLS"), (1, 1, True, vid, stream_rec["id"], "RecordedHLS")]
    status, ctype, body = _http(fake_server, "GET", "/streamfiles/stream0.m3u8?k=x")
    assert status == 200 and ctype == "video/MP2T" and b"stream0-000000000.ts" in body and b"ENDLIST" not in body
    assert _http(fake_server, "GET", "/streamfiles/stream0-000000000.ts")[1] == "video/mpeg"
    assert _http(fake_server, "PUT", "/api/streams/0/keep")[2] == b'{"code":200}'
    assert _http(fake_server, "DELETE", "/api/streams/0")[0] == 200
    assert _http(fake_server, "DELETE", "/api/streams/0")[0] == 200  # idempotent
    assert _http(fake_server, "GET", "/streamfiles/stream0.m3u8")[0] == 404
    status, _, body = _http(fake_server, "PUT", "/api/streams/0/keep")
    assert status == 500 and json.loads(body)["errors"] == "StreamIsUndefined"
    assert json.loads(_http(fake_server, "GET", base + "?ss=0&mode=0")[2]) == {"streamId": 0}  # ids are reused
    assert [m for m, _, _ in handler.writes].count("GET") == 6  # every start attempt is recorded


def test_fake_server_out_of_range(stream_rec, fake_server, handler):
    vid = stream_rec["_video"]["id"]
    path = os.path.join(FIXTURES, os.path.basename(handler.root), "api", "videos", str(vid), "duration.json")
    if not os.path.isfile(path):
        pytest.skip("no captured duration")
    status, _, body = _http(fake_server, "GET", f"/api/streams/recorded/{vid}/hls?ss=999999&mode=0")
    assert status == 500 and json.loads(body)["errors"] == "OutOfRange"


def test_live_gr_subchannels_setting(kodi, fake_server):
    """Hide terrestrial sub-channels (025): the setting brings back every watchable service."""
    import xbmcaddon
    from resources.lib.epgstation import items, live

    xbmcaddon.settings["show_subchannels"] = "true"
    run_plugin("action=live&type=GR")
    sched = _get(fake_server, "/api/schedules?GR=true&BS=false&CS=false&SKY=false&startAt=0&endAt=99999999999999")
    want = [items.to_half(s["channel"]["name"]) for s in sched if live.watchable(s["channel"])]
    assert [li.props["epgstation.channel"] for _, li, _ in kodi.items] == want


def test_guide_now_hides_gr_subchannels(kodi, fake_server):
    """Hide terrestrial sub-channels (025): 放送中 lists no GR sub-channel unless the setting is on."""
    import xbmcaddon
    from resources.lib.epgstation import live

    sched = _get(fake_server, "/api/schedules?GR=true&BS=false&CS=false&SKY=false&startAt=0&endAt=99999999999999")
    sub = live.gr_subchannel_ids([s["channel"] for s in sched])
    assert sub
    run_plugin("action=guide_now&t=now")
    hidden = [url for url, _, _ in kodi.items if any(f"channelId={i}" in url for i in sub)]
    assert hidden == []
    xbmcaddon.settings["show_subchannels"] = "true"
    kodi.items.clear()
    run_plugin("action=guide_now&t=now")
    assert any(any(f"channelId={i}" in url for i in sub) for url, _, _ in kodi.items)
