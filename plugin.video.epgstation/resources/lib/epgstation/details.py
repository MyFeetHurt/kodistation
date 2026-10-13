"""Full EPG detail view (backlog 002): the 【録画情報】 block at the end of a recording's plot, the audio / aspect
stream details and the drop log text.

No Kodi imports here: everything is unit-testable (like items.py). items.recorded_item builds the block and the
ドロップログ context entry; plugin.py adds the AudioStreamDetail / aspect and runs do_droplog (one GET on demand).

API facts (EPGStation 2.10.0 source):
- The drop counts come with every RecordedItem as `dropLogFile` {id, errorCnt, dropCnt, scramblingCnt}; openapi.json
  calls it `dropLog`, but RecordedItemUtil.ts:135 sends `dropLogFile`. Only recordings made with isEnabledDropCheck
  have one.
- GET /dropLogs/{dropLogFileId}?maxsize=<KB>: text/plain; 404 unknown id, 416 when the file is larger than maxsize.
  Event lines ("drop (pid: 0x100D, counter: 4, expected: 3, time: ...)"), a blank line, then one summary line per PID
  ("pid: 0x100D, error: 0, drop: 91, scrambling: 0, packet: 35730801, name: MPEG2 VIDEO").
- audioComponentType / audioSamplingRate describe the main audio stream only (ProgramDB.ts, isMain).
"""
from .recorded import files_line, time_line

# English fallbacks; plugin.py passes the localized ones (strings.po 30251-, Kodi's language).
DEFAULT_STRINGS = {
    "detail_header": "[Recording]", "detail_broadcast": "Broadcast: {0}", "detail_genre": "Genre: {0}",
    "detail_video": "Video: {0}", "detail_audio": "Audio: {0}",
    "detail_drops": "Drops {0} / Errors {1} / Scrambled {2}", "detail_encoding": "Encoding",
    "droplog": "Drop log", "droplog_large": "Drop log too large", "droplog_gone": "No drop log",
    # used inside the block too: recorded.py / items.py keys
    "files": "Files: {0}", "weekdays": "Sun,Mon,Tue,Wed,Thu,Fri,Sat", "length": "Length",
    "protect_line": "Protected (not auto-deleted)",
}

# ARIB STD-B10 audio component types (EPGStation client/src/lib/event.ts AudioComponentType): label per language.
AUDIO_JA = {1: "モノラル", 2: "デュアルモノ", 3: "ステレオ", 9: "5.1ch"}
AUDIO_EN = {1: "Mono", 2: "Dual mono", 3: "Stereo", 9: "5.1ch"}
# The other defined modes: EPGStation's mode text ("3/1モード" / "3/1 mode"); 0 and 18-31 are reserved (no line).
AUDIO_MODES = {
    4: "2/1", 5: "3/0", 6: "2/2", 7: "3/1", 8: "3/2", 10: "3/3.1", 11: "2/0/0-2/0/2-0.1", 12: "5/2.1",
    13: "3/2/2.1", 14: "2/0/0-3/0/2-0.1", 15: "0/2/0-3/0/2-0.1", 16: "2/0/0-3/2/3-0.2", 17: "3/3/3-5/2/3-3/0/0.2",
}
# Kodi's audio channel count per mode (the flag row icon); modes not listed get no AudioStreamDetail.
AUDIO_CHANNELS = {1: 1, 2: 2, 3: 2, 4: 3, 5: 3, 6: 4, 7: 4, 8: 5, 9: 6, 10: 7, 12: 8, 13: 8, 17: 24}
AUDIO_CODEC = "aac"  # ISDB audio is MPEG-2 AAC

VIDEO_NAMES = {"mpeg2": "MPEG-2", "h.264": "H.264", "h.265": "H.265"}
# videoComponentType low nibble -> (Kodi aspect, label)
ASPECTS = {1: (1.33, "4:3"), 2: (1.78, "16:9"), 3: (1.78, "16:9"), 4: (2.21, ">16:9")}


def block_strings(strings):
    """The strings for the block: Kodi's localized ones (interface text follows Kodi's language), English where Kodi
    has none yet. Only the genre names and the audio label follow genre_language."""
    return dict(DEFAULT_STRINGS, **{k: v for k, v in (strings or {}).items() if v})


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def audio_label(rec, lang="ja"):
    """"ステレオ" / "Stereo" for the main audio stream; "" when unknown, reserved or absent."""
    mode = _int(rec.get("audioComponentType"))
    names = AUDIO_EN if lang == "en" else AUDIO_JA
    if mode in names:
        return names[mode]
    if mode in AUDIO_MODES:
        return AUDIO_MODES[mode] + (" mode" if lang == "en" else "モード")
    return ""


def sampling_label(rec):
    """48000 -> "48kHz", 44100 -> "44.1kHz"; "" when absent."""
    rate = _int(rec.get("audioSamplingRate"))
    if not rate:
        return ""
    return f"{rate / 1000:g}kHz"


def audio_stream(rec):
    """{"codec": "aac", "channels": n} for Kodi's AudioStreamDetail, or None when the mode is unknown."""
    channels = AUDIO_CHANNELS.get(_int(rec.get("audioComponentType")))
    return {"codec": AUDIO_CODEC, "channels": channels} if channels else None


def _aspect(rec):
    vct = _int(rec.get("videoComponentType"))
    return ASPECTS.get(vct & 0x0F) if vct is not None else None


def aspect(rec):
    """Kodi's VideoStreamDetail aspect from videoComponentType (0xb3 -> 1.78); 0 when unknown."""
    a = _aspect(rec)
    return a[0] if a else 0


def video_label(rec):
    """"MPEG-2 1080i 16:9"; "" without video fields."""
    vtype = rec.get("videoType")
    parts = [VIDEO_NAMES.get(vtype, vtype or ""), rec.get("videoResolution") or ""]
    if not any(parts):
        return ""
    a = _aspect(rec)
    return " ".join(p for p in parts + [a[1] if a else ""] if p)


def drop_counts(rec):
    """(drop, error, scrambling) of the recording's dropLogFile, or None without one."""
    log = rec.get("dropLogFile")
    if not isinstance(log, dict):
        return None
    return tuple(_int(log.get(k)) or 0 for k in ("dropCnt", "errorCnt", "scramblingCnt"))


def has_drop_problem(rec):
    """User decision 2a: the ドロップログ entry only when any of drop / error / scrambling count > 0."""
    counts = drop_counts(rec)
    return bool(counts and any(counts) and rec["dropLogFile"].get("id") is not None)


def detail_lines(rec, channel, genres, lang="ja", strings=None, extra=()):
    """lang (genre_language) picks the audio label and the genre separator; genres come in already localized; labels
    from strings. English genres are joined with "; " (ARIB subgenre names contain ", ", subgenres (009)).
    The 【録画情報】 block: header, then only the lines with data, in this order: 放送, ジャンル, 映像, 音声,
    ファイル, ドロップ (red when drop or scrambling > 0), エンコード中, then `extra` (Partial 長さ, 保護)."""
    s = block_strings(strings)
    lines = [s["detail_header"], s["detail_broadcast"].format(f"{channel} {time_line(rec, s)}".strip())]
    if genres:
        lines.append(s["detail_genre"].format(("; " if lang == "en" else ", ").join(genres)))
    video = video_label(rec)
    if video:
        lines.append(s["detail_video"].format(video))
    audio = " ".join(p for p in (audio_label(rec, lang), sampling_label(rec)) if p)
    if audio:
        lines.append(s["detail_audio"].format(audio))
    files = files_line(rec, s)
    if files:
        lines.append(files)
    counts = drop_counts(rec)
    if counts is not None:
        line = s["detail_drops"].format(*counts)
        lines.append(f"[COLOR red]{line}[/COLOR]" if counts[0] or counts[2] else line)
    if rec.get("isEncoding"):
        lines.append(s["detail_encoding"])
    lines.extend(extra)
    return lines


def droplog_text(text):
    """The drop log for the text viewer: the per-PID summary first, a blank line, then the event lines. A log without
    a summary comes back as it is."""
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    summary = [ln for ln in lines if ln.startswith("pid:")]
    events = [ln for ln in lines if ln and not ln.startswith("pid:")]
    if not summary:
        return "\n".join(events)
    return "\n".join(summary + ([""] + events if events else []))
