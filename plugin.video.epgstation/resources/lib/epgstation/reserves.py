"""Scheduled recordings (backlog 007): the 録画予約 list, its 競合 / 重複 / 除外 folders and the reservation actions.

No Kodi imports here: everything is unit-testable (like items.py). plugin.py turns the dicts into ListItems and runs
the one audited write path (do_reserve_action). This module never imports live or guide: 014 (record from the guide)
will make the guide import reserves, never the other way round.

Reservation states follow EPGStation's getCnts priority: conflict > skip > overlap > normal.
Actions (one context entry per reservation, chosen by action_for):
    stop       normal, listed in /recording   DELETE /reserves/{id}   (stops, keeps the partial recording)
    cancel     normal or conflict, not ended  DELETE /reserves/{id}   (manual: deleted; rule: becomes a skip)
    unskip     skip, not ended                DELETE /reserves/{id}/skip
    unoverlap  overlap, not ended             DELETE /reserves/{id}/overlap

The feature is meant to be removable. Hooks outside this module (code hooks are marked "reservations (007)"):
- plugin.py: do_root (録画予約), do_reserves, do_reserve_info, do_reserve_action, _reserve_strings, _reserve_error,
  _all_reserves, _reserve_of_recording, _wait_stopped, SORT_RESERVES, NON_DIRECTORY_ACTIONS, _item_strings "stop",
  the reserves import
- items.py: root_menu (録画予約), recorded_item (録画を停止 entry), rule_name (shared with rule_folders), "stop" string
- api.py: the "writes (007)" section (delete, cancel_reserve, remove_skip, remove_overlap), _request, reserves(),
  reserve(), reserve_lists(), ApiError.detail
- strings 30030-30057 (both .po)
- tests: test_reserves.py; the reservation tests in test_plugin.py; test_smoke_tool.py reservation tests; kodi_stubs
  (xbmcgui.py Dialog.yesno / yesno_answers / yesnos / DLG_YESNO_NO_BTN, xbmc.py executebuiltin / builtins /
  getLocalizedString), conftest.py resets; fixtures api/reserves.json, api/reserves/cnts.json, reserves_meta.json
- tools: fake_epgstation.py (reservation state, writes, write_error), snapshot_api.py --reserves (make
  snapshot-reserves), kodi_smoke.py check_reserves, tools/dev/test_reserve.py

Record from the guide (backlog 014) builds on this module (guide.py imports it, never the other way round). Remove it
first; its hooks are marked "record from the guide (014)":
- this module: the "record from the guide (014)" section (RESERVED, lists_index … add_error) and its strings
- guide.py: program_item / program_items index and records, mark_channels
- plugin.py: do_record, _reserve_index, _records_for, _encode_mode, _record_strings, _mark_strings, RECORD_STRINGS,
  the guide listings' index/records (do_live, do_guide_now, do_guide_genre, do_guide_channel), NON_DIRECTORY_ACTIONS
- api.py: post(), create_reserve() in the writes section, _request's body
- settings.xml: category "record" (record_encode); strings 30061-30071, 30240-30244 (both .po)
- tests: the 014 sections of test_reserves.py, test_guide.py, test_plugin.py and test_smoke_tool.py
- tools: fake_epgstation.py POST /reserves (add_conflict), kodi_smoke.py guide/live reservation markers,
  tools/dev/test_reserve.py pick / snapshot / adopt
"""
import time

from . import epgmeta
from .genres import item_genres
from .items import (
    MENU_ICONS,
    build_plot,
    extended_props,
    fmt_time,
    folder,
    metadata_info,
    plugin_url,
    raw_plot_texts,
    to_half,
)

PAGE = 1000  # reservations per GET /reserves page (the server's default limit is 24)
STOP_WAIT = 5.0  # seconds to wait for a stopped recording to leave /recording (the server stops it asynchronously)
STOP_STEP = 0.5
NORMAL = "normal"
STATES = ("conflict", "overlap", "skip")  # folder order (web UI words 競合, 重複, 除外)
OPS = ("stop", "cancel", "unskip", "unoverlap")
RECORDING = "[COLOR red]●[/COLOR] "
MARKERS = {"conflict": "[COLOR red][{0}][/COLOR] ", "overlap": "[COLOR grey][{0}][/COLOR] ",
           "skip": "[COLOR grey][{0}][/COLOR] "}

DEFAULT_STRINGS = {
    "reserves": "Scheduled recordings", "conflict": "Conflicts", "overlap": "Duplicates", "skip": "Skipped",
    "delete": "Delete reservation", "skip_this": "Skip this airing", "stop": "Stop recording",
    "unskip": "Undo skip", "unoverlap": "Record anyway",
    "q_delete": "Delete this reservation?",
    "q_skip": "Do not record this airing? The rule stays.",
    "q_stop": "Stop recording? What has been recorded so far is kept.",
    "q_unskip": "Undo the skip and record this airing?",
    "q_unoverlap": "This program was recorded before. Record it anyway?",
    "q_moves": "This airing will not be recorded.",
    "q_airing": "Already airing: recording starts now",
    "q_stop_rest": "The rest of this airing will not be recorded.",
    "done_delete": "Reservation deleted", "done_skip": "Airing will not record", "done_stop": "Recording stopped",
    "done_unskip": "Skip undone", "done_unoverlap": "Will record", "failed": "Failed: {0}",
    "failed_net": "Failed (connection error)",
    "gone": "Reservation is gone", "changed": "Reservation has changed",
    "rule": "Rule: {0}", "manual": "Manual reservation", "line_conflict": "Conflict: will not record",
    "line_overlap": "Recorded before: will not record", "line_skip": "Skipped: will not record",
    "recording": "Recording now", "weekdays": "Sun,Mon,Tue,Wed,Thu,Fri,Sat",
    # record from the guide (014)
    "record": "Record", "q_record": "Record this program?",
    "q_record_airing": "On air: recording starts now, partway in",
    "encode": "Encode: {0}", "done_record": "Recording scheduled", "done_record_now": "Recording started",
    "already": "Already scheduled", "add_conflict": "Not scheduled: no free tuner (conflict)",
    "ended": "This program has ended", "by_rule": "Scheduled: rule", "by_manual": "Scheduled: manual",
    "program_gone": "Program no longer in the guide",
}


def _strings(strings):
    # Kodi returns "" for string IDs it has not loaded yet (addon updated without restart).
    return dict(DEFAULT_STRINGS, **{k: v for k, v in (strings or {}).items() if v})


# --- states -------------------------------------------------------------------------------------------------------

def classify(r):
    """normal | conflict | skip | overlap, with EPGStation's getCnts priority (conflict > skip > overlap)."""
    if r.get("isConflict"):
        return "conflict"
    if r.get("isSkip"):
        return "skip"
    if r.get("isOverlap"):
        return "overlap"
    return NORMAL


def upcoming(reserves, now_ms):
    """Reservations that have not ended (endAt > now), in the given order. EPGStation keeps ended ones until a
    restart or the end of a recording; they cannot record any more."""
    return [r for r in reserves if r["endAt"] > now_ms]


def counts(reserves):
    out = {NORMAL: 0, "conflict": 0, "skip": 0, "overlap": 0}
    for r in reserves:
        out[classify(r)] += 1
    return out


def split(reserves, state):
    return [r for r in reserves if classify(r) == state]


def is_recording(r, records):
    """A normal reservation that EPGStation records now: matched in /recording (records), not by the clock (a failed
    recording, or the seconds before the recorder starts, are inside the time window but not recording)."""
    return classify(r) == NORMAL and recording_still_listed(records or (), r)


def _is_rule(r):
    return r.get("ruleId") is not None


def action_for(r, now_ms, records=()):
    """The one action (OPS) the context menu offers for this reservation now, or None (ended). records: /recording."""
    if r["endAt"] <= now_ms:
        return None
    state = classify(r)
    if state == "skip":
        return "unskip"
    if state == "overlap":
        return "unoverlap"  # a cancel would leave it an overlap (EPGStation), so none is offered
    if is_recording(r, records):
        return "stop"
    return "cancel"


def entry_label(op, r, strings=None):
    s = _strings(strings)
    if op == "cancel":
        return s["skip_this"] if _is_rule(r) else s["delete"]
    return s[op]


def _question(op, r, s):
    if op == "cancel":
        return s["q_skip"] if _is_rule(r) else s["q_delete"]
    if op == "stop":
        return f"{s['q_stop']} {s['q_stop_rest']}" if _is_rule(r) else s["q_stop"]
    return s["q_" + op]


def confirm_message(op, r, channel, strings=None, now_ms=None):
    """The yes/no dialog text: name, "10/12(月) 21:00-21:30 {channel}", the question; for unskip / unoverlap of a
    program already on air a warning line (EPGStation starts a new recording now, partway in)."""
    s = _strings(strings)
    text = f"{to_half(r['name'])}\n{time_line(r, s)} {channel}\n{_question(op, r, s)}"
    if op in ("unskip", "unoverlap") and now_ms is not None and r["startAt"] <= now_ms < r["endAt"]:
        text += "\n" + s["q_airing"]
    return text


def done_message(op, r, strings=None):
    s = _strings(strings)
    if op == "cancel":
        return s["done_skip"] if _is_rule(r) else s["done_delete"]
    return s["done_" + op]


def error_message(status, detail, strings=None):
    """Toast text of a failed write: the reservation is gone (EPGStation's 500 "ReservationIsNotFound"), no answer
    at all (timeout, connection reset: status None), else "失敗しました: {server text or HTTP status}"."""
    s = _strings(strings)
    if detail == "ReservationIsNotFound":
        return s["gone"]
    if status is None:
        return s["failed_net"]
    return s["failed"].format(detail or f"HTTP {status}")


# --- dates --------------------------------------------------------------------------------------------------------

def _date(ms, s):
    names = s["weekdays"].split(",")
    lt = time.localtime(ms / 1000)
    wd = names[(lt.tm_wday + 1) % 7] if len(names) == 7 else ""  # tm_wday: Monday = 0; the list starts Sunday
    if wd.isascii():
        return f"{wd} {lt.tm_mon}/{lt.tm_mday}".strip()
    return f"{lt.tm_mon}/{lt.tm_mday}({wd})"


def date_time(ms, strings=None):
    """"10/12(月) 21:00" (en: "Mon 10/12 21:00")."""
    return f"{_date(ms, _strings(strings))} {fmt_time(ms, '%H:%M')}"


def time_line(r, strings=None):
    """"10/12(月) 21:00-21:30"."""
    return f"{date_time(r['startAt'], strings)}-{fmt_time(r['endAt'], '%H:%M')}"


# --- folders and items --------------------------------------------------------------------------------------------

def type_folders(cnts, strings=None):
    """"競合 (N)", "重複 (N)", "除外 (N)": the non-empty states, in that order."""
    s = _strings(strings)
    return [folder(f"{s[state]} ({cnts[state]})", MENU_ICONS["reserves"], action="reserves", type=state)
            for state in STATES if cnts.get(state)]


def _state_line(r, recording, s):
    state = classify(r)
    if state != NORMAL:
        return s["line_" + state]
    return s["recording"] if recording else ""


def reserve_info(r, channel, rule_name, recording=False, lang="ja", strings=None, on_error=None):
    """InfoTagVideo fields of a reservation (mediatype video, like the guide)."""
    s = _strings(strings)
    name = to_half(r["name"])
    meta = epgmeta.metadata(r, on_error=(lambda e: on_error(r, e)) if on_error else None)
    texts = [to_half(meta["plot"]).replace("\r\n", "\n")] if meta is not None else raw_plot_texts(r)
    info = {k: v for k, v in metadata_info(meta, name, lang).items() if k in ("cast", "directors", "writers", "tags")}
    line = time_line(r, s)
    origin = s["rule"].format(rule_name) if _is_rule(r) else s["manual"]
    head = "\n".join(x for x in (line, origin, _state_line(r, recording, s)) if x)
    air_date = fmt_time(r["startAt"], "%Y-%m-%d")
    info.update({
        "mediatype": "video",
        "title": name,
        "genres": item_genres(r, lang),
        "studios": [channel],
        "duration": (r["endAt"] - r["startAt"]) // 1000,
        "firstaired": air_date,
        "premiered": air_date,
        "tagline": f"{channel}  {line}",
        "plotoutline": line,
        "plot": build_plot([head] + texts),
    })
    return info


def rule_label(r, rules):
    """The rule's name from {ruleId: name} (items.rule_name), else its id; "" for a manual reservation."""
    if not _is_rule(r):
        return ""
    return rules.get(r["ruleId"]) or str(r["ruleId"])


def reserve_item(r, channels, logos, rules, api, now_ms, lang="ja", strings=None, on_error=None, in_folder=False,
                 records=()):
    """Item dict of a reservation. channels: {id: name} (items.channel_names); logos: channel ids with hasLogoData;
    rules: {ruleId: name}; records: /recording (● and 録画を停止 only for a reservation listed there). in_folder:
    inside its own state folder (no state marker). Click: reserve_info (Kodi's information dialog); one context entry
    (action_for), run through reserve_action."""
    s = _strings(strings)
    ch_name = to_half(channels.get(r["channelId"]) or str(r["channelId"]))
    name = to_half(r["name"])
    state = classify(r)
    recording = is_recording(r, records)
    marker = RECORDING if recording else ""
    if state != NORMAL and not in_folder:
        marker = MARKERS[state].format(s[state])
    rule = rule_label(r, rules)
    info = reserve_info(r, ch_name, rule, recording, lang, s, on_error)
    art = {}
    has_logo = r["channelId"] in logos
    if has_logo:
        logo = api.logo_url(r["channelId"]) + api.kodi_headers()
        art = {"icon": logo, "thumb": logo}
    props = {
        "epgstation.reserveId": str(r["id"]),
        "epgstation.channelId": str(r["channelId"]),
        "epgstation.channel": ch_name,
        "epgstation.ruleId": str(r["ruleId"]) if _is_rule(r) else "",
        "epgstation.state": state,
        "epgstation.recording": str(recording).lower(),
        "epgstation.startAt": fmt_time(r["startAt"]),
        "epgstation.endAt": fmt_time(r["endAt"]),
    }
    if r.get("programId") is not None:
        props["epgstation.programId"] = str(r["programId"])
    props.update(extended_props(r))
    op = action_for(r, now_ms, records)
    context = [(entry_label(op, r, s), plugin_url(action="reserve_action", op=op, reserveId=r["id"]))] if op else []
    return {
        "label": f"{marker}{date_time(r['startAt'], s)} {ch_name}  {name}",
        "path": plugin_url(action="reserve_info", reserveId=r["id"], name=ch_name, logo=1 if has_logo else None,
                           rule=rule or None, recording=1 if recording else None),
        "folder": False,
        "playable": False,
        "info": info,
        "art": art,
        "props": props,
        "stream": None,
        "context": context,
    }


def reserve_items(reserves, channels, logos, rules, api, now_ms, lang="ja", strings=None, on_error=None,
                  in_folder=False, records=()):
    return [reserve_item(r, channels, logos, rules, api, now_ms, lang, strings, on_error, in_folder, records)
            for r in reserves]


def listing(reserves, state, channels, logos, rules, api, now_ms, lang="ja", strings=None, on_error=None,
            records=()):
    """The 録画予約 directory from one GET /reserves?type=all: state None -> the non-empty state folders, then
    every upcoming normal reservation; a state -> that state's upcoming reservations (no marker)."""
    up = upcoming(reserves, now_ms)
    if state is None:
        return type_folders(counts(up), strings) + reserve_items(split(up, NORMAL), channels, logos, rules, api,
                                                                 now_ms, lang, strings, on_error, False, records)
    return reserve_items(split(up, state), channels, logos, rules, api, now_ms, lang, strings, on_error, True, records)


# --- in-progress recordings ---------------------------------------------------------------------------------------

def _same(r, rec):
    if r.get("channelId") != rec.get("channelId"):
        return False
    if rec.get("programId") is not None:
        return r.get("programId") == rec["programId"]
    return r.get("startAt") == rec.get("startAt")


def match_recording(reserves, rec):
    """The reservation of an in-progress RecordedItem (it has no reserveId; the recorder copies channelId,
    programId and startAt from the reservation): same channel and programId, or same channel and startAt when
    there is no programId (time-specified). None when there is none."""
    return next((r for r in reserves if _same(r, rec)), None)


def recording_still_listed(records, r):
    """True while /recording still lists the recording of reservation r."""
    return any(_same(r, rec) for rec in records)


def context_stop(rec_id, strings=None):
    """録画を停止 context entry of an in-progress recording (録画中 and the lists that prepend it)."""
    return (_strings(strings)["stop"], plugin_url(action="reserve_action", op="stop", recordedId=rec_id))


# --- record from the guide (014) ----------------------------------------------------------------------------------
# The guide marks its programs from one GET /reserves/lists over the listed window (never cached) and offers one
# reservation entry per program: 録画 when it is not reserved, else this module's action (action_for).

RECORD_WAIT = 5.0  # seconds to wait for an on-air add to show in /recording (EPGStation starts it a moment later)
RESERVED = "[COLOR red]○[/COLOR] "  # reserved, will record (● while it records, 007's RECORDING)
LIST_STATES = (("normal", NORMAL), ("conflicts", "conflict"), ("skips", "skip"), ("overlaps", "overlap"))
_RANK = {state: i for i, (_, state) in enumerate(LIST_STATES)}  # the guide's choice: normal > conflict > skip > overlap


def lists_index(lists):
    """{programId: stub reservation {id, programId, ruleId?, isConflict, isSkip, isOverlap}} from a GET
    /reserves/lists answer. Items without programId (time-specified) are left out. Several reservations for one
    program (a rule may match a manually reserved one): normal > conflict > skip > overlap, manual before rule."""
    best = {}
    for key, state in LIST_STATES:
        for item in (lists or {}).get(key) or []:
            pid = item.get("programId")
            if pid is None:
                continue
            stub = {"id": item["reserveId"], "programId": pid, "isConflict": state == "conflict",
                    "isSkip": state == "skip", "isOverlap": state == "overlap"}
            if item.get("ruleId") is not None:
                stub["ruleId"] = item["ruleId"]
            rank = (_RANK[state], _is_rule(stub))
            if pid not in best or rank < best[pid][0]:
                best[pid] = (rank, stub)
    return {pid: stub for pid, (_, stub) in best.items()}


def program_reserve(stub, program, channel_id=None):
    """The stub with the program's channel, times and name: enough for action_for, entry_label and is_recording."""
    return dict(stub, channelId=program.get("channelId", channel_id), startAt=program["startAt"],
                endAt=program["endAt"], name=program["name"])


def marker(r, records, strings=None):
    """Label prefix of a reserved program: 007's [競合] / [除外] / [重複], ● while /recording lists it, else ○."""
    s = _strings(strings)
    state = classify(r)
    if state != NORMAL:
        return MARKERS[state].format(s[state])
    return RECORDING if is_recording(r, records) else RESERVED


def reserve_line(r, strings=None):
    """Plot line(s) under the time line: 録画予約: ルール / 手動, plus 007's state line for conflict/skip/overlap."""
    s = _strings(strings)
    line = s["by_rule"] if _is_rule(r) else s["by_manual"]
    state = classify(r)
    return line + ("\n" + s["line_" + state] if state != NORMAL else "")


def _on_air(program, now_ms):
    return program["startAt"] <= now_ms < program["endAt"]


def needs_records(index, programs, now_ms):
    """GET /recording (for ●) only when a listed program on air has a normal reservation."""
    return bool(index) and any(_on_air(p, now_ms) and p["id"] in index and classify(index[p["id"]]) == NORMAL
                               for p in programs)


def lists_window(programs):
    """(startAt, endAt) of GET /reserves/lists covering the listed programs, None for none."""
    programs = list(programs)
    if not programs:
        return None
    return min(p["startAt"] for p in programs), max(p["endAt"] for p in programs)


def can_record(program, now_ms):
    """Not ended (EPGStation does not check this for programId reservations: an ended one would get no timer)."""
    return program["endAt"] > now_ms


def record_context(program, channel, strings=None):
    """録画 context entry (RunPlugin)."""
    return (_strings(strings)["record"],
            plugin_url(action="record", programId=program["id"], channelId=program.get("channelId"), name=channel))


def reserve_context(r, now_ms, records=(), strings=None):
    """007's entry for a reserved program, None when it has none (ended)."""
    op = action_for(r, now_ms, records)
    if op is None:
        return None
    return (entry_label(op, r, strings), plugin_url(action="reserve_action", op=op, reserveId=r["id"]))


def guide_mark(program, channel, index, records, now_ms, strings=None):
    """(label marker, plot line, context entry or None) of a guide program. index None (the lists GET failed):
    nothing, never a guessed 録画 on a program that may be reserved."""
    if index is None:
        return "", "", None
    stub = index.get(program["id"])
    if stub is None:
        return "", "", record_context(program, channel, strings) if can_record(program, now_ms) else None
    r = program_reserve(stub, program)
    return marker(r, records, strings), reserve_line(r, strings), reserve_context(r, now_ms, records, strings)


def record_message(program, channel, now_ms, encode_mode=None, strings=None):
    """The 録画 yes/no text: name, "10/12(月) 21:00-21:30 {channel}", the question; on air: the partway warning;
    with an encode mode: "エンコード: H.264"."""
    s = _strings(strings)
    lines = [to_half(program["name"]), f"{time_line(program, s)} {channel}", s["q_record"]]
    if program["startAt"] <= now_ms:
        lines.append(s["q_record_airing"])
    if encode_mode:
        lines.append(s["encode"].format(encode_mode))
    return "\n".join(lines)


def record_done(program, now_ms, strings=None):
    s = _strings(strings)
    return s["done_record_now"] if program["startAt"] <= now_ms else s["done_record"]


ADD_ERRORS = {"ReservationManageModelAddReserveConflict": "add_conflict",
              "ReservationManageModelCheckReservedProgramError": "already", "ProgramIsNotFound": "program_gone"}


def maybe_applied(status, detail):
    """No answer (status None), or EPGStation's 500 "IPCTimeout": its IPC gave up after 5 s but the operator still
    completes the add, so the reservation may exist."""
    return status is None or detail == "IPCTimeout"


def add_error(status, detail, strings=None):
    """Toast text of a failed POST /reserves: EPGStation's named 500 errors, no answer (or IPCTimeout), else
    失敗しました: …."""
    s = _strings(strings)
    if detail in ADD_ERRORS:
        return s[ADD_ERRORS[detail]]
    if maybe_applied(status, detail):
        return s["failed_net"]
    return s["failed"].format(detail or f"HTTP {status}")
