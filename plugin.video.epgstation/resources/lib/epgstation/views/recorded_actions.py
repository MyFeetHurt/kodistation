"""Protect / unprotect / delete a recording, or delete one of its files (context entries of a recording).

Same pattern as the reservation writes: read now, check, ask (Cancel focused), read and check again, exactly one
write, a toast, a refresh. Every write (and every recording found changed) invalidates the library cache, so lists
never show the recording as it was before.
"""
import xbmc
import xbmcgui

from .. import items, library, recorded, ui
from ..api import ApiError
from . import recordings

RECORDED_STRINGS = (  # recorded.DEFAULT_STRINGS keys -> string IDs
    ("protect", 30072), ("unprotect", 30073), ("delete", 30074), ("marker", 30075), ("q_protect", 30076),
    ("q_unprotect", 30077), ("q_delete", 30078), ("q_encoding", 30079), ("files", 30080), ("done_protect", 30081),
    ("done_unprotect", 30082), ("done_delete", 30083), ("gone", 30084), ("changed", 30085), ("protected", 30086),
    ("protect_line", 30087), ("delete_file", 30245), ("choose_file", 30246), ("q_delete_file", 30247),
    ("delete_keep", 30248), ("done_delete_file", 30249), ("file_gone", 30250))
SHARED_STRINGS = (("failed", 30050), ("failed_net", 30059), ("weekdays", 30026))


def _invalidate_library():
    if not library.invalidate(ui.profile_path(), ui.server_url()):
        ui.log("recorded: could not invalidate the library cache", xbmc.LOGWARNING)


def _target(ctx, op, recorded_id, strings):
    """The recording read now if op is still possible on it; else say why (gone / protected / changed), refresh and
    return None. A failed read is a short toast (no dialog, no write)."""
    try:
        rec = ctx.api.recorded_item(recorded_id)
    except ApiError as e:
        if e.status == 404:
            rec = None
        else:
            ui.log(f"recorded: {op} lookup failed: {e}", xbmc.LOGERROR)
            ctx.notify(recorded.error_message(e.status, e.detail, strings), xbmcgui.NOTIFICATION_ERROR)
            return None
    if rec is None:
        message = strings["gone"]
        _invalidate_library()
    elif not recorded.can(op, rec):
        message = recorded.refusal(op, rec, strings)
        _invalidate_library()
    else:
        return rec
    ctx.notify(message, xbmcgui.NOTIFICATION_WARNING)
    ui.refresh()
    return None


def _pick_file(ctx, rec, strings):
    """The video file the user picks in Kodi's select list (recorded.can() keeps 2+ files), None on Back."""
    files = recorded.delete_file_choices(rec)
    idx = xbmcgui.Dialog().select(strings["choose_file"],
                                  items.video_choices(files, recordings.file_durations(ctx, rec, files)))
    return files[idx] if 0 <= idx < len(files) else None


def recorded_action(ctx):
    """URL: op=protect|unprotect|delete|delete_file and id=N (delete_file: the file is chosen in a select list after
    the re-read, never from the URL)."""
    strings = ui.strings(RECORDED_STRINGS + SHARED_STRINGS)
    op = ctx.params.get("op")
    if op not in recorded.OPS:
        raise ui.BadParamError(f"op={op!r}")
    recorded_id = ctx.int_param("id")
    rec = _target(ctx, op, recorded_id, strings)
    if rec is None:
        return
    video = None
    if op == "delete_file":
        video = _pick_file(ctx, rec, strings)
        if video is None:
            return
    try:
        channel = items.channel_names(ctx.api.channels()).get(rec["channelId"])
    except ApiError as e:
        ui.log(str(e), xbmc.LOGWARNING)
        channel = None
    channel = items.to_half(channel or str(rec["channelId"]))
    label = yes = recorded.entry_label(op, strings)
    if video is not None:  # heading ファイルを削除, yes button 削除
        message = recorded.confirm_file_message(rec, video["id"], channel, strings)
        yes = recorded.entry_label("delete", strings)
    else:
        message = recorded.confirm_message(op, rec, channel, strings)
    if not ctx.confirm(label, message, yes):
        return
    shown = recorded.fingerprint(rec)
    rec = _target(ctx, op, recorded_id, strings)  # again: the dialog may have stayed open a while
    if rec is None:
        return
    # another recording than the dialog showed (files, encode), or the chosen file is no longer in it
    if recorded.fingerprint(rec) != shown or (video is not None and video["id"] not in
                                              [v.get("id") for v in rec.get("videoFiles") or []]):
        _invalidate_library()
        ctx.notify(strings["changed"], xbmcgui.NOTIFICATION_WARNING)
        ui.refresh()
        return
    _write(ctx, op, rec, strings, video)


def _write(ctx, op, rec, strings, video=None):
    """Exactly one write for a confirmed op, then the library marker, a toast and a refresh."""
    what = f"{op} recorded {rec['id']} ({items.to_half(rec['name'])})"
    if video is not None:
        what += f" file {video['id']} ({video.get('name')})"
    ui.log(f"recorded: {what}")
    try:
        if op == "protect":
            ctx.api.protect_recorded(rec["id"])
        elif op == "unprotect":
            ctx.api.unprotect_recorded(rec["id"])
        elif op == "delete_file":
            ctx.api.delete_video_file(video["id"])
        else:
            ctx.api.delete_recorded(rec["id"])
    except ApiError as e:
        maybe = recorded.maybe_applied(e.status, e.detail)
        note = " (no answer: the server may still have applied it)" if maybe else ""
        ui.log(f"recorded: {what} failed: {e}{note}", xbmc.LOGERROR)
        _invalidate_library()  # it may have applied anyway
        icon = xbmcgui.NOTIFICATION_WARNING if recorded.is_refusal(e.detail) else xbmcgui.NOTIFICATION_ERROR
        ctx.notify(recorded.error_message(e.status, e.detail, strings), icon)
        ui.refresh()
        return
    ui.log(f"recorded: {what} done")
    _invalidate_library()
    ctx.notify(recorded.done_message(op, strings))
    ui.refresh()
