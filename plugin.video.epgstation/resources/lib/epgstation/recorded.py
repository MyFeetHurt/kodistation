"""Protect, unprotect and delete recordings and delete one of their files: context entries,
the lock marker, dialog texts and toasts.

No Kodi imports here: everything is unit-testable (like items.py). It imports items only; items imports this module
lazily in recorded_item (like reserves). views/recorded_actions.py runs the writes.

Actions (recorded.actions_for; EPGStation 2.10.0 behaviour):
    protect    not protected                     PUT /recorded/{id}/protect    (never auto-deleted)
    unprotect  protected                         PUT /recorded/{id}/unprotect
    delete     not protected and not recording   DELETE /recorded/{id}         (every file, thumbnails, drop log;
                                                  cancels its encodes first; refuses protected: RecordedIsProtected)
    delete_file 2+ files, not protected, not     DELETE /videos/{fileId}       (that one file; refuses protected:
                recording, not encoding                                            RecordedIsProtected; unknown file:
                                                                                   VideoFileIsNotFound; no encode
                                                                                   cancel; in progress or the last
                                                                                   file: the whole recording goes)
No 削除 on a protected recording (unprotect first) or on one in progress (録画を停止 keeps the file).
ファイルを削除 picks the file after the re-read (Kodi select list), names the file that stays and never offers
the last one.
"""
import time
import unicodedata

from .items import fmt_size, fmt_time, plugin_url, to_half

OPS = ("protect", "unprotect", "delete", "delete_file")  # delete_file
MARKER = " [COLOR gold][{0}][/COLOR]"

DEFAULT_STRINGS = {
    "protect": "Protect", "unprotect": "Unprotect", "delete": "Delete", "marker": "Protected",
    "q_protect": "Protect this recording? It will not be auto-deleted and cannot be deleted while protected.",
    "q_unprotect": "Remove protection? The recording can then be deleted, also automatically.",
    "q_delete": "Delete this recording? The files cannot be recovered.",
    "q_encoding": "Encoding: will be cancelled",
    "files": "Files: {0}",
    "done_protect": "Protected", "done_unprotect": "Protection removed", "done_delete": "Recording deleted",
    "gone": "Recording is gone", "changed": "Recording has changed", "protected": "Protected: cannot delete",
    "protect_line": "Protected (not auto-deleted)",
    "failed": "Failed: {0}", "failed_net": "Failed (connection error)",
    "weekdays": "Sun,Mon,Tue,Wed,Thu,Fri,Sat",
    "delete_file": "Delete a file", "choose_file": "Choose the file to delete",
    "q_delete_file": "Delete this file? It cannot be recovered.", "delete_keep": "Delete: {0} / Keep: {1}",
    "done_delete_file": "File deleted", "file_gone": "File is gone",
}


def _strings(strings):
    # Kodi returns "" for string IDs it has not loaded yet (addon updated without restart).
    return dict(DEFAULT_STRINGS, **{k: v for k, v in (strings or {}).items() if v})


def is_protected(rec):
    return bool(rec.get("isProtected"))


def can(op, rec):
    """op is possible on the recording as it is now: protect / unprotect toggle the flag; delete needs an
    unprotected, finished recording; delete_file also 2+ files and no encode running (DELETE /videos does not
    cancel encodes, and deletes the whole recording for the last file or one in progress)."""
    if op == "protect":
        return not is_protected(rec)
    if op == "unprotect":
        return is_protected(rec)
    if op == "delete_file":
        return (not is_protected(rec) and not rec.get("isRecording") and not rec.get("isEncoding")
                and len(rec.get("videoFiles") or []) >= 2)
    return op == "delete" and not is_protected(rec) and not rec.get("isRecording")


def actions_for(rec):
    """The ops the context menu offers, in menu order."""
    return [op for op in OPS if can(op, rec)]


def entry_label(op, strings=None):
    return _strings(strings)[op]


def context_entries(rec, strings=None):
    """(label, RunPlugin URL) per action (after 別のファイルを選ぶ and 録画を停止)."""
    return [(entry_label(op, strings), plugin_url(action="recorded_action", op=op, id=rec["id"]))
            for op in actions_for(rec)]


def marker(rec, strings=None):
    """Label suffix of a protected recording: " [保護]" in gold (text, not a glyph: Estuary's fonts have no lock)."""
    return MARKER.format(_strings(strings)["marker"]) if is_protected(rec) else ""


def protect_line(strings=None):
    """Plot line of a protected recording (poster views hide the label)."""
    return _strings(strings)["protect_line"]


def refusal(op, rec, strings=None):
    """Toast when op is no longer possible: a delete of a protected recording says why, the rest "changed"."""
    s = _strings(strings)
    return s["protected"] if op in ("delete", "delete_file") and is_protected(rec) else s["changed"]


def files_line(rec, strings=None):
    """"ファイル: TS 1.2 GB, H.264 350.0 MB" ("" without files)."""
    files = rec.get("videoFiles") or []
    if not files:
        return ""
    return _strings(strings)["files"].format(", ".join(f"{v['name']} {fmt_size(v.get('size', 0))}" for v in files))


def _date(ms, s):  # as reserves._date (this module does not import reserves)
    names = s["weekdays"].split(",")
    lt = time.localtime(ms / 1000)
    wd = names[(lt.tm_wday + 1) % 7] if len(names) == 7 else ""  # tm_wday: Monday = 0; the list starts Sunday
    if wd.isascii():
        return f"{wd} {lt.tm_mon}/{lt.tm_mday}".strip()
    return f"{lt.tm_mon}/{lt.tm_mday}({wd})"


def time_line(rec, strings=None):
    """"10/12(月) 21:00-21:30" (en: "Mon 10/12 21:00-21:30")."""
    s = _strings(strings)
    return f"{_date(rec['startAt'], s)} {fmt_time(rec['startAt'], '%H:%M')}-{fmt_time(rec['endAt'], '%H:%M')}"


NAME_WIDTH = 28  # one line of Estuary's yes/no text box (855 px) holds about 30 full-width characters


def one_line(text, width=NAME_WIDTH):
    """text cut to about one dialog line: full-width (and ambiguous) characters count 1, the rest 0.6; "…" when cut."""
    used = 0.0
    for i, ch in enumerate(text):
        used += 1.0 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 0.6
        if used > width:
            return text[:i].rstrip() + "…"
    return text


def confirm_message(op, rec, channel, strings=None):
    """The yes/no text, four lines (Estuary's dialog shows four before it scrolls): the question first (the delete's
    "cannot be undone" must be seen at once), the name cut to one line, "10/12(月) 21:00-21:30 {channel}", for
    delete the files with sizes, and "(エンコード中: 中止されます)" on that line for a recording being encoded."""
    s = _strings(strings)
    lines = [s["q_" + op], one_line(to_half(rec["name"])), f"{time_line(rec, s)} {channel}"]
    if op == "delete":
        files = files_line(rec, s)
        if rec.get("isEncoding"):  # on the files line: the text keeps to Estuary's four lines
            files = f"{files} ({s['q_encoding']})" if files else s["q_encoding"]
        lines.append(files)
    return "\n".join(x for x in lines if x)


def delete_file_choices(rec):
    """The files the select list offers, in the server's order (each one may go while another stays)."""
    return list(rec.get("videoFiles") or []) if can("delete_file", rec) else []


def _file_text(v):
    return f"{v['name']} {fmt_size(v.get('size', 0))}"


def confirm_file_message(rec, video_file_id, channel, strings=None):
    """The yes/no text of ファイルを削除, four lines: the question, the name cut to one line, "10/12(月) 21:00-21:30
    {channel}", "削除: TS 136.8 MB / 残す: H.264 5.0 MB" (the file that goes and the ones that stay)."""
    s = _strings(strings)
    files = rec.get("videoFiles") or []
    gone = [v for v in files if v.get("id") == video_file_id]
    keep = [v for v in files if v.get("id") != video_file_id]
    lines = [s["q_delete_file"], one_line(to_half(rec["name"])), f"{time_line(rec, s)} {channel}",
             s["delete_keep"].format(", ".join(_file_text(v) for v in gone), ", ".join(_file_text(v) for v in keep))]
    return "\n".join(x for x in lines if x)


def fingerprint(rec):
    """What the dialog showed of a recording: id, name, start, channel, its files (id, size) and isEncoding. The
    re-read after the yes must match. Sizes only for a finished recording: the TS of one in progress
    grows."""
    growing = bool(rec.get("isRecording"))
    files = sorted((v.get("id"), None if growing else v.get("size")) for v in rec.get("videoFiles") or [])
    return (rec.get("id"), rec.get("name"), rec.get("startAt"), rec.get("channelId"), tuple(files),
            bool(rec.get("isEncoding")))


def done_message(op, strings=None):
    return _strings(strings)["done_" + op]


GONE_ERRORS = ("RecordedIdIsNotFound", "RecordedIsNull")


def maybe_applied(status, detail):
    """No answer (status None), or EPGStation's 500 "IPCTimeout": its IPC gave up after 5 s but the operator keeps
    going (a delete of many files), so the write may have been applied."""
    return status is None or detail == "IPCTimeout"


def error_message(status, detail, strings=None):
    """Toast text of a failed read or write: EPGStation's named 500 errors, no answer (or IPCTimeout), else
    失敗しました: {server text or HTTP status}."""
    s = _strings(strings)
    if detail == "RecordedIsProtected":
        return s["protected"]
    if detail in GONE_ERRORS:
        return s["gone"]
    if detail == "VideoFileIsNotFound":
        return s["file_gone"]
    if maybe_applied(status, detail):
        return s["failed_net"]
    return s["failed"].format(detail or f"HTTP {status}")


def is_refusal(detail):
    """A named refusal (warning toast) rather than a failure (error toast)."""
    return detail in ("RecordedIsProtected", "VideoFileIsNotFound") or detail in GONE_ERRORS
