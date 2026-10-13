import json
import os

import pytest
from resources.lib.epgstation import epgmeta, items, recorded
from resources.lib.epgstation.api import EPGStation

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _records(name):
    path = os.path.join(FIX, name, "api", "recorded.json")
    if not os.path.isfile(path):
        pytest.skip(f"no {name} fixtures")
    return json.load(open(path, encoding="utf-8"))["records"]


@pytest.fixture
def api():
    return EPGStation("http://epgs.test:8888")


@pytest.mark.parametrize("fixture_set", ["dev", "dev2"])
def test_every_real_recording_converts(api, fixture_set):
    """Every captured recording must produce a sane item - catches shape surprises."""
    for rec in _records(fixture_set):
        d = items.recorded_item(rec, api, {}, "ja")
        assert d["label"]
        assert d["info"]["duration"] == (rec["endAt"] - rec["startAt"]) // 1000
        assert d["path"].startswith("plugin://plugin.video.epgstation/?")
        assert d["props"]["epgstation.recordedId"] == str(rec["id"])
        plot = d["info"]["plot"]
        assert plot == items.to_half(plot)  # displayed half-width, like before
        if rec.get("description"):
            # the sectioned plot drops the description when an extended section contains it
            assert epgmeta.key(rec["description"]) in epgmeta.key(plot)
        assert d["info"]["mediatype"] in ("movie", "episode")


def test_plot_sections_and_files(api):
    """Backlog 015 AC 8: description, then each extended section under 【key】 (no ◇), then the file lines."""
    rec = {"id": 1, "channelId": 1, "startAt": 0, "endAt": 60000, "name": "ｎ",
           "description": "ｄｅｓｃ", "extended": "◇番組内容\r\nｂｏｄｙ\n◇出演者\n【出演】ａ\n◇音楽\nｄｅｓｃ",
           "videoType": "mpeg2", "videoResolution": "1080i",
           "videoFiles": [{"id": 9, "name": "TS", "type": "ts", "size": 2048}]}
    plot = items.recorded_item(rec, api, {}, "ja")["info"]["plot"]
    # full detail view (002): the 【録画情報】 block replaces the "mpeg2 1080i" / "TS (ts): ..." lines
    assert plot == ("desc\n\nbody\n\n【出演者】\n【出演】a\n\n[Recording]\n"
                    f"Broadcast: 1 {recorded.time_line(rec)}\nVideo: MPEG-2 1080i\nFiles: TS 2.0 KB")
    assert "◇" not in plot


def test_plot_without_metadata_keeps_raw_text(api):
    """A blank name (no metadata): today's plot, description + raw extended, half-width."""
    rec = {"id": 1, "channelId": 1, "startAt": 0, "endAt": 60000, "name": "",
           "description": "ｄｅｓｃ", "extended": "◇番組内容\r\nｂｏｄｙ", "videoFiles": []}
    d = items.recorded_item(rec, api, {}, "ja")
    assert d["info"]["plot"] == f"desc\n\n◇番組内容\nbody\n\n[Recording]\nBroadcast: 1 {recorded.time_line(rec)}"
    assert d["info"]["mediatype"] == "video"


def test_stream_details_and_art(api):
    rec = {"id": 1, "channelId": 5, "startAt": 0, "endAt": 1000, "name": "n", "thumbnails": [7],
           "videoType": "mpeg2", "videoResolution": "1080i", "videoFiles": []}
    d = items.recorded_item(rec, api, {5: "NHK"}, "ja", logos={5})
    assert d["stream"] == {"codec": "mpeg2video", "width": 1920, "height": 1080, "aspect": 0}  # 002: no component
    assert d["audio"] is None
    assert d["art"]["thumb"] == "http://epgs.test:8888/api/thumbnails/7"
    assert d["art"]["icon"] == "http://epgs.test:8888/api/channels/5/logo"
    # artwork (003): no logo URL for a channel without hasLogoData (or when the logos are not known)
    assert "icon" not in items.recorded_item(rec, api, {5: "NHK"}, "ja")["art"]
    assert d["info"]["studios"] == ["NHK"]
    assert d["playable"] is False


def test_recording_now_is_marked(api):
    rec = {"id": 1, "channelId": 5, "startAt": 0, "endAt": 1000, "name": "Live", "isRecording": True}
    assert "●" in items.recorded_item(rec, api, {}, "ja")["label"]


def test_blank_raw_extended_keys_dropped(api):
    rec = {"id": 1, "channelId": 5, "startAt": 0, "endAt": 1000, "name": "n",
           "extended": "◇番組内容\n x \n◇ \njunk"}
    props = items.recorded_item(rec, api, {}, "ja")["props"]
    assert props["epgstation.extended.番組内容"] == "x"
    assert not any(k.endswith(".") for k in props)


def test_pick_video_prefers_type_then_size():
    files = [{"id": 1, "type": "ts", "size": 100}, {"id": 2, "type": "encoded", "size": 10},
             {"id": 3, "type": "encoded", "size": 20}]
    assert items.pick_video(files, "encoded")["id"] == 3
    assert items.pick_video(files, "ts")["id"] == 1
    assert items.pick_video([{"id": 1, "type": "ts", "size": 1}], "encoded")["id"] == 1
    assert items.pick_video([], "ts") is None


def test_basic_auth_header_suffix():
    api = EPGStation("http://h:1/", "user", "pw")
    assert api.kodi_headers() == "|Authorization=Basic%20dXNlcjpwdw%3D%3D"
    assert EPGStation("http://h:1").kodi_headers() == ""


def _recorded(name, rec_id):
    path = os.path.join(FIX, name, "api", "recorded", f"{rec_id}.json")
    if not os.path.isfile(path):
        pytest.skip(f"no {name} recording {rec_id}")
    return json.load(open(path, encoding="utf-8"))


def test_real_duration_flags_partial_recording(api):
    rec = _recorded("dev", 7)  # クレヨンしんちゃん: 30 min slot, ~16 min file
    d = items.recorded_item(rec, api, {}, "ja", {"partial": "途中まで", "length": "長さ"}, real_duration=290.1)
    assert d["info"]["duration"] == 290
    assert d["label"].endswith(" [COLOR yellow][途中まで][/COLOR]")
    assert d["props"]["epgstation.partial"] == "true"
    assert "長さ: 4:50 / 30:00" in d["info"]["plot"]


def test_real_duration_complete_recording_not_partial(api):
    rec = _recorded("dev", 4)
    d = items.recorded_item(rec, api, {}, "ja", real_duration=305.3)
    assert d["info"]["duration"] == 305
    assert "[Partial]" not in d["label"]
    assert "epgstation.partial" not in d["props"]
    assert "Length:" not in d["info"]["plot"]


def test_real_duration_ignored_while_recording(api):
    rec = {"id": 1, "channelId": 5, "startAt": 0, "endAt": 600000, "name": "Live", "isRecording": True}
    d = items.recorded_item(rec, api, {}, "ja", real_duration=30.0)
    assert d["info"]["duration"] == 600
    assert "Partial" not in d["label"]
    assert "epgstation.partial" not in d["props"]


def test_context_label_from_strings(api):
    rec = _recorded("dev", 4)
    d = items.recorded_item(rec, api, {}, "ja", {"choose": "ビデオファイルを選択"})
    assert d["context"][0] == ("ビデオファイルを選択", "plugin://plugin.video.epgstation/?action=choose&id=4")
    assert items.recorded_item(rec, api, {}, "ja")["context"][0][0] == "Choose video file"


def test_empty_localized_strings_fall_back_to_english(api):
    d = items.recorded_item(_recorded("dev", 4), api, {}, "ja", {"choose": "", "partial": ""}, real_duration=10)
    assert d["context"][0][0] == "Choose video file"
    assert "[Partial]" in d["label"]


def test_fmt_duration():
    assert items.fmt_duration(290.1) == "4:50"
    assert items.fmt_duration(3900) == "65:00"
    assert items.fmt_duration(5.6) == "0:06"
    assert items.fmt_duration(0) == "0:00"


def test_video_choices():
    files = [{"id": 4, "name": "TS", "type": "ts", "size": 512 * 1024 * 1024},
             {"id": 6, "name": "H.264", "type": "encoded", "size": 2048}]
    assert items.video_choices(files, {4: 305.38}) == ["TS (ts) 512.0 MB  5:05", "H.264 (encoded) 2.0 KB"]
    assert items.video_choices(files, {}) == ["TS (ts) 512.0 MB", "H.264 (encoded) 2.0 KB"]


def test_pick_video_on_dev_recording_4():
    files = _recorded("dev", 4)["videoFiles"]
    assert items.pick_video(files, "encoded")["id"] == 6
    assert items.pick_video(files, "ts")["id"] == 4


def test_default_prefer():
    assert items.default_prefer({"isRecording": True}, "encoded") == "ts"
    assert items.default_prefer({"isRecording": False}, "encoded") == "encoded"
    assert items.default_prefer({}, "ts") == "ts"


def test_get_converts_http_exceptions(monkeypatch):
    import http.client
    import urllib.request

    from resources.lib.epgstation.api import ApiError

    def broken(*a, **kw):
        raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr(urllib.request, "urlopen", broken)
    with pytest.raises(ApiError):
        EPGStation("http://h:1").get("/version")


def _halfwidth(name):
    """{id: {name, description, extended, rawExtended}} as EPGStation returns them with isHalfWidth=true."""
    path = os.path.join(FIX, name, "halfwidth.json")
    if not os.path.isfile(path):
        pytest.skip(f"no {name} half-width texts")
    return json.load(open(path, encoding="utf-8"))


@pytest.mark.parametrize("fixture_set", ["dev", "dev2"])
def test_fixtures_are_full_width_like_the_addon_reads_them(fixture_set):
    """The API fixtures are captured with isHalfWidth=false: no rawExtended (EPGStation leaves it out)."""
    recs = _records(fixture_set)
    assert not any("rawExtended" in r for r in recs)
    assert any(r["name"] != items.to_half(r["name"]) for r in recs)


# EPGStation with needToReplaceEnclosingCharacters (dev) rewrites enclosed characters in `extended`
# (StrUtil.replaceEnclosedCharacters) but not in rawExtended. Its whole table (src/util/StrUtil.ts, 2.10.0):
ENCLOSED = {
    "\U0001f14a": "[HV]", "\U0001f13f": "[P]", "\U0001f14c": "[SD]", "\U0001f146": "[W]", "\U0001f14b": "[MV]",
    "\U0001f210": "[手]", "\U0001f211": "[字]", "\U0001f212": "[双]", "\U0001f213": "[デ]", "\U0001f142": "[S]",
    "\U0001f214": "[二]", "\U0001f215": "[多]", "\U0001f216": "[解]", "\U0001f14d": "[SS]", "\U0001f131": "[B]",
    "\U0001f13d": "[N]", "\U0001f217": "[天]", "\U0001f218": "[交]", "\U0001f219": "[映]", "\U0001f21a": "[無]",
    "\U0001f21b": "[料]", "\u26bf": "[鍵]", "\U0001f21c": "[前]", "\U0001f21d": "[後]", "\U0001f21e": "[再]",
    "\U0001f21f": "[新]", "\U0001f220": "[初]", "\U0001f221": "[終]", "\U0001f222": "[生]", "\U0001f223": "[販]",
    "\U0001f224": "[声]", "\U0001f225": "[吹]", "\U0001f14e": "[PPV]", "\u3299": "[秘]", "\U0001f200": "[ほか]",
}


def _replace_enclosed(text):
    for char, repl in ENCLOSED.items():
        text = text.replace(char, repl)
    return text


@pytest.mark.parametrize("fixture_set", ["dev", "dev2"])
def test_to_half_reproduces_half_width_api(fixture_set):
    """to_half(full-width text) == what EPGStation returns with isHalfWidth=true, for every captured recording."""
    half = _halfwidth(fixture_set)
    full = {str(r["id"]): r for r in _records(fixture_set)}
    assert half and set(half) == set(full)
    for rec_id, texts in half.items():
        for field in ("name", "description", "extended"):
            assert items.to_half(full[rec_id].get(field)) == texts.get(field), (rec_id, field)


@pytest.mark.parametrize("fixture_set", ["dev", "dev2"])
def test_raw_extended_rebuilt_from_extended(fixture_set):
    """raw_extended(full extended), to_half'd, == the half-width rawExtended.

    EPGStation trims `extended`, so the last value can lack the trailing CRLF that rawExtended has; it may
    replace enclosed characters in `extended` only (ENCLOSED); and it
    writes a key that already starts with ◇ without adding another one (seen on dev: "◇おしらせ"), so
    the rebuilt keys never start with ◇.
    """
    full = {str(r["id"]): r for r in _records(fixture_set)}
    checked = 0
    for rec_id, texts in _halfwidth(fixture_set).items():
        if "rawExtended" not in texts:
            continue
        raw = items.raw_extended(full[rec_id].get("extended"))
        got = {items.to_half(k): items.to_half(v).rstrip() for k, v in raw.items()}
        want = {k.lstrip("\u25c7"): _replace_enclosed(v).rstrip() for k, v in texts["rawExtended"].items()}
        assert got == want, rec_id
        checked += 1
    assert checked


@pytest.mark.parametrize("fixture_set", ["dev", "dev2"])
def test_labels_unchanged_by_full_width_texts(api, fixture_set):
    """Labels and plot outline show exactly the half-width text the addon showed before (isHalfWidth=true)."""
    half = _halfwidth(fixture_set)
    for rec in _records(fixture_set):
        d = items.recorded_item(rec, api, {}, "ja")
        assert d["label"] == half[str(rec["id"])]["name"]
        assert d["info"]["plotoutline"] == (half[str(rec["id"])].get("description") or "")
        for key, value in (half[str(rec["id"])].get("rawExtended") or {}).items():
            key = key.strip().lstrip("\u25c7")
            if key:
                assert d["props"]["epgstation.extended." + key] == _replace_enclosed(value).strip()


# Guide programs whose values contain lines starting with ◇ (dev /api/schedules/detail, schedule_details.json).
# 報道特集 (327220206442887) is a genuine EPGStation ambiguity: the whole value of 公式ページ is the single line
# "◇番組HP http://www.tbs.co.jp/houtoku/", and EPGStation's `extended` ("…\n◇公式ページ\n◇番組HP http://…\n◇公式ページ2…")
# is the same text it would write for an empty 公式ページ followed by a section named "番組HP http://…".
AMBIGUOUS = {"327220206442887": {"公式ページ": "", "番組HP http://www.tbs.co.jp/houtoku/": ""}}


def test_raw_extended_with_diamond_lines_in_values():
    """Sections are joined with a bare \n, values use \r\n: "\r\n◇X(旧Twitter)" inside a value is no header."""
    path = os.path.join(FIX, "dev", "schedule_details.json")
    details = json.load(open(path, encoding="utf-8"))
    assert len(details) >= 6
    for program_id, texts in details.items():
        got = {items.to_half(k): items.to_half(v).rstrip() for k, v in items.raw_extended(texts["extended"]).items()}
        want = {k.lstrip("\u25c7"): _replace_enclosed(v).rstrip() for k, v in texts["rawExtended"].items()}
        if program_id in AMBIGUOUS:
            for key, value in AMBIGUOUS[program_id].items():
                assert got.pop(key) == value
            want = {k: v for k, v in want.items() if k not in AMBIGUOUS[program_id]}
        assert got == want, (program_id, texts["name"])


def test_raw_extended_value_lines_starting_with_diamond():
    ext = "◇番組内容\nbody\n◇公式ページ\n◇番組HP\r\nhttp://x\r\n◇X(旧Twitter)\r\n@x\n◇出演者\na"
    assert items.raw_extended(ext) == {"番組内容": "body", "公式ページ": "◇番組HP\r\nhttp://x\r\n◇X(旧Twitter)\r\n@x",
                                       "出演者": "a"}


def test_to_half_table():
    assert items.to_half("ＡＢＣ！～　”’‘￥〜") == "ABC!~ \"'`\\~"
    assert items.to_half("（字）") == "(字)"
    assert items.to_half("") == "" and items.to_half(None) is None


def test_raw_extended_keys_and_crlf():
    # EPGStation: "\n".join("◇" + key + "\n" + value); values keep their CRLFs
    assert items.raw_extended("◇番組内容\nbody\r\n2行目\n◇出演者\n【出演】a") == {
        "番組内容": "body\r\n2行目", "出演者": "【出演】a"}
    assert items.raw_extended("") == {} and items.raw_extended(None) == {}
    assert items.raw_extended("◇a\n◇b\nx\n◇c") == {"a": "", "b": "x", "c": ""}  # empty values, trimmed end


def test_in_progress_matching_filters_like_recorded():
    recs = [
        {"id": 1, "channelId": 10, "genre1": 6, "genre2": None, "genre3": None, "ruleId": 2},
        {"id": 2, "channelId": 11, "genre1": 0, "genre2": 6, "genre3": None, "ruleId": None},
        {"id": 3, "channelId": 10, "genre1": 7, "genre2": None, "genre3": None, "ruleId": 3},
    ]
    ids = lambda **kw: [r["id"] for r in items.in_progress_matching(recs, **kw)]  # noqa: E731
    assert ids() == []  # unfiltered lists have their own "Recording now" folder
    assert ids(channel_id="10") == [1, 3]
    assert ids(genre="6") == [1, 2]
    assert ids(rule_id="2") == [1]
    assert ids(channel_id="10", genre="7") == [3]


def test_extended_props_from_half_width_raw_extended(api):
    """A half-width RecordedItem (rawExtended present) still gives the extended props."""
    rec = {"id": 1, "channelId": 5, "startAt": 0, "endAt": 1000, "name": "n",
           "rawExtended": {"番組内容": " x ", "◇おしらせ": "y"}}
    props = items.recorded_item(rec, api, {}, "ja")["props"]
    assert props["epgstation.extended.番組内容"] == "x"
    assert props["epgstation.extended.おしらせ"] == "y"  # same key as rebuilt from `extended`


def _synthetic(key, base=("dev", 20)):
    """A real dev recording (default dev 20, a BS movie) with the EPG text of a hand-written synthetic entry
    (tests/fixtures/synthetic/epg_texts.json: shapes the free channels lack) instead of its own texts and genres."""
    with open(os.path.join(FIX, "synthetic", "epg_texts.json"), encoding="utf-8") as f:
        text = next(i for i in json.load(f)["items"] if i["key"] == key)
    rec = {k: v for k, v in _recorded(*base).items()
           if not k.startswith(("genre", "subGenre")) and k not in ("name", "description", "extended")}
    rec.update({k: v for k, v in text.items() if k not in ("key", "why")})
    return rec


def test_movie_with_year_from_epg_text(api):
    """synthetic edition-pg [2K][映]北の灯台守<4Kデジタルリマスター版><PG-12>: a movie with the production year, no
    premiered."""
    rec = _synthetic("edition-pg")
    d = items.recorded_item(rec, api, {}, "ja")
    info = d["info"]
    assert info["mediatype"] == "movie"
    assert info["title"] == "北の灯台守"
    assert info["originaltitle"] == items.to_half(rec["name"])
    assert info["year"] == 1981
    assert "premiered" not in info and "firstaired" not in info  # Kodi takes the year from either
    assert info["dateadded"].startswith(items.fmt_time(rec["startAt"], "%Y-%m-%d"))
    assert info["directors"] == ["佐伯隆一"]
    assert "4Kデジタルリマスター版" in info["tags"]
    assert "tvshowtitle" not in info and "episode" not in info
    assert d["label"] == items.to_half(rec["name"])
    assert d["props"]["epgstation.kind"] == "movie"
    assert d["props"]["epgstation.show"] == "北の灯台守"


def test_movie_without_year_has_no_premiered(api):
    """dev2 102 映画「日本女侠伝 真赤な度胸花」 (no year in the EPG): no air year shown as the movie year."""
    rec = _recorded("dev2", 102)
    info = items.recorded_item(rec, api, {}, "ja")["info"]
    assert info["mediatype"] == "movie" and "year" not in info
    assert "premiered" not in info and "firstaired" not in info  # Kodi takes the year from either
    assert info["dateadded"].startswith(items.fmt_time(rec["startAt"], "%Y-%m-%d"))


def test_film_genre_other_alone_is_not_a_movie(api):
    """BS10プレミアムNAVI[字] (free BS10 guide text, genre 映画/その他 only, a channel promo) on a dev recording: an
    episode (016 AC 5). Like every episode it shows the air year: premiered is set (Kodi takes the year from it), no
    production year."""
    with open(os.path.join(FIX, "dev", "schedule_texts.json"), encoding="utf-8") as f:
        text = next(t for t in json.load(f) if t["name"] == "BS10プレミアムNAVI[字]")
    assert (text["genre1"], text["subGenre1"]) == (6, 15) and "genre2" not in text
    rec = {k: v for k, v in _recorded("dev", 20).items() if not k.startswith(("genre", "subGenre"))}
    rec.update(text)
    info = items.recorded_item(rec, api, {}, "ja")["info"]
    assert info["mediatype"] == "episode" and "year" not in info
    assert info["premiered"] == items.fmt_time(rec["startAt"], "%Y-%m-%d")
    assert info["tvshowtitle"] == "BS10プレミアムNAVI"
    assert "season" not in info and "episode" not in info


def test_movie_year_from_year_country_line(api):
    """dev 20 必殺仕掛人 ("１９７３年／日本" at the end of the extended text): year 1973, "日本" is no writer."""
    info = items.recorded_item(_recorded("dev", 20), api, {}, "ja")["info"]
    assert info["mediatype"] == "movie" and info["year"] == 1973
    assert "日本" not in info["writers"]


def test_dubbed_movie(api):
    """synthetic dub-movie [映]ネオン・ハーバー（吹替版）[字]: year from "(1994年 アメリカ)", 吹替版 + 字幕, dub voices
    as roles."""
    info = items.recorded_item(_synthetic("dub-movie"), api, {}, "ja")["info"]
    assert info["mediatype"] == "movie"
    assert info["title"] == "ネオン・ハーバー"
    assert info["year"] == 1994
    assert info["tags"][:2] == ["吹替版", "字幕"]
    assert info["cast"][:2] == [("マーカス・ベルウッド", "吹替: 森川陽太", 0),
                                ("ジェーン・カーステアズ", "吹替: 香坂みのり", 1)]
    assert "リディア・モーガン" in info["writers"]
    assert items.recorded_item(_synthetic("dub-movie"), api, {}, "ja")["props"]["epgstation.version"] == "吹替版"


def test_dubbed_movie_english(api):
    """genre_language en: flag/version tags in English, dub voice roles labelled in English."""
    info = items.recorded_item(_synthetic("dub-movie"), api, {}, "en")["info"]
    assert info["tags"][:2] == ["Dubbed", "Subtitles"]
    assert info["cast"][0] == ("マーカス・ベルウッド", "Dub: 森川陽太", 0)


@pytest.mark.parametrize("role, en", [
    ("出演", "Cast"), ("出演者", "Cast"), ("キャスト", "Cast"), ("声", "Voice"), ("声の出演", "Voice"),
    ("ゲスト", "Guest"), ("MC", "Host"), ("司会", "Host"), ("語り", "Narrator"), ("ナレーション", "Narrator"),
    ("キャスター", "Anchor"), ("解説", "Commentator"), ("実況", "Play-by-play"), ("アナウンサー", "Announcer"),
    ("リポーター", "Reporter"), ("アシスタント", "Co-host"), ("ナビゲーター", "Presenter"), ("講師", "Lecturer"),
    ("パーソナリティ", "Host"),
    ("江戸川コナン", "江戸川コナン"),  # a character name stays as it is
    ("解説キャスター※週替わり", "解説キャスター※週替わり"),  # exact matches only
    ("吹替: 森川陽太", "Dub: 森川陽太"),
    ("", ""), (None, None),
])
def test_english_roles(role, en):
    assert items.localized_role(role, "en") == en
    assert items.localized_role(role, "ja") == role


def test_english_dub_roles_fit_the_cast_panel(api):
    """Estuary's cast role line holds about 131 px: "Dub: " + a 6-character voice name. Keeps the English dub label
    short if someone changes it: every dub role of the synthetic dubbed texts (voice names of 4-5 characters, the
    usual length of a Japanese name) fits in len("Dub: ") + 6 characters."""
    recs = [_synthetic(key) for key in ("dub-movie", "dub-paren-version")]
    roles = [r for rec in recs for _, r, _ in items.recorded_item(rec, api, {}, "en")["info"].get("cast", [])
             if r and r.startswith(items.DUB_ROLE_EN)]
    assert len(roles) == 6 and items.DUB_ROLE_EN == "Dub: "
    assert [r for r in roles if len(r) > len("Dub: ") + 6] == []


def test_english_roles_on_a_recording(api):
    """dev 22 ブラタモリ with genre_language en: label roles in English; dev 15 character names unchanged."""
    cast = items.recorded_item(_recorded("dev", 22), api, {}, "en")["info"]["cast"]
    assert [(n, r) for n, r, _ in cast] == [("タモリ", "Cast"), ("佐藤茉那", "Announcer"), ("あいみょん", "Narrator")]
    cast = items.recorded_item(_recorded("dev", 15), api, {}, "en")["info"]["cast"]
    assert ("高山みなみ", "江戸川コナン") in [(n, r) for n, r, _ in cast]


def test_episode_range(api):
    """synthetic range 放課後のグループ課題 #1-5(全10話) on dev 13: episode 1, end 5, cast."""
    rec = _synthetic("range", ("dev", 13))
    d = items.recorded_item(rec, api, {}, "ja")
    info = d["info"]
    assert info["mediatype"] == "episode"
    assert info["tvshowtitle"] == "放課後のグループ課題"
    assert (info["season"], info["episode"]) == (1, 1)
    assert d["props"]["epgstation.episodeEnd"] == "5"
    assert d["props"]["epgstation.kind"] == "tv"
    assert info["cast"][0][0] == "藤原陽太"
    assert info["premiered"] == items.fmt_time(rec["startAt"], "%Y-%m-%d")
    assert "year" not in info


def test_episode_number_and_subtitle(api):
    """dev 18 アニメ　マロニエ王国の七人の騎士（２）「湖上の春」[字]: episode 2, title = subtitle."""
    info = items.recorded_item(_recorded("dev", 18), api, {}, "ja")["info"]
    assert info["tvshowtitle"] == "マロニエ王国の七人の騎士"
    assert (info["season"], info["episode"]) == (1, 2)
    assert info["title"] == "湖上の春"


def test_season_from_name(api):
    """dev 8 孤独のグルメ 傑作選 Season6 #8 #9: season 6; dev 13 朱色の仮面 第1話 (no marker): season 1."""
    info = items.recorded_item(_recorded("dev", 8), api, {}, "ja")["info"]
    assert (info["season"], info["episode"]) == (6, 8)
    info = items.recorded_item(_recorded("dev", 13), api, {}, "ja")["info"]
    assert (info["season"], info["episode"]) == (1, 1)


def test_tv_without_episode_and_flag_tags(api):
    """dev 4 ニュース・気象情報[字]: episode type, show parsed, no season/episode, tag 字幕 (en Subtitles)."""
    rec = _recorded("dev", 4)
    info = items.recorded_item(rec, api, {}, "ja")["info"]
    assert info["mediatype"] == "episode"
    assert info["tvshowtitle"] == info["title"] == "ニュース・気象情報"
    assert "season" not in info and "episode" not in info
    assert info["tags"] == ["字幕"]
    assert items.recorded_item(rec, api, {}, "en")["info"]["tags"] == ["Subtitles"]


def test_new_series_tag_english(api):
    # [新]朱色の仮面　第１話「仮面屋ペル」[字][デ]
    info = items.recorded_item(_recorded("dev", 13), api, {}, "en")["info"]
    assert info["tags"] == ["New series", "Subtitles", "Data broadcast"]
    assert (info["tvshowtitle"], info["episode"], info["title"]) == ("朱色の仮面", 1, "仮面屋ペル")


def test_flag_tags_english_table_covers_parser_flags():
    from resources.lib.epgstation import epgmeta as em

    for ja in list(em.FLAG_TAG.values()) + [v for v, _ in em.VERSION] + em.SUBGENRE[em.G_WELFARE][4:]:
        assert items.tag_en(ja) != ja, ja
    assert items.tag_en("4Kデジタルリマスター版") == "4Kデジタルリマスター版"  # editions stay as in the EPG


def test_blank_name_falls_back_to_plain_item(api):
    rec = {"id": 1, "channelId": 5, "startAt": 0, "endAt": 1000, "name": " ", "description": "d"}
    info = items.recorded_item(rec, api, {}, "ja")["info"]
    assert info["mediatype"] == "video"
    assert info["premiered"] and "cast" not in info and "tags" not in info


def test_parser_error_falls_back_and_reports(api, monkeypatch):
    def boom(*a, **kw):
        raise ValueError("odd text")

    monkeypatch.setattr(epgmeta, "parse_title", boom)
    errors = []
    rec = _recorded("dev", 20)
    d = items.recorded_item(rec, api, {}, "ja", on_error=lambda r, e: errors.append((r["id"], str(e))))
    assert d["info"]["mediatype"] == "video"
    assert d["info"]["title"] == items.to_half(rec["name"])
    assert errors == [(20, "odd text")]


# --- backlog 005: movies, TV shows, rule counts ------------------------------------------------------------------

def _entries(fixture_set):
    """Slim library entries of a fixture set's /recorded + /recording records, newest first."""
    from resources.lib.epgstation import library

    recs = _records(fixture_set)
    live = json.load(open(os.path.join(FIX, fixture_set, "api", "recording.json"), encoding="utf-8"))["records"]
    entries = [library.slim(r, epgmeta.metadata(r)) for r in recs + live]
    return sorted(entries, key=lambda e: (e["startAt"], e["id"]), reverse=True), {r["id"]: r for r in recs + live}


def _entry(i, show, start, thumb=None, key=None, kind="tv", channel=1, rule=None, rec=False):
    return {"id": i, "name": show, "kind": kind, "show": show, "key": key or epgmeta.key(show), "season": None,
            "episode": None, "version": "", "year": None, "startAt": start, "ruleId": rule, "channelId": channel,
            "thumb": thumb, "rec": rec}


@pytest.mark.parametrize("fixture_set", ["dev", "dev2"])
def test_group_shows_on_real_recordings(fixture_set):
    entries, _ = _entries(fixture_set)
    shows = items.group_shows(entries)
    tv = [e for e in entries if e["kind"] != "movie"]
    assert sum(s["count"] for s in shows) == len(tv)
    assert {s["key"] for s in shows} == {e["key"] for e in tv}
    by_id = {e["id"]: e for e in entries}
    for s in shows:
        assert s["count"] == len(s["ids"]) == len([e for e in tv if e["key"] == s["key"]])
        starts = [by_id[i]["startAt"] for i in s["ids"]]
        assert starts == sorted(starts, reverse=True)
        assert s["newest"]["id"] == s["ids"][0] and s["oldest"]["id"] == s["ids"][-1]
        with_thumb = [by_id[i] for i in s["ids"] if by_id[i]["thumb"] is not None]
        assert s["thumb"] == (with_thumb[0] if with_thumb else None)
    newest = [s["newest"]["startAt"] for s in shows]
    assert newest == sorted(newest, reverse=True)
    live = json.load(open(os.path.join(FIX, fixture_set, "api", "recording.json"), encoding="utf-8"))["records"]
    for rec in live:  # in-progress recordings are grouped like finished ones (007 refreshed the dev ones)
        if by_id[rec["id"]]["kind"] != "movie":
            assert next(s for s in shows if rec["id"] in s["ids"])["key"] == by_id[rec["id"]]["key"]
    if fixture_set == "dev":
        gourmet = next(s for s in shows if s["key"] == "孤独のグルメ")
        assert by_id[gourmet["ids"][0]]["season"] == 6


def test_group_shows_title_thumb_and_channels():
    entries = [
        _entry(5, "プレバト!!", 500, channel=2),          # newest, no thumbnail
        _entry(4, "プレバト", 400, thumb=44, channel=1),
        _entry(3, "プレバト!!", 300, thumb=33, channel=2),
        _entry(2, "プレバト", 200, channel=3),
        _entry(1, "映画X", 100, kind="movie"),
    ]
    (show,) = items.group_shows(entries)
    assert show["key"] == "プレバト" and show["count"] == 4 and show["ids"] == [5, 4, 3, 2]
    assert show["title"] == "プレバト!!"  # 2 vs 2: the newest spelling wins the tie
    assert show["thumb"]["id"] == 4
    assert show["channels"] == [2, 1, 3]
    entries.append(_entry(0, "プレバト", 50))
    assert items.group_shows(entries)[0]["title"] == "プレバト"  # most frequent


def test_tvshow_folder_fields(api):
    entries = [_entry(3, "ドラマA", 1_700_000_200_000, channel=2), _entry(2, "ドラマA", 1_700_000_100_000,
               thumb=22, channel=1), _entry(1, "ドラマA", 1_600_000_000_000, thumb=11, channel=1)]
    records = {3: {"id": 3, "channelId": 2, "description": "ｎｅｗｅｓｔ", "genre1": 3, "subGenre1": 0},
               2: {"id": 2, "channelId": 1}, 1: {"id": 1, "channelId": 1}}
    (show,) = items.group_shows(entries)
    d = items.tvshow_folder(show, records, api, {1: "NHK", 2: "BS"}, "ja", logos={1, 7})
    assert d["folder"] and not d["playable"]
    assert d["label"] == "ドラマA (3)"
    assert d["path"] == items.plugin_url(action="show", key="ドラマa")
    info = d["info"]
    assert info["mediatype"] == "tvshow" and info["title"] == info["tvshowtitle"] == "ドラマA"
    assert info["plot"] == "newest"
    assert info["genres"] == items.item_genres(records[3], "ja") and info["genres"]
    assert info["studios"] == ["BS", "NHK"]
    assert info["premiered"] == items.fmt_time(1_600_000_000_000, "%Y-%m-%d")
    assert info["dateadded"] == items.fmt_time(1_700_000_200_000, "%Y-%m-%d %H:%M:%S")
    assert d["props"]["TotalEpisodes"] == "3"
    thumb = api.thumbnail_url(22)
    assert d["art"] == {"thumb": thumb, "poster": thumb, "fanart": thumb, "landscape": thumb, "icon": api.logo_url(1)}
    # no episode with a thumbnail: the channel logo of the newest one (artwork (003): thumb too)
    (bare,) = items.group_shows([_entry(9, "X", 1, channel=7)])
    logo = api.logo_url(7)
    assert items.tvshow_folder(bare, {}, api, {}, "en", logos={7})["art"] == {"thumb": logo, "icon": logo}


def test_movie_label(api):
    rec = {"id": 1, "channelId": 1, "startAt": 0, "endAt": 60000, "genre1": 6,
           "name": "スーパーマン（２０２５）［字幕版］", "videoFiles": []}
    meta = epgmeta.metadata(rec)
    assert meta["kind"] == "movie" and meta["version"] == "字幕版"
    item = items.recorded_item(rec, api, {}, "ja")
    assert items.movie_label(item, meta, "ja") == "スーパーマン [字幕版]"
    assert items.movie_label(item, meta, "en") == "スーパーマン [Subtitled]"
    plain = dict(rec, name="スーパーマン（２０２５）")
    pmeta = epgmeta.metadata(plain)
    assert items.movie_label(items.recorded_item(plain, api, {}, "ja"), pmeta, "ja") == "スーパーマン"
    # recording / partial markers stay
    live = items.recorded_item(dict(rec, isRecording=True), api, {}, "ja")
    assert items.movie_label(live, meta, "ja") == "[COLOR red]●[/COLOR] スーパーマン [字幕版]"
    part = items.recorded_item(dict(rec, endAt=600000, videoFiles=[{"id": 1, "type": "ts", "name": "TS"}]),
                               api, {}, "ja", real_duration=10)
    assert items.movie_label(part, meta, "ja").endswith("スーパーマン [字幕版] [COLOR yellow][Partial][/COLOR]")


@pytest.mark.parametrize("fixture_set", ["dev", "dev2"])
def test_movie_labels_sort_versions_together(api, fixture_set):
    entries, records = _entries(fixture_set)
    labels = sorted(items.movie_label(items.recorded_item(records[e["id"]], api, {}, "ja"), e, "ja")
                    for e in entries if e["kind"] == "movie")
    assert all(labels) and len(labels) == len([e for e in entries if e["kind"] == "movie"])
    for e in entries:  # versions of one movie are next to each other (two versions: synthetic test below)
        if e["kind"] == "movie" and e["version"]:
            same = [i for i, label in enumerate(labels) if label.startswith(items.to_half(e["show"]) + " [")]
            assert same == list(range(same[0], same[0] + len(same)))


def test_movie_labels_sort_two_versions_together(api):
    """Two versions of one movie (吹替版 / 字幕版, synthetic names on dev 20) sort next to each other: the label is
    "show [version]", so another title starting with the show comes after both."""
    from resources.lib.epgstation import library

    names = ["[映]ネオン・ハーバー２　帰港[字]", "[映]ネオン・ハーバー（字幕版）", "[映]ネオン・ハーバー（吹替版）[字]",
             "[映]ネオン"]
    recs = [dict(_recorded("dev", 20), id=i, name=n) for i, n in enumerate(names, 1)]
    labels = sorted(items.movie_label(items.recorded_item(r, api, {}, "ja"), library.slim(r, epgmeta.metadata(r)), "ja")
                    for r in recs)
    versions = [i for i, label in enumerate(labels) if label.startswith("ネオン・ハーバー [")]
    assert len(versions) == 2 and versions[1] == versions[0] + 1


def test_rule_counts_and_folders():
    entries = [_entry(1, "a", 1, rule=1), _entry(2, "b", 2, rule=1, rec=True), _entry(3, "c", 3, rule=3),
               _entry(4, "d", 4)]
    counts = items.rule_counts(entries)
    assert counts == {1: 2, 3: 1}
    rules = {"rules": [
        {"id": 1, "searchOption": {"keyword": "ニュース"}, "saveOption": {"directory": "news"}},
        {"id": 2, "searchOption": {"keyword": "空"}},
        {"id": 3, "searchOption": {}, "saveOption": {"directory": "アニメ"}},
        {"id": 4, "searchOption": {}},
    ]}
    folders = items.rule_folders(rules, {**counts, 4: 5})
    assert [f["label"] for f in folders] == ["ニュース (2)", "アニメ (1)", "Rule 4 (5)"]
    assert [f["path"] for f in folders] == [items.plugin_url(action="list", ruleId=i, name=n)
                                            for i, n in ((1, "ニュース"), (3, "アニメ"), (4, "Rule 4"))]
    assert items.rule_folders(rules, {}) == []


@pytest.mark.parametrize("fixture_set", ["dev", "dev2"])
def test_rule_folders_on_real_rules(fixture_set):
    entries, _ = _entries(fixture_set)
    rules = json.load(open(os.path.join(FIX, fixture_set, "api", "rules.json"), encoding="utf-8"))
    folders = items.rule_folders(rules, items.rule_counts(entries))
    assert folders and not any(f["label"].endswith("(0)") for f in folders)
    if fixture_set == "dev":
        assert [f["label"].rsplit(" (", 1)[0] for f in folders] == ["アニメ", "映画", "ドラマ", "ドキュメンタリー"]


def test_root_and_browse_menus():
    strings = {k: k.upper() for k in ("movies", "tvshows", "recent", "recording", "search", "browse",
                                      "channels", "genres", "rules")}
    root = items.root_menu(strings)
    assert [d["label"] for d in root] == ["MOVIES", "TVSHOWS", "RECENT", "RECORDING", "SEARCH", "BROWSE"]
    assert [d["path"] for d in root] == [items.plugin_url(action=a) for a in
                                         ("movies", "tvshows", "list", "recording", "search_ask", "browse")]
    browse = items.browse_menu(strings)
    assert [d["label"] for d in browse] == ["CHANNELS", "GENRES", "RULES"]
    assert [d["path"] for d in browse] == [items.plugin_url(action=a) for a in ("channels", "genres", "rules")]
    assert all(d["folder"] for d in root + browse if d["label"] != "SEARCH")
    search = next(d for d in root if d["label"] == "SEARCH")  # protect / delete (006): a click action
    assert search["folder"] is False and search["playable"] is False


def test_tvshow_folder_blank_title_falls_back(api):
    """Review fix 5: a show without a name is labelled by its channel, else "Unknown" - never " (N)"."""
    (show,) = items.group_shows([_entry(2, "", 200, key="", channel=5), _entry(1, "", 100, key="", channel=5)])
    d = items.tvshow_folder(show, {}, api, {5: "NHK総合"}, "ja", unknown="不明")
    assert d["label"] == "NHK総合 (2)" and d["info"]["title"] == d["info"]["tvshowtitle"] == "NHK総合"
    d = items.tvshow_folder(show, {}, api, {}, "ja", unknown="不明")
    assert d["label"] == "不明 (2)"
    assert items.tvshow_folder(show, {}, api, {}, "en")["label"] == "Unknown (2)"
    assert items.tvshow_folder(show, {}, api, {}, "en", unknown="")["label"] == "Unknown (2)"


def test_movie_entries_find_show_and_label_movies(api):
    """Review fix 9: the Movies filter / labels and the show lookup live in items.py."""
    entries = [_entry(3, "映画B", 300, kind="movie"), _entry(2, "ドラマ", 200), _entry(1, "映画A", 100, kind="movie"),
               _entry(4, "ドラマ", 50)]
    entries[0]["version"] = "吹替版"
    assert [e["id"] for e in items.movie_entries(entries)] == [3, 1]
    assert [e["id"] for e in items.movie_entries(list(reversed(entries)))] == [3, 1]  # newest first
    show = items.find_show(entries, "ドラマ")
    assert show["ids"] == [2, 4]
    assert items.find_show(entries, "映画b") is None and items.find_show(entries, "nope") is None
    dicts = [{"label": "ｘ映画B", "info": {"title": "映画B", "originaltitle": "ｘ映画B"}},
             {"label": "映画A", "info": {"title": "映画A", "originaltitle": "映画A"}}]
    out = items.label_movies(dicts, items.movie_entries(entries), "en")
    assert [d["label"] for d in out] == ["映画B [Dubbed]", "映画A"]
    assert dicts[0]["label"] == "ｘ映画B"  # inputs untouched


# --- episode labels inside a show folder (user request before the 005 merge) --------------------------------------

def _fixture_rec(fixture_set, rid):
    for name in ("recorded.json", "recording.json"):
        for r in json.load(open(os.path.join(FIX, fixture_set, "api", name), encoding="utf-8"))["records"]:
            if r["id"] == rid:
                return r
    pytest.skip(f"no recording {rid} in {fixture_set}")


def _ep_label(api, rec, lang="ja", real_duration=None):
    item = items.recorded_item(rec, api, {}, lang, real_duration=real_duration)
    return items.episode_label(item, epgmeta.metadata(rec), lang)


def _air(rec):
    return items.fmt_time(rec["startAt"], "%Y-%m-%d")


def test_episode_label_with_number(api):
    assert _ep_label(api, _fixture_rec("dev", 13)) == "1x1 仮面屋ペル"
    assert _ep_label(api, _fixture_rec("dev", 8)) == "6x8 傑作選 #9"
    rec = _fixture_rec("dev2", 108)  # ロメリア戦記（第２話）, no subtitle: number + air date
    assert _ep_label(api, rec) == f"1x2 {_air(rec)}"
    rec = _fixture_rec("dev2", 99)  # 相棒 season20 #10: season from the name
    assert _ep_label(api, rec) == f"20x10 {_air(rec)}"
    rec = _synthetic("range-comma", ("dev", 18))  # [字]魔法学園クロニクル (第2期)#5,6
    assert _ep_label(api, rec) == f"2x5-6 {_air(rec)}"


def test_episode_label_without_number(api):
    rec = _fixture_rec("dev", 22)  # subtitle: air date + subtitle
    assert _ep_label(api, rec) == f"{_air(rec)} なぜ農業王国に?食卓を支える大地の原点に迫る"
    rec = _fixture_rec("dev", 23)  # the show is the whole title, only flags remain: air date alone
    assert _ep_label(api, rec) == _air(rec)
    longer = "新プロジェクトＸ～挑戦者たち～シジミが国を動かした　宍道湖を守れ！　後編［字］"
    rec = dict(_fixture_rec("dev", 23), name=longer)
    meta = dict(epgmeta.metadata(_fixture_rec("dev", 23)), subtitle="")  # name longer than the show
    item = items.recorded_item(rec, api, {}, "ja")
    assert items.episode_label(item, meta, "ja") == f"{_air(rec)} 後編[字]"
    rec = _fixture_rec("dev", 6)  # the name is the show plus flags only: air date alone
    assert _ep_label(api, rec) == _air(rec)
    rec = _fixture_rec("dev", 5)  # 天気予報: name == show
    assert _ep_label(api, rec) == _air(rec)


def test_episode_label_keeps_markers_and_version(api):
    rec = dict(_fixture_rec("dev", 13), isRecording=True)  # in progress
    assert _ep_label(api, rec) == "[COLOR red]●[/COLOR] 1x1 仮面屋ペル"
    rec = dict(_fixture_rec("dev", 13), isRecording=False)
    partial = _ep_label(api, rec, real_duration=10)
    assert partial == "1x1 仮面屋ペル [COLOR yellow][Partial][/COLOR]"
    meta = dict(epgmeta.metadata(rec), version="字幕版")
    item = items.recorded_item(rec, api, {}, "ja")
    assert items.episode_label(item, meta, "ja") == "1x1 仮面屋ペル [字幕版]"
    assert items.episode_label(item, meta, "en") == "1x1 仮面屋ペル [Subtitled]"


def test_episode_label_without_metadata(api):
    rec = {"id": 1, "channelId": 1, "startAt": 0, "endAt": 60000, "name": "", "videoFiles": []}
    item = items.recorded_item(rec, api, {}, "ja")
    assert items.episode_label(item, None, "ja") == _air(rec)
    rec = dict(rec, name="ｘｙ")
    assert items.episode_label(items.recorded_item(rec, api, {}, "ja"), None, "ja") == f"{_air(rec)} xy"


@pytest.mark.parametrize("fixture_set", ["dev", "dev2"])
def test_episode_labels_on_every_tv_recording(api, fixture_set):
    entries, records = _entries(fixture_set)
    for e in entries:
        if e["kind"] == "movie":
            continue
        rec = records[e["id"]]
        meta = epgmeta.metadata(rec)
        label = items.episode_label(items.recorded_item(rec, api, {}, "ja"), meta, "ja")
        assert label.strip() and "  " not in label
        if meta["episode"] is not None:
            assert label.replace("[COLOR red]●[/COLOR] ", "").startswith(f"{meta['season'] or 1}x{meta['episode']}")
        else:
            assert label.replace("[COLOR red]●[/COLOR] ", "").startswith(_air(rec))


def test_label_episodes_and_list_category():
    dicts = [{"label": "ｘ", "info": {"originaltitle": "ｘ", "dateadded": "2026-01-02 03:04:05"}}]
    out = items.label_episodes(dicts, [None], "ja")
    assert out[0]["label"] == "2026-01-02 ｘ" and dicts[0]["label"] == "ｘ"
    strings = {"recent": "最近の録画", "search": "検索", "rules": "ルール別"}
    assert items.list_category({}, {}, "ja", strings) == "最近の録画"
    assert items.list_category({"offset": "50"}, {}, "ja", strings) == "最近の録画"
    assert items.list_category({"keyword": "x"}, {}, "ja", strings) == "検索"
    assert items.list_category({"channelId": "5"}, {5: "NHK総合"}, "ja", strings) == "NHK総合"
    assert items.list_category({"channelId": "6"}, {}, "ja", strings) == "6"
    assert items.list_category({"genre": "7"}, {}, "ja", strings) == "アニメ・特撮"
    assert items.list_category({"genre": "7"}, {}, "en", strings) == items.GENRE_EN[7]
    assert items.list_category({"ruleId": "3", "name": "アニメ"}, {}, "ja", strings) == "アニメ"
    assert items.list_category({"ruleId": "3"}, {}, "ja", strings) == "ルール別"


def test_show_title_fallbacks():
    (show,) = items.group_shows([_entry(1, "", 1, key="", channel=5)])
    assert items.show_title(show, {5: "NHK"}) == "NHK"
    assert items.show_title(show, {}, "不明") == "不明"
    assert items.show_title(show, {}) == "Unknown"
    (named,) = items.group_shows([_entry(1, "ＡＢ", 1)])
    assert items.show_title(named, {}) == "AB"


# --- artwork (003) -----------------------------------------------------------------------------------------------

ICON = "/addons/plugin.video.epgstation/resources/icon.png"


def test_newest_thumb():
    assert items.newest_thumb({}) is None
    assert items.newest_thumb({"thumbnails": []}) is None
    assert items.newest_thumb({"thumbnails": [5]}) == 5
    assert items.newest_thumb({"thumbnails": [5, 9]}) == 9
    assert items.newest_thumb({"thumbnails": [9, 5]}) == 9  # order is not specified by the server


def test_logo_art(api):
    assert items.logo_art(5, api, {5}, ICON) == api.logo_url(5)
    assert items.logo_art(5, api, {6}, ICON) == ICON
    assert items.logo_art(5, api, None, ICON) == ICON
    assert items.logo_art(5, api, frozenset(), "") == ""
    auth = EPGStation("http://epgs.test:8888", "u", "p")
    assert items.logo_art(5, auth, {5}, ICON) == auth.logo_url(5) + auth.kodi_headers()


@pytest.mark.parametrize("auth", [False, True])
def test_recording_art_cases(auth):
    api = EPGStation("http://epgs.test:8888", "u" if auth else "", "p")
    h = api.kodi_headers()
    assert bool(h) is auth
    thumb, logo = api.thumbnail_url(9) + h, api.logo_url(5) + h
    good = {"thumb": thumb, "poster": thumb, "fanart": thumb, "landscape": thumb}
    # good (unchecked or checked not blank) thumbnail: thumbnail art, logo icon (else the addon icon)
    assert items.recording_art(9, 5, api, {5}, frozenset(), ICON) == dict(good, icon=logo)
    assert items.recording_art(9, 5, api, set(), frozenset(), ICON) == dict(good, icon=ICON)
    assert items.recording_art(9, 5, api, set(), frozenset(), "") == good
    # blank or no thumbnail: logo, else the addon icon, as thumb and icon; never poster / fanart / landscape
    for tid, blank in ((9, {9}), (None, frozenset())):
        assert items.recording_art(tid, 5, api, {5}, blank, ICON) == {"thumb": logo, "icon": logo}
        assert items.recording_art(tid, 5, api, set(), blank, ICON) == {"thumb": ICON, "icon": ICON}
        assert items.recording_art(tid, 5, api, set(), blank, "") == {}


def test_recorded_item_art(api):
    rec = {"id": 1, "channelId": 5, "startAt": 0, "endAt": 1000, "name": "n", "thumbnails": [7, 12]}
    d = items.recorded_item(rec, api, {}, "ja", logos={5}, fallback_icon=ICON)
    assert d["art"]["thumb"] == d["art"]["fanart"] == d["art"]["poster"] == d["art"]["landscape"] == \
        api.thumbnail_url(12)
    assert d["art"]["icon"] == api.logo_url(5)
    d = items.recorded_item(rec, api, {}, "ja", logos={5}, blank={12}, fallback_icon=ICON)
    assert d["art"] == {"thumb": api.logo_url(5), "icon": api.logo_url(5)}
    d = items.recorded_item(dict(rec, thumbnails=[], isRecording=True), api, {}, "ja", logos=set(),
                            fallback_icon=ICON)
    assert d["art"] == {"thumb": ICON, "icon": ICON}
    assert "/logo" not in json.dumps(items.recorded_item(rec, api, {}, "ja", logos={6})["art"])


def test_group_shows_skips_blank_thumbs_and_candidates():
    entries = [_entry(3, "S", 300, thumb=33, channel=2), _entry(2, "S", 200, thumb=22, channel=1),
               _entry(1, "S", 100, thumb=11, channel=1), _entry(5, "T", 50, thumb=55, channel=4),
               _entry(6, "M", 40, thumb=66, kind="movie")]
    assert items.show_thumb_candidates(entries, frozenset()) == [33, 55]
    assert items.show_thumb_candidates(entries, {33}) == [22, 55]
    shows = items.group_shows(entries, {33, 22, 55})
    assert shows[0]["thumb"]["id"] == 1 and shows[1]["thumb"] is None
    assert items.show_thumb_candidates(entries, {33, 22, 11, 55}) == []
    assert items.group_shows(entries)[0]["thumb"]["id"] == 3  # default: nothing known blank


def test_tvshow_folder_blank_and_logo_rules(api):
    entries = [_entry(2, "S", 200, thumb=22, channel=2), _entry(1, "S", 100, thumb=11, channel=1)]
    (show,) = items.group_shows(entries, {22})
    d = items.tvshow_folder(show, {}, api, {}, "ja", logos={1, 2}, fallback_icon=ICON)
    t = api.thumbnail_url(11)
    assert d["art"] == {"thumb": t, "poster": t, "fanart": t, "landscape": t, "icon": api.logo_url(1)}
    # every episode blank: logo of the newest episode's channel, no poster / fanart
    (show,) = items.group_shows(entries, {22, 11})
    d = items.tvshow_folder(show, {}, api, {}, "ja", logos={1, 2}, fallback_icon=ICON)
    assert d["art"] == {"thumb": api.logo_url(2), "icon": api.logo_url(2)}
    d = items.tvshow_folder(show, {}, api, {}, "ja", logos={1}, fallback_icon=ICON)
    assert d["art"] == {"thumb": ICON, "icon": ICON}
    (show,) = items.group_shows(entries)
    d = items.tvshow_folder(show, {}, api, {}, "ja", logos=None, fallback_icon=ICON)
    assert d["art"]["icon"] == ICON and "/logo" not in json.dumps(d["art"])


def test_channel_folders_logos(api):
    options = {"channels": [{"channelId": 5, "cnt": 3}, {"channelId": 6, "cnt": 1}]}
    out = items.channel_folders(options, {5: "NHK", 6: "X"}, {5}, api)
    assert out[0]["art"] == {"icon": api.logo_url(5), "thumb": api.logo_url(5)}
    assert out[1]["art"] == {"icon": "DefaultStudios.png", "thumb": "DefaultStudios.png"}
    assert [d["label"] for d in out] == ["NHK (3)", "X (1)"]
    assert items.channel_folders(options, {})[0]["art"]["icon"] == "DefaultStudios.png"  # logos unknown


def test_menu_icons():
    strings = {k: k.upper() for k in ("movies", "tvshows", "recent", "recording", "reserves", "live", "search",
                                      "browse", "channels", "genres", "rules")}
    root = items.root_menu(strings)
    icons = [d["art"] for d in root]
    want = ["DefaultMovies.png", "DefaultTVShows.png", "DefaultRecentlyAddedEpisodes.png", "DefaultPVRRecordings.png",
            "DefaultPVRTimers.png", "DefaultPVRChannels.png", "DefaultAddonsSearch.png", "DefaultFolder.png"]
    assert icons == [{"icon": w, "thumb": w} for w in want]
    browse = items.browse_menu(strings)
    assert [d["art"]["icon"] for d in browse] == ["DefaultStudios.png", "DefaultGenre.png",
                                                 "DefaultPVRTimerRules.png"]
    assert all(d["art"]["thumb"] == d["art"]["icon"] for d in browse)
    genres = items.genre_folders([{"id": 1, "genres": [[0, 0]]}, {"id": 2, "genres": [[6, 0], [6, 1]]}])
    assert genres and all(d["art"] == {"icon": "DefaultGenre.png", "thumb": "DefaultGenre.png"} for d in genres)
    rules = items.rule_folders({"rules": [{"id": 1, "searchOption": {"keyword": "k"}}]}, {1: 2})
    assert rules[0]["art"] == {"icon": "DefaultPVRTimerRules.png", "thumb": "DefaultPVRTimerRules.png"}
    assert items.folder("next", action="list", offset=50)["art"] == {}  # 次へ: no art


def test_other_menu_icons(api):
    from resources.lib.epgstation import guide, live, reserves

    assert [d["art"]["icon"] for d in live.type_folders(["GR", "BS", "CS"])] == ["DefaultPVRChannels.png"] * 3
    assert [d["art"]["icon"] for d in guide.menu()] == ["DefaultPVRGuide.png", "DefaultGenre.png"]
    now = 1_760_000_000_000
    assert {d["art"]["icon"] for d in guide.slot_folders(now)} == {"DefaultPVRGuide.png"}
    assert {d["art"]["icon"] for d in guide.genre_folders({0: 3, 6: 2})} == {"DefaultGenre.png"}
    assert {d["art"]["icon"] for d in reserves.type_folders({"conflict": 1, "skip": 2, "overlap": 3})} == \
        {"DefaultPVRTimers.png"}
    buckets = {0: [{"id": 1}], 1: [{"id": 2}]}
    with_logo = guide.day_folders({"id": 5, "name": "N", "hasLogoData": True}, buckets, now, api=api)
    assert {d["art"]["icon"] for d in with_logo} == {api.logo_url(5)}
    assert all(d["art"]["thumb"] == d["art"]["icon"] for d in with_logo)
    no_logo = guide.day_folders({"id": 5, "name": "N", "hasLogoData": False}, buckets, now, api=api)
    assert {d["art"]["icon"] for d in no_logo} == {"DefaultPVRGuide.png"}
    assert {d["art"]["icon"] for d in guide.day_folders({"id": 5, "name": "N", "hasLogoData": True}, buckets,
                                                         now)} == {"DefaultPVRGuide.png"}  # no api: no URL


# --- full detail view (002) ------------------------------------------------------------------------------------------

def _dev2(rec_id):
    return next(r for r in _records("dev2") if r["id"] == rec_id)


DROPS = {"id": 7, "dropCnt": 141, "errorCnt": 1, "scramblingCnt": 0}  # no free recording has drops yet (020 U4)


def test_recorded_item_block_replaces_tech_lines(api):
    rec = dict(_dev2(68), dropLogFile=DROPS)  # dev2 68 ジョン・ウィック２: 5.1ch, videoComponentType 179
    d = items.recorded_item(rec, api, {rec["channelId"]: "ＢＳ１２トゥエルビ"}, "ja")
    plot = d["info"]["plot"]
    block = plot.rsplit("\n\n", 1)[1].split("\n")
    assert block[0] == "[Recording]" and block[1].startswith("Broadcast: ＢＳ１２トゥエルビ ")
    assert block[2:] == ["Genre: 映画 / 邦画", "Video: MPEG-2 1080i 16:9", "Audio: 5.1ch 48kHz",  # BS12 EPG: 邦画
                         f"Files: TS {items.fmt_size(rec['videoFiles'][0]['size'])}",
                         "[COLOR red]Drops 141 / Errors 1 / Scrambled 0[/COLOR]"]
    assert "mpeg2 1080i" not in plot and "(ts):" not in plot
    assert d["audio"] == {"codec": "aac", "channels": 6} and d["stream"]["aspect"] == 1.78


def test_partial_and_protect_lines_once_inside_the_block(api):
    rec = dict(_recorded("dev", 7), isProtected=True)  # 30 min slot
    d = items.recorded_item(rec, api, {}, "ja", {"length": "長さ", "protect_line": "保護中"}, real_duration=290.1)
    plot = d["info"]["plot"]
    block = plot.rsplit("\n\n", 1)[1].split("\n")
    assert block[-2:] == ["長さ: 4:50 / 30:00", "保護中"]
    assert plot.count("長さ: ") == 1 and plot.count("保護中") == 1


def test_droplog_entry_only_when_a_count_is_above_zero(api):
    rec = dict(_dev2(68), dropLogFile=DROPS)
    d = items.recorded_item(rec, api, {}, "ja", {"droplog": "ドロップログ"})
    assert d["context"][-1] == ("ドロップログ", "plugin://plugin.video.epgstation/?action=droplog&id=68&log=7")
    errors_only = _dev2(125)  # real dropLogFile 2, errorCnt 1: also offered (decision 2a: any count)
    assert items.recorded_item(errors_only, api, {}, "ja")["context"][-1] == (
        "Drop log", "plugin://plugin.video.epgstation/?action=droplog&id=125&log=2")
    assert not any("droplog" in c[1] for c in items.recorded_item(
        dict(rec, dropLogFile=dict(DROPS, dropCnt=0, errorCnt=0)), api, {}, "ja")["context"])  # all zero: none
    for rec in _records("dev") + _records("dev2"):
        log = rec.get("dropLogFile")
        offered = any("action=droplog" in c[1] for c in items.recorded_item(rec, api, {}, "ja")["context"])
        assert offered == bool(log and (log["dropCnt"] or log["errorCnt"] or log["scramblingCnt"])), rec["id"]


def test_recorded_item_makes_no_api_request():
    """Building the items (block, audio, entry) needs nothing beyond the RecordedItem: no request, only URLs."""
    calls = []

    class Recorder(EPGStation):
        def _request(self, *a, **kw):
            calls.append(a)
            raise AssertionError("no request while listing")

    api = Recorder("http://epgs.test:8888")
    for rec in _records("dev") + _records("dev2"):
        items.recorded_item(rec, api, {}, "ja")
    assert calls == []


# --- subgenres (009) ------------------------------------------------------------------------------------------------

def _g(i, start, *pairs, rec=False):
    return {"id": i, "startAt": start, "genres": [list(p) for p in pairs], "rec": rec}


SUB_ENTRIES = [
    _g(1, 10, (6, 0)),
    _g(2, 30, (6, 0), (6, 15)),  # two subgenres of one genre in one recording: once per pair, once for the genre
    _g(3, 20, (6, 1), (7, 0)),  # genre2-only style: genre_pairs already dropped the empty genre1
    _g(4, 40, (4, 4), rec=True),  # in progress
    _g(5, 50, (4, 4)),
    _g(6, 60, (11, 5), (11, 6)),
    _g(7, 5, (8, None)),
    _g(8, 6),
]


def test_genre_and_subgenre_counts():
    assert items.genre_counts(SUB_ENTRIES) == {6: 3, 7: 1, 4: 2, 11: 1, 8: 1}
    assert items.subgenre_counts(SUB_ENTRIES, 6) == {0: 2, 15: 1, 1: 1}
    assert items.subgenre_counts(SUB_ENTRIES, 4) == {4: 2}
    assert items.subgenre_counts(SUB_ENTRIES, 8) == {None: 1}
    assert items.subgenre_counts(SUB_ENTRIES, 3) == {}
    assert items.genre_counts([{"id": 1}]) == {}  # an entry without "genres"


def test_genre_folders_order_labels_and_single_subgenre_shortcut():
    ja = items.genre_folders(SUB_ENTRIES, "ja")
    assert [d["label"] for d in ja] == ["音楽 (2)", "映画 (3)", "アニメ・特撮 (1)", "ドキュメンタリー・教養 (1)",
                                        "福祉 (1)"]
    paths = [d["path"] for d in ja]
    assert paths[0] == items.plugin_url(action="list", genre=4)  # one subgenre only (D3): the list directly
    assert paths[1] == items.plugin_url(action="genre", genre=6)
    assert paths[2] == items.plugin_url(action="list", genre=7)
    assert paths[3] == items.plugin_url(action="list", genre=8)
    assert paths[4] == items.plugin_url(action="genre", genre=11)
    assert all(d["folder"] for d in ja)
    en = items.genre_folders(SUB_ENTRIES, "en")
    assert [d["label"] for d in en] == ["Music (2)", "Movies (3)", "Anime / Tokusatsu (1)", "Documentary (1)",
                                        "Welfare (1)"]
    assert items.genre_folders([], "ja") == []


def test_genre_folders_ignore_a_missing_subgenre():
    """(G, None) never makes a subgenre level of its own, and stays in the genre's list / すべて."""
    one = [_g(1, 1, (6, 0)), _g(2, 2, (6, None))]
    assert items.genre_folders(one)[0]["path"] == items.plugin_url(action="list", genre=6)
    two = one + [_g(3, 3, (6, 1))]
    assert items.genre_folders(two)[0]["path"] == items.plugin_url(action="genre", genre=6)
    subs = items.subgenre_folders(6, two, "ja", "すべて ({0})")
    assert [d["label"] for d in subs] == ["すべて (3)", "洋画 (1)", "邦画 (1)"]
    assert subs[0]["path"] == items.plugin_url(action="list", genre=6)


def test_subgenre_folders():
    out = items.subgenre_folders(6, SUB_ENTRIES + [_g(9, 1, (6, 9))], "ja", "すべて ({0})")
    assert [d["label"] for d in out] == ["すべて (4)", "洋画 (2)", "邦画 (1)", "#9 (1)", "その他 (1)"]
    assert out[0]["path"] == items.plugin_url(action="list", genre=6)
    assert out[1]["path"] == items.plugin_url(action="list", genre=6, subGenre=0)
    assert out[-1]["path"] == items.plugin_url(action="list", genre=6, subGenre=15)
    en = items.subgenre_folders(11, SUB_ENTRIES, "en")
    assert [d["label"] for d in en] == ["All (1)", "Text (subtitles) (1)",
                                        "Explanation on sound multiplex broadcast (1)"]
    assert [d["label"] for d in items.subgenre_folders(8, SUB_ENTRIES, "ja", "すべて ({0})")] == ["すべて (1)"]


def test_subgenre_entries_newest_first():
    assert items.subgenre_entries(SUB_ENTRIES, 6, 0) == [2, 1]
    assert items.subgenre_entries(SUB_ENTRIES, 4, 4) == [5, 4]
    assert items.subgenre_entries(SUB_ENTRIES, 8, None) == [7]
    assert items.subgenre_entries(SUB_ENTRIES, 6, 2) == []


def test_list_category_subgenre():
    s = {"recent": "最近の録画", "search": "検索", "rules": "ルール別"}
    assert items.list_category({"genre": "11", "subGenre": "5"}, {}, "ja", s) == "福祉 / 文字(字幕)"
    assert items.list_category({"genre": "11", "subGenre": "5"}, {}, "en", s) == "Welfare / Text (subtitles)"
    assert items.list_category({"genre": "6", "subGenre": "9"}, {}, "ja", s) == "映画 / #9"
    assert items.list_category({"genre": "6", "subGenre": "x"}, {}, "ja", s) == "映画 / #x"
    assert items.list_category({"genre": "99"}, {}, "ja", s) == "99"
    assert items.list_category({"genre": "x"}, {}, "ja", s) == "x"


# --- transcoded playback (011) ----------------------------------------------------------------------------------

STREAM_STRINGS = {"stream": "変換して再生", "choose": "再生するファイルを選択", "stop": "録画を停止", "protect": "保護",
                  "delete": "削除"}


def _actions(d):
    """"action" (or "action:op") of each context entry."""
    import urllib.parse

    out = []
    for _, url in d["context"]:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        out.append(query["action"][0] + (":" + query["op"][0] if "op" in query else ""))
    return out


def test_stream_entry_after_choose_before_writes(api):
    multi = _recorded("dev", 4)
    assert len(multi["videoFiles"]) > 1
    d = items.recorded_item(multi, api, {}, "ja", STREAM_STRINGS)
    acts = _actions(d)
    assert acts[:2] == ["choose", "stream"] and acts.count("stream") == 1
    assert d["context"][1] == ("変換して再生", f"plugin://plugin.video.epgstation/?action=stream&id={multi['id']}")
    single = dict(multi, videoFiles=multi["videoFiles"][:1])
    assert _actions(items.recorded_item(single, api, {}, "ja", STREAM_STRINGS))[0] == "stream"


def test_stream_entry_before_stop_while_recording(api):
    rec = dict(_recorded("dev", 4), isRecording=True)
    acts = _actions(items.recorded_item(rec, api, {}, "ja", STREAM_STRINGS))
    assert acts.index("stream") < acts.index("reserve_action:stop")


def test_stream_entry_absent_without_files_or_string(api):
    rec = _recorded("dev", 4)
    assert "stream" not in _actions(items.recorded_item(dict(rec, videoFiles=[]), api, {}, "ja", STREAM_STRINGS))
    assert "stream" not in _actions(items.recorded_item(rec, api, {}, "ja"))
    assert "stream" not in _actions(items.recorded_item(rec, api, {}, "ja", dict(STREAM_STRINGS, stream="")))
