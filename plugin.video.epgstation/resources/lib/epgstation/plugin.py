"""Entry point: route plugin://plugin.video.epgstation/?action=... to a view, with the shared error handling.

The views live in views/ (one module per feature) and the logic they use next to this module (no Kodi imports
there, so it is unit-tested directly).
"""
import sys

import xbmc
import xbmcgui

from . import api as epgs_api
from . import ui
from .api import ApiError
from .items import PLUGIN
from .views import guide, live, recorded_actions, recordings, reservations, stream

DIRECTORY, PLAY, SCRIPT = "directory", "play", "script"  # what Kodi waits for: a listing, a resolved URL, nothing

ACTIONS = {
    "root": (recordings.root, DIRECTORY),
    "browse": (recordings.browse, DIRECTORY),
    "movies": (recordings.movies, DIRECTORY),
    "tvshows": (recordings.tvshows, DIRECTORY),
    "show": (recordings.show, DIRECTORY),
    "list": (recordings.recorded_list, DIRECTORY),
    "recording": (recordings.recording, DIRECTORY),
    "channels": (recordings.channels, DIRECTORY),
    "genres": (recordings.genres, DIRECTORY),
    "genre": (recordings.genre, DIRECTORY),
    "rules": (recordings.rules, DIRECTORY),
    "search": (recordings.search, DIRECTORY),
    "search_ask": (recordings.search_ask, SCRIPT),
    "play": (recordings.play, PLAY),
    "choose": (recordings.choose, PLAY),
    "droplog": (recordings.droplog, SCRIPT),
    "stream": (stream.stream, SCRIPT),
    "recorded_action": (recorded_actions.recorded_action, SCRIPT),
    "live": (live.live_menu, DIRECTORY),
    "live_play": (live.live_play, PLAY),
    "guide_now": (guide.guide_now, DIRECTORY),
    "guide_genres": (guide.guide_genres, DIRECTORY),
    "guide_genre": (guide.guide_genre, DIRECTORY),
    "guide_channel": (guide.guide_channel, DIRECTORY),
    "guide_info": (guide.guide_info, SCRIPT),
    "reserves": (reservations.reserve_list, DIRECTORY),
    "reserve_info": (reservations.reserve_info, SCRIPT),
    "reserve_action": (reservations.reserve_action, SCRIPT),
    "record": (reservations.record, SCRIPT),
}


class Plugin:
    def __init__(self, argv):
        self.ctx = ui.Context(argv)

    def run(self):
        ctx = self.ctx
        action = ctx.params.get("action", "root")
        if action not in ACTIONS:
            ui.log(f"unknown action {action}", xbmc.LOGWARNING)
            return
        view, kind = ACTIONS[action]
        try:
            if not ui.server_url():
                _ask_for_server(ctx, kind)
                return
            _run(ctx, action, view, kind)
        finally:
            epgs_api.join_workers()  # after endOfDirectory / setResolvedUrl: no worker thread outlives the run
            ui.release_addon()


def _fail(ctx, kind):
    """Tell Kodi the action failed, in the form it waits for."""
    if kind == PLAY:
        ctx.fail_resolve()
    elif kind == DIRECTORY:
        ctx.fail_directory()


def _run(ctx, action, view, kind):
    try:
        view(ctx)
    except ui.BadParamError as e:
        ui.log(f"{action}: bad parameter {e}", xbmc.LOGERROR)
        ctx.notify(ui.s(30029), xbmcgui.NOTIFICATION_ERROR)
        _fail(ctx, kind)
    except ApiError as e:
        ui.log(str(e), xbmc.LOGERROR)
        ctx.notify(f"{ui.s(30100)}: {e}", xbmcgui.NOTIFICATION_ERROR)
        _fail(ctx, kind)


def _ask_for_server(ctx, kind):
    """No server URL yet (first start): offer the settings instead of failing against a guessed address."""
    _fail(ctx, kind)
    if not xbmcgui.Dialog().yesno(ui.addon().getAddonInfo("name"), ui.s(30290),
                                  defaultbutton=xbmcgui.DLG_YESNO_YES_BTN):
        return
    ui.addon().openSettings()
    if ui.server_url():  # set now: open the addon's menu, as the user meant to
        xbmc.executebuiltin(f"ActivateWindow(Videos,{PLUGIN},return)")


def run():
    Plugin(sys.argv).run()
