"""Tiny fake of Kodi's xbmc module - records what the addon does."""
LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR = 0, 1, 2, 3
logs = []
played = []  # (url, listitem) per Player().play()
player_file = ""  # test knob: what Player() is playing ("" = nothing)
waited = 0.0  # seconds Monitor().waitForAbort() was asked to wait (it returns at once)
wait_hook = None  # test knob: called with `waited` after every waitForAbort (fires player callbacks)
players = []  # every Player() created, newest last
visible = set()  # test knob: conditions getCondVisibility() reports true
builtins = []  # reservations (007): every executebuiltin() command
# transcoded playback (011)
player_time = 0.0  # test knob: Player().getTime() while something plays
player_tag = None  # test knob: the "epgstation.stream" property of the playing item (None: no getPlayingItem)
abort_after = None  # test knob: waitForAbort() returns True once `waited` reaches this many seconds
stop_calls = []  # every Player().stop() (the file playing then)
KODI_STRINGS = {222: "Cancel"}  # Kodi's own strings the addon uses


def executebuiltin(function, wait=False):
    builtins.append(function)


def getLocalizedString(string_id):  # noqa: N802
    return KODI_STRINGS.get(string_id, "")


def getCondVisibility(condition):  # noqa: N802
    return condition in visible


def log(msg, level=LOGDEBUG):
    logs.append((level, msg))


class VideoStreamDetail:
    def __init__(self, width=0, height=0, aspect=0.0, duration=0, codec="", stereomode="", language="", hdrtype=""):
        self.width, self.height, self.codec, self.aspect = width, height, codec, aspect


class AudioStreamDetail:  # full detail view (002)
    def __init__(self, channels=-1, codec="", language=""):
        self.channels, self.codec, self.language = channels, codec, language


class InfoTagVideo:
    def __init__(self):
        self.data = {"streams": [], "audio_streams": []}

    def __getattr__(self, name):
        if name.startswith("set"):
            key = name[3:].lower()
            return lambda value: self.data.__setitem__(key, value)
        raise AttributeError(name)

    def addVideoStream(self, stream):  # noqa: N802
        self.data["streams"].append(stream)

    def addAudioStream(self, stream):  # noqa: N802 - full detail view (002)
        self.data["audio_streams"].append(stream)


class Player:
    """Subclasses override the callbacks (onAVStarted, onPlayBackError, ...); tests call them via wait_hook."""

    def __init__(self):
        players.append(self)

    def onAVStarted(self):  # noqa: N802
        pass

    def onPlayBackStarted(self):  # noqa: N802
        pass

    def onPlayBackError(self):  # noqa: N802
        pass

    def onPlayBackStopped(self):  # noqa: N802
        pass

    def onPlayBackEnded(self):  # noqa: N802
        pass

    def play(self, item=None, listitem=None, windowed=False, startpos=-1):
        played.append((item, listitem))

    def isPlaying(self):  # noqa: N802
        return bool(player_file)

    def getPlayingFile(self):  # noqa: N802
        if not player_file:
            raise RuntimeError("Kodi is not playing any file")
        return player_file

    def getTime(self):  # noqa: N802 - transcoded playback (011)
        if not player_file:
            raise RuntimeError("Kodi is not playing any file")
        return player_time

    def getPlayingItem(self):  # noqa: N802 - transcoded playback (011): Kodi 20+
        if player_tag is None:
            raise AttributeError("getPlayingItem")
        if not player_file:
            raise RuntimeError("Kodi is not playing any file")
        import xbmcgui
        li = xbmcgui.ListItem(path=player_file)
        li.setProperty("epgstation.stream", player_tag)
        return li

    def stop(self):  # transcoded playback (011)
        global player_file
        stop_calls.append(player_file)
        player_file = ""


class Monitor:
    def waitForAbort(self, timeout=None):  # noqa: N802
        global waited
        waited += timeout or 0
        if wait_hook is not None:
            wait_hook(waited)
        return abort_after is not None and waited >= abort_after


class Actor:
    def __init__(self, name="", role="", order=-1, thumbnail=""):
        self.name, self.role, self.order, self.thumbnail = name, role, order, thumbnail

    def __repr__(self):
        return f"Actor({self.name!r}, {self.role!r}, {self.order})"
