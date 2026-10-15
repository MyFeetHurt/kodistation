"""Program guide: what's on at a time, by genre, a channel's days, and a program's information dialog."""
import xbmc
import xbmcgui

from .. import guide, live, reserves, ui
from ..api import ApiError

GUIDE_STRINGS = (("whats_on", 30020), ("by_genre", 30021), ("schedule", 30022), ("now", 30023), ("today", 30024),
                 ("tomorrow", 30025), ("weekdays", 30026), ("channel_schedule", 30027), ("gone", 30028))
LISTS_TIMEOUT = 5  # seconds for a listing's GET /reserves/lists and /recording (the reservation markers)

on_parse_error = ui.parse_error_logger("program")


def guide_strings():
    from .live import live_strings

    return dict(live_strings(), **ui.strings(GUIDE_STRINGS))


def mark_strings():
    """Strings of a guide listing with reservation markers (the guide's "gone" wins)."""
    from .reservations import record_strings

    out = record_strings()
    out.update(guide_strings())
    return out


def _cache():
    return guide.GuideCache(ui.profile_path(), ui.server_url(), now=lambda: ui.now_ms() / 1000)


def _schedules(ctx):
    """The next 24 h of every watchable broadcast type (disk cache, guide.TTL), GR sub-channels left out unless the
    setting shows them."""
    schedules = _cache().schedules(ctx.api, live.live_types(ctx.api.channels()))
    return schedules if ui.show_subchannels() else live.drop_subchannels(schedules)


# --- reservation markers ------------------------------------------------------------------------------------------

def _reserve_index(ctx, start, end):
    """reserves.lists_index of GET /reserves/lists for [start, end] (never cached); None when it fails or the answer
    has another shape (the listing then shows no markers and no reservation entries)."""
    try:
        return reserves.lists_index(ctx.api.reserve_lists(start, end, timeout=LISTS_TIMEOUT))
    except (ApiError, KeyError, TypeError, AttributeError) as e:
        ui.log(f"reserve lists: {e} (no reservation markers or entries)", xbmc.LOGWARNING)
        return None


def _records_for(ctx, index, programs, now):
    """GET /recording (for ●) only when a listed program on air has a normal reservation."""
    if not reserves.needs_records(index, programs, now):
        return ()
    try:
        return ctx.api.recording(timeout=LISTS_TIMEOUT)["records"]
    except (ApiError, KeyError, TypeError) as e:
        ui.log(f"recording for markers: {e}", xbmc.LOGWARNING)
        return ()


def marks(ctx, programs, now, window=None):
    """(index, records) for a listing's markers: one lists GET over the listed programs (window: its own range)."""
    window = window or reserves.lists_window(programs)
    if window is None:
        return {}, ()
    index = _reserve_index(ctx, *window)
    return index, _records_for(ctx, index, programs, now)


# --- views ------------------------------------------------------------------------------------------------------------

def _program_list(ctx, rows, now, kind, category):
    index, records = marks(ctx, [p for _, p in rows], now)
    out = guide.program_items(rows, ctx.api, now, ui.lang(), mark_strings(), kind, on_parse_error, index=index,
                              records=records)
    ctx.add(out, sort=ui.SORT_GUIDE, category=category, cache=False)


def guide_now(ctx):
    """No t: the hour folders (no request). t=now or ms: one program per channel airing at t."""
    strings, now = guide_strings(), ui.now_ms()
    t = ctx.params.get("t")
    if not t:
        ctx.add(guide.slot_folders(now, strings), content="files", category=strings["whats_on"] or None,
                cache=False)  # labels follow the clock
        return
    at = now if t == "now" else ctx.int_param("t")
    _program_list(ctx, guide.on_air_at(_schedules(ctx), at), now, "slot", guide.slot_header(at, now, strings))


def guide_genres(ctx):
    counts = guide.genre_counts(_schedules(ctx), ui.now_ms())
    ctx.add(guide.genre_folders(counts, ui.lang()), content="files", category=ui.s(30021), cache=False)


def guide_genre(ctx):
    number, now = ctx.int_param("genre"), ui.now_ms()
    _program_list(ctx, guide.genre_programs(_schedules(ctx), number, now), now, "genre",
                  guide.genre_name(number, ui.lang()))


def guide_channel(ctx):
    """No day: the day folders of the next 8 days. day=<local midnight ms>: that day's programs."""
    p, strings, now = ctx.params, guide_strings(), ui.now_ms()
    channel_id = ctx.int_param("channelId")
    name = p.get("name") or str(channel_id)
    if "day" not in p:
        days = ctx.api.channel_schedule(channel_id, guide.local_midnight(now), days=guide.DAYS)
        channel = days[0]["channel"] if days else {"id": channel_id, "name": name}
        out = guide.day_folders(channel, guide.day_buckets(days, now), now, strings, ctx.api)
        ctx.add(out, content="files", category=strings["channel_schedule"].format(name), cache=False)
        return
    start = guide.local_midnight(ctx.int_param("day"))  # absolute day: the URL stays right after midnight
    sched = ctx.api.channel_schedule(channel_id, start, days=1)
    channel = sched[0]["channel"] if sched else {"id": channel_id, "name": name}
    progs = guide.day_programs(sched, start, now)
    index, records = marks(ctx, progs, now)
    out = guide.program_items([(channel, x) for x in progs], ctx.api, now, ui.lang(), mark_strings(), "day",
                              on_parse_error, context=False, index=index, records=records)
    ctx.add(out, sort=ui.SORT_GUIDE, category=f"{name} {guide.date_text(start, strings)}", cache=False)


def guide_info(ctx):
    """Click on a future program: Kodi's information dialog with the program as it is now in the guide."""
    p, strings = ctx.params, guide_strings()
    program_id = ctx.int_param("programId")
    try:
        program = ctx.api.schedule_detail(program_id)
    except ApiError as e:
        ui.log(f"guide info {p.get('programId')}: {e}", xbmc.LOGWARNING)
        ctx.notify(strings["gone"] if e.status == 404 else f"{ui.s(30100)}: {e}", xbmcgui.NOTIFICATION_ERROR)
        return
    channel = {"id": p.get("channelId") or program.get("channelId"), "name": p.get("name") or "",
               "hasLogoData": p.get("logo") == "1"}
    d = guide.program_item(channel, program, ctx.api, ui.now_ms(), ui.lang(), strings, "day", on_parse_error,
                           context=False)
    li = ui.listitem(d)
    if d["playable"]:
        # on air since the list was made: the dialog's Play plays it through live_play. A future program keeps no
        # path: Play then gives Kodi's own "Playback failed" (no request); Estuary always shows that button.
        li.setPath(d["path"])
    xbmcgui.Dialog().info(li)
