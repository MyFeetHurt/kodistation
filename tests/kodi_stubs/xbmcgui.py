import xbmc

NOTIFICATION_INFO, NOTIFICATION_WARNING, NOTIFICATION_ERROR = "info", "warning", "error"
notifications = []
# Tests set these to script dialog answers.
input_answers = []
select_answers = []
selects = []  # (heading, items) of every select dialog shown
info_shown = []  # program guide (013): ListItems passed to Dialog().info
textviewers = []  # full detail view (002): (heading, text, usemono) of every Dialog().textviewer
DLG_YESNO_NO_BTN, DLG_YESNO_YES_BTN, DLG_YESNO_CUSTOM_BTN = 10, 11, 12  # reservations (007)
yesno_answers = []  # reservations (007): test knob, answers of the next yes/no dialogs (empty: False = No)
yesnos = []  # every yes/no dialog shown: dict of its arguments
yesno_hook = None  # test knob: called while a yes/no dialog is "open" (before it answers)
# transcoded playback (011)
numeric_answers = []  # test knob: answers of the next Dialog().numeric() (empty: "" = cancelled)
numerics = []  # (type, heading, default) of every numeric dialog
progress_log = []  # ("create" | "update" | "close", args) of every DialogProgress
progress_cancel_after = None  # test knob: iscanceled() is True from this many calls on


class ListItem:
    def __init__(self, label="", label2="", path="", offscreen=False):
        self.label, self.path = label, path
        self.art, self.props, self.context = {}, {}, []
        self.mimetype, self.content_lookup = None, True
        self.tag = xbmc.InfoTagVideo()

    def setMimeType(self, mimetype):  # noqa: N802
        self.mimetype = mimetype

    def setContentLookup(self, enable):  # noqa: N802
        self.content_lookup = enable

    def getVideoInfoTag(self):  # noqa: N802
        return self.tag

    def setArt(self, art):  # noqa: N802
        self.art.update(art)

    def setProperty(self, key, value):  # noqa: N802
        self.props[key] = value

    def getProperty(self, key):  # noqa: N802
        return self.props.get(key, "")

    def addContextMenuItems(self, items):  # noqa: N802
        self.context += items

    def setPath(self, path):  # noqa: N802
        self.path = path

    def getPath(self):  # noqa: N802
        return self.path


class Dialog:
    def notification(self, heading, message, icon="", time=5000, sound=True):
        notifications.append((heading, message, icon))

    def input(self, heading, defaultt="", type=0, option=0, autoclose=0):
        return input_answers.pop(0) if input_answers else ""

    def textviewer(self, heading, text, usemono=False):  # full detail view (002)
        textviewers.append((heading, text, usemono))

    def info(self, listitem):  # program guide (013)
        info_shown.append(listitem)
        return True

    def yesno(self, heading, message, nolabel="", yeslabel="", autoclose=0, defaultbutton=DLG_YESNO_NO_BTN):
        yesnos.append({"heading": heading, "message": message, "nolabel": nolabel, "yeslabel": yeslabel,
                       "defaultbutton": defaultbutton})
        answer = yesno_answers.pop(0) if yesno_answers else False
        if yesno_hook is not None:
            yesno_hook()
        return answer

    def numeric(self, type, heading, defaultt="", bHiddenInput=False):  # noqa: A002, N803 - Kodi API (011)
        numerics.append((type, heading, defaultt))
        return numeric_answers.pop(0) if numeric_answers else ""

    def select(self, heading, items, autoclose=0, preselect=-1, useDetails=False):
        selects.append((heading, list(items)))
        return select_answers.pop(0) if select_answers else -1


class DialogProgress:  # transcoded playback (011)
    def __init__(self):
        self.checks = 0

    def create(self, heading, message=""):
        progress_log.append(("create", heading, message))

    def update(self, percent, message=""):
        progress_log.append(("update", percent))

    def iscanceled(self):
        self.checks += 1
        return progress_cancel_after is not None and self.checks >= progress_cancel_after

    def close(self):
        progress_log.append(("close",))
