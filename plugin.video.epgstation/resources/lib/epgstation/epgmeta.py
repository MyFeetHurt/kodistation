"""Structured metadata from EPG text: show, episode, movie year, flags, sectioned plot and credits.

A port of sections 1-5 of the user's epgXport.js (2026-10-10), which turns EPGStation recordings into NFO
metadata. From here on this module is KODIstation's own parser; tests/fixtures/epgmeta_expected.json is its
regression fixture (tools/epgmeta_expected.py rewrites it from this module after a deliberate change).

Input is the FULL-WIDTH EPG text (isHalfWidth=false), exactly what EPGStation passes to encode commands.

Pure stdlib (re, unicodedata), Python 3.8 syntax. All regexes are compiled once at import.

JS -> Python regex rules, applied by _rx() so the patterns below read like the JS sources:
- \\d -> [0-9], \\w -> [A-Za-z0-9_], \\s / \\S -> the JS whitespace class (Python's \\s has \\x1c-\\x1f and \\x85
  but lacks \\ufeff), . -> [^\\n\\r\\u2028\\u2029], $ -> \\Z (with "m": before a JS line terminator or at the end),
  "m" ^ -> after a JS line terminator or at the start, [ & ~ | inside a class are escaped, \\u{1F100} -> \\U0001F100,
  "i" -> re.I | re.ASCII (JS non-unicode "i" never folds a non-ASCII character to an ASCII pattern letter).
- JS "g" only decides replace-all (Python count=0) vs replace-first (count=1) at each call site.
- firstIndex(re, text, from) is pat.search(text, from): ^ and lookbehinds see the whole string, like JS lastIndex.
- Intentional divergence: JS regexes without "u" count UTF-16 units, Python counts code points, so an astral-plane
  character (emoji, rare kanji like 𠮷) is one character here and two in epgXport ({1,2}, {13,}, "."). Python is the
  intended reading: "𠮷𠮷「タイトル」" stays whole like "吉吉「タイトル」" (epgXport cuts it at 「). Tested.
- Python has no variable-width lookbehind. The JS patterns that use one are rewritten where they are defined
  (CUT, COLON, BLURB_AFTER_EPISODE); tests pin each rewrite.
"""
import re
import unicodedata

# JavaScript \s (class content), its characters (for trim) and line terminators.
_JS_WS = r"\t\n\x0b\x0c\r \xa0  -     　﻿"
_JS_WS_CHARS = "\t\n\x0b\x0c\r \xa0 " + "".join(map(chr, range(0x2000, 0x200B))) + "    　﻿"
_JS_LT = r"\n\r  "


def _rx(src, flags=""):
    """Compile a JavaScript regex source with JavaScript semantics (see the module docstring)."""
    out, i, in_class, multiline = [], 0, False, "m" in flags
    while i < len(src):
        c = src[i]
        if c == "\\":
            e = src[i + 1]
            i += 2
            if e == "u" and src[i:i + 1] == "{":
                end = src.index("}", i)
                out.append(f"\\U{int(src[i + 1:end], 16):08x}")
                i = end + 1
            elif e in "dws":
                out.append({"d": "0-9", "w": "A-Za-z0-9_", "s": _JS_WS}[e] if in_class
                           else {"d": "[0-9]", "w": "[A-Za-z0-9_]", "s": "[" + _JS_WS + "]"}[e])
            elif e in "DWS":
                if in_class:
                    if e == "S" and out[-1] == _JS_WS and src[i:i + 1] == "]":
                        out[-1] = r"\s\S"  # [\s\S]: any character
                        continue
                    raise ValueError("negated class escape inside a class: " + src)
                out.append({"D": "[^0-9]", "W": "[^A-Za-z0-9_]", "S": "[^" + _JS_WS + "]"}[e])
            elif e in "bB":
                raise ValueError("word boundary (Unicode in Python, ASCII in JS): " + src)
            else:
                out.append("\\" + e)
            continue
        if in_class:
            if c == "]":
                in_class = False
                out.append(c)
            elif c in "[&~|":
                out.append("\\" + c)
            else:
                out.append(c)
        elif c == "[":
            in_class = True
            out.append(c)
            if src[i + 1:i + 2] == "^":
                out.append("^")
                i += 1
        elif c == ".":
            out.append("[^" + _JS_LT + "]")
        elif c == "$":
            out.append("(?=[" + _JS_LT + r"]|\Z)" if multiline else r"\Z")
        elif c == "^" and multiline:
            out.append("(?:(?<=[" + _JS_LT + r"])|\A)")
        else:
            out.append(c)
        i += 1
    return re.compile("".join(out), re.I | re.ASCII if "i" in flags else 0)


def _trim(text):
    """JavaScript String.prototype.trim."""
    return text.strip(_JS_WS_CHARS)


def _sub(pat, repl, text, count=0):
    return pat.sub(repl, text, count)


INF = float("inf")

# =====================================================================================================================
# 1. Constants and tables
# =====================================================================================================================

GENRE = ("ニュース・報道|スポーツ|情報・ワイドショー|ドラマ|音楽|バラエティ|映画|アニメ・特撮|ドキュメンタリー・教養|"
         "劇場・公演|趣味・教育|福祉").split("|")

SUBGENRE = [s.split("|") for s in [
    "定時・総合|天気|特集・ドキュメント|政治・国会|経済・市況|海外・国際|解説|討論・会談|報道特番|ローカル・地域|交通",
    "スポーツニュース|野球|サッカー|ゴルフ|その他の球技|相撲・格闘技|オリンピック・国際大会|マラソン・陸上・水泳|モータースポーツ|"
    "マリン・ウィンタースポーツ|競馬・公営競技",
    "芸能・ワイドショー|ファッション|暮らし・住まい|健康・医療|ショッピング・通販|グルメ・料理|イベント|番組紹介・お知らせ",
    "国内ドラマ|海外ドラマ|時代劇",
    "国内ロック・ポップス|海外ロック・ポップス|クラシック・オペラ|ジャズ・フュージョン|歌謡曲・演歌|ライブ・コンサート|ランキング・リクエスト|"
    "カラオケ・のど自慢|民謡・邦楽|童謡・キッズ|民族音楽・ワールドミュージック",
    "クイズ|ゲーム|トークバラエティ|お笑い・コメディ|音楽バラエティ|旅バラエティ|料理バラエティ",
    "洋画|邦画|アニメ",
    "国内アニメ|海外アニメ|特撮",
    "社会・時事|歴史・紀行|自然・動物・環境|宇宙・科学・医学|カルチャー・伝統文化|文学・文芸|スポーツ|ドキュメンタリー全般|インタビュー・討論",
    "現代劇・新劇|ミュージカル|ダンス・バレエ|落語・演芸|歌舞伎・古典",
    "旅・釣り・アウトドア|園芸・ペット・手芸|音楽・美術・工芸|囲碁・将棋|麻雀・パチンコ|車・オートバイ|コンピュータ・TVゲーム|会話・語学|"
    "幼児・小学生|中学生・高校生|大学生・受験|生涯教育・資格|教育問題",
    "高齢者|障害者|社会福祉|ボランティア|手話|字幕|解説放送",
]]

G_NEWS, G_DRAMA, G_MOVIE, G_STAGE, G_WELFARE = 0, 3, 6, 9, 11
SG_OTHER = 15  # subGenre その他

FLAG = (r"音声解説付?|新|終|再|字|デ|解|無料?|二|多|双|SS?|初|生|N|映|吹|手|天|交|HV|SD|[248]K|英|韓|中|録|ほか|"
        r"前編|後編|字/[日英]+|PG-?12(?:相当)?|R-?1[58]\+?(?:指定版)?")

FLAG_TAG = {"新": "新番組", "終": "最終回", "再": "再放送", "字": "字幕", "二": "二か国語", "解": "解説放送",
            "吹": "吹替", "デ": "データ放送", "初": "初放送", "生": "生放送"}

ED = (r"(?:HD)?(?:[248]K)?(?:デジタル)?(?:リマスター|レストア|修復)版?|(?:HD)?完全版|ノーカット(?:字幕)?版|"
      r"オリジナル版|スペシャル・エクステンデッド・カット")
EDITION = ED + r"|(?:日本語)?(?:字幕|吹替)(?:版|スーパー)?|レターボックスサイズ|HD"
VERSION = [("吹替版", _rx(r"吹替|\[吹\]")), ("字幕版", _rx(r"字幕(?:版|スーパー)|[(<]字幕[)>]"))]

BLOCK = _rx(r"劇場|シアター|ドラマ|洋画|邦画|アニメイズム|一挙|連日|放送|特集|[出主]演|時代劇|デイ$|×|周年|祭り|SP$|"
            r"BS初|CS初|版$")

UMBRELLA = _rx(r"^(NHKスペシャル|プチプチ・アニメ|手話で楽しむみんなのテレビ)[\s×]?")

PREFIX = _rx("^(?:" + "|".join([
    r"【[^】]*】",
    r"([●★☆])[^●★☆]*\1(?=\S)",
    r"[☆★◆●]+\s?",
    r"[^\s◆◇]{1,3}[◆◇]",
    r"(?:TV)?アニメA?・?\s?(?=\S)",
    r"(?:韓国|中国)(?:ドラマ|時代劇|バラエティ)(?:・セレクション・)?\s?",
    r"韓ドラ☆?\s",
    r"(?:金曜|BS)?時代劇(?:アワー)?\s?",
    r"TBSレジェンドドラマ・",
    r"ミステリー(?:・セレクション|S)・",
    r"連続ドラマW\s",
    r"(?:将棋|囲碁)\)",
    r"土曜プレミアム・",
    r"KIDS STATION STAGE\s",
]) + ")")

SLOT = _rx(r"^(?:ドラマ\d*|[^\s「\u0001]*(?:劇場|のサスペンス|アワー))\s(?=[^\s「▽▼【#])")

WRAP = _rx(r"^(?:大河ドラマ|日\d|韓ドラ|ドラマ|スペシャル|連続テレビ小説|藤子・F・不二雄ワールド|橋田壽賀子ドラマ|"
           r"新?木曜ドラマ|ドラマDiVE|劇場版|映画|シネマ|[^「」\s]*(?:主演|×)[^「」\s]*)(?=[「『])")

MOVIE_SLOT = _rx(r"^[^「『]*(?:映画|ロードショー|シネマ|ラウンジ|劇場|シアター)[「『]([^」』]+)[」』]")

N = r"(\d+|[一二三四五六七八九十百]+)"

EPS = [
    _rx(r"#(\d+)(?:\s?[-~,・&]\s?#?(\d+))*"),
    _rx(r"\(?第" + N + r"(?:[話回](?:、第?" + N + r"[話回])?|、" + N + r"[話回])\)?"),
    _rx(r"(?<![第全\d])(\d+)話"),
    _rx(r"(?<![A-Za-z])(?:Chapter|Ep\.?|No\.|Vol\.)\s?(\d+)", "i"),
    _rx(r"(\d+)\. PHASE-\d+"),
]

PAREN = _rx(r"\((\d{1,3})\)(?:\((\d{1,3})\))?")

SEASON = _rx("|".join([
    r"\(?(?:第[\d一二三四五六七八九十]+(?:期(?!生)|シリーズ|部)|シーズン\s?\d+|Season\s?\d+|\d+期(?!生))\)?",
    r"・第\d+部",
    r"S\d+(?=\s?#|:)",
    r"SECOND SEASON",
    r"~?\d+(?:st|nd|rd|th) season~?",
    r"\s[壱弐参肆伍陸漆捌玖](?=\s#)",
    r"(?<=[^\x00-\x7f\s\d])\d{1,2}(?=\s#)",
]), "gi")

# KODIstation: the season number of the forms SEASON strips, in this order (first match wins).
_KANJI_SEASON = "壱弐参肆伍陸漆捌玖"
SEASON_NUMBER = [
    _rx(r"(?:Season|シーズン)\s?(\d+)", "i"),
    _rx(r"第([\d一二三四五六七八九十]+)(?:期(?!生)|シリーズ|部)"),
    _rx(r"(\d+)期(?!生)"),
    _rx(r"S(\d+)(?=\s?#|:)", "i"),
    _rx(r"(\d+)(?:st|nd|rd|th) season", "i"),
    _rx(r"(SECOND) SEASON", "i"),
    _rx(r"\s([" + _KANJI_SEASON + r"])(?=\s#)"),
    _rx(r"(?<=[^\x00-\x7f\s\d])(\d{1,2})(?=\s#)"),
]


def season_of(text, before=None):
    """Season number from a (noise-stripped) name: "Season6", "シーズン10", "第2期", "第三シリーズ", "S13 #",
    "京都迷宮案内4 #3" -> 4. None when there is none. before: the episode marker's index in text; a marker after
    it is a part, not a season ("第67回 … 第2部")."""
    for pat in SEASON_NUMBER:
        m = pat.search(text)
        if m and (before is None or m.start() < before):
            n = m.group(1)
            if n.upper() == "SECOND":
                return 2
            if n in _KANJI_SEASON:
                return _KANJI_SEASON.index(n) + 1
            return parse_number(n)
    return None


# CUT: where the show name ends. The JS alternatives with a fixed-width lookbehind (or none) form one regex;
# the four with a variable-width lookbehind are computed in _cut_index().
CUT_FIXED = _rx("|".join([
    r"(?<!の)(?<!^[^\s「])(?<!^[^\s「]{2})「",  # JS (?<!の|^[^\s「]{1,2})「
    r"[『▽▼◆◇★【\u0001]",
    r"“[^”]*”(?=\s|$|【)",
    r"(?<!傑作)\s?選(?=[\s「“▽\u0001]|$)",
    r"ゲスト",
    r"ベストセレクション",
    r"特別編(?!成)",
    r"~(?=[^~]*編[^~]*~)",
    r"\((?![ァ-ヶー]+\))[^()\d]+\)$",
    r"\(\d{1,2}\/\d{1,2}\)",
    r"(?<=[^\d\s])(?:19[5-9]|20[0-4])\d(?![\d年.])",
]), "g")
# JS (?<=^[^\s!?]*[!?]+)(?![!?])(?=LA): the only candidate is the end of the first "!?" run, if nothing but
# non-space characters come before it (greedy [!?]+ makes (?![!?]) hold automatically).
_CUT_BANG_HEAD = _rx(r"[^\s!?]*[!?]+")
_CUT_BANG_LA = _rx(r"[^\s\u0001「▽▼【★◆!?(]{13,}[!?▽▼/]|[^\s\u0001「▽▼【★◆]*\.\.\.")
# JS (?<=[^\s!?/][!?]+)\/(?!\/): a "/" after a "!?" run whose preceding character is not space, ! ? or /.
_CUT_SLASH = _rx(r"(?<=[!?])\/(?!\/)")
_NOT_BEFORE_BANG_RUN = _rx(r"[\s!?/]")
# JS (?<=^(?!(?:Mrs|Miss|Prof|Vol)\.)[A-Za-z]{3,}\.)(?=LA): the only candidate is after a leading "Word.".
_CUT_DOT_HEAD = _rx(r"(?!(?:Mrs|Miss|Prof|Vol)\.)[A-Za-z]{3,}\.")
_CUT_DOT_LA = _rx(r"[^\sA-Za-z\d.ぁ-ゖ\u0001]")
# JS (?<=^[^~]*[!?])~: the first "~", if "!" or "?" is right before it.


def _cut_index(text, pos):
    """JS firstIndex(CUT, text, pos): the earliest CUT match at or after pos (INF when none)."""
    if pos > len(text):
        return INF
    m = CUT_FIXED.search(text, pos)
    best = m.start() if m else INF
    m = _CUT_BANG_HEAD.match(text)
    if m and pos <= m.end() < best and _CUT_BANG_LA.match(text, m.end()):
        best = m.end()
    for m in _CUT_SLASH.finditer(text, pos):
        if m.start() >= best:
            break
        start = m.start() - 1
        while start > 0 and text[start - 1] in "!?":
            start -= 1
        if start > 0 and not _NOT_BEFORE_BANG_RUN.match(text[start - 1]):
            best = m.start()
            break
    m = _CUT_DOT_HEAD.match(text)
    if m and pos <= m.end() < best and _CUT_DOT_LA.match(text, m.end()):
        best = m.end()
    tilde = text.find("~")
    if pos <= tilde < best and tilde > 0 and text[tilde - 1] in "!?":
        best = tilde
    return best


# JS (?<!^[A-Za-z]{1,3}):(?![^(]*\)) as three fixed-width lookbehinds.
COLON = _rx(r"(?<!^[A-Za-z])(?<!^[A-Za-z]{2})(?<!^[A-Za-z]{3}):(?![^(]*\))")

SUFFIX = _rx(r"(?:\s?(?:\d+(?:\.\d+)?min\.?|(?<![ァ-ヶー])ミニ|(?<![A-Za-z])mini|\d+分版|(?<![A-Za-z])PR|"
             r"スペシャル|傑作選?|(?:\d+分)?ダイジェスト|\d+周年PR|直前(?:SP|スペシャル)?)|\sセレクション)!*$")

SUFFIX_KEEP = _rx(r"^(?:[A-Z\d]{1,5}|ドキュメント|ニュース)$")

PIN = _rx(r"\s*[{[]\w+[-=][^}\]]+[}\]]", "g")

# =====================================================================================================================
# 2. Text normalisation
# =====================================================================================================================

_GAIJI = _rx(r"[\u{1F100}-\u{1F2FF}]", "gu")
_WAVE = _rx(r"[〜～]", "g")
_SPACES = _rx(r"\s+", "g")


def norm(text):
    """Gaiji flags -> "[x]" (before NFKC, which would make 🈑 a bare 字), NFKC, 〜 -> ~, ♯ -> #, spaces collapsed."""
    text = str(text) if text else ""
    text = _GAIJI.sub(lambda m: "[" + unicodedata.normalize("NFKC", m.group(0)) + "]", text)
    text = _WAVE.sub("~", unicodedata.normalize("NFKC", text)).replace("♯", "#")
    return _trim(_SPACES.sub(" ", text))


_KEY_PUNCT = _rx(r"""[\s・:/!?~.。、,'"“”’\-]""", "g")


def key(text):
    """Loose comparison key (NFKC, lower case, no spaces or punctuation)."""
    return _KEY_PUNCT.sub("", unicodedata.normalize("NFKC", text).lower())


_DIGITS = _rx(r"^\d+$")


def parse_number(text):
    """"12" -> 12, "十二" -> 12, "二十" -> 20, "百" -> 100."""
    if _DIGITS.match(text):
        return int(text)
    total = digit = 0
    for c in text:
        i = "〇一二三四五六七八九".find(c)
        if i >= 0:
            digit = i
        else:  # 十 or 百
            total += (digit or 1) * (100 if c == "百" else 10)
            digit = 0
    return total + digit


# =====================================================================================================================
# 3. Title parsing: EPG programme name -> {kind, show, episode, subtitle, ...}
# =====================================================================================================================

# JS (?<=[#＃][\d０-９]+\S*)　{2}.*$ (replace with ""): the leftmost "#N…" token is the leftmost lookbehind
# position, so a consuming group that is put back is equivalent.
BLURB_AFTER_EPISODE = _rx(r"([#＃][\d０-９]\S*)　{2}.*$")

FLAG_BRACKET = _rx(r"[\[【](?:" + FLAG + "|" + EDITION + r")[\]】]", "g")

TITLE_NOISE = _rx("|".join([
    r"\((?:" + EDITION + r"|最終回|主演.*?|\d+ch .*?)\)",
    r"\([^\d()]\)(?=[\s\[【<(▼▽◆#\u0001]|$)",
    r"\([^\d()]+\)(?=#)",
    r"[(<]?/?全\d+話[)>]?",
    r"(?<![一-鿿])<(?!第\d+節)[^>]*>",
    r"<(?![ァ-ヶー]+>|第\d+節)[^>]*>",
    r"\sEND$",
    r"[’']\d\d(?!\d)",
]), "g")

EDITION_WORD = _rx(r"\s?(?:" + ED + r")(?=[\s#\u0001]|$)", "g")

_NOTE_TAIL = _rx(r"※.*$")
_CAST_TAIL = _rx(r"\s?【?出演:.*$")
_NAME_YEAR = _rx(r"\(((?:19|20)\d\d)(?:年(?:\s?\S{0,4}公開)?)?\)")
_YEAR_RANGE = _rx(r"\((?:19|20)\d\d~(?:(?:19|20)\d\d)?\)", "g")
_SQUARE = _rx(r"\[([^\]]*)\]", "g")
_DELIMS = _rx(r"\s*\u0001[\s\u0001]*", "g")
_DELIM_ENDS = _rx(r"^\u0001|\u0001$", "g")


def strip_noise(normalized):
    """Strip flags, editions, notes and a release year (returned for movies). Returns (text, year)."""
    year = []

    def take_year(m):
        year.append(m.group(1))
        return ""

    text = _sub(_NOTE_TAIL, "", normalized, 1)
    text = _sub(_CAST_TAIL, "", text, 1)
    text = _sub(_NAME_YEAR, take_year, text, 1)
    text = _YEAR_RANGE.sub("\u0001", text)
    text = FLAG_BRACKET.sub("\u0001", text)
    text = TITLE_NOISE.sub("", text)
    text = EDITION_WORD.sub("", text)
    text = _SQUARE.sub(r"【\1】", text)
    text = _DELIM_ENDS.sub("", _DELIMS.sub("\u0001", text))
    return _trim(text), (year[0] if year else None)


_PR = _rx(r"^PR\s")
_SERIES = _rx(r"^([^\s「\u0001]+)(?=\s|「[^」]*」$)")


def parse_umbrella(text, dir):
    """Umbrella slots: "NHKスペシャル 最新科学で見えた「うつ病の正体」…" -> show "NHKスペシャル"."""
    umbrella = UMBRELLA.search(text)
    if not umbrella:
        return None
    rest = text[len(umbrella.group(0)):]
    series = _SERIES.search(_sub(_PR, "", rest, 1))
    return {"kind": "tv", "show": dir or (series.group(1) if series else umbrella.group(1)),
            "subtitle": clean_subtitle(rest)}


_LEADING_BRACKET = _rx(r"^【([^】]+)】\s?(?=([#“「])|$)")
_IKKYO = _rx(r"一挙")
_IKKYO_HOSO = _rx(r"一挙放送")
_LEAD_DELIM = _rx(r"^\u0001")
_TRAIL_DELIM = _rx(r"\u0001$")
_BLOCK_LABEL = _rx(r"【([^】]*)】", "g")
_OPEN_QUOTE = _rx(r"^([「『])")


def take_leading_show(title, has_genre):
    """A leading 【show】 or 「show」 is the show ("wrapped"); otherwise strip leading labels and block labels."""
    m = _LEADING_BRACKET.search(title["text"])
    if m:
        label = m.group(1)
        title["show"] = "【" + label + "】" if m.group(2) == "#" and not _IKKYO.search(label) \
            else _sub(_IKKYO_HOSO, "", label, 1)
        title["text"] = title["text"][len(m.group(0)):]
        return
    while True:
        m = PREFIX.search(title["text"]) or (not has_genre(G_STAGE) and SLOT.search(title["text"]))
        if not m:
            break
        title["text"] = _sub(_LEAD_DELIM, "", title["text"][len(m.group(0)):], 1)
    text = _BLOCK_LABEL.sub(lambda m: "\u0001" if BLOCK.search(m.group(1)) else m.group(0), title["text"])
    title["text"] = _sub(_TRAIL_DELIM, "", _sub(WRAP, "", text, 1), 1)
    m = _OPEN_QUOTE.search(title["text"])
    if m:
        close = title["text"].find("」" if m.group(1) == "「" else "』")
        if close > 0:
            title["show"] = title["text"][1:close]
            title["text"] = title["text"][close + 1:]


_MEDLEY = _rx(r"^([^#◆◇]+)[◆◇](?=[^◆◇#\s]+\s?#)")


def take_medley_prefix(title):
    """"X◆Show #N": X is a programme label and becomes the subtitle."""
    m = _MEDLEY.search(title["text"])
    if m and title["show"] is None:
        title["medley"] = m.group(1)
        title["text"] = title["text"][len(m.group(0)):]


TAIL = _rx("|".join([
    r"[\s\u0001]▼.*$",
    r"\s~[^~]+~(?=\s?#)",
    r"[▽▼]\S*主演$",
    r"◆.*$",
    r" FRIDAY ANIME NIGHT$",
    r"\s一挙放送(?:\(\d+\))?(?=\s|$)",
]), "g")
_LEAD_YEAR = _rx(r"^((?:19|20)\d\d) ")


def drop_tails(title):
    """Drop tails; a leading "YYYY " goes to the subtitle."""

    def take(m):
        title["leadYear"] = m.group(1)
        return ""

    title["text"] = _trim(_sub(_LEAD_YEAR, take, TAIL.sub("", title["text"]), 1))


def first_index(pat, text, pos):
    """JS firstIndex: index of the first match at or after pos (INF when none)."""
    if pat is CUT_FIXED:
        return _cut_index(text, pos)
    if pos > len(text):
        return INF
    m = pat.search(text, pos)
    return m.start() if m else INF


QUOTED_TITLE_BEFORE_EPISODE = _rx(r"^[^「\s]+「[^」]+」(?=\s?#\d+(?!\d)\s?[^\s\u0001])")
_GLUED_YEAR = _rx(r"^((?:19|20)\d\d)(?!\d)\s?")
_SPACE_OR_LT = _rx(r"[\s<]")
_ONLY_DELIMS = _rx(r"^[\s\u0001]*$")


def cut_show(title, has_episode_marker, has_genre):
    """Find where the show name ends (not wrapped): the earliest marker, then the space rule."""
    text = title["text"]
    quoted = (has_episode_marker and QUOTED_TITLE_BEFORE_EPISODE.search(text) or None)
    quoted = quoted.group(0) if quoted else ""
    markers = EPS + [SEASON, CUT_FIXED, PAREN] + ([] if has_episode_marker else [COLON])
    marker_at = min(first_index(p, text, len(quoted) or 1) for p in markers)  # never cut at 0
    if marker_at == INF:
        marker_at = len(text)
    words = _trim(text[:marker_at]).split(" ")
    cut = marker_at
    if not has_episode_marker and len(words) > 1 and text[marker_at:marker_at + 1] != ":":
        count = space_rule_word_count(words, text[marker_at:], marker_at >= len(text), has_genre)
        cut = len(" ".join(words[:count]))
    title["show"] = text[:cut]

    year = _GLUED_YEAR.search(text[cut:])
    if year:
        title["gluedYear"] = "" if has_genre(G_NEWS) else \
            year.group(1) + (" " if _SPACE_OR_LT.search(text[cut + 4:cut + 5]) else "")
        cut += len(year.group(0))
    title["cut"] = cut

    text = title["text"] = SEASON.sub(lambda m: "\u0001" * len(m.group(0)), text)

    m = None if has_episode_marker else PAREN.search(text, marker_at)
    if m and _ONLY_DELIMS.search(text[marker_at:m.start()]):
        title.update(episode=int(m.group(1)), episodeEnd=m.group(2), epAt=m.start(), epLen=len(m.group(0)))


_ASCII_WORD = _rx(r"^[\x21-\x7e]+$")
_SP_END = _rx(r"SP$")
_BRACKET_START = _rx(r"^【")
_HAS_DIGIT = _rx(r"\d")
_PAREN_DIGIT_START = _rx(r"^\(\d")
_NUMBERED = _rx(r"[^\x00-\x7f\d]\d{1,3}$")


def space_rule_word_count(words, rest, at_end, has_genre):
    """How many space-separated words form the show (default 1)."""
    def ascii_word(w):
        return bool(_ASCII_WORD.search(w))

    if ascii_word(words[0]):
        count = next((i for i, w in enumerate(words) if not ascii_word(w)), len(words))
    else:
        count = 1
    two_words = (len(words) == 2 and count == 1 and len(words[1]) < 11 and not _SP_END.search(words[1]) and
                 (bool(_BRACKET_START.search(rest)) and not _HAS_DIGIT.search(words[1]) or
                  (at_end or bool(_PAREN_DIGIT_START.search(rest))) and len(words[1]) > 4))
    numbered_drama = has_genre(G_DRAMA) and count == 1 and bool(_NUMBERED.search(words[1]))
    if two_words or numbered_drama:
        count = 2
    nxt = next((w for w in words[count:] if w), None)
    if len("".join(words[:count])) <= 2 and nxt and not _DIGITS.search(nxt):
        count = words.index(nxt, count) + 1
    return count


def find_episode(title):
    """The first episode marker after the show."""
    if title["epAt"] >= 0:
        return
    rest = title["text"][title["cut"]:]
    for pat in (EPS + [PAREN] if title["wrapped"] else EPS):
        m = pat.search(rest)
        if m:
            groups = m.groups() + (None, None)
            title.update(episode=parse_number(m.group(1)), episodeEnd=groups[1] or groups[2],
                         epAt=title["cut"] + m.start(), epLen=len(m.group(0)))
            return


_ARC_MARKS = _rx(r"[▼▽\u0001]", "g")
_SHORT_ARC = _rx(r"^[^\s「」『』]{0,10}$")


def join_wrapped_arc(title):
    """An anime arc joins the show: "「東京リベンジャーズ」三天戦争編 第51話" -> "東京リベンジャーズ 三天戦争編"."""
    if not (title["wrapped"] and title["epAt"] > 0):
        return
    arc = _trim(_ARC_MARKS.sub(" ", SEASON.sub("", title["text"][:title["epAt"]])))
    if _SHORT_ARC.search(arc) and not BLOCK.search(arc):
        title["show"] += " " + arc
        title["cut"] = title["epAt"]


_TRAILING_LABEL = _rx(r"【[^】]*】\s*$")


def build_subtitle(title):
    """The subtitle: the text after the episode marker, else after the show."""
    text, cut, ep_at, ep_len, show = title["text"], title["cut"], title["epAt"], title["epLen"], title["show"]
    if ep_at >= 0:
        sub = clean_subtitle(_sub(_TRAILING_LABEL, "", text[ep_at + ep_len:], 1)) or clean_subtitle(text[cut:ep_at])
    elif title["wrapped"] and text.endswith("「" + show + "」"):
        sub = clean_subtitle(text[:text.rfind(" ")])
    else:
        sub = clean_subtitle(text[cut:])
    if title.get("medley") is not None:
        title["subtitle"] = clean_subtitle(title["medley"])
    else:
        title["subtitle"] = " ".join(x for x in [title.get("leadYear"), _trim(title["gluedYear"] + sub)] if x)


_KANA_READING = _rx(r"(?<=[一-鿿])\([ぁ-ゖー]+\)(?![ぁ-ゖ])", "g")
_SP_TAIL = _rx(r"(?<=[^\sA-Z])(?:\d+(?:\.\d)?時間)?SP$")
_DELIM = _rx(r"\u0001", "g")
_SHOW_EDGES = _rx(r"[\s・:/,、(]+$|^[\s・:]+", "g")


def tidy_show(show):
    """No season words, kana readings ("夢華録(むかろく)" -> "夢華録"), trailing SP or punctuation."""
    show = _sub(_SP_TAIL, "", _KANA_READING.sub("", SEASON.sub("", show)), 1)
    return _SHOW_EDGES.sub("", _SPACES.sub(" ", _DELIM.sub(" ", show)))


_ENDS_WITH_SPACE = _rx(r"\s$")
_SHOW_TAIL_PUNCT = _rx(r"[\s・:]+$")


def move_version_suffix(title, dir):
    """Move short-version suffixes from the show to the subtitle (not with a DIR)."""
    text, ep_at, ep_len, cut = title["text"], title["epAt"], title["epLen"], title["cut"]
    own = text[ep_at:ep_at + ep_len] if ep_at >= 0 and ep_at == cut and not _ENDS_WITH_SPACE.search(text[:ep_at]) \
        else ""
    while not dir:
        m = SUFFIX.search(title["show"])
        head = title["show"][:m.start()] if m else ""
        if not m or len(key(head)) <= 3 or SUFFIX_KEEP.search(_trim(head)):
            break
        if own:
            title["episode"] = title["episodeEnd"] = None
        title["subtitle"] = " ".join(x for x in [_trim(m.group(0)) + own, title["subtitle"]] if x)
        own = ""
        title["show"] = _sub(_SHOW_TAIL_PUNCT, "", head, 1)


_MOVIE_TAIL = _rx(r"[\u0001【▽◆].*$")
_EDITIONS = _rx(ED, "g")
_DRAMA_GLUED = _rx(r"(?<=[^\x00-\x7f\d])(\d{1,3})$")
_EIGA_FLAG = _rx(r"\[映画?\]")


def parse_title(name, genres=(), dir="", film_text=False):
    """Parse an EPG programme name (JS parse()). genres = [(level1, level2), ...]; dir = the rule's directory.
    film_text (KODIstation): the EPG text has a film line ("2023年 124分"), so 映画/その他 counts as a film genre.

    Returns {kind, show, episode, episode_end, season, subtitle, year, edition, version}; missing values are None.
    season (KODIstation): the season number in the name, only when an episode number was parsed.
    """
    def has_genre(n):
        return any(int(g[0]) == n for g in genres)

    normalized = norm(_sub(BLURB_AFTER_EPISODE, r"\1", str(name), 1))
    # KODIstation: 映画/その他 alone is mostly channel promos and film-star documentaries, not a film
    is_movie_genre = any(int(g) == G_MOVIE and (film_text or int(sg) != SG_OTHER) for g, sg in genres) or \
        bool(_EIGA_FLAG.search(normalized))
    text, year = strip_noise(normalized)
    result = dict.fromkeys(("kind", "show", "episode", "episode_end", "season", "subtitle", "year", "edition",
                            "version"))

    umbrella = parse_umbrella(text, dir)
    if umbrella:
        result.update(umbrella)
        return result

    title = {"text": text, "show": None, "cut": 0, "episode": None, "episodeEnd": None, "epAt": -1, "epLen": 0,
             "gluedYear": ""}
    take_leading_show(title, has_genre)
    take_medley_prefix(title)
    drop_tails(title)

    has_episode_marker = any(p.search(title["text"]) for p in EPS)
    kind = "movie" if is_movie_genre and not has_episode_marker else "tv"
    if kind == "movie":
        show = title["show"] if title["show"] is not None else _sub(_MOVIE_TAIL, "", title["text"], 1)
        show = show.replace("\u0001", " ")
        slot = MOVIE_SLOT.search(show)
        show = (slot.group(1) if slot else None) or show
        edition = " ".join(m.group(0) for m in _EDITIONS.finditer(normalized)) or None
        version = next((v for v, pat in VERSION if pat.search(normalized)), None)
        result.update(kind=kind, show=_trim(show), year=year, edition=edition, version=version, subtitle="")
        return result

    title["wrapped"] = title["show"] is not None
    if not title["wrapped"]:
        cut_show(title, has_episode_marker, has_genre)
    find_episode(title)
    join_wrapped_arc(title)
    glued = title["episode"] is None and not title["wrapped"] and has_genre(G_DRAMA) and \
        _DRAMA_GLUED.search(title["show"])
    if glued:
        title["episode"] = int(glued.group(1))
        title["show"] = title["show"][:glued.start()]
    build_subtitle(title)
    title["show"] = tidy_show(title["show"])
    move_version_suffix(title, dir)

    rest = title["text"].replace("\u0001", " ")
    if not title["show"]:
        title["show"] = _trim(rest) or norm(name)
    dir_key = key(PIN.sub("", dir))
    if dir_key and title["episode"] is None and key(title["show"]) != dir_key:
        for i in range(1, len(rest) + 1):
            if key(rest[:i]) == dir_key:
                title["subtitle"] = clean_subtitle(rest[i:])
                break
    end = title["episodeEnd"]
    result.update(kind=kind, show=dir or title["show"], episode=title["episode"],
                  episode_end=parse_number(end) if end is not None else None, subtitle=title["subtitle"], year=year,
                  season=season_of(text, _episode_index(text, title)) if title["episode"] is not None else None)
    return result


def _episode_index(text, title):
    """Where the parsed episode marker ("#8", "第67回") is in text (None when it cannot be found there)."""
    if title["epAt"] < 0:
        return None
    marker = title["text"][title["epAt"]:title["epAt"] + title["epLen"]]
    i = text.find(marker) if marker and "\u0001" not in marker else -1
    return i if i >= 0 else None


_LABEL_OR_ANGLE = _rx(r"【([^】]*)】|<([^>]*)>", "g")
_TILDE_HEAD = _rx(r"^~([^~]*)~\s?")
_LEAD_DECOR = _rx(r"^(?:[▽▼◆◇★:~・/\-\s]|選(?=[\s「“]|$)|\d{1,2}(?=[\s「]))+")
_ONLY_QUOTES = _rx(r"^(?:「[^「」]*」[\s/]*)+$")
_QUOTE = _rx(r"「[^」]*」", "g")
_ONE_QUOTE = _rx(r"^「([^「」]*)」(\(.*\))?$")
_ONE_PAREN = _rx(r"^\(([^()]*)\)$")


def clean_subtitle(text):
    """No seasons or leading decorations, unwrap 【…】 and an outer 「…」. "「A」「B」" -> "A/B"."""
    text = _LABEL_OR_ANGLE.sub(r" \1\2 ", SEASON.sub(" ", text))
    text = _trim(_SPACES.sub(" ", text.replace("\u0001", " ")))
    text = _trim(_sub(_LEAD_DECOR, "", _sub(_TILDE_HEAD, r"\1 ", text, 1), 1))
    if _ONLY_QUOTES.search(text):
        text = "/".join(q[1:-1] for q in _QUOTE.findall(text))
    else:
        m = _ONE_QUOTE.search(text) or _ONE_PAREN.search(text)
        if m:
            groups = m.groups() + (None,)
            text = m.group(1) + (groups[1] or "")
    if "「" not in text:
        text = text.replace("」", "")
    if "」" not in text:
        text = text.replace("「", "")
    return _trim(text)


# =====================================================================================================================
# 4. EXTENDED text: plot sections and credits
# =====================================================================================================================

CREDIT_SECTION = _rx(r"出演|(?<!音)声|キャスト|司会|語り|ナレ|スタッフ|原作|脚本|監督|演出|関連情報")
DIRECTOR_LABEL = _rx(r"監督|演出")
WRITER_LABEL = _rx(r"脚本|^作$|原作|原案|構成")
ACTOR_LABEL = _rx(r"出演|声|語り|ナレ|司会|MC|ゲスト|キャスト|キャスター|解説|実況|アナ|リポーター|"
                  r"レポーター|進行|講師|案内|パーソナリティ|.役$")
NOT_A_NAME = _rx(r"^[\d(《〈<[]|[：:「」『』◆◇☆※]|^(?:キャスト|役名|出演者?)$")
CREDIT_LINE_LABEL = _rx(r"^(?:出演者?|声の出演|キャスト|監督|演出|脚本|原作|ナレーション|ナレーター|語り|司会)$")

_CRLF = _rx(r"\r\n?", "g")
# KODIstation, the header rule of items.raw_extended: sections are joined with a bare \n and a header line ends with a
# bare \n; values use \r\n, so "\r\n◇X(旧Twitter)\r\n" or "◇公式ページ\n◇番組HP\r\n…" inside a value is no header.
_SECTION_SPLIT = _rx(r"(?<=[^\r])\n(?=◇[^\r\n]*(?:\n|$))")
_SECTION = _rx(r"^(?:◇+([^\n]*)\n?)?([\s\S]*)$")
_SECTION_KEY = _rx(r"^【(.*)】$|\d+$", "g")


def extended_sections(ext):
    """Split EXTENDED ("◇key\\nvalue…") into [(key, text)]; "◇【番組内容】" -> "番組内容", "◇出演者2" -> "出演者"."""
    out = []
    for part in _SECTION_SPLIT.split(ext):
        m = _SECTION.match(_CRLF.sub("\n", part))
        out.append((_SECTION_KEY.sub(r"\1", norm(m.group(1))), _trim(m.group(2))))
    return out


_PROGRAM_CONTENT = _rx(r"^番組内容")
_LABEL_LINE = _rx(r"^【[^】\n]*】\n")  # a section text that starts with a "【label】" line of its own (not only that)


def build_plot(desc, sections):
    """DESCRIPTION, then each EXTENDED section under "【key】"; parts contained in another part are dropped.

    KODIstation: no "【key】" line above a section text whose first line is a "【label】" of its own.
    """
    every = [(None, _trim(desc))] + list(sections)
    keys = [key(text) for _, text in every]
    parts = [(k, text) for i, (k, text) in enumerate(every)
             if text and not any(j != i and keys[i] in keys[j] and (keys[i] != keys[j] or j < i)
                                 for j in range(len(every)))]
    out = []
    for i, (k, text) in enumerate(parts):
        prev = parts[i - 1][0] if i else None
        header = k and not _PROGRAM_CONTENT.search(k) and k != prev and not _LABEL_LINE.search(text)
        out.append(("【" + k + "】\n" if header else "") + text)
    return "\n\n".join(out)


_CREDIT_LINE_SPLIT = _rx(r"(?:^|(?<=[\s／/]))([^\s：:／/、。「」()（）]{1,8})[：:]", "m")
_VALUE_END = _rx(r"\n|　|\s{2}")
_VALUE_TAIL = _rx(r"[／/\s]+$")


def credit_lines(text):
    """"label：value" runs in running text -> [(label, value)] for the credit labels only."""
    runs, found = _CREDIT_LINE_SPLIT.split(text), []
    for i in range(1, len(runs), 2):
        label = norm(runs[i])
        if CREDIT_LINE_LABEL.search(label):
            found.append((label, _sub(_VALUE_TAIL, "", _VALUE_END.split(runs[i + 1])[0], 1)))
    return found


_ENTRY_SPLIT = _rx(r"[，、,／\n](?![^(（)）]*[)）])")
_ETC = _rx(r"\s*\[?(?:ほか|他)(?:多数)?\]?$")  # "[ほか]": EPGStation's 🈀
_DATE_NOTE = _rx(r"\s+\d{4}年.*$|(?:\s*\((?=[^()]*\d)[^()]*\))+$")
_BOOK_NOTE = _rx(r"\s*(?:『[^』]*』)?\s*(?:\([^()]*\))?$")
_ROLE_DOTS = _rx(r"^(?:(.*?)\s?\.{3}\s?)?(.*)$")
_VOICE_LABEL = _rx(r"^声")
_VOICED = _rx(r"^(.+?)\s?\(([^()]+)\)$")
_ROLE_FIRST = _rx(r"^\(([^()]+)\)\s?([^()]+)$")
_YAKU = _rx(r"(.)役$")
_COLON_SECTIONS = _rx(r"声|キャスト|出演")
_CREDIT_RUNS = _rx(r"【([^】]+)】|^\[([^\]\n]{1,12})\]$|^(?!https?[：:])([^：:\n【「」[]{1,20})[：:]", "m")
_NO_SECTION_LABEL = _rx(r"関連|スタッフ")
DUB_ROLE = "吹替: "
_CAST_LABEL = _rx(r"出演|キャスト")  # on-screen cast only, not 解説 / ナレーション / 司会
_VOICE_PREFIX = _rx(r"^(?:声|吹替)\s?:\s?")  # "(声:山寺宏一)"
_NOT_A_VOICE = _rx(r"^(?:声|吹替)?$|版$")  # "(声)", "(山路和弘/ソフト版)": no voice actor of this broadcast
_TRAILING_PAREN = _rx(r"\([^()]*\)$")
# a production year line "１９７３年／日本" after the credits is not a credit entry (KODIstation)
_YEAR_LINE = _rx(r"^[ \t　]*(?:19|20|１９|２０)[\d０-９]{2}年(?:[／/ 　].*)?$", "m")


def _name_length(name):
    """The length the 20-character name limit checks: without one trailing "(…)"
    ("ハーブ・エデルマン(マイケル・ジョーダン)"), unless another "(" remains ("A(x) B(y)" is a list)."""
    m = _TRAILING_PAREN.search(name)
    return m.start() if m and "(" not in name[:m.start()] else len(name)


def build_credits(sections, desc="", dubbed=False):
    """People: [("director"|"credits", name) | ("actor", (name, role))].

    dubbed (KODIstation, not in epgXport): in a dubbed broadcast a cast entry (出演/キャスト label)
    "マーカス・ベルウッド(森川陽太)" or "…(声:森川陽太)" is the actor and their Japanese voice, which becomes the role
    "吹替: 森川陽太" (Kodi shows it under the actor's name; invented names). Elsewhere
    "(…)" is an affiliation or reading ("佐久間大介(Snow Man)") and stays part of the name, as in epgXport.
    """
    people, seen = [], set()

    def add(label, text, colon_label=False, section_key=""):
        if DIRECTOR_LABEL.search(label):
            tag = "director"
        elif WRITER_LABEL.search(label):
            tag = "credits"
        elif ACTOR_LABEL.search(label) or (colon_label and _COLON_SECTIONS.search(section_key)):
            tag = "actor"
        else:
            return
        for entry in _ENTRY_SPLIT.split(_YEAR_LINE.sub("", text or "")):
            entry = _sub(_DATE_NOTE, "", _sub(_ETC, "", norm(entry), 1), 1)
            if tag != "actor":
                entry = _sub(_BOOK_NOTE, "", entry, 1)
            m = _ROLE_DOTS.match(entry)
            role, name = m.group(1), m.group(2)
            voiced = _VOICE_LABEL.search(label) and _VOICED.search(name)
            if voiced:
                name, role = voiced.group(1), voiced.group(2)
            role_first = tag == "actor" and _ROLE_FIRST.search(name)
            if role_first:
                role, name = role_first.group(1), role_first.group(2)
            dub = dubbed and tag == "actor" and not (voiced or role_first or role) and _CAST_LABEL.search(label) \
                and _VOICED.search(name)
            if dub:
                voice = _sub(_VOICE_PREFIX, "", dub.group(2), 1)
                if not _NOT_A_VOICE.search(voice):
                    name, role = dub.group(1), DUB_ROLE + voice
            if not name or _name_length(name) > 20 or NOT_A_NAME.search(name) or (tag, name) in seen:
                continue
            seen.add((tag, name))
            people.append((tag, (name, role or _sub(_YAKU, r"\1", label, 1)) if tag == "actor" else name))

    for section_key, text in sections:
        if not CREDIT_SECTION.search(section_key):
            continue
        # [text0, 【label】, [label] (a line of its own), label：, text1, ...]
        runs = _CREDIT_RUNS.split(text)
        for i in range(0, len(runs), 4):
            if i:
                label = norm(runs[i - 3] or runs[i - 2] or runs[i - 1])
            else:
                label = "" if _NO_SECTION_LABEL.search(section_key) else norm(section_key)
            add(label, runs[i], i > 0 and runs[i - 1] is not None, section_key)
    for text in [desc] + [t for k, t in sections if not CREDIT_SECTION.search(k)]:
        for label, value in credit_lines(text):
            add(label, value)
    return people


def parse_extended(desc, ext, dubbed=False):
    """DESCRIPTION + EXTENDED -> {"plot": str, "people": [...]} (JS extended()); dubbed: see build_credits."""
    sections = extended_sections(ext or "")
    return {"plot": build_plot(desc or "", sections), "people": build_credits(sections, desc or "", dubbed)}


def is_dubbed(name):
    """A dubbed broadcast: 吹替 / [吹] in the name (epgXport's VERSION 吹替版 rule), movie or not."""
    return bool(VERSION[0][1].search(norm(name)))


# =====================================================================================================================
# 5. Genre fields, flag tags and the movie year
# =====================================================================================================================

_FLAG_MARK = _rx(r"\[(.)\]", "g")


def genre_fields(genres, raw_name):
    """Genres -> {"genre_tags": [...], "flag_tags": [...]}; raw_name = norm(name). 福祉 手話/字幕/解説放送 are flags."""
    tags, flag_tags = [], [FLAG_TAG[c] for c in _FLAG_MARK.findall(raw_name) if c in FLAG_TAG]
    for g, sg in genres:
        if g == G_WELFARE and sg > 3:
            flag_tags.append(SUBGENRE[g][sg] if sg < len(SUBGENRE[g]) else None)
        elif 0 <= g < len(GENRE):
            tags.append(GENRE[g])
            if 0 <= sg < len(SUBGENRE[g]):
                tags.append(SUBGENRE[g][sg])
    return {"genre_tags": list(dict.fromkeys(tags)), "flag_tags": flag_tags}


MOVIE_YEAR = _rx(r"((?:19|20)\d\d)年\s?[・/]?\s?(?:本編)?\d{2,3}分|\d{2,3}分。((?:19|20)\d\d)年|"
                 r"製作年?\D{0,3}((?:19|20)\d\d)|((?:19|20)\d\d)年\s?[^\s\d()~]{1,8}(?:制作|製作)")
# KODIstation's fallbacks (not in epgXport), tried in this order when MOVIE_YEAR finds nothing:
# 1. a year with the country / format / 制作 in brackets: "(1995年 アメリカ)", "(1999年:アメリカ)",
#    "(1996年・アメリカ・139分・カラー)", "(2018年制作)"; not a publication "(1968年 文藝春秋刊)", not a date
#    "(2016年6月3日 …)" or a note "(2022年初回放送)";
# 2. a bare "(2013年)" / "(2013)", but not an award edition "第89回(2017年)".
MOVIE_YEAR_COUNTRY = _rx(r"(?<!回)\(((?:19|20)\d\d)年(?:[\s:・/][^()]{0,40}|制作|製作)(?<!刊)\)")
MOVIE_YEAR_BARE = _rx(r"(?<!回)\(((?:19|20)\d\d)年?\)")
# 3. a production year line "YYYY年/country" or "YYYY年 country" before 監督/原作/脚本/N分/◇ or the end:
#    "1973年/日本 ◇おしらせ", "2014年 韓国 監督:…"; not "2020年 女子決勝「…" or "◇制作 2026年 ◇出演者".
MOVIE_YEAR_LINE = _rx(r"(?:^|\s)((?:19|20)\d\d)年\s?[/\s]\s?[^\s\d()◇「【]{1,12}(?=\s(?:監督|原作|脚本|\d+分|◇)|$)")


def movie_year(desc, ext):
    """A movie year from the EPG text: "(1983年 141分)", "製作年:1975", "(1995年 アメリカ)"."""
    text = norm((desc or "") + " " + (ext or ""))
    m = MOVIE_YEAR.search(text)
    if m:
        return next(g for g in m.groups() if g)
    m = MOVIE_YEAR_COUNTRY.search(text) or MOVIE_YEAR_BARE.search(text) or MOVIE_YEAR_LINE.search(text)
    return m.group(1) if m else None


# =====================================================================================================================
# Entry point for KODIstation
# =====================================================================================================================

def genres_of(item):
    """[(genre, subGenre or 0)] of an API item (genre1..3 / subGenre1..3), like epgXport's GENREn / SUBGENREn."""
    return [(int(item[f"genre{i}"]), int(item.get(f"subGenre{i}") or 0))
            for i in (1, 2, 3) if item.get(f"genre{i}") is not None]


def metadata(item, on_error=None):
    """Everything the parser knows about one RecordedItem / ScheduleProgramItem (full-width texts).

    Returns None for a blank name, and instead of raising (the caller falls back to the plain item):
        {"kind": "movie"|"tv", "show", "episode", "episode_end", "season", "subtitle", "year" (int), "edition",
         "version", "tags": [...], "cast": [(name, role)], "directors": [...], "writers": [...], "plot"}
    year: the year in the name, for movies else the production year from the EPG text.
    tags: edition words, version, flag tags (字幕, 再放送, ...), de-duplicated, like epgXport's NFO tags.
    on_error(exception) is called when the parser fails (not for a blank name), so the caller can log it.
    """
    try:
        name = item.get("name")
        if not norm(name):
            return None
        genres = genres_of(item)
        desc, ext = item.get("description") or "", item.get("extended") or ""
        title = parse_title(name, genres, "", bool(MOVIE_YEAR.search(norm(desc + " " + ext))))
        extended = parse_extended(desc, ext, is_dubbed(name))
        flags = genre_fields(genres, norm(name))["flag_tags"]
        extra = (title["edition"].split(" ") if title["edition"] else []) + [title["version"]]
        year = title["year"] or (movie_year(desc, ext) if title["kind"] == "movie" else None)
        people = extended["people"]
        out = {k: title[k] for k in ("kind", "show", "episode", "episode_end", "season", "subtitle", "edition",
                                     "version")}
        out.update(year=int(year) if year else None, tags=[t for t in dict.fromkeys(extra + flags) if t],
                   cast=[v for tag, v in people if tag == "actor"],
                   directors=[v for tag, v in people if tag == "director"],
                   writers=[v for tag, v in people if tag == "credits"], plot=extended["plot"])
        return out
    except Exception as e:  # noqa: BLE001 - never break a listing over one odd EPG text
        if on_error:
            on_error(e)
        return None
