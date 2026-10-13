SORT_METHOD_UNSORTED, SORT_METHOD_LABEL, SORT_METHOD_DATEADDED, SORT_METHOD_DURATION = 0, 1, 2, 3
SORT_METHOD_VIDEO_YEAR, SORT_METHOD_EPISODE = 18, 24
SORT_METHOD_TITLE, SORT_METHOD_GENRE, SORT_METHOD_STUDIO = 9, 16, 33  # program guide (013)


class _State:
    def reset(self):
        self.items, self.sort_methods, self.content, self.category = [], [], None, None
        self.ended = None  # None = not called, else succeeded flag
        self.cache = None  # cacheToDisc of endOfDirectory
        self.resolved = None  # (succeeded, ListItem)


state = _State()
state.reset()


def addDirectoryItem(handle, url, listitem, isFolder=False, totalItems=0):  # noqa: N802
    state.items.append((url, listitem, isFolder))
    return True


def addSortMethod(handle, sortMethod, labelMask="", label2Mask=""):  # noqa: N802
    state.sort_methods.append((sortMethod, labelMask, label2Mask))


def setContent(handle, content):  # noqa: N802
    state.content = content


def endOfDirectory(handle, succeeded=True, updateListing=False, cacheToDisc=True):  # noqa: N802
    state.ended = succeeded
    state.cache = cacheToDisc


def setResolvedUrl(handle, succeeded, listitem):  # noqa: N802
    state.resolved = (succeeded, listitem)


def setPluginCategory(handle, category):  # noqa: N802
    state.category = category
