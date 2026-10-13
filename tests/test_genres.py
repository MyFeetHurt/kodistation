from resources.lib.epgstation.genres import (
    SUBGENRE_EN,
    SUBGENRE_JA,
    genre_name,
    genre_pairs,
    item_genres,
    subgenre_name,
)


def test_genre_with_subgenre():
    assert genre_name(6, 1) == "映画 / 邦画"


def test_genre_english_with_subgenre():  # subgenres (009): ARIB STD-B10 Annex H names
    assert genre_name(6, 1, "en") == "Movies / Japanese movies"
    assert genre_name(11, 5, "en") == "Welfare / Text (subtitles)"
    assert genre_name(6, None, "en") == "Movies"


def test_subgenre_en_has_the_keys_of_subgenre_ja():
    assert SUBGENRE_EN.keys() == SUBGENRE_JA.keys()
    for g in SUBGENRE_JA:
        assert SUBGENRE_EN[g].keys() == SUBGENRE_JA[g].keys(), g
        assert all(SUBGENRE_EN[g].values())
    assert SUBGENRE_EN[12] == {} and SUBGENRE_EN[13] == {}


def test_subgenre_en_spot_values():
    assert SUBGENRE_EN[6][0] == "Overseas movies"
    assert SUBGENRE_EN[2][5] == "Gourmet, cooking"  # ARIB typo "cocking" fixed
    assert SUBGENRE_EN[5][6] == "Cooking variety"
    assert SUBGENRE_EN[11][6] == "Explanation on sound multiplex broadcast"
    assert SUBGENRE_EN[10][3] == 'Japanese chess (shogi) and "go"'
    assert SUBGENRE_EN[15][15] == "Others"


def test_subgenre_name():
    assert subgenre_name(6, 0) == "洋画" and subgenre_name(6, 0, "en") == "Overseas movies"
    assert subgenre_name(6, 9) is None and subgenre_name(6, None, "en") is None and subgenre_name(99, 0) is None


def test_unknown_subgenre_en_falls_back_to_the_genre():
    assert genre_name(0, 0xB, "en") == "News"
    assert item_genres({"genre1": 7, "subGenre1": 12}, "en") == ["Anime / Tokusatsu"]


def test_item_genres_english():
    item = {"genre1": 6, "subGenre1": 0, "genre2": 6, "subGenre2": 0, "genre3": 7, "subGenre3": 0}
    assert item_genres(item, "en") == ["Movies / Overseas movies", "Anime / Tokusatsu / Japanese animation"]


def test_genre_pairs():
    item = {"genre1": 6, "subGenre1": 15, "genre2": 6, "subGenre2": 0, "genre3": 6, "subGenre3": 0}
    assert genre_pairs(item) == [[6, 0], [6, 15]]
    assert genre_pairs({"genre1": None, "genre2": 6, "subGenre2": 0}) == [[6, 0]]  # genre2 only
    assert genre_pairs({"genre1": 3}) == [[3, None]]  # no subGenre
    assert genre_pairs({"genre1": 3, "subGenre1": 1, "genre3": 3}) == [[3, None], [3, 1]]
    assert genre_pairs({"genre1": 99, "subGenre1": 0, "genre2": True}) == []  # out of range, not an int
    assert genre_pairs({}) == []


def test_missing_or_bad_genre():
    assert genre_name(None) is None
    assert genre_name(99) is None


def test_item_genres_dedupes_and_skips_missing():
    item = {"genre1": 5, "subGenre1": 2, "genre2": 5, "subGenre2": 2, "genre3": None}
    assert item_genres(item) == ["バラエティ / トークバラエティ"]


def test_empty_subgenre_falls_back_to_genre():
    # 0/0xb is blank in the ARIB table
    assert genre_name(0, 0xB) == "ニュース・報道"
