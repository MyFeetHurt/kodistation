"""Reservations (録画予約): the list and its actions, and recording a program from the guide.

Every write follows the same pattern: read the target now, check it still allows the action, ask (Cancel focused),
read and check again after the yes (the dialog may have stayed open while things changed), exactly one write, a
toast, a refresh. Nothing is written when a read fails.
"""
import xbmc
import xbmcgui

from .. import items, reserves, ui
from ..api import ApiError

RESERVE_STRINGS = (  # reserves.DEFAULT_STRINGS keys -> string IDs
    ("reserves", 30030), ("conflict", 30031), ("overlap", 30032), ("skip", 30033), ("delete", 30034),
    ("skip_this", 30035), ("stop", 30036), ("unskip", 30037), ("unoverlap", 30038), ("q_delete", 30039),
    ("q_skip", 30040), ("q_stop", 30041), ("q_unskip", 30042), ("q_unoverlap", 30043), ("q_moves", 30044),
    ("done_delete", 30045), ("done_skip", 30046), ("done_stop", 30047), ("done_unskip", 30048),
    ("done_unoverlap", 30049), ("failed", 30050), ("gone", 30051), ("changed", 30052), ("rule", 30053),
    ("manual", 30054), ("line_conflict", 30055), ("line_overlap", 30056), ("line_skip", 30057),
    ("recording", 30002), ("weekdays", 30026), ("q_airing", 30058), ("failed_net", 30059), ("q_stop_rest", 30060))
RECORD_STRINGS = (
    ("record", 30061), ("q_record", 30062), ("q_record_airing", 30063), ("encode", 30064), ("done_record", 30065),
    ("done_record_now", 30066), ("already", 30067), ("add_conflict", 30068), ("ended", 30069), ("by_rule", 30070),
    ("by_manual", 30071), ("program_gone", 30028))

on_parse_error = ui.parse_error_logger("reservation")


def reserve_strings():
    return ui.strings(RESERVE_STRINGS)


def record_strings():
    return ui.strings(RESERVE_STRINGS + RECORD_STRINGS)


def _all_reserves(ctx):
    """Every reservation (GET /reserves?type=all, pages of reserves.PAGE until total)."""
    out, offset = [], 0
    while True:
        page = ctx.api.reserves(offset=offset, limit=reserves.PAGE)
        out += page["reserves"]
        offset += reserves.PAGE
        if offset >= page.get("total", 0) or not page["reserves"]:
            return out


def reserve_list(ctx):
    """The 競合 / 重複 / 除外 folders and every upcoming normal reservation; type=<state>: that state."""
    state = ctx.params.get("type")
    if state is not None and state not in reserves.STATES:
        raise ui.BadParamError(f"type={state!r}")
    strings = reserve_strings()
    all_reserves = _all_reserves(ctx)
    channels = ctx.api.channels()
    rules = {r["id"]: items.rule_name(r) for r in ctx.api.rules().get("rules", [])}
    records = ctx.api.recording()["records"]  # ● and 録画を停止 only for what really records
    out = reserves.listing(all_reserves, state, items.channel_names(channels),
                           {c["id"] for c in channels if c.get("hasLogoData")}, rules, ctx.api, ui.now_ms(),
                           ui.lang(), strings, on_parse_error, records)
    category = strings[state] if state else strings["reserves"]
    ctx.add(out, sort=ui.SORT_GUIDE, category=category or None, cache=False)


def _gone(ctx, strings):
    """The reservation (or program) is not there any more: say so and reload the list."""
    ctx.notify(strings["gone"], xbmcgui.NOTIFICATION_WARNING)
    ui.refresh()


def reserve_info(ctx):
    """Click on a reservation: Kodi's information dialog with the reservation as it is now (one GET)."""
    p, strings = ctx.params, reserve_strings()
    reserve_id = ctx.int_param("reserveId")
    try:
        r = ctx.api.reserve(reserve_id)
    except ApiError as e:
        if e.status != 404:
            raise
        _gone(ctx, strings)
        return
    cid = r["channelId"]
    rules = {r["ruleId"]: p["rule"]} if r.get("ruleId") is not None and p.get("rule") else {}
    # recording=1: the list saw it in /recording (the 録画中 line; no extra GET for the dialog)
    records = [{k: r[k] for k in ("channelId", "programId", "startAt") if k in r}] \
        if p.get("recording") == "1" else []
    d = reserves.reserve_item(r, {cid: p.get("name") or str(cid)}, {cid} if p.get("logo") == "1" else set(),
                              rules, ctx.api, ui.now_ms(), ui.lang(), strings, on_parse_error, records=records)
    xbmcgui.Dialog().info(ui.listitem(d))  # no path: Kodi's Play button gives its own "Playback failed"


def _fresh_reserve(ctx, reserve_id):
    """GET /reserves/{id}, None when it no longer exists."""
    try:
        return ctx.api.reserve(reserve_id)
    except ApiError as e:
        if e.status == 404:
            return None
        raise


def _reserve_of_recording(ctx, recorded_id):
    """The reservation of a recording in progress (RecordedItem has no reserveId): the reservations at its start
    (GET /reserves/lists), matched by channel + programId / startAt. None when it is not recording."""
    try:
        rec = ctx.api.recorded_item(recorded_id)
    except ApiError as e:
        if e.status == 404:
            return None
        raise
    if not rec.get("isRecording"):
        return None
    lists = ctx.api.reserve_lists(rec["startAt"], rec["startAt"])
    ids = [x["reserveId"] for key in ("normal", "conflicts", "skips", "overlaps") for x in lists.get(key) or []]
    candidates = [r for r in (_fresh_reserve(ctx, i) for i in ids) if r is not None]
    return reserves.match_recording(candidates, rec)


def _wait_listed(ctx, r, listed, wait):
    """Wait until /recording does (listed=True) or no longer does (False) list r: EPGStation starts and stops
    recorders a moment after it answers. At most `wait` s in all (monotonic deadline, each request and pause capped
    to what is left). True when it got there in time."""
    monitor, deadline = xbmc.Monitor(), ui.clock() + wait
    while True:
        left = deadline - ui.clock()
        if left <= 0.05:
            return False
        try:
            if reserves.recording_still_listed(ctx.api.recording(timeout=left)["records"], r) == listed:
                return True
        except (ApiError, KeyError, TypeError) as e:
            ui.log(f"reserve: recording check: {e}", xbmc.LOGWARNING)
            return False
        left = deadline - ui.clock()
        if left <= 0 or monitor.waitForAbort(min(reserves.STOP_STEP, left)):
            return False


def _reserve_target(ctx, op):
    """The reservation the action is for, read now (reserveId, or recordedId of a recording in progress), and
    whether `op` is still its action: (reserve or None, still_valid)."""
    if "recordedId" in ctx.params:
        r = _reserve_of_recording(ctx, ctx.int_param("recordedId"))
    else:
        r = _fresh_reserve(ctx, ctx.int_param("reserveId"))
    if r is None:
        return None, False
    return r, reserves.action_for(r, ui.now_ms(), ctx.api.recording()["records"]) == op


def _not_now(ctx, r, strings):
    """Gone or changed since the list (or since the dialog opened): say so, refresh, write nothing."""
    if r is None:
        _gone(ctx, strings)
        return
    ctx.notify(strings["changed"], xbmcgui.NOTIFICATION_WARNING)
    ui.refresh()


def _channel_name(ctx, channel_id):
    try:
        name = items.channel_names(ctx.api.channels()).get(channel_id)
    except ApiError as e:
        ui.log(str(e), xbmc.LOGWARNING)
        name = None
    return items.to_half(name or str(channel_id))


def reserve_action(ctx):
    """Stop / cancel / unskip / unoverlap one reservation. URL: op=stop|cancel|unskip|unoverlap and reserveId=N, or
    op=stop and recordedId=N (録画中)."""
    p, strings = ctx.params, reserve_strings()
    op = p.get("op")
    if op not in reserves.OPS:
        raise ui.BadParamError(f"op={op!r}")
    if "recordedId" in p and op != "stop":
        raise ui.BadParamError(f"op={op!r} with recordedId")
    try:
        r, valid = _reserve_target(ctx, op)
    except ApiError as e:
        if e.status is not None:
            raise
        ui.log(f"reserve: {op} lookup failed: {e}", xbmc.LOGERROR)
        ctx.notify(strings["failed_net"], xbmcgui.NOTIFICATION_ERROR)
        return
    if not valid:
        _not_now(ctx, r, strings)
        return
    channel = _channel_name(ctx, r["channelId"])
    label = reserves.entry_label(op, r, strings)
    if not ctx.confirm(label, reserves.confirm_message(op, r, channel, strings, ui.now_ms()), label):
        return
    r, valid = _reserve_target(ctx, op)  # again: what the yes was for may have changed meanwhile
    if not valid:
        _not_now(ctx, r, strings)
        return
    what = f"{op} reserve {r['id']} ({items.to_half(r['name'])})"
    ui.log(f"reserve: {what}")
    try:
        if op in ("stop", "cancel"):
            ctx.api.cancel_reserve(r["id"])
        elif op == "unskip":
            ctx.api.remove_skip(r["id"])
        else:
            ctx.api.remove_overlap(r["id"])
    except ApiError as e:
        maybe = "" if e.status else " (no answer: the server may still have applied it)"
        ui.log(f"reserve: {what} failed: {e}{maybe}", xbmc.LOGERROR)
        ctx.notify(reserves.error_message(e.status, e.detail, strings), xbmcgui.NOTIFICATION_ERROR)
        ui.refresh()
        return
    ui.log(f"reserve: {what} done")
    ctx.notify(reserves.done_message(op, r, strings))
    if op == "stop":  # so the refreshed 録画中 no longer shows it
        _wait_listed(ctx, r, False, reserves.STOP_WAIT)
    ui.refresh()


# --- record from the guide ----------------------------------------------------------------------------------------

def _encode_mode(ctx):
    """Setting 録画後のエンコード: 1 -> the server's first encode mode (GET /config encode); else or none: None."""
    if ui.setting("record_encode") != "1":
        return None
    modes = ctx.api.config().get("encode") or []
    if not modes:
        ui.log("reserve: the server has no encode mode: recording without encode")
        return None
    return str(modes[0])


def _recordable(ctx, program_id, strings):
    """The guide program read now if it can still be reserved: in the guide, not ended, no reservation for it
    (GET /reserves/lists over its time). Else say why, refresh and return None."""
    try:
        program = ctx.api.schedule_detail(program_id)
    except ApiError as e:
        if e.status != 404:
            raise
        reason = "program_gone"
    else:
        if not reserves.can_record(program, ui.now_ms()):
            reason = "ended"
        elif program_id in _lists_index_or_error(ctx, program):
            reason = "already"
        else:
            return program
    ctx.notify(strings[reason], xbmcgui.NOTIFICATION_WARNING)
    ui.refresh()
    return None


def _lists_index_or_error(ctx, program):
    """The reservations over the program's time; an answer of another shape is an ApiError (no write)."""
    lists = ctx.api.reserve_lists(program["startAt"], program["endAt"])
    try:
        return reserves.lists_index(lists)
    except (KeyError, TypeError, AttributeError) as e:
        raise ApiError(f"/reserves/lists: unexpected answer: {e!r}", 200, "unexpected answer") from e


def _lookup_failed(ctx, exc, strings):
    """A read before or after the dialog failed: short toast, no write."""
    ui.log(f"reserve: record lookup failed: {exc}", xbmc.LOGERROR)
    ctx.notify(reserves.add_error(exc.status, exc.detail, strings), xbmcgui.NOTIFICATION_ERROR)


def _confirm_record(ctx, program, channel, mode, strings):
    return ctx.confirm(strings["record"], reserves.record_message(program, channel, ui.now_ms(), mode, strings),
                       strings["record"])


def _recheck(ctx, program_id, strings):
    """_recordable after a yes; a failed read is a short toast (no write)."""
    try:
        return _recordable(ctx, program_id, strings)
    except ApiError as e:
        _lookup_failed(ctx, e, strings)
        return None


def record(ctx):
    """Record a guide program: exactly one POST /reserves. URL: programId=P, channelId=C, name=<channel>."""
    p, strings = ctx.params, record_strings()
    program_id = ctx.int_param("programId")
    channel = p.get("name") or p.get("channelId") or ""
    try:
        program = _recordable(ctx, program_id, strings)
        if program is None:
            return
        mode = _encode_mode(ctx)
    except ApiError as e:
        _lookup_failed(ctx, e, strings)
        return
    asked_on_air = program["startAt"] <= ui.now_ms()
    if not _confirm_record(ctx, program, channel, mode, strings):
        return
    program = _recheck(ctx, program_id, strings)  # ended or reserved (another dialog, the web UI) meanwhile?
    if program is None:
        return
    if not asked_on_air and program["startAt"] <= ui.now_ms():
        # it started while the dialog was open: the yes was for a future program, ask once more with the warning
        if not _confirm_record(ctx, program, channel, mode, strings):
            return
        program = _recheck(ctx, program_id, strings)
        if program is None:
            return
    what = f"add program {program_id} ({items.to_half(program['name'])})"
    ui.log(f"reserve: {what}")
    try:
        reserve_id = ctx.api.create_reserve(program_id, mode)
    except ApiError as e:
        maybe = " (no answer: the server may still have applied it)" if reserves.maybe_applied(e.status, e.detail) \
            else ""
        ui.log(f"reserve: {what} failed: {e}{maybe}", xbmc.LOGERROR)
        icon = xbmcgui.NOTIFICATION_WARNING if e.detail in reserves.ADD_ERRORS else xbmcgui.NOTIFICATION_ERROR
        ctx.notify(reserves.add_error(e.status, e.detail, strings), icon)
        ui.refresh()
        return
    ui.log(f"reserve: {what} done: reserve {reserve_id}")
    ctx.notify(reserves.record_done(program, ui.now_ms(), strings))
    ui.refresh()
    if program["startAt"] <= ui.now_ms():
        r = {"channelId": program.get("channelId"), "programId": program["id"], "startAt": program["startAt"]}
        if _wait_listed(ctx, r, True, reserves.RECORD_WAIT):
            ui.refresh()  # now with ● and 録画を停止
