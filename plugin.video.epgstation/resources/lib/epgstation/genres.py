"""ARIB STD-B10 program genres.

Japanese names are copied from EPGStation's client/src/lib/event.ts (v2.10.0) so
the addon shows exactly what the EPGStation web UI shows.
"""

GENRE_EN = {
    0: "News", 1: "Sports", 2: "Information", 3: "Drama", 4: "Music",
    5: "Variety", 6: "Movies", 7: "Anime / Tokusatsu", 8: "Documentary",
    9: "Theater", 10: "Hobby / Education", 11: "Welfare", 12: "Reserved",
    13: "Reserved", 14: "Extended", 15: "Other",
}

GENRE_JA = {
    0: "ニュース・報道",
    1: "スポーツ",
    2: "情報・ワイドショー",
    3: "ドラマ",
    4: "音楽",
    5: "バラエティ",
    6: "映画",
    7: "アニメ・特撮",
    8: "ドキュメンタリー・教養",
    9: "劇場・公演",
    10: "趣味・教育",
    11: "福祉",
    12: "予備",
    13: "予備",
    14: "拡張",
    15: "その他",
}

SUBGENRE_JA = {
    0: {
        0: "定時・総合",
        1: "天気",
        2: "特集・ドキュメント",
        3: "政治・国会",
        4: "経済・市況",
        5: "海外・国際",
        6: "解説",
        7: "討論・会談",
        8: "報道特番",
        9: "ローカル・地域",
        10: "交通",
        15: "その他",
    },
    1: {
        0: "スポーツニュース",
        1: "野球",
        2: "サッカー",
        3: "ゴルフ",
        4: "その他の球技",
        5: "相撲・格闘技",
        6: "オリンピック・国際大会",
        7: "マラソン・陸上・水泳",
        8: "モータースポーツ",
        9: "マリン・ウィンタースポーツ",
        10: "競馬・公営競技",
        15: "その他",
    },
    2: {
        0: "芸能・ワイドショー",
        1: "ファッション",
        2: "暮らし・住まい",
        3: "健康・医療",
        4: "ショッピング・通販",
        5: "グルメ・料理",
        6: "イベント",
        7: "番組紹介・お知らせ",
        15: "その他",
    },
    3: {
        0: "国内ドラマ",
        1: "海外ドラマ",
        2: "時代劇",
        15: "その他",
    },
    4: {
        0: "国内ロック・ポップス",
        1: "海外ロック・ポップス",
        2: "クラシック・オペラ",
        3: "ジャズ・フュージョン",
        4: "歌謡曲・演歌",
        5: "ライブ・コンサート",
        6: "ランキング・リクエスト",
        7: "カラオケ・のど自慢",
        8: "民謡・邦楽",
        9: "童謡・キッズ",
        10: "民族音楽・ワールドミュージック",
        15: "その他",
    },
    5: {
        0: "クイズ",
        1: "ゲーム",
        2: "トークバラエティ",
        3: "お笑い・コメディ",
        4: "音楽バラエティ",
        5: "旅バラエティ",
        6: "料理バラエティ",
        15: "その他",
    },
    6: {
        0: "洋画",
        1: "邦画",
        2: "アニメ",
        15: "その他",
    },
    7: {
        0: "国内アニメ",
        1: "海外アニメ",
        2: "特撮",
        15: "その他",
    },
    8: {
        0: "社会・時事",
        1: "歴史・紀行",
        2: "自然・動物・環境",
        3: "宇宙・科学・医学",
        4: "カルチャー・伝統文化",
        5: "文学・文芸",
        6: "スポーツ",
        7: "ドキュメンタリー全般",
        8: "インタビュー・討論",
        15: "その他",
    },
    9: {
        0: "現代劇・新劇",
        1: "ミュージカル",
        2: "ダンス・バレエ",
        3: "落語・演芸",
        4: "歌舞伎・古典",
        15: "その他",
    },
    10: {
        0: "旅・釣り・アウトドア",
        1: "園芸・ペット・手芸",
        2: "音楽・美術・工芸",
        3: "囲碁・将棋",
        4: "麻雀・パチンコ",
        5: "車・オートバイ",
        6: "コンピュータ・ＴＶゲーム",
        7: "会話・語学",
        8: "幼児・小学生",
        9: "中学生・高校生",
        10: "大学生・受験",
        11: "生涯教育・資格",
        12: "教育問題",
        15: "その他",
    },
    11: {
        0: "高齢者",
        1: "障害者",
        2: "社会福祉",
        3: "ボランティア",
        4: "手話",
        5: "文字(字幕)",
        6: "音声解説",
        15: "その他",
    },
    12: {
    },
    13: {
    },
    14: {
        0: "BS/地上デジタル放送用番組付属情報",
        1: "広帯域 CS デジタル放送用拡張",
        3: "サーバー型番組付属情報",
        4: "IP 放送用番組付属情報",
    },
    15: {
        15: "その他",
    },
}


# English subgenre names: ARIB STD-B10 Version 4.6-E1 (English, 2008), Annex H (Normative) "Genre designation in
# content descriptor", pp. 185-193, https://www.arib.or.jp/english/html/overview/doc/6-STD-B10v4_6-E1.pdf
# Copied verbatim (ARIB's ", " kept) except two typos fixed: "Gourmet, cocking" and "Cocking variety". Same keys as
# SUBGENRE_JA (14/2 is not in EPGStation's table, so not here either).
SUBGENRE_EN = {
    0: {
        0: "Regular, general", 1: "Weather report", 2: "Special program, documentary",
        3: "Politics, national assembly", 4: "Economics, market report", 5: "Overseas, international report",
        6: "News analysis", 7: "Discussion, conference", 8: "Special report", 9: "Local program",
        10: "Traffic report", 15: "Others",
    },
    1: {
        0: "Sports news", 1: "Baseball", 2: "Soccer", 3: "Golf", 4: "Other ball games", 5: "Sumo, combative sports",
        6: "Olympic, international games", 7: "Marathon, athletic sports, swimming", 8: "Motor sports",
        9: "Marine sports, winter sports", 10: "Horse race, public race", 15: "Others",
    },
    2: {
        0: "Gossip/tabloid show", 1: "Fashion", 2: "Living, home", 3: "Health, medical treatment",
        4: "Shopping, mail-order business", 5: "Gourmet, cooking", 6: "Events", 7: "Program guide, information",
        15: "Others",
    },
    3: {0: "Japanese dramas", 1: "Overseas dramas", 2: "Period dramas", 15: "Others"},
    4: {
        0: "Japanese rock, pop music", 1: "Overseas rock, pop music", 2: "Classic, opera", 3: "Jazz, fusion",
        4: "Popular songs, Japanese popular songs (enka songs)", 5: "Live concert", 6: "Ranking, request music",
        7: "Karaoke, amateur singing contests", 8: "Japanese ballad, Japanese traditional music",
        9: "Children's song", 10: "Folk music, world music", 15: "Others",
    },
    5: {
        0: "Quiz", 1: "Game", 2: "Talk variety", 3: "Comedy program", 4: "Music variety", 5: "Tour variety",
        6: "Cooking variety", 15: "Others",
    },
    6: {0: "Overseas movies", 1: "Japanese movies", 2: "Animation", 15: "Others"},
    7: {0: "Japanese animation", 1: "Overseas animation", 2: "Special effects", 15: "Others"},
    8: {
        0: "Social, current events", 1: "History, travel record", 2: "Nature, animal, environment",
        3: "Space, science, medical science", 4: "Culture, traditional culture", 5: "Literature, literary art",
        6: "Sports", 7: "Total documentary", 8: "Interviews, discussions", 15: "Others",
    },
    9: {
        0: "Modern drama, Western-style drama", 1: "Musical", 2: "Dance, ballet", 3: "Comic story, entertainment",
        4: "Kabuki, classical drama", 15: "Others",
    },
    10: {
        0: "Trip, fishing, outdoor entertainment", 1: "Gardening, pet, handicrafts",
        2: "Music, art, industrial art", 3: 'Japanese chess (shogi) and "go"', 4: "Mah-jong, pinball games",
        5: "Cars, motorbikes", 6: "Computer, TV games", 7: "Conversation, languages",
        8: "Little children, schoolchildren", 9: "Junior high school and high school students",
        10: "University students, examinations", 11: "Lifelong education, qualifications",
        12: "Educational problem", 15: "Others",
    },
    11: {
        0: "Old aged persons", 1: "Handicapped persons", 2: "Social welfare", 3: "Volunteers", 4: "Sign language",
        5: "Text (subtitles)", 6: "Explanation on sound multiplex broadcast", 15: "Others",
    },
    12: {},
    13: {},
    14: {
        0: "Appendix information for BS/terrestrial digital broadcast program",
        1: "Extension for broadband CS digital broadcasting",
        3: "Appendix information for server-type program",
        4: "Appendix information for IP broadcast program",
    },
    15: {15: "Others"},
}


def subgenre_name(genre, sub_genre, lang="ja"):
    """The subgenre's own name ("洋画", en "Overseas movies"), or None when the table has none."""
    table = SUBGENRE_JA if lang == "ja" else SUBGENRE_EN
    return table.get(genre, {}).get(sub_genre)


def genre_name(genre, sub_genre=None, lang="ja"):
    """'映画 / 洋画' style label (en 'Movies / Overseas movies'); None when genre is missing or out of range."""
    if genre is None or genre not in GENRE_JA:
        return None
    name = GENRE_JA[genre] if lang == "ja" else GENRE_EN[genre]
    sub = subgenre_name(genre, sub_genre, lang)
    return f"{name} / {sub}" if sub else name


def item_genres(item, lang="ja"):
    """All genre labels on a recorded item (genre1..3), de-duplicated, in order."""
    out = []
    for n in ("1", "2", "3"):
        label = genre_name(item.get("genre" + n), item.get("subGenre" + n), lang)
        if label and label not in out:
            out.append(label)
    return out


def _slot_int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def genre_pairs(item):
    """Sorted unique [[genre, subGenre], ...] of genre1..3 (genre in the table; subGenre None when missing)."""
    out = set()
    for n in ("1", "2", "3"):
        g = _slot_int(item.get("genre" + n))
        if g is not None and g in GENRE_JA:
            out.add((g, _slot_int(item.get("subGenre" + n))))
    return [list(p) for p in sorted(out, key=lambda p: (p[0], -1 if p[1] is None else p[1]))]
