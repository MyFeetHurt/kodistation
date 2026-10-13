"""Full EPG detail view (backlog 002): details.py, pure."""
import json
import os

import pytest
from resources.lib.epgstation import details, items, recorded
from resources.lib.epgstation.api import EPGStation
from resources.lib.epgstation.genres import item_genres

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
JA = {"detail_header": "【録画情報】", "detail_broadcast": "放送: {0}", "detail_genre": "ジャンル: {0}",
      "detail_video": "映像: {0}", "detail_audio": "音声: {0}",
      "detail_drops": "ドロップ {0} / エラー {1} / スクランブル {2}", "detail_encoding": "エンコード中",
      "droplog": "ドロップログ", "files": "ファイル: {0}", "weekdays": "日,月,火,水,木,金,土",
      "length": "長さ", "protect_line": "保護中 (自動削除の対象外)"}


def _records(name):
    path = os.path.join(FIX, name, "api", "recorded.json")
    if not os.path.isfile(path):
        pytest.skip(f"no {name} fixtures")
    return json.load(open(path, encoding="utf-8"))["records"]


def _droplog(log_id):
    """A real drop log of the dev2 set (free channels; 2 = recording 125, errors only)."""
    with open(os.path.join(FIX, "dev2", "api", "dropLogs", f"{log_id}.txt"), encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("mode,ja,en", [(1, "モノラル", "Mono"), (2, "デュアルモノ", "Dual mono"),
                                        (3, "ステレオ", "Stereo"), (9, "5.1ch", "5.1ch"),
                                        (7, "3/1モード", "3/1 mode"), (0, "", ""), (18, "", ""), (None, "", ""),
                                        ("x", "", "")])
def test_audio_label(mode, ja, en):
    rec = {} if mode is None else {"audioComponentType": mode}
    assert details.audio_label(rec, "ja") == ja
    assert details.audio_label(rec, "en") == en


@pytest.mark.parametrize("mode,channels", [(1, 1), (2, 2), (3, 2), (4, 3), (5, 3), (6, 4), (7, 4), (8, 5), (9, 6),
                                           (10, 7), (12, 8), (13, 8), (17, 24), (11, None), (0, None), (None, None)])
def test_audio_stream_channels(mode, channels):
    stream = details.audio_stream({"audioComponentType": mode})
    assert stream == ({"codec": "aac", "channels": channels} if channels else None)


def test_sampling_label():
    assert details.sampling_label({"audioSamplingRate": 48000}) == "48kHz"
    assert details.sampling_label({"audioSamplingRate": 44100}) == "44.1kHz"
    assert details.sampling_label({"audioSamplingRate": 22050}) == "22.05kHz"
    assert details.sampling_label({}) == ""


@pytest.mark.parametrize("vct,aspect,label", [(0xB1, 1.33, "MPEG-2 1080i 4:3"), (0xB3, 1.78, "MPEG-2 1080i 16:9"),
                                              (0xB2, 1.78, "MPEG-2 1080i 16:9"), (0xB4, 2.21, "MPEG-2 1080i >16:9"),
                                              (None, 0, "MPEG-2 1080i")])
def test_aspect_and_video_label(vct, aspect, label):
    rec = {"videoType": "mpeg2", "videoResolution": "1080i"}
    if vct is not None:
        rec["videoComponentType"] = vct
    assert details.aspect(rec) == aspect
    assert details.video_label(rec) == label
    assert details.video_label({}) == "" and details.aspect({}) == 0


def _rec(**kw):
    rec = {"id": 1, "channelId": 5, "startAt": 1760311500000, "endAt": 1760312100000, "name": "n",
           "genre1": 7, "subGenre1": 0, "genre2": 10, "subGenre2": 8,
           "videoType": "mpeg2", "videoResolution": "1080i", "videoComponentType": 179,
           "audioComponentType": 3, "audioSamplingRate": 48000,
           "videoFiles": [{"id": 9, "name": "TS", "type": "ts", "size": 2048}]}
    rec.update(kw)
    return rec


def test_detail_lines_order_and_labels():
    rec = _rec(dropLogFile={"id": 3, "dropCnt": 0, "errorCnt": 1, "scramblingCnt": 0}, isEncoding=True)
    lines = details.detail_lines(rec, "NHK Eテレ1", item_genres(rec), "ja", JA, ["長さ: 4:50 / 10:00", "保護中"])
    assert lines == ["【録画情報】", f"放送: NHK Eテレ1 {recorded.time_line(rec, JA)}",
                     "ジャンル: アニメ・特撮 / 国内アニメ, 趣味・教育 / 幼児・小学生", "映像: MPEG-2 1080i 16:9",
                     "音声: ステレオ 48kHz", "ファイル: TS 2.0 KB", "ドロップ 0 / エラー 1 / スクランブル 0",
                     "エンコード中", "長さ: 4:50 / 10:00", "保護中"]
    assert lines[1].split(" ")[-2].endswith(")") and "(" in lines[1]  # "10/13(火) 08:25-08:35"


def test_detail_lines_omit_lines_without_data():
    rec = {"id": 1, "channelId": 5, "startAt": 0, "endAt": 60000, "name": "n"}
    assert details.detail_lines(rec, "ch", [], "ja", JA) == ["【録画情報】", f"放送: ch {recorded.time_line(rec, JA)}"]


def test_detail_lines_red_only_for_drops_or_scrambling():
    def drops(**counts):
        log = dict({"id": 1, "dropCnt": 0, "errorCnt": 0, "scramblingCnt": 0}, **counts)
        return [ln for ln in details.detail_lines(_rec(dropLogFile=log), "c", [], "ja", JA) if "ドロップ" in ln][0]
    assert drops() == "ドロップ 0 / エラー 0 / スクランブル 0"
    assert drops(errorCnt=1) == "ドロップ 0 / エラー 1 / スクランブル 0"
    assert drops(dropCnt=141, errorCnt=1) == "[COLOR red]ドロップ 141 / エラー 1 / スクランブル 0[/COLOR]"
    assert drops(scramblingCnt=5) == "[COLOR red]ドロップ 0 / エラー 0 / スクランブル 5[/COLOR]"


def test_detail_lines_japanese_kodi_with_english_genres():
    """Kodi in Japanese, genre_language = en: Japanese labels (strings.po), English genre names and audio label."""
    rec = _rec()
    lines = details.detail_lines(rec, "NHK", item_genres(rec, "en"), "en", JA)
    assert lines[0] == "【録画情報】" and lines[1] == f"放送: NHK {recorded.time_line(rec, JA)}" and "(" in lines[1]
    # subgenres (009): English ARIB subgenre names, joined with "; " (the names contain ", ")
    assert lines[2:] == ["ジャンル: Anime / Tokusatsu / Japanese animation; Hobby / Education / Little children, "
                         "schoolchildren", "映像: MPEG-2 1080i 16:9", "音声: Stereo 48kHz", "ファイル: TS 2.0 KB"]
    # and through recorded_item: the 長さ / 保護 lines stay Japanese too
    d = items.recorded_item(dict(rec, isProtected=True), EPGStation("http://epgs.test:8888"), {5: "NHK"}, "en", JA,
                            real_duration=60.0)
    block = d["info"]["plot"].split("\n\n")[-1].split("\n")
    assert block[0] == "【録画情報】" and block[2].startswith("ジャンル: Anime") and block[-2:] == [
        "長さ: 1:00 / 10:00", "保護中 (自動削除の対象外)"]


def test_has_drop_problem_decision_2a():
    log = {"id": 7, "dropCnt": 0, "errorCnt": 0, "scramblingCnt": 0}
    assert not details.has_drop_problem({})
    assert not details.has_drop_problem({"dropLogFile": log})
    for key in ("dropCnt", "errorCnt", "scramblingCnt"):
        assert details.has_drop_problem({"dropLogFile": dict(log, **{key: 1})})


def test_droplog_text_summary_first():
    text = _droplog(2)  # the event line comes first in the file, the per-PID summary after it
    assert text.startswith("error: (pid: 0x16FC")
    out = details.droplog_text(text).split("\n")
    summary = [ln for ln in text.split("\n") if ln.startswith("pid:")]
    assert len(summary) > 20 and out[:len(summary)] == summary and out[len(summary)] == ""
    assert out[len(summary) + 1].startswith("error: (pid: 0x16FC") and out[-1] == out[len(summary) + 1]
    assert len(out) == len([ln for ln in text.split("\n") if ln]) + 1
    events = "error: (pid: 0x1410, counter: 3, expected: 2)\r\ndrop (pid: 0x0000, counter: 1)\r\n\r\n"
    out = details.droplog_text(events + "pid: 0x0000, error: 0, drop: 2, name: PAT\r\n").split("\n")
    assert out == ["pid: 0x0000, error: 0, drop: 2, name: PAT", "", "error: (pid: 0x1410, counter: 3, expected: 2)",
                   "drop (pid: 0x0000, counter: 1)"]


def test_droplog_text_without_summary():
    assert details.droplog_text("drop (pid: 0x0100)\r\n\r\n") == "drop (pid: 0x0100)"
    assert details.droplog_text("") == ""
    assert details.droplog_text("pid: 0x0000, drop: 0\n") == "pid: 0x0000, drop: 0"


@pytest.mark.parametrize("fixture_set", ["dev", "dev2"])
def test_every_fixture_record_has_a_sane_block(fixture_set):
    for rec in _records(fixture_set):
        genres = item_genres(rec)
        lines = details.detail_lines(rec, "ch", genres, "ja", JA)
        assert lines[0] == "【録画情報】" and lines[1].startswith("放送: ch ")
        prefixes = [ln.split(":")[0].split(" ")[0] for ln in lines[2:]]
        order = ["ジャンル", "映像", "音声", "ファイル", "ドロップ", "[COLOR"]
        assert [order.index(p) for p in prefixes] == sorted(order.index(p) for p in prefixes)
        assert ("映像: MPEG-2 1080i 16:9" in lines) == (rec.get("videoComponentType") == 179)
        assert any(ln.startswith("音声: ") for ln in lines) == (rec.get("audioComponentType") is not None)
        d = items.recorded_item(rec, EPGStation("http://epgs.test:8888"), {rec["channelId"]: "ch"}, "ja", JA)
        assert d["info"]["plot"].endswith("\n".join(lines))
        if rec.get("audioComponentType") == 3:
            assert "音声: ステレオ 48kHz" in lines and d["audio"] == {"codec": "aac", "channels": 2}
        if rec.get("audioComponentType") == 9:
            assert "音声: 5.1ch 48kHz" in lines and d["audio"] == {"codec": "aac", "channels": 6}
        if rec.get("audioComponentType") is None:
            assert d["audio"] is None


def test_detail_genre_separator_follows_genre_language():  # subgenres (009)
    rec = _rec()
    ja = details.detail_lines(rec, "NHK", item_genres(rec, "ja"), "ja", JA)[2]
    assert ja == "ジャンル: アニメ・特撮 / 国内アニメ, 趣味・教育 / 幼児・小学生"
    en = details.detail_lines(rec, "NHK", ["Movies / Overseas movies", "Information / Gourmet, cooking"], "en", JA)[2]
    assert en == "ジャンル: Movies / Overseas movies; Information / Gourmet, cooking"
    one = details.detail_lines(rec, "NHK", ["Movies / Overseas movies"], "en", JA)[2]
    assert one == "ジャンル: Movies / Overseas movies"
