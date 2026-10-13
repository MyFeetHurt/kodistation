"""epgmeta.py: the EPG metadata parser against its expected outputs and pinned regex rewrites."""
import json
import os
import subprocess
import sys

import epgmeta_expected
import pytest
from resources.lib.epgstation import epgmeta


def test_every_case_matches_expected_output():
    """Every EPG text (dev + dev2 recordings, synthetic texts, 4 days of free dev guide) gives the expected parse.

    On a deliberate parser change: uv run python tools/epgmeta_expected.py --write, and review the diff.
    """
    with open(epgmeta_expected.EXPECTED, encoding="utf-8") as f:
        expected = {c["id"]: c for c in json.load(f)}
    cases = epgmeta_expected.inputs()
    assert {c[0] for c in cases} == set(expected), "case list changed: rewrite the expected file"
    mismatches = []
    for case in cases:
        got = json.loads(epgmeta_expected.dumps(epgmeta_expected.build_case(*case)))
        want = expected[case[0]]
        for field in sorted(set(got) | set(want)):
            if got.get(field) != want.get(field):
                mismatches.append("{} {} ({})\n  got:  {}\n  want: {}".format(
                    case[0], field, case[2].get("name"), json.dumps(got.get(field), ensure_ascii=False)[:300],
                    json.dumps(want.get(field), ensure_ascii=False)[:300]))
    assert not mismatches, f"{len(mismatches)} mismatches:\n" + "\n".join(mismatches[:20])
    assert len(cases) > 2000


ADDON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin.video.epgstation")


def _python(code, *flags):
    return subprocess.run([sys.executable, *flags, "-c", code], cwd=ADDON, capture_output=True, text=True)


def test_regexes_compile_without_warnings():
    """No FutureWarning (nested sets) or other warning from any pattern: import in a fresh interpreter with -W error
    (in-process, re's cache would hide the warning)."""
    assert _python("import re; re.compile('[[a]')", "-W", "error").returncode != 0  # the check can fail
    result = _python("from resources.lib.epgstation import epgmeta", "-W", "error")
    assert result.returncode == 0, result.stderr


def _show(name, genres=()):
    d = epgmeta.parse_title(name, genres)
    return d["show"], d["subtitle"]


# Expected values: the JS parse() of the same names (run once while writing these tests).
@pytest.mark.parametrize("name, show, subtitle", [
    # CUT (?<!の|^[^\s「]{1,2})「
    ("舞台「桃源暗鬼」", "舞台「桃源暗鬼」", ""),
    ("みんなの「うた」", "みんなの「うた」", ""),
    # CUT (?<=^[^\s!?]*[!?]+)(?![!?])(?=...)
    ("ラヴィット！生バンドで熱唱できるのは誰？カラオケ王決定戦[字]", "ラヴィット!",
     "生バンドで熱唱できるのは誰?カラオケ王決定戦"),
    ("ショー！あいうえお…まさか", "ショー!", "あいうえお...まさか"),
    ("アニメ！？／特集", "!?/特集", ""),
    # CUT (?<=[^\s!?/][!?]+)\/(?!\/)
    ("なないろ日和！／都議会", "なないろ日和!", "都議会"),
    ("a!!/b", "a!!", "b"),
    ("Hey!!//x", "Hey!!//x", ""),
    ("!/x", "!/x", ""),
    # CUT (?<=^(?!(?:Mrs|Miss|Prof|Vol)\.)[A-Za-z]{3,}\.)(?=...)
    ("ＤａｙＤａｙ．カスハラ対策", "DayDay.", "カスハラ対策"),
    ("ＡＢＣ．カスハラ", "ABC.", "カスハラ"),
    ("Ｍｒ．サンデー", "Mr.サンデー", ""),
    ("Ｍｒｓ．サンデー", "Mrs.サンデー", ""),
    ("Ａｂ．カスハラ", "Ab.カスハラ", ""),
    ("Ａｂｃ．ｄｅｆ", "Abc.def", ""),
    # CUT (?<=^[^~]*[!?])~
    ("Show!~tagline~", "Show!", "tagline"),
    ("ショー！〜今夜のテーマ〜", "ショー!", "今夜のテーマ"),
    ("ニュース～今日の話題～", "ニュース~今日の話題~", ""),
    # COLON (?<!^[A-Za-z]{1,3}):
    ("Ｒｅ：ゼロから始める異世界生活", "Re:ゼロから始める異世界生活", ""),
    ("ＡＢＣ：特集", "ABC:特集", ""),
    ("ＡＢＣＤ：特集", "ABCD", "特集"),
    ("メーデー！１３：航空機事故", "メーデー!13", "航空機事故"),
])
def test_lookbehind_rewrites(name, show, subtitle):
    assert _show(name) == (show, subtitle)


def test_astral_characters_count_as_one_character():
    """Intentional divergence: JS regexes without "u" count UTF-16 units, so in epgXport "𠮷𠮷" is 4 characters and
    "𠮷𠮷「タイトル」" is cut at 「 (show 𠮷𠮷, subtitle タイトル). Python counts code points, so 𠮷𠮷 is a 2-character
    word like 吉吉 and the title is kept whole, as the CUT rule intends (舞台「桃源暗鬼」)."""
    assert _show("𠮷𠮷「タイトル」") == ("𠮷𠮷「タイトル」", "")
    assert _show("吉吉「タイトル」") == ("吉吉「タイトル」", "")


def test_blurb_after_episode_dropped():
    d = epgmeta.parse_title("サザエさん　＃２０７　　時代を超えて愛される", [])
    assert (d["show"], d["episode"], d["subtitle"]) == ("サザエさん", 207, "")


def _fullwidth(fixture_set, rec_id):
    return _fullwidth_all(fixture_set)[str(rec_id)]


def _synthetic(key):
    """A hand-written EPG text of tests/fixtures/synthetic/epg_texts.json (shapes the free channels lack)."""
    with open("tests/fixtures/synthetic/epg_texts.json", encoding="utf-8") as f:
        return next(i for i in json.load(f)["items"] if i["key"] == key)


def test_half_width_input_loses_credit_separators():
    """Documented limitation: the parser expects full-width text (isHalfWidth=false), like epgXport.

    Half-width turns ／ into /, so "岩城鉄平/小川信吾/北村さやの" stays one name (synthetic dub-japanese).
    """
    from resources.lib.epgstation.items import to_half

    rec = _synthetic("dub-japanese")
    full = epgmeta.parse_extended(rec["description"], rec["extended"])["people"]
    assert ("actor", ("岩城鉄平", "声の出演")) in full and len(full) == 7
    half = epgmeta.parse_extended(to_half(rec["description"]), to_half(rec["extended"]))["people"]
    assert ("actor", ("岩城鉄平/小川信吾/北村さやの", "声の出演")) in half
    assert len(half) == 2  # the 3 actors are lost too (one name > 20 chars)


def test_metadata_movie_with_year_from_epg_text():
    m = epgmeta.metadata(_fullwidth("dev", 20))  # 映画『必殺仕掛人 梅安蟻地獄』: year, director, cast from the text
    assert (m["kind"], m["year"]) == ("movie", 1973)
    assert m["directors"] == ["渡邊祐介"]
    assert m["cast"][0] == ("緒形 拳", "藤枝梅安")
    m = epgmeta.metadata(_synthetic("edition-pg"))  # [2K][映]北の灯台守<4Kデジタルリマスター版><PG-12>
    assert (m["kind"], m["show"], m["year"]) == ("movie", "北の灯台守", 1981)
    assert m["directors"] == ["佐伯隆一"]
    assert m["tags"] == ["4Kデジタルリマスター版"]
    assert m["cast"][0] == ("川端伸介", "出演")


def test_metadata_dubbed_movie():
    m = epgmeta.metadata(_synthetic("dub-movie"))  # [映]ネオン・ハーバー（吹替版）[字]
    assert (m["kind"], m["show"], m["version"]) == ("movie", "ネオン・ハーバー", "吹替版")
    assert m["tags"] == ["吹替版", "字幕"]
    assert m["writers"] == ["リディア・モーガン", "ケネス・ブライト"]
    assert m["year"] == 1994  # "（１９９４年　アメリカ）": not found by epgXport's MOVIE_YEAR
    # dubbed: "マーカス・ベルウッド（森川陽太）" is the actor and his Japanese voice (epgXport: one name)
    assert m["cast"][:2] == [("マーカス・ベルウッド", "吹替: 森川陽太"), ("ジェーン・カーステアズ", "吹替: 香坂みのり")]


@pytest.mark.parametrize("line, person", [
    ("【出演】マーカス・ベルウッド（森川陽太）", ("マーカス・ベルウッド", "吹替: 森川陽太")),
    ("【キャスト】マーカス・ベルウッド（森川陽太）", ("マーカス・ベルウッド", "吹替: 森川陽太")),
    ("【出演】マーカス・ベルウッド（声：森川陽太）", ("マーカス・ベルウッド", "吹替: 森川陽太")),
    ("【出演】マーカス・ベルウッド（吹替：森川陽太）", ("マーカス・ベルウッド", "吹替: 森川陽太")),
    ("【出演】マーカス・ベルウッド（声）", ("マーカス・ベルウッド(声)", "出演")),  # no voice actor named
    ("【出演】マーカス・ベルウッド（吹替）", ("マーカス・ベルウッド(吹替)", "出演")),
    # another dub
    ("【出演】マーカス・ベルウッド（大槻健二／ソフト版）", ("マーカス・ベルウッド(大槻健二/ソフト版)", "出演")),
    ("【解説】淀川長治（映画評論家）", ("淀川長治(映画評論家)", "解説")),  # not an on-screen cast label
    ("【ナレーション】森川智之（俳優）", ("森川智之(俳優)", "ナレーション")),
    ("【司会】山田太郎（アナウンサー）", ("山田太郎(アナウンサー)", "司会")),
])
def test_dub_voice_split_only_for_cast_entries(line, person):
    people = epgmeta.parse_extended("", "◇出演者\n" + line, dubbed=True)["people"]
    assert people == [("actor", person)]


def test_dub_voice_only_in_dubbed_broadcasts():
    ext = "◇出演者\n【出演】マーカス・ベルウッド（森川陽太）\n【声】高山みなみ（江戸川コナン）"
    assert epgmeta.parse_extended("", ext, dubbed=True)["people"] == [
        ("actor", ("マーカス・ベルウッド", "吹替: 森川陽太")), ("actor", ("高山みなみ", "江戸川コナン"))]
    # not dubbed: "(…)" is an affiliation or a reading and stays in the name, as in epgXport
    ext = "◇出演者\n【出演】佐久間大介（Ｓｎｏｗ　Ｍａｎ）"
    assert epgmeta.parse_extended("", ext)["people"] == [("actor", ("佐久間大介(Snow Man)", "出演"))]
    assert epgmeta.is_dubbed("[映]ネオン・ハーバー（吹替版）[字]") and epgmeta.is_dubbed("SUITS #4[吹]")
    assert not epgmeta.is_dubbed("[映]ネオン・ハーバー（字幕版）")
    # an episode of a dubbed series (dev guide: [SS]SUITS/スーツ シーズン7 #4[吹])
    m = epgmeta.metadata({"name": "ＳＵＩＴＳ　＃４［吹］",
                          "extended": "◇出演者\n【出演】ガブリエル・マクト（桐本拓哉）"})
    assert m["cast"] == [("ガブリエル・マクト", "吹替: 桐本拓哉")]


@pytest.mark.parametrize("text, year", [
    ("（１９９５年　アメリカ）", "1995"),
    ("(1999年:アメリカ)", "1999"),
    ("(2025年 アイルランド/イギリス/カナダ/韓国/アメリカ)", "2025"),
    ("(1996年・アメリカ・139分・カラー)", "1996"),
    ("(2013年)", "2013"),
    ("(2018年制作)", "2018"),
    ("(1983年 141分)", "1983"),  # epgXport's own rule
    ("(2016年6月3日 東京国際フォーラム)", None),  # a date
    ("(2022年初回放送)", None),
    ("1999年大晦日目前のNY", None),
    ("（２０２５年　アメリカ）（２００８）", "2025"),  # year + country before a bare year
    ("【原作】三島由紀夫（１９６８年　文藝春秋刊）", None),  # publication year
    ("【原作】三島由紀夫（１９６８年　文藝春秋刊）（１９７０年　日本）", "1970"),
])
def test_movie_year(text, year):
    assert epgmeta.movie_year("あらすじ " + text, "") == year


def test_movie_year_not_an_award_year():
    ext = "◇出演／関連情報\n【受賞】\n第８９回（２０１７年）アカデミー賞\n\n◇ \n（２０１６年　アメリカ）"
    assert epgmeta.movie_year("", ext) == "2016"
    assert epgmeta.movie_year("", "【受賞】第８９回（２０１７年）アカデミー賞") is None
    assert epgmeta.movie_year("", "カンヌ（２０１３年）") == "2013"  # a bare year, nothing better


def test_metadata_episode_range():
    m = epgmeta.metadata(_synthetic("range"))  # 放課後のグループ課題 #1-5(全10話)
    assert (m["kind"], m["episode"], m["episode_end"]) == ("tv", 1, 5)
    assert m["show"] == "放課後のグループ課題"
    assert m["cast"][0] == ("藤原陽太", "出演")
    assert m["year"] is None


def test_rule_directory_prefix_splits_show_and_subtitle():
    """A name that starts with its rule's directory (no episode number): the rest is the subtitle (020: was prod's
    rule directory; synthetic names)."""
    d = epgmeta.parse_title("サンプル劇場港の朝[字]", [(3, 0)], "サンプル劇場")
    assert (d["show"], d["subtitle"], d["episode"]) == ("サンプル劇場", "港の朝", None)
    assert epgmeta.parse_title("サンプル劇場港の朝[字]", [(3, 0)])["show"] == "サンプル劇場港の朝"
    d = epgmeta.parse_title("ニュースサンプル　港の朝", [(3, 0)], "ニュース")
    assert (d["show"], d["subtitle"]) == ("ニュース", "サンプル 港の朝")


def test_bang_question_tilde_cuts_the_title():
    """JS (?<=^[^~]*[!?])~: the first "~" right after "!" or "?" ends the show (synthetic bang-tilde)."""
    m = epgmeta.metadata(_synthetic("bang-tilde"))  # なぜ光る!?~深海サンプル生物の秘密~
    assert (m["show"], m["subtitle"]) == ("なぜ光る!?", "深海サンプル生物の秘密")
    assert epgmeta.parse_title("なぜ光る~深海の秘密~")["show"] != "なぜ光る!?"


def test_metadata_blank_name_or_parser_error_is_none(monkeypatch):
    assert epgmeta.metadata({"name": "　"}) is None
    assert epgmeta.metadata({}) is None

    def broken(*args):
        raise IndexError("boom")

    monkeypatch.setattr(epgmeta, "parse_title", broken)
    assert epgmeta.metadata({"name": "x"}) is None
    errors = []
    assert epgmeta.metadata({"name": "x"}, on_error=errors.append) is None
    assert [type(e) for e in errors] == [IndexError]  # 015b logs it with the recording id
    assert epgmeta.metadata({"name": " "}, on_error=errors.append) is None and len(errors) == 1  # blank: no error


def test_metadata_for_every_captured_item():
    """No captured name makes the parser fail (a failure would silently drop the item's metadata)."""
    items = list(_fullwidth_all("dev").values()) + list(_fullwidth_all("dev2").values())
    with open("tests/fixtures/synthetic/epg_texts.json", encoding="utf-8") as f:
        items += json.load(f)["items"]
    with open("tests/fixtures/dev/schedule_texts.json", encoding="utf-8") as f:
        items += json.load(f)

    def fail(exc):
        raise exc

    count = 0
    for item in items:
        if epgmeta.norm(item.get("name")):
            assert epgmeta.metadata(item, on_error=fail) is not None, item["name"]
            count += 1
    assert count > 2000


@pytest.mark.parametrize("name, genres, kind", [
    ("BS10プレミアムNAVI ミニ", [(6, 15)], "tv"),  # 映画/その他 alone: a channel promo, not a film (BS10 guide)
    ("[映]ネオン・ハーバー（吹替版）", [(6, 0), (6, 15)], "movie"),  # another 映画 subgenre
    ("【ＫＮＴＶ】［映画］チャンス商会～初恋を探して～", [(6, 15)], "movie"),  # [映画] in the name
    ("トム・クルーズ：ライツ・カメラ・アクション", [(6, 15)], "tv"),
    ("ネオン・ハーバー", [(6, 0)], "movie"),
])
def test_movie_kind_needs_a_film_genre_or_flag(name, genres, kind):
    assert epgmeta.parse_title(name, genres)["kind"] == kind


@pytest.mark.parametrize("name, season", [
    ("孤独のグルメ　傑作選　Ｓｅａｓｏｎ６　＃８　＃９", 6),
    ("CSI:科学捜査班 シーズン10 #23", 10),
    ("水戸黄門・第36部 #10", 36),
    ("古畑任三郎(第2シリーズ)<デジタルリマスター版> #6-#10", 2),
    ("[字]魔法学園クロニクル (第2期)#5,6", 2),
    ("古代の宇宙人S13 #168~#172", 13),
    ("京都迷宮案内4 #3", 4),
    ("ゆるキャン△ SECOND SEASON #1", 2),
    ("鬼平犯科帳 第三シリーズ 第5話", 3),
    ("将棋)第84期 名人戦", None),  # no episode: no season
    ("BSイレブン競馬中継 第2部", None),
    ("TOKYO MER~走る緊急救命室~ #2", None),  # no season marker
    ("2026年度 第67回 日本女子アマチュアゴルフ選手権競技 第2部", None),  # marker after the episode: a part
    ("水戸黄門 第23部▼第30回", 23),
    ("遠山の金さん 第5部 第15話", 5),
    ("囲碁）厳選棋譜解説 第68期 本因坊戦 No.7", 68),
    ("乃木坂スター誕生！ 5期生 #3", None),  # 期生: a member generation, not a season
])
def test_season_only_with_an_episode(name, season):
    d = epgmeta.parse_title(name)
    assert d["season"] == season
    if season:
        assert d["episode"] is not None


def test_metadata_season():
    m = epgmeta.metadata(_fullwidth("dev", 8))  # 孤独のグルメ 傑作選 Season6 #8 #9
    assert (m["season"], m["episode"]) == (6, 8)
    m = epgmeta.metadata(_fullwidth("dev", 13))  # [新]朱色の仮面 第1話: no season marker
    assert (m["season"], m["episode"]) == (None, 1)


_HISSATSU = ("◇出演者\n藤枝梅安：緒形　拳\r\nほか\r\n\r\n\r\n【スタッフ】\r\n監督：渡邊祐介\r\n"
             "脚本：宮川一郎、渡邊祐介\r\n\r\n１９７３年／日本\n◇おしらせ\n明日１０月１１日（日）よる７時からは映画")


def test_movie_year_from_year_country_line():
    """dev 20 必殺仕掛人: the extended text ends "１９７３年／日本"; the line is not a writer."""
    assert epgmeta.movie_year("", _HISSATSU) == "1973"
    people = epgmeta.parse_extended("", _HISSATSU)["people"]
    assert ("credits", "日本") not in people
    assert [p for p in people if p[0] == "credits"] == [("credits", "宮川一郎"), ("credits", "渡邊祐介")]
    assert epgmeta.metadata(_fullwidth("dev", 20))["year"] == 1973
    assert "日本" not in epgmeta.metadata(_fullwidth("dev", 20))["writers"]


def test_year_line_removal_keeps_the_next_line():
    """A line that is only "2014年" is dropped, not the credit line after it."""
    people = epgmeta.parse_extended("", "◇出演者\n山田太郎\r\n2014年\r\n鈴木花子")["people"]
    assert people == [("actor", ("山田太郎", "出演者")), ("actor", ("鈴木花子", "出演者"))]


@pytest.mark.parametrize("ext, year", [
    ("◇番組内容\n2014年　韓国\r\n監督：キム・ギドク", "2014"),  # BS12-style movie header
    ("◇番組内容\n2020年 女子決勝「日本×アメリカ」", None),
    ("◇制作\n2026年\n◇出演者\n山田太郎", None),
])
def test_movie_year_line_forms(ext, year):
    assert epgmeta.movie_year("", ext) == year


def test_name_limit_ignores_a_trailing_paren():
    """dev 19 寅さん: "ハーブ・エデルマン（マイケル・ジョーダン）" is 21 characters with the role; the name is not."""
    ext = "◇出演者\n【出演】ハーブ・エデルマン（マイケル・ジョーダン）、香川京子（高井圭子）"
    assert epgmeta.parse_extended("", ext)["people"] == [
        ("actor", ("ハーブ・エデルマン(マイケル・ジョーダン)", "出演")), ("actor", ("香川京子(高井圭子)", "出演"))]
    # several "(…)" in one entry is a list, not a name: still dropped when over 20 characters
    ext = "◇出演者\n【出演】サンドウィッチ(伊達みきお) マン(富澤たけし)"
    assert epgmeta.parse_extended("", ext)["people"] == []
    ext = "◇出演者\n【出演】ヤマダタロウヤマダタロウヤマダタロウヤマダタロウ(x)"  # the name alone is over 20
    assert epgmeta.parse_extended("", ext)["people"] == []


def test_audio_section_is_not_a_voice_credit():
    """dev 21 ジョン・ウィック: "◇音声 英語・日本語字幕" is audio/subtitle info, not a cast entry (声 in 音声)."""
    assert epgmeta.parse_extended("", "◇音声\n英語・日本語字幕")["people"] == []
    assert epgmeta.parse_extended("", "◇声の出演\n森川陽太")["people"] == [("actor", ("森川陽太", "声の出演"))]
    cast = epgmeta.metadata(_fullwidth("dev", 21))["cast"]
    assert all("字幕" not in n for n, _ in cast) and len(cast) == 9 and all(r for _, r in cast)


def test_bracketed_etc_after_a_name():
    """dev 15 名探偵コナン: EPGStation turns 🈀 into "[ほか]", which stays after the last name."""
    ext = "◇声の出演\n大滝警部：若本規夫\r\n遠山銀司郎：てらそままさき　[ほか]\n◇監督・演出"
    assert epgmeta.parse_extended("", ext)["people"] == [
        ("actor", ("若本規夫", "大滝警部")), ("actor", ("てらそままさき", "遠山銀司郎"))]
    assert ("てらそままさき", "遠山銀司郎") in epgmeta.metadata(_fullwidth("dev", 15))["cast"]
    assert epgmeta.parse_extended("", "◇出演者\n森圭介［ほか］")["people"] == [("actor", ("森圭介", "出演者"))]


def test_sections_split_only_on_bare_newline_headers():
    """Sections are joined with a bare \\n, values use \\r\\n: "\\r\\n◇X(旧Twitter)" inside a value is no header
    (the rule items.raw_extended uses)."""
    ext = "◇番組内容\nbody\n◇公式ページ\n◇番組HP\r\nhttp://x\r\n◇X(旧Twitter)\r\n@x\n◇出演者\na"
    assert epgmeta.extended_sections(ext) == [
        ("番組内容", "body"), ("公式ページ", "◇番組HP\nhttp://x\n◇X(旧Twitter)\n@x"), ("出演者", "a")]
    from resources.lib.epgstation import items

    with open("tests/fixtures/dev/schedule_details.json", encoding="utf-8") as f:
        details = json.load(f)
    for texts in details.values():  # real TBS/NTV programs: the same sections as raw_extended finds
        keys = [k for k, _ in epgmeta.extended_sections(texts["extended"]) if k]
        assert len(keys) == len(items.raw_extended(texts["extended"])), texts["name"]
        assert not any("旧Twitter" in k or "Instagram" in k for k in keys), texts["name"]


def test_plot_no_outer_header_above_a_label_line():
    """dev 13: "【監督・演出】" right above "【監督】" says nothing; an inline "【出演】名前…" or a lone label line
    keeps the outer header."""
    plot = epgmeta.build_plot("説明", [("監督・演出", "【監督】\n山本泰一郎\n【キャラクターデザイン】\n須藤昌朋")])
    assert plot == "説明\n\n【監督】\n山本泰一郎\n【キャラクターデザイン】\n須藤昌朋"
    plot = epgmeta.build_plot("説明", [("出演者", "【出演】中村雅俊，田中健")])
    assert plot == "説明\n\n【出演者】\n【出演】中村雅俊，田中健"
    plot = epgmeta.build_plot("説明", [("番組概要", "【報知映画賞受賞作特集】")])
    assert plot == "説明\n\n【番組概要】\n【報知映画賞受賞作特集】"
    for rec_id in (13, 15):
        lines = epgmeta.metadata(_fullwidth("dev", rec_id))["plot"].split("\n")
        assert not any(a.startswith("【") and a.endswith("】") and b.startswith("【") and b.endswith("】")
                       for a, b in zip(lines, lines[1:])), rec_id


def test_film_genre_other_with_a_film_runtime_is_a_movie():
    """映画/その他 alone is a movie when the EPG text has epgXport's "YYYY年 NNN分" film line."""
    item = {"name": "Ｋ－シアター「ソウルメイト」", "description": "2023年　124分", "genre1": 6, "subGenre1": 15}
    m = epgmeta.metadata(item)
    assert (m["kind"], m["show"], m["year"]) == ("movie", "ソウルメイト", 2023)
    assert epgmeta.metadata(dict(item, description="韓国映画の魅力を紹介"))["kind"] == "tv"


def test_angle_bracket_heading_is_not_a_name():
    """dev 8 孤独のグルメ: "〈第8話ゲスト〉" is a heading in the cast list, not an actor."""
    cast = epgmeta.metadata(_fullwidth("dev", 8))["cast"]
    assert not any(n.startswith("〈") for n, _ in cast)
    assert ("岡田浩暉", "竹内社長") in cast
    assert epgmeta.parse_extended("", "◇出演者\n〈ゲスト〉\n山田太郎")["people"] == [("actor", ("山田太郎", "出演者"))]


def _with_extended(n=50):
    recs = [r for _, r in sorted(_fullwidth_all("dev2").items(), key=lambda kv: int(kv[0])) if r.get("extended")]
    assert len(recs) >= n
    return recs[:n]


def _fullwidth_all(fixture_set):
    """{id: RecordedItem} as captured with isHalfWidth=false (full-width EPG texts, like the addon reads them)."""
    with open(f"tests/fixtures/{fixture_set}/api/recorded.json", encoding="utf-8") as f:
        return {str(r["id"]): r for r in json.load(f)["records"]}


def test_no_regex_compiled_while_parsing(monkeypatch):
    """All patterns are module constants: nothing reaches re's compiler during metadata()."""
    import re

    def no_compile(*args, **kwargs):
        raise AssertionError(f"regex compiled at runtime: {args[:1]}")

    monkeypatch.setattr(re, "_compile", no_compile)
    for rec in _with_extended():
        assert epgmeta.metadata(rec) is not None


def test_speed():
    """Backlog 015 AC 10: metadata() over 50 real recordings <= 150 ms (best of 3); runs in CI on the dev VM."""
    import time

    recs = _with_extended()
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        for rec in recs:
            epgmeta.metadata(rec)
        best = min(best, time.perf_counter() - start)
    print(f"metadata() x50: {best * 1000:.1f} ms")
    assert best <= 0.150, f"{best * 1000:.1f} ms"


def test_import_time():
    """Importing epgmeta (compiling every regex), best of 3 in a fresh interpreter (re/unicodedata already loaded).

    Backlog 015 AC 10 asks for <= 50 ms (dev VM: ~28 ms). The hard limit here is 150 ms so a loaded CI runner does
    not block the deploy; EPGS_PERF=1 enforces the 50 ms target.
    """
    code = ("import re, time, unicodedata; t = time.perf_counter(); from resources.lib.epgstation import epgmeta; "
            "print(time.perf_counter() - t)")
    times = []
    for _ in range(3):
        result = _python(code)
        assert result.returncode == 0, result.stderr
        times.append(float(result.stdout))
    print(f"import epgmeta: {min(times) * 1000:.1f} ms")
    limit = 0.050 if os.environ.get("EPGS_PERF") == "1" else 0.150
    assert min(times) <= limit, f"{min(times) * 1000:.1f} ms"


def test_class_table_marks_lost_classes(tmp_path):
    """tools/epgmeta_expected.py --classes (020): classes per file, LOST when the last file has none of one."""
    old = [{"id": "a/1", "parse": {"kind": "movie", "version": "吹替版", "year": "1999"}, "flag_tags": ["吹替"],
            "genre_tags": ["映画"], "people": [["actor", ["X", "吹替: Y"]], ["director", "Z"]], "movie_year": "1999"},
           {"id": "a/1/dir", "dir": "d", "parse": {"kind": "tv"}},
           {"id": "schedule/0", "parse": {"kind": "tv", "episode_end": 5}, "flag_tags": [], "genre_tags": ["ドラマ"],
            "people": [["actor", ["V", "声の出演"]]], "movie_year": None}]
    new = [old[2]]
    for name, cases in (("old.json", old), ("new.json", new)):
        (tmp_path / name).write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    assert epgmeta_expected.classes(old[0]) == ["parse.kind=movie", "parse.version=吹替版", "parse.year",
                                                "people.actor", "role=吹替", "people.director", "flag=吹替",
                                                "genre=映画", "movie_year"]
    assert epgmeta_expected.classes(old[1]) == ["dir"]
    lines = epgmeta_expected.class_table([str(tmp_path / "old.json"), str(tmp_path / "new.json")])
    rows = {line.split("\t")[0]: line.split("\t")[1:] for line in lines[1:-1]}
    assert rows["role=声"] == ["1", "1"] and rows["parse.episode_end"] == ["1", "1"]
    assert rows["role=吹替"] == ["1", "0", "LOST"] and rows["dir"] == ["1", "0", "LOST"]
    assert lines[-1] == "14 classes; old.json: 14, new.json: 5"
