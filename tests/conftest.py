import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests", "kodi_stubs"))
sys.path.insert(0, os.path.join(ROOT, "plugin.video.epgstation"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import fake_epgstation  # noqa: E402
import local_env  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")


@pytest.fixture(autouse=True)
def _no_local_env(monkeypatch):
    """Tests never see the developer's local.env (backlog 019): make exports its keys, tools load the file."""
    monkeypatch.setenv("LOCAL_ENV", os.path.join(ROOT, "tests", "no-such-local.env"))
    for key in local_env.KEYS:
        monkeypatch.setenv(key, "")  # recorded, so a value a tool loads during the test is removed afterwards
        monkeypatch.delenv(key)
    # the public audit never reads the real Gitea credentials file in tests
    monkeypatch.setenv("PUBLIC_AUDIT_CREDENTIALS_FILE", os.path.join(ROOT, "tests", "no-such-credentials"))


@pytest.fixture(scope="session", params=["dev", "dev2"])
def fake_server(request):
    """A fake EPGStation serving each captured fixture set."""
    root = os.path.join(FIXTURES, request.param)
    if not os.path.isdir(os.path.join(root, "api")):
        pytest.skip(f"no fixtures in {root}")
    srv, url = fake_epgstation.serve(root)
    yield url
    srv.shutdown()


@pytest.fixture
def handler(fake_server):
    """The fake server's handler class (knobs multiply / delay / mutate), reset afterwards."""
    h = fake_epgstation.HANDLERS[fake_server]
    yield h
    h.multiply, h.delay, h.mutate = 1, 0.0, None
    h.live_error, h.config_patch = None, None
    h.droplog_body = None  # full detail view (002)
    h.now = None  # program guide (013)
    h.reset_reserves()  # reservations (007): state, writes, write_error, stop_drops_recording


@pytest.fixture
def kodi(fake_server, tmp_path):
    """Reset the Kodi stubs and point the addon at the fake server."""
    import xbmc
    import xbmcaddon
    import xbmcgui
    import xbmcplugin
    import xbmcvfs

    xbmcvfs.profile_dir = str(tmp_path / "profile")
    xbmcplugin.state.reset()
    xbmc.logs.clear()
    xbmc.played.clear()
    xbmc.player_file, xbmc.waited, xbmc.wait_hook = "", 0.0, None
    xbmc.players.clear()
    xbmc.visible.clear()
    xbmcgui.notifications.clear()
    xbmcgui.input_answers.clear()
    xbmcgui.select_answers.clear()
    xbmcgui.selects.clear()
    xbmcgui.info_shown.clear()  # program guide (013)
    xbmcgui.textviewers.clear()  # full detail view (002)
    xbmcgui.yesno_answers.clear()  # reservations (007)
    xbmcgui.yesnos.clear()
    xbmcgui.yesno_hook = None
    xbmc.builtins.clear()
    xbmc.player_time, xbmc.player_tag, xbmc.abort_after = 0.0, None, None  # transcoded playback (011)
    xbmc.stop_calls.clear()
    xbmcgui.numeric_answers.clear()
    xbmcgui.numerics.clear()
    xbmcgui.progress_log.clear()
    xbmcgui.progress_cancel_after = None
    xbmcaddon.settings.clear()
    xbmcaddon.opened_settings.clear()
    xbmcaddon.settings["server_url"] = fake_server
    fake_epgstation.HANDLERS[fake_server].reset_reserves()  # reservations (007): no state from an earlier test
    return xbmcplugin.state


def run_plugin(query=""):
    from resources.lib.epgstation.plugin import Plugin

    Plugin(["plugin://plugin.video.epgstation/", "1", "?" + query if query else ""]).run()
