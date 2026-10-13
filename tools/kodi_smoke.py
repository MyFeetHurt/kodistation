#!/usr/bin/env python3
"""End-to-end smoke test: ask the dev VM's Kodi to list the plugin's directories.

Kodi runs the real addon code against the real EPGStation, so this checks the
whole chain. Exits non-zero on failure.

    tools/kodi_smoke.py [--host HOST] [--show URL] [--screenshot out.png]

HOST defaults to $KODI_HOST (local.env, backlog 019); without one it refuses (backlog 023).
ssh to the box uses $KODI_SSH_USER.
--screenshot grabs the VM console with `virsh screenshot $SCREENSHOT_VM` on $SCREENSHOT_SSH (both from local.env).
"""
import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugin.video.epgstation"))
import local_env  # noqa: E402  (019: hosts from local.env)
from resources.lib.epgstation import details, epgmeta, guide, items, live, reserves, thumbs  # noqa: E402  (003: thumbs)
from resources.lib.epgstation.api import EPGStation  # noqa: E402
from resources.lib.epgstation.genres import genre_name, genre_pairs, item_genres  # noqa: E402  (002, 009)

PLUGIN = "plugin://plugin.video.epgstation/"
# live TV (012): live; reservations (007): reserves; protect / delete (006): search_ask (検索 is a click action)
ROOT_ACTIONS = ["movies", "tvshows", "list", "recording", "reserves", "live", "search_ask", "browse"]
PROPS = ["title", "plot", "genre", "studio", "runtime", "premiered", "firstaired", "art", "file", "streamdetails",
         "originaltitle", "showtitle", "season", "episode", "year", "cast", "director", "writer", "tag"]


def rpc(host, method, **params):
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(f"http://{host}:8080/jsonrpc", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.loads(resp.read())
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def listdir(host, url):
    return rpc(host, "Files.GetDirectory", directory=url, media="video", properties=PROPS).get("files") or []


def epgs_records(epgs):
    """{id: RecordedItem} of the dev EPGStation, full-width like the addon reads them (GET only)."""
    out = {}
    for path in ("/api/recorded", "/api/recording"):
        query = urllib.parse.urlencode({"isHalfWidth": "false", "offset": 0, "limit": 1000})
        with urllib.request.urlopen(f"{epgs}{path}?{query}", timeout=30) as resp:
            out.update({r["id"]: r for r in json.loads(resp.read())["records"]})
    return out


def expected_metadata(rec, lang):
    """What Kodi should report for this recording: backlog 015 AC 4, from epgmeta.metadata()."""
    meta = epgmeta.metadata(rec)
    name = items.to_half(rec["name"])
    if meta is None:
        return {"originaltitle": "", "title": name}
    show = items.to_half(meta["show"])
    want = {"originaltitle": name,
            "cast": [(items.to_half(n), items.to_half(items.localized_role(r, lang) or "")) for n, r in meta["cast"]],
            "director": [items.to_half(n) for n in meta["directors"]],
            "writer": [items.to_half(n) for n in meta["writers"]],
            "tag": items.localized_tags([items.to_half(t) for t in meta["tags"]], lang)}
    if meta["kind"] == "movie":
        # no air date: Kodi would take the movie's year from premiered / firstaired
        want.update(type="movie", title=show or name, premiered="", firstaired="", year=meta["year"] or 0)
    else:
        want.update(type="episode", showtitle=show, title=items.to_half(meta["subtitle"]) or show or name,
                    season=(meta["season"] or 1) if meta["episode"] is not None else -1,
                    episode=meta["episode"] if meta["episode"] is not None else -1)
    want["plot_start"] = items.to_half(meta["plot"]).replace("\r\n", "\n")
    return want


def check_metadata(files, records):
    """Failures where Kodi's fields differ from epgmeta.metadata() (either genre_language setting)."""
    failures, checked = [], 0
    for f in files:
        if f.get("filetype") != "file":
            continue
        rec_id = int(urllib.parse.parse_qs(urllib.parse.urlparse(f["file"]).query)["id"][0])
        if rec_id not in records:
            failures.append(f"recording {rec_id} listed by Kodi but not by EPGStation")
            continue
        got = dict(f, cast=[(a["name"], a.get("role", "")) for a in f.get("cast") or []])
        diffs = {}
        for lang in ("ja", "en"):
            want = expected_metadata(records[rec_id], lang)
            plot_start = want.pop("plot_start", "")
            diffs[lang] = {k: (got.get(k), v) for k, v in want.items() if got.get(k) != v}
            if not got.get("plot", "").startswith(plot_start):
                diffs[lang]["plot"] = (got.get("plot", "")[:60], plot_start[:60])
        if diffs["ja"] and diffs["en"]:
            failures.append(f"recording {rec_id} metadata differs from epgmeta: {diffs['ja']}")
        checked += 1
    return failures, checked


def gui_labels(host, url, timeout=20, with_category=False):
    """Open url in Kodi's video window and return the labels the GUI really shows.

    Files.GetDirectory returns ListItem labels, but the GUI may show something else
    (e.g. the info-tag title when the sort method has no labelMask). Returns to Home after.
    with_category: return (labels, window header from setPluginCategory).
    """
    rpc(host, "GUI.ActivateWindow", window="home")
    time.sleep(1)
    rpc(host, "GUI.ActivateWindow", window="videos", parameters=[url, "return"])
    try:
        deadline = time.time() + timeout
        n = 0
        while time.time() < deadline:
            info = rpc(host, "XBMC.GetInfoLabels", labels=["Container.FolderPath", "Container.NumItems",
                                                           "System.HasActiveModalDialog"])
            if (info["Container.FolderPath"].rstrip("/") == url.rstrip("/")
                    and info["System.HasActiveModalDialog"] != "true" and info["Container.NumItems"].isdigit()
                    and int(info["Container.NumItems"]) > 0):
                n = int(info["Container.NumItems"])
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(f"GUI did not load {url} within {timeout}s")
        # Absolute positions: 0 may be the ".." item, so read one more than NumItems.
        keys = [f"Container.ListItemAbsolute({i}).Label" for i in range(n + 1)]
        info = rpc(host, "XBMC.GetInfoLabels", labels=keys + ["Container.PluginCategory"])
        labels = [info[k] for k in keys if info[k] and info[k] != ".."]
        return (labels, info["Container.PluginCategory"]) if with_category else labels
    finally:
        rpc(host, "GUI.ActivateWindow", window="home")


def screenshot_target():
    """(virsh host, VM domain) from $SCREENSHOT_SSH / $SCREENSHOT_VM; SystemExit when either is unset."""
    virsh_host, vm = local_env.get("SCREENSHOT_SSH"), local_env.get("SCREENSHOT_VM")
    if not virsh_host or not vm:
        sys.exit("--screenshot needs SCREENSHOT_SSH (the libvirt host) and SCREENSHOT_VM (the domain) in local.env")
    return virsh_host, vm


def screenshot(host, url, out):
    """Open url in Kodi's video window and grab the VM console via virsh on the libvirt host."""
    virsh_host, vm = screenshot_target()
    rpc(host, "GUI.ActivateWindow", window="videos", parameters=[url, "return"])
    time.sleep(4)
    cmd = (f"virsh -c qemu:///system screenshot {shlex.quote(vm)} /tmp/epgs.ppm >/dev/null && "
           "python3 -c \"from PIL import Image; Image.open('/tmp/epgs.ppm').save('/tmp/epgs.png')\"")
    subprocess.run(["ssh", virsh_host, cmd], check=True)
    subprocess.run(["scp", "-q", f"{virsh_host}:/tmp/epgs.png", out], check=True)
    print(f"screenshot -> {out}")


def retry_once(what, check, wait=3):
    """Run check() (-> failures). A recording that starts or finishes while the smoke runs makes Kodi's
    listing and the EPGStation snapshot disagree, so on failure wait and run it once more (it re-fetches both)."""
    failures = check()
    if failures:
        print(f"  {what}: {len(failures)} mismatch(es), fetching again in {wait} s")
        time.sleep(wait)
        failures = check()
    return failures


def file_id(f):
    return int(urllib.parse.parse_qs(urllib.parse.urlparse(f["file"]).query)["id"][0])


def kinds_and_keys(records):
    movie_ids, keys = set(), {}
    for rec in records.values():
        meta = epgmeta.metadata(rec)
        if meta is not None and meta["kind"] == "movie":
            movie_ids.add(rec["id"])
        else:
            keys.setdefault(items.show_key(meta, rec), []).append(rec["id"])
    return movie_ids, keys


def check_movies(host, epgs):
    """Backlog 005: Movies = every kind-movie recording (fetched again from EPGStation for this comparison)."""
    records = epgs_records(epgs)
    movie_ids, _ = kinds_and_keys(records)
    movies = listdir(host, PLUGIN + "?action=movies")
    print(f"movies: {len(movies)} items: {[f['label'] for f in movies][:6]}")
    failures = []
    got = {file_id(f) for f in movies if f["filetype"] == "file"}
    if got != movie_ids or len(movies) != len(movie_ids):
        failures.append(f"movies list {sorted(got)}, expected the kind-movie recordings {sorted(movie_ids)}")
    failures += [f"movie {f['label']!r} has type {f.get('type')!r}" for f in movies if f.get("type") != "movie"]
    meta_failures, checked = check_metadata(movies, records)
    print(f"  metadata: {checked} movies checked against epgmeta.metadata(), {len(meta_failures)} differ")
    return failures + meta_failures


def check_shows(host, epgs):
    """Backlog 005: TV shows = one folder per show key; the first folder lists exactly its episodes."""
    records = epgs_records(epgs)
    _, keys = kinds_and_keys(records)
    shows = listdir(host, PLUGIN + "?action=tvshows")
    print(f"tvshows: {len(shows)} folders (EPGStation has {len(keys)} show keys): {[f['label'] for f in shows][:6]}")
    failures = []
    if len(shows) != len(keys):
        failures.append(f"tvshows has {len(shows)} folders, expected {len(keys)} distinct show keys")
    failures += [f"show {f['label']!r} has type {f.get('type')!r}" for f in shows if f.get("type") != "tvshow"]
    failures += [f"show folder with a blank name: {f['label']!r}" for f in shows if f["label"].startswith(" (")]
    if shows:
        first = shows[0]
        key = urllib.parse.parse_qs(urllib.parse.urlparse(first["file"]).query)["key"][0]
        eps = listdir(host, first["file"])
        print(f"  {first['label']}: {len(eps)} episodes")
        if {file_id(f) for f in eps} != set(keys.get(key, [])):
            failures.append(f"show {first['label']!r} lists {[file_id(f) for f in eps]}, expected {keys.get(key)}")
        failures += episode_label_failures(eps)
        bad = [f["label"] for f in eps if f.get("type") == "episode" and f.get("showtitle") != first.get("title")]
        if bad:
            failures.append(f"episodes of {first.get('title')!r} with another showtitle: {bad}")
        ep_failures, ep_checked = check_metadata(eps, records)
        print(f"  metadata: {ep_checked} episodes of the first show checked, {len(ep_failures)} differ")
        failures += ep_failures
    return failures


# --- subgenres (009): ジャンル別 counts genre1-3, a genre's subgenre level, a subgenre list ---------------------------

def genre_entries(records):
    """Library-like entries {"id", "startAt", "genres"} of EPGStation's recordings (what the addon counts)."""
    return [{"id": r["id"], "startAt": r["startAt"], "genres": genre_pairs(r)} for r in records.values()]


def genres_failures(files, entries):
    wants = [_labels(items.genre_folders(entries, lang)) for lang in ("ja", "en")]
    return [] if _labels(files) in wants else [f"ジャンル別 {_labels(files)}, expected {wants[0]}"]


def subgenre_folder_failures(files, genre, entries):
    # Names follow genre_language, the すべて/All entry follows Kodi's UI language: accept every pairing.
    wants = [_labels(items.subgenre_folders(genre, entries, lang, _po(ui)[30270]))
             for lang in ("ja", "en") for ui in ("ja", "en")]
    return [] if _labels(files) in wants else [f"genre {genre}: {_labels(files)}, expected {wants[0]}"]


def subgenre_list_failures(files, genre, sub_genre, entries):
    want = items.subgenre_entries(entries, genre, sub_genre)
    got = [file_id(f) for f in files]
    return [] if got == want else [f"subgenre {genre}/{sub_genre} lists {got}, expected {want}"]


def check_genres(host, epgs):
    """ジャンル別 = one folder per genre in genre1-3 with its count; the genre with the most subgenres: すべて + its
    subgenres; its first subgenre lists exactly those recordings, newest first, header "genre / subgenre"."""
    entries = genre_entries(epgs_records(epgs))
    files = listdir(host, PLUGIN + "?action=genres")
    print(f"ジャンル別: {_labels(files)}")
    failures = genres_failures(files, entries)
    nested = [f for f in files if query_param(f, "action") == "genre"]
    if not nested:
        return failures + ["ジャンル別 has no genre with subgenres"]
    top = max(nested, key=lambda f: len(items.subgenre_counts(entries, int(query_param(f, "genre")))))
    genre = int(query_param(top, "genre"))
    subs = listdir(host, top["file"])
    print(f"  {top['label']}: {_labels(subs)}")
    failures += subgenre_folder_failures(subs, genre, entries)
    if len(subs) > 1:
        sub_genre = int(query_param(subs[1], "subGenre"))
        listed = listdir(host, subs[1]["file"])
        failures += subgenre_list_failures(listed, genre, sub_genre, entries)
        headers = {genre_name(genre, sub_genre, lang) for lang in ("ja", "en")}
        failures += check_gui(host, subs[1]["file"], headers)
    return failures


def check_gui(host, url, header=None):
    """The GUI shows every label Kodi lists for url, and (if given) the window header."""
    listed = listdir(host, url)
    if not listed:
        return []
    gui, category = gui_labels(host, url, with_category=True)
    print(f"gui {url.split('?', 1)[-1][:40]}: header {category!r}, {gui[:6]}")
    failures = [f"GUI {url} does not show the label {f['label']!r}" for f in listed if f["label"] not in gui]
    if header is not None and category not in ({header} if isinstance(header, str) else header):
        failures.append(f"GUI {url} header {category!r}, expected {header!r}")
    return failures


def episode_label_failures(eps):
    """Show-folder labels: "SxE ..." when Kodi has an episode number, else the air date first."""
    out = []
    for f in eps:
        label = f["label"].replace("[COLOR red]●[/COLOR] ", "")
        if f.get("episode", -1) >= 0:
            want = f"{max(f.get('season', 1), 1)}x{f['episode']}"
        else:
            want = (f.get("premiered") or "")[:10]
        if not want or not label.startswith(want):
            out.append(f"show-folder label {f['label']!r} does not start with {want!r}")
    return out


# --- artwork (003): art of menus, folders, recordings and shows (GET only) ---------------------------------------

ROOT_ICONS = {"movies": "DefaultMovies.png", "tvshows": "DefaultTVShows.png",
              "list": "DefaultRecentlyAddedEpisodes.png", "recording": "DefaultPVRRecordings.png",
              "reserves": "DefaultPVRTimers.png", "live": "DefaultPVRChannels.png",
              "search_ask": "DefaultAddonsSearch.png", "browse": "DefaultFolder.png"}
BROWSE_ICONS = {"channels": "DefaultStudios.png", "genres": "DefaultGenre.png", "rules": "DefaultPVRTimerRules.png"}
LIVE_ICONS = {"guide_now": "DefaultPVRGuide.png", "guide_genres": "DefaultGenre.png", "live": "DefaultPVRChannels.png"}
_ART_THUMB = re.compile(r"/api/thumbnails/(\d+)(?:$|[|?])")
_ART_LOGO = re.compile(r"/api/channels/(\d+)/logo(?:$|[|?])")


def art_url(value):
    """Kodi's art value ("image://<quoted url>/" or a plain file name) as the URL / name the addon set."""
    if value.startswith("image://"):
        value = urllib.parse.unquote(value[len("image://"):].rstrip("/"))
    return value


def art_ref(value):
    """("thumb", thumbnail id) | ("logo", channel id) | ("file", name or path) of one art value."""
    url = art_url(value)
    m = _ART_THUMB.search(url)
    if m:
        return "thumb", int(m.group(1))
    m = _ART_LOGO.search(url)
    if m:
        return "logo", int(m.group(1))
    return "file", url


def _art_refs(f):
    return {k: art_ref(v) for k, v in (f.get("art") or {}).items()}


def _common_art_failures(f, refs, logos, what):
    """Every item: a thumb, logos only of hasLogoData channels, never a logo as poster / fanart / landscape."""
    out = []
    if "thumb" not in refs:
        out.append(f"{what} {f['label']!r} has no thumb art: {f.get('art')}")
    out += [f"{what} {f['label']!r} has the logo of channel {v} (no hasLogoData) as {k}"
            for k, (kind, v) in refs.items() if kind == "logo" and v not in logos]
    out += [f"{what} {f['label']!r} has a logo as {k}" for k in ("poster", "fanart", "landscape")
            if refs.get(k, ("",))[0] == "logo"]
    return out


def recording_art_failures(files, records, logos, blank, strict=True):
    """Recording items vs the 003 rules: the newest thumbnail unless it is blank, else the logo (hasLogoData) else the
    addon icon, no poster / fanart then. strict=False (library lists): another thumbnail id than the newest is only
    noted (the library cache may lag behind a regeneration for up to its TTL)."""
    out = []
    for f in files:
        if f.get("filetype") != "file" or query_param(f, "action") != "play":
            continue
        rec = records.get(file_id(f))
        if rec is None:
            continue
        refs = _art_refs(f)
        out += _common_art_failures(f, refs, logos, "recording")
        tid = items.newest_thumb(rec)
        thumb = refs.get("thumb", ("", None))
        if tid is None or tid in blank:
            want = ("logo", rec["channelId"]) if rec["channelId"] in logos else None
            if (want and thumb != want) or (not want and thumb[0] != "file"):
                out.append(f"recording {f['label']!r} (thumbnail {tid}, blank or none) has thumb {thumb}, expected "
                           f"{want or 'the addon icon'}")
            if {"poster", "fanart", "landscape"} & set(refs):
                out.append(f"recording {f['label']!r} without a good thumbnail has poster/fanart: {f.get('art')}")
        elif thumb[0] != "thumb":
            out.append(f"recording {f['label']!r} has thumb {thumb}, expected thumbnail {tid}")
        elif thumb[1] != tid:
            msg = f"recording {f['label']!r} shows thumbnail {thumb[1]}, the newest is {tid}"
            if strict:
                out.append(msg)
            else:
                print(f"  note: {msg} (library cache)")
    return out


def show_art_failures(shows, logos, blank):
    """TV show folders: a thumb, never a known-blank thumbnail, logos only with hasLogoData, no logo as fanart."""
    out = []
    for f in shows:
        refs = _art_refs(f)
        out += _common_art_failures(f, refs, logos, "show")
        out += [f"show {f['label']!r} shows the blank thumbnail {v} as {k}" for k, (kind, v) in refs.items()
                if kind == "thumb" and v in blank]
    return out


def menu_icon_failures(files, want, what):
    """Folders whose action has an icon in want: art icon = thumb = that Default*.png."""
    out = []
    for f in files:
        icon = want.get(query_param(f, "action"))
        art = f.get("art") or {}
        if icon and (art_url(art.get("icon", "")) != icon or art_url(art.get("thumb", "")) != icon):
            out.append(f"{what} item {f['label']!r} art {art}, expected icon = thumb = {icon!r}")
    return out


def channel_folder_failures(files, logos):
    """分類から探す > チャンネル: icon = thumb = the channel's logo when it has one, else DefaultStudios.png."""
    out = []
    for f in files:
        cid = int(query_param(f, "channelId") or 0)
        want = ("logo", cid) if cid in logos else ("file", "DefaultStudios.png")
        refs = _art_refs(f)
        if refs.get("icon") != want or refs.get("thumb") != want:
            out.append(f"channel folder {f['label']!r} art {f.get('art')}, expected {want}")
    return out


def epgs_blank(epgs, ids):
    """The blank ones of these thumbnail ids by the addon's rule, fetched from EPGStation (GET)."""
    out = set()
    for tid in ids:
        try:
            with urllib.request.urlopen(f"{epgs}/api/thumbnails/{tid}", timeout=30) as resp:
                if thumbs.blank_data(resp.read()):
                    out.add(tid)
        except OSError:
            pass  # missing: the addon shows the thumbnail URL (unknown counts as good)
    return out


def check_art(host, epgs):
    """Backlog 003: menu icons, channel folder logos, and the art of 最近の録画 / 録画中 / 映画 / TV番組 against
    EPGStation (hasLogoData from /channels, blank thumbnails fetched here)."""
    channels = epgs_get(epgs, "/channels")
    logos = {c["id"] for c in channels if c.get("hasLogoData")}
    records = epgs_records(epgs)
    blank = epgs_blank(epgs, sorted({t for t in (items.newest_thumb(r) for r in records.values()) if t}))
    failures = menu_icon_failures(listdir(host, PLUGIN), ROOT_ICONS, "root")
    failures += menu_icon_failures(listdir(host, PLUGIN + "?action=browse"), BROWSE_ICONS, "browse")
    failures += menu_icon_failures(listdir(host, PLUGIN + "?action=live"), LIVE_ICONS, "live")
    failures += channel_folder_failures(listdir(host, PLUGIN + "?action=channels"), logos)
    recent = listdir(host, PLUGIN + "?action=list") + listdir(host, PLUGIN + "?action=recording")
    failures += recording_art_failures(recent, records, logos, blank)
    failures += recording_art_failures(listdir(host, PLUGIN + "?action=movies"), records, logos, blank, strict=False)
    shows = listdir(host, PLUGIN + "?action=tvshows")
    failures += show_art_failures(shows, logos, blank)
    with_logo = sum(1 for f in recent if f.get("filetype") == "file" and _art_refs(f).get("thumb", ("",))[0] == "logo")
    print(f"art: {len(logos)} channels with logos, {len(blank)} blank thumbnails {sorted(blank)}, {with_logo} recent "
          f"items with logo art, {len(shows)} shows, {len(failures)} art failures")
    return failures


# --- live TV (012): listings only. The smoke never opens action=live_play or a /streams/live URL. ---------------

def epgs_get(epgs, path, **params):
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    with urllib.request.urlopen(f"{epgs}/api{path}{query}", timeout=30) as resp:
        return json.loads(resp.read())


def streams_failures(epgs, when):
    """EPGStation must have no stream running (tuners are shared with production recordings; transcoded playback
    (011): a recorded HLS stream left behind loads the server and fills data/streamfiles)."""
    streams = epgs_get(epgs, "/streams", isHalfWidth="true")
    print(f"streams {when}: {streams}")
    return [] if streams == {"items": []} else [f"EPGStation has streams {when}: {streams}"]


def expected_live(schedules, now_ms):
    """[(channelId, half-width channel name, half-width program name)] the live list should show, in order."""
    out = []
    for sched in schedules:
        if not live.watchable(sched["channel"]):
            continue
        cur, _ = live.now_next(sched["programs"], now_ms)
        if cur is not None:
            out.append((sched["channel"]["id"], items.to_half(sched["channel"]["name"]), items.to_half(cur["name"])))
    return out


def query_param(f, key):
    return urllib.parse.parse_qs(urllib.parse.urlparse(f["file"]).query).get(key, [""])[0]


def live_item_failures(files, want, logos, markers=None):
    """Kodi's channel list vs expected_live(); logos: ids of channels with hasLogoData. markers (014): {channelId:
    the reservation marker its current program should carry ("" for none)}; None: not checked."""
    failures = []
    got = [int(query_param(f, "channelId") or 0) for f in files]
    if got != [w[0] for w in want]:
        failures.append(f"live channels {got}, expected {[w[0] for w in want]}")
        return failures
    for f, (cid, name, program) in zip(files, want):
        if f.get("filetype") != "file" or query_param(f, "action") != "live_play":
            failures.append(f"live item {f['label']!r} is not a playable live_play item")
        mark = (markers or {}).get(cid, "")
        if markers is None and f["label"].startswith(("[COLOR red]", "[COLOR grey]")):
            mark = f["label"].split("[/COLOR] ", 1)[0] + "[/COLOR] "
        if not f["label"].startswith(mark + name) or program not in f["label"]:
            failures.append(f"live label {f['label']!r} lacks {mark + name!r} / {program!r}")
        art = urllib.parse.unquote(json.dumps(f.get("art") or {}))
        if cid in logos and f"/api/channels/{cid}/logo" not in art:
            failures.append(f"live item {f['label']!r} has no logo art: {f.get('art')}")
    with_genres = sum(1 for f in files if f.get("genre"))
    if files and with_genres < len(files) // 2:
        failures.append(f"only {with_genres} of {len(files)} live items have genres")
    return failures


LIVE_HEADERS = {"GR": {"地上波", "Terrestrial"}, "SKY": {"SKY"}, None: {"ライブTV", "Live TV"}}


def live_list_url(types):
    """(URL of the first channel list, allowed window headers): with one broadcast type do_live lists its
    channels directly under ライブTV, else the first type's folder."""
    if len(types) == 1:
        return PLUGIN + "?action=live", LIVE_HEADERS[None]
    return PLUGIN + f"?action=live&type={types[0]}", LIVE_HEADERS.get(types[0], {types[0]})


def check_live(host, epgs):
    """Backlog 012 AC 2/3/12: type folders from /channels (or the channels directly for a single type), the
    first type's channel list vs EPGStation /schedules (GET only)."""
    channels = epgs_get(epgs, "/channels")
    types = live.live_types(channels)
    if not types:
        return []
    failures = []
    top = listdir(host, PLUGIN + "?action=live")
    print(f"live: {[f['label'] for f in top][:6]}")
    got_guide = [query_param(f, "action") for f in top[:len(GUIDE_ACTIONS)]]
    if got_guide != GUIDE_ACTIONS:  # program guide (013): 放送中, ジャンル別 before the types / channels
        failures.append(f"live starts with {got_guide}, expected the guide folders {GUIDE_ACTIONS}")
    if len(types) > 1:
        got_types = [query_param(f, "type") for f in top[len(GUIDE_ACTIONS):]]
        if got_types != types:
            failures.append(f"live type folders {got_types}, expected {types}")
    url, _ = live_list_url(types)
    now = time.time() * 1000
    listed = [f for f in listdir(host, url) if query_param(f, "action") == "live_play"]
    flags = {t: "true" if t == types[0] else "false" for t in ("GR", "BS", "CS", "SKY")}
    schedules = epgs_get(epgs, "/schedules", startAt=int(now), endAt=int(now + live.WINDOW_MS),
                         isHalfWidth="false", **flags)
    want = expected_live(schedules, time.time() * 1000)
    logos = {c["id"] for c in channels if c.get("hasLogoData")}
    markers = live_markers(epgs, schedules, time.time() * 1000)
    print(f"live {types[0]}: {len(listed)} channels (expected {len(want)}): {[f['label'] for f in listed][:4]}; "
          f"{sum(1 for m in markers.values() if m)} reserved now (014)")
    return failures + live_item_failures(listed, want, logos, markers)


def live_markers(epgs, schedules, now_ms):
    """{channelId: reservation marker of its current program} from GET /reserves/lists [now, now] (014)."""
    current = {}
    for sched in schedules:
        cur, _ = live.now_next(sched["programs"], now_ms)
        if cur is not None and live.watchable(sched["channel"]):
            current[sched["channel"]["id"]] = dict(cur, channelId=sched["channel"]["id"])
    index, records = epgs_marks(epgs, list(current.values()), now_ms, (now_ms, now_ms))
    s = mark_strings(GUIDE_STRINGS[0])
    return {cid: reserves.guide_mark(p, "", index, records, now_ms, s)[0] for cid, p in current.items()}


def check_live_gui(host, epgs):
    """The first channel list in the GUI: every label shown, the window header."""
    types = live.live_types(epgs_get(epgs, "/channels"))
    if not types:
        return []
    url, headers = live_list_url(types)
    listed = listdir(host, url)
    if not listed:
        return [f"live list {url} is empty"]
    gui, category = gui_labels(host, url, with_category=True)
    print(f"gui live {types[0]}: header {category!r}, {gui[:4]}")
    failures = [f"GUI live list does not show {f['label']!r}" for f in listed if f["label"] not in gui]
    if category not in headers:
        failures.append(f"GUI live list header {category!r}, expected one of {sorted(headers)}")
    return failures


# --- program guide (013): listings only. Never opens action=live_play, action=guide_info or a stream. ----------

GUIDE_ACTIONS = ["guide_now", "guide_genres"]
GUIDE_HEADERS = {"genres": {"ジャンル別", "By genre"}}
NHK = 3211240960  # NHK総合1: the channel whose 番組表 the smoke checks
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
GUIDE_KEYS = {"whats_on": 30020, "by_genre": 30021, "schedule": 30022, "now": 30023, "today": 30024,
              "tomorrow": 30025, "weekdays": 30026, "channel_schedule": 30027, "gone": 30028, "left": 30019}


GUIDE_CACHE = "~/.kodi/userdata/addon_data/plugin.video.epgstation/guide-schedules.json"


def _is_local(host):
    """True when host is an address of this machine (the smoke runs on the VM itself: CI in host mode)."""
    try:
        with socket.socket() as s:
            s.bind((host, 0))
        return True
    except OSError:
        return False


def clear_guide_cache(host):
    """Delete the plugin's 10-min guide cache so Kodi lists what EPGStation has now (no stale smoke results)."""
    if _is_local(host):
        try:
            os.remove(os.path.expanduser(GUIDE_CACHE))
        except FileNotFoundError:
            pass
    else:
        subprocess.run(["ssh", local_env.ssh_target(host), f"rm -f {GUIDE_CACHE}"], check=True)


def po_strings(lang):
    """{key: text} of the guide strings in en (msgid) or ja (msgstr), from the addon's strings.po."""
    import re

    folder = "resource.language.ja_jp" if lang == "ja" else "resource.language.en_gb"
    path = os.path.join(REPO, "plugin.video.epgstation", "resources", "language", folder, "strings.po")
    with open(path, encoding="utf-8") as f:
        found = {int(i): (en, ja) for i, en, ja in
                 re.findall(r'msgctxt "#(\d+)"\nmsgid "([^"]*)"\nmsgstr "([^"]*)"', f.read())}
    return {k: found[i][1 if lang == "ja" else 0] for k, i in GUIDE_KEYS.items()}


GUIDE_STRINGS = (po_strings("ja"), po_strings("en"))
_LABEL_API = EPGStation("http://smoke")


def mark_strings(strings):
    """Guide strings plus the reservation state names the markers use (record from the guide, 014)."""
    lang = 0 if strings is GUIDE_STRINGS[0] else 1
    return dict(RESERVE_STRINGS[lang], **strings)


def guide_labels(rows, now_ms, style, strings, index=None, records=()):
    """index / records (014): the reservation markers as the plugin draws them from GET /reserves/lists."""
    s = mark_strings(strings)
    return [guide.program_item(ch, p, _LABEL_API, now_ms, "ja", s, style, index=index, records=records)["label"]
            for ch, p in rows]


def epgs_marks(epgs, programs, now_ms, window=None):  # record from the guide (014)
    """(index, records) like plugin._marks: GET /reserves/lists over the listed programs, /recording only when a
    program on air has a normal reservation (GET only)."""
    window = window or reserves.lists_window(programs)
    if window is None:
        return {}, ()
    index = reserves.lists_index(epgs_get(epgs, "/reserves/lists", startAt=int(window[0]), endAt=int(window[1])))
    records = ()
    if reserves.needs_records(index, programs, now_ms):
        records = epgs_get(epgs, "/recording", isHalfWidth="false", offset=0, limit=100)["records"]
    return index, records


def marked_count(files):
    """Items carrying a reservation marker (○, ●, [競合] / [除外] / [重複], also after ▶)."""
    return sum(1 for f in files if f["label"].replace(guide.ON_AIR, "", 1).startswith(("[COLOR red]", "[COLOR grey]")))


def _labels(files):
    return [f["label"] for f in files]


def guide_now_failures(files, schedules, now_ms, marks=(None, ())):
    """The 現在 slot vs guide.on_air_at() of EPGStation's /schedules: same labels, all live_play."""
    rows = guide.on_air_at(schedules, now_ms)
    wants = [guide_labels(rows, now_ms, "slot", s, *marks) for s in GUIDE_STRINGS]  # markers follow Kodi's language
    want = wants[0]
    failures = []
    if _labels(files) not in wants:
        failures.append(f"guide 現在 lists {len(files)} items {_labels(files)[:3]}, expected {len(want)} {want[:3]}")
    failures += [f"guide 現在 item {f['label']!r} is not a live_play file" for f in files
                 if f.get("filetype") != "file" or query_param(f, "action") != "live_play"]
    return failures


def guide_genres_failures(files, schedules, now_ms):
    counts = guide.genre_counts(schedules, now_ms)
    wants = [_labels(guide.genre_folders(counts, lang)) for lang in ("ja", "en")]
    return [] if _labels(files) in wants else [f"guide genres {_labels(files)}, expected {wants[0]}"]


def guide_genre_failures(files, schedules, genre, now_ms, marks=(None, ())):
    """One genre folder: labels (with the reservation markers, 014), on-air items first and playable (live_play),
    the others guide_info, genres set."""
    rows = guide.genre_programs(schedules, genre, now_ms)
    wants = [guide_labels(rows, now_ms, "genre", s, *marks) for s in GUIDE_STRINGS]
    failures = []
    if _labels(files) not in wants:
        failures.append(f"guide genre {genre}: {len(files)} items {_labels(files)[:3]}, expected {len(wants[0])} "
                        f"{wants[0][:3]}")
    seen_future = False
    for f in files:
        on_air = f["label"].startswith(guide.ON_AIR)
        action = query_param(f, "action")
        if on_air and (seen_future or action != "live_play"):
            failures.append(f"guide genre {genre}: on-air item {f['label']!r} not first / not live_play ({action})")
        if not on_air:
            seen_future = True
            if action != "guide_info":
                failures.append(f"guide genre {genre}: item {f['label']!r} is {action}, expected guide_info")
        if not f.get("genre"):
            failures.append(f"guide genre {genre}: item {f['label']!r} has no genres")
    return failures


def guide_day_failures(day_files, today_files, days, now_ms, marks=(None, ())):
    """NHK総合1's 番組表: day folders and today's programs (with the reservation markers, 014) vs
    /schedules/{id}?days=8."""
    buckets = guide.day_buckets(days, now_ms)
    channel = days[0]["channel"] if days else {"id": NHK, "name": ""}
    wants = [_labels(guide.day_folders(channel, buckets, now_ms, s)) for s in GUIDE_STRINGS]
    failures = []
    if _labels(day_files) not in wants:
        failures.append(f"guide day folders {_labels(day_files)}, expected {wants[0]}")
    today_wants = [guide_labels([(channel, p) for p in buckets.get(0, [])], now_ms, "day", s, *marks)
                   for s in GUIDE_STRINGS]  # the state markers ([重複] / [Duplicates]) follow Kodi's language
    want_today = today_wants[0]
    if _labels(today_files) not in today_wants:
        failures.append(f"guide today {_labels(today_files)[:3]}, expected {want_today[:3]}")
    return failures


def _type_flags(types):
    return {t: "true" if t in types else "false" for t in ("GR", "BS", "CS", "SKY")}


def check_guide(host, epgs):
    """Backlog 013 smoke: 放送中 slots, 現在, ジャンル別, ニュース・報道, NHK総合1 day folders and today (GET only).

    Starts by deleting the plugin's guide cache (also on retry_once's second run), so Kodi's lists and the GETs
    here come from the same EPG."""
    clear_guide_cache(host)
    channels = epgs_get(epgs, "/channels")
    types = live.live_types(channels)
    if not types:
        return []
    failures = []
    before = epgs_get(epgs, "/reserves/cnts")  # print only: counts change on their own (write_failures checks)
    slots = listdir(host, PLUGIN + "?action=guide_now")
    print(f"guide 放送中: {len(slots)} folders: {_labels(slots)[:3]} … {_labels(slots)[-1:]}")
    if len(slots) != guide.SLOTS + 1 or not slots[0]["label"].startswith(("現在 (", "Now (")):
        failures.append(f"guide 放送中 folders {_labels(slots)[:3]} ({len(slots)}), expected 25 starting with 現在")
    # The cache was just deleted: Kodi's first guide list fetches /schedules, the next ones reuse it, and the GET
    # below follows within seconds (retry_once re-runs everything, cache deletion included).
    now_files = listdir(host, PLUGIN + "?action=guide_now&t=now")
    genres = listdir(host, PLUGIN + "?action=guide_genres")
    news = listdir(host, PLUGIN + "?action=guide_genre&genre=0")
    now = time.time() * 1000
    schedules = epgs_get(epgs, "/schedules", startAt=int(now), endAt=int(now + guide.WINDOW_MS),
                         isHalfWidth="false", **_type_flags(types))
    print(f"guide 現在: {len(now_files)} items; ジャンル別: {_labels(genres)[:4]}; ニュース・報道: {len(news)} items")
    now_rows = [p for _, p in guide.on_air_at(schedules, now)]
    news_rows = [p for _, p in guide.genre_programs(schedules, 0, now)]
    now_marks, news_marks = epgs_marks(epgs, now_rows, now), epgs_marks(epgs, news_rows, now)
    print(f"guide marks (014): 現在 {marked_count(now_files)} of {len(now_files)} marked "
          f"(EPGStation lists {len(now_marks[0])}); ニュース・報道 {marked_count(news)} of {len(news)} "
          f"(lists {len(news_marks[0])})")
    failures += guide_now_failures(now_files, schedules, now, now_marks)
    failures += guide_genres_failures(genres, schedules, now)
    failures += guide_genre_failures(news, schedules, 0, now, news_marks)
    nhk = next((c for c in channels if c["id"] == NHK), None)
    if nhk is not None:
        url = items.plugin_url(action="guide_channel", channelId=NHK, name=items.to_half(nhk["name"]))
        day_files = listdir(host, url)
        today = listdir(host, day_files[0]["file"]) if day_files else []
        days = epgs_get(epgs, f"/schedules/{NHK}", startAt=guide.local_midnight(now), days=guide.DAYS,
                        isHalfWidth="false")
        now = time.time() * 1000
        today_rows = guide.day_buckets(days, now).get(0, [])
        today_marks = epgs_marks(epgs, today_rows, now)
        print(f"guide NHK総合1: {_labels(day_files)[:3]}; today {len(today)} items, {marked_count(today)} marked "
              f"(lists {len(today_marks[0])})")
        failures += guide_day_failures(day_files, today, days, now, today_marks)
    print(f"guide: /reserves/cnts {before} -> {epgs_get(epgs, '/reserves/cnts')} (print only)")
    return failures


# --- reservations (007): listings only. The smoke never opens action=reserve_action or action=reserve_info and never
# sends anything but GET to EPGStation; write_failures() checks the access log and kodi.log (counts: print only). --

RESERVE_HEADERS = {None: {"録画予約", "Scheduled recordings"}, "overlap": {"重複", "Duplicates"},
                   "conflict": {"競合", "Conflicts"}, "skip": {"除外", "Skipped"}}


def _po(lang):
    import re

    folder = "resource.language.ja_jp" if lang == "ja" else "resource.language.en_gb"
    path = os.path.join(REPO, "plugin.video.epgstation", "resources", "language", folder, "strings.po")
    with open(path, encoding="utf-8") as f:
        found = {int(i): (en, ja) for i, en, ja in
                 re.findall(r'msgctxt "#(\d+)"\nmsgid "([^"]*)"\nmsgstr "([^"]*)"', f.read())}
    return {i: v[1 if lang == "ja" else 0] for i, v in found.items()}


# reserves.DEFAULT_STRINGS keys -> string IDs (plugin.RESERVE_STRINGS; plugin.py imports Kodi, so it is repeated)
RESERVE_IDS = {"conflict": 30031, "overlap": 30032, "skip": 30033, "delete": 30034, "skip_this": 30035,
               "stop": 30036, "unskip": 30037, "unoverlap": 30038, "rule": 30053, "manual": 30054,
               "line_conflict": 30055, "line_overlap": 30056, "line_skip": 30057, "recording": 30002,
               "weekdays": 30026}
RESERVE_STRINGS = tuple({k: _po(lang)[i] for k, i in RESERVE_IDS.items()} for lang in ("ja", "en"))


def epgs_reserves(epgs):
    """Every reservation of the dev EPGStation (GET /reserves?type=all, pages of reserves.PAGE)."""
    out, offset = [], 0
    while True:
        page = epgs_get(epgs, "/reserves", type="all", isHalfWidth="false", offset=offset, limit=reserves.PAGE)
        out += page["reserves"]
        offset += reserves.PAGE
        if offset >= page["total"] or not page["reserves"]:
            return out


def expected_reserves(all_reserves, channels, rules, state, now_ms, records=()):
    """[labels] of the 録画予約 list (state None) or a state folder, once per language (ja, en)."""
    names = items.channel_names(channels)
    logos = {c["id"] for c in channels if c.get("hasLogoData")}
    out = []
    for strings in RESERVE_STRINGS:
        dicts = reserves.listing(all_reserves, state, names, logos, rules, _LABEL_API, now_ms, "ja", strings, None,
                                 records)
        out.append([d["label"] for d in dicts])
    return out


def reserve_list_failures(files, wants, what):
    labels = _labels(files)
    if labels in wants:
        return []
    return [f"{what} lists {len(labels)} items {labels[:3]}, expected {len(wants[0])} {wants[0][:3]}"]


def reserve_item_failures(f, r, channel):
    """One reservation item as Kodi reports it: video item, studio = channel, plot starting with the time line, the
    reserve_info path (never playable)."""
    out = []
    lines = [reserves.time_line(r, strings) for strings in RESERVE_STRINGS]
    if f.get("filetype") != "file" or query_param(f, "action") != "reserve_info":
        out.append(f"reservation {f['label']!r} is not a reserve_info item: {f.get('file')}")
    if (f.get("studio") or [None])[0] != items.to_half(channel):
        out.append(f"reservation {f['label']!r} studio {f.get('studio')}, expected {channel!r}")
    if not any((f.get("plot") or "").startswith(line) for line in lines):
        out.append(f"reservation {f['label']!r} plot does not start with its time line: {(f.get('plot') or '')[:40]!r}")
    if not f.get("genre") and r.get("genre1") is not None:
        out.append(f"reservation {f['label']!r} has no genre")
    return out


def check_reserves(host, epgs):
    """Backlog 007 smoke: 録画予約 folders and items, the state folders and one item's fields vs dev GETs. The
    counts are printed only (the 10-min update, the guard or a finishing recording change them; write_failures()
    checks for writes directly)."""
    before = epgs_get(epgs, "/reserves/cnts")
    files = listdir(host, PLUGIN + "?action=reserves")
    now = time.time() * 1000
    all_reserves = epgs_reserves(epgs)
    channels = epgs_get(epgs, "/channels")
    rules = {r["id"]: items.rule_name(r) for r in epgs_get(epgs, "/rules", type="all", limit=1000)["rules"]}
    records = epgs_get(epgs, "/recording", isHalfWidth="false", offset=0, limit=100)["records"]
    folders = [f for f in files if f.get("filetype") == "directory"]
    print(f"録画予約: {len(files)} items, folders {_labels(folders)}; EPGStation {len(all_reserves)} reservations "
          f"{before}")
    want = expected_reserves(all_reserves, channels, rules, None, now, records)
    failures = reserve_list_failures(files, want, "録画予約")
    for f in folders:
        state = query_param(f, "type")
        sub = listdir(host, f["file"])
        want = expected_reserves(all_reserves, channels, rules, state, time.time() * 1000, records)
        print(f"  {f['label']}: {len(sub)} items")
        failures += reserve_list_failures(sub, want, f"録画予約 {state}")
        if any("[COLOR grey]" in x["label"] for x in sub):
            failures.append(f"録画予約 {state} items carry a state marker")
    by_id = {str(r["id"]): r for r in all_reserves}
    names = items.channel_names(channels)
    first = next((f for f in files if f.get("filetype") == "file"), None)
    if first is not None:
        r = by_id.get(query_param(first, "reserveId"))
        if r is None:
            failures.append(f"first reservation {first['label']!r} is not on EPGStation")
        else:
            failures += reserve_item_failures(first, r, names.get(r["channelId"], str(r["channelId"])))
    print(f"録画予約: /reserves/cnts {before} -> {epgs_get(epgs, '/reserves/cnts')} (print only)")
    return failures


# --- full detail view (002) ---------------------------------------------------------------------------------------

# details.DEFAULT_STRINGS keys -> string IDs (plugin.DETAIL_STRINGS + the block keys of plugin._item_strings)
DETAIL_IDS = {"detail_header": 30251, "detail_broadcast": 30252, "detail_genre": 30253, "detail_video": 30254,
              "detail_audio": 30255, "detail_drops": 30256, "detail_encoding": 30257, "droplog": 30258,
              "files": 30080, "weekdays": 30026, "length": 30013, "protect_line": 30087}
DETAIL_STRINGS = tuple({k: _po(lang)[i] for k, i in DETAIL_IDS.items()} for lang in ("ja", "en"))


ADDON_SETTINGS = "~/.kodi/userdata/addon_data/plugin.video.epgstation/settings.xml"


def kodi_ui_lang(host):
    """"ja" when Kodi's GUI language is Japanese (the addon's labels come from ja_jp strings.po), else "en" (en_gb, also
    Kodi's fallback for languages without a strings.po)."""
    value = rpc(host, "Settings.GetSettingValue", setting="locale.language").get("value") or ""
    return "ja" if value == "resource.language.ja_jp" else "en"


def genre_lang(settings_xml):
    """The addon's genre_language ("ja" / "en") from its settings.xml text (missing: the default, ja)."""
    m = re.search(r'<setting id="genre_language"[^>]*>([^<]*)</setting>', settings_xml or "")
    return "en" if m and m.group(1).strip() == "1" else "ja"


def expected_block(rec, channel, ui, glang):
    """The 【録画情報】 block lines without the extra (長さ / 保護) lines: labels in Kodi's language (ui), genre names
    and the audio label in genre_language (glang)."""
    strings = DETAIL_STRINGS[0 if ui == "ja" else 1]
    return details.detail_lines(rec, channel, item_genres(rec, glang), glang, strings)


def detail_failures(f, rec, channel, ui, glang):
    """One recording as Kodi reports it: the plot's last paragraph starts with the block (broadcast, genre, video,
    audio, files, drops in that order), streamdetails audio = details.audio_stream, video aspect = details.aspect."""
    out = []
    last = (f.get("plot") or "").split("\n\n")[-1]
    want_block = "\n".join(expected_block(rec, channel, ui, glang))
    if not (last == want_block or last.startswith(want_block + "\n")):
        out.append(f"recording {rec['id']} block {last[:120]!r}, expected {want_block[:120]!r}")
    sd = f.get("streamdetails") or {}
    audio = [(a.get("codec"), a.get("channels")) for a in sd.get("audio") or []]
    want = details.audio_stream(rec)
    if audio != ([(want["codec"], want["channels"])] if want else []):
        out.append(f"recording {rec['id']} streamdetails audio {audio}, expected {want}")
    video = sd.get("video") or []
    if video and abs(float(video[0].get("aspect") or 0) - details.aspect(rec)) > 0.01:
        out.append(f"recording {rec['id']} video aspect {video[0].get('aspect')}, expected {details.aspect(rec)}")
    return out


def droplog_requests(text, start_iso, end_iso):
    """Access log lines of GET /api/dropLogs in [start_iso, end_iso]."""
    out = []
    for line in text.splitlines():
        m = _ACCESS.match(line)
        if m and start_iso <= m.group(1) <= end_iso and m.group(4).startswith("/api/dropLogs"):
            out.append(line)
    return out


def check_details(host, epgs):
    """Backlog 002 smoke: every listed recording's 【録画情報】 block and audio / aspect stream details vs the dev
    GETs, and listing Recent / Movies / TV shows makes no /api/dropLogs request."""
    started = time.time()
    files = [f for f in listdir(host, PLUGIN + "?action=list") if f.get("filetype") == "file"]
    listdir(host, PLUGIN + "?action=movies")
    listdir(host, PLUGIN + "?action=tvshows")
    ended = time.time()
    records = epgs_records(epgs)
    names = items.channel_names(epgs_get(epgs, "/channels"))
    ui = kodi_ui_lang(host)
    settings = read_vm_file(host, ADDON_SETTINGS)
    if settings is None:
        return [f"details: {ADDON_SETTINGS} not readable (genre_language unknown)"]
    glang = genre_lang(settings)
    print(f"details: Kodi language {ui}, genre_language {glang}")
    failures, checked, drops = [], 0, 0
    for f in files:
        rec = records.get(int(query_param(f, "id") or 0))
        if rec is None:
            continue
        failures += detail_failures(f, rec, names.get(rec["channelId"], str(rec["channelId"])), ui, glang)
        checked += 1
        drops += bool(rec.get("dropLogFile"))
    print(f"details: {checked} recordings checked ({drops} with a drop log), {len(failures)} failures")
    if not checked:
        failures.append("details: no recording checked")
    text = read_vm_file(host, ACCESS_LOG)
    if text is None:
        print(f"details: {ACCESS_LOG} not readable: dropLogs check skipped")
    else:
        failures += [f"listing requested a drop log: {line}" for line in
                     droplog_requests(text, _iso(started), _iso(ended + 1))]
    return failures


# --- review M1: direct write checks for the smoke's time range ----------------------------------------------------

ACCESS_LOG = "~/EPGStation/logs/Service/access.log"
KODI_LOG = "~/.kodi/temp/kodi.log"
KODI_OLD_LOG = "~/.kodi/temp/kodi.old.log"  # Kodi moves kodi.log here when it starts
READ_METHODS = {"GET", "HEAD", "OPTIONS"}  # Kodi itself sends HEAD (content lookup)
_ACCESS = re.compile(r'^\[(\S+)\] \[\w+\] access - (\S+) .*?"(\w+) (\S+)[^"]*" \S+ \S+ "[^"]*" "([^"]*)"')


def read_vm_file(host, path, offset=0):
    """Text of a file on the VM from byte offset on: read locally when the smoke runs on the VM (CI), else over
    ssh $KODI_SSH_USER@host. None when it cannot be read."""
    if _is_local(host):
        try:
            with open(os.path.expanduser(path), "rb") as f:
                f.seek(offset)
                return f.read().decode("utf-8", "replace")
        except OSError:
            return None
    try:
        done = subprocess.run(["ssh", local_env.ssh_target(host), f"tail -c +{offset + 1} {path}"], capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def vm_file_size(host, path):
    if _is_local(host):
        try:
            return os.path.getsize(os.path.expanduser(path))
        except OSError:
            return None
    try:
        done = subprocess.run(["ssh", local_env.ssh_target(host), f"stat -c %s {path}"], capture_output=True, text=True,
                              timeout=60)
        return int(done.stdout.strip()) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def access_writes(text, start_iso, end_iso):
    """(writes by Kodi or the addon, other writes) of EPGStation access log lines in [start_iso, end_iso]: any
    method but GET/HEAD/OPTIONS. Kodi's requests carry its own User-Agent ("Kodi…"), the addon's api.USER_AGENT
    ("KODIstation/…"); others (the dev guard, a tester's tool, the web UI) are only reported."""
    kodi, other = [], []
    for line in text.splitlines():
        m = _ACCESS.match(line)
        if not m or not start_iso <= m.group(1) <= end_iso or m.group(3).upper() in READ_METHODS:
            continue
        ua = m.group(5)
        (kodi if ua.startswith("KODIstation/") or "Kodi" in ua else other).append(line)
    return kodi, other


WRITE_LOG_TAGS = ("[epgstation] reserve:", "[epgstation] recorded:")  # protect / delete (006): recorded


def kodi_write_lines(text):
    """kodi.log lines of the addon's write paths (do_reserve_action, do_record, do_recorded_action)."""
    return [line for line in text.splitlines() if any(tag in line for tag in WRITE_LOG_TAGS)]


def _iso(secs):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(secs))


def addon_reads(text, start_iso, end_iso):
    """GETs with the addon's User-Agent in [start_iso, end_iso]: the self-check of the write check (the smoke makes
    hundreds; none means the regex or the log path broke)."""
    n = 0
    for line in text.splitlines():
        m = _ACCESS.match(line)
        if m and start_iso <= m.group(1) <= end_iso and m.group(3).upper() == "GET" \
                and m.group(5).startswith("KODIstation/"):
            n += 1
    return n


def _kodi_log_since(host, offset):
    """kodi.log text added since offset; when kodi.log is now smaller (Kodi restarted and moved it to kodi.old.log):
    kodi.old.log from offset plus all of kodi.log. None when it cannot be read."""
    if offset is None:
        return None
    size = vm_file_size(host, KODI_LOG)
    if size is not None and size < offset:
        old, new = read_vm_file(host, KODI_OLD_LOG, offset), read_vm_file(host, KODI_LOG, 0)
        return None if old is None and new is None else (old or "") + (new or "")
    return read_vm_file(host, KODI_LOG, offset)


def write_failures(host, start, end, kodi_offset):
    """The smoke wrote nothing: no non-GET request from Kodi in EPGStation's access log for [start, end] (seconds)
    and no "[epgstation] reserve:" / "recorded:" line added to kodi.log since kodi_offset. Self-check: the addon's
    GETs must be in the window. On the VM itself (CI) an unreadable log fails; over ssh from a workstation it is a
    note."""
    failures = []
    local = _is_local(host)

    def unreadable(what):
        if local:
            failures.append(f"write check: {what} not readable on the VM")
        else:
            print(f"write check: {what} not readable over ssh: skipped")

    text = read_vm_file(host, ACCESS_LOG)
    if text is None:
        unreadable(ACCESS_LOG)
    else:
        lo, hi = _iso(start), _iso(end + 1)
        kodi, other = access_writes(text, lo, hi)
        reads = addon_reads(text, lo, hi)
        print(f"write check: access log {lo}..{hi}: {reads} addon GETs, {len(kodi)} writes from Kodi, "
              f"{len(other)} from others")
        for line in other:
            print(f"  note: write by another client: {line}")
        failures += [f"Kodi wrote to EPGStation during the smoke: {line}" for line in kodi]
        if not reads:
            failures.append(f"write check: no KODIstation/ GET in {ACCESS_LOG} for {lo}..{hi} (regex or log path "
                            "broken?)")
    text = _kodi_log_since(host, kodi_offset)
    if text is None:
        unreadable(KODI_LOG)
    else:
        lines = kodi_write_lines(text)
        print(f"write check: kodi.log: {len(lines)} reserve/recorded lines during the smoke")
        failures += [f"kodi.log has a write line during the smoke: {line}" for line in lines]
    return failures


def main():
    local_env.load()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=local_env.get("KODI_HOST"))
    ap.add_argument("--epgs", help="EPGStation the addon uses (default http://HOST:8888)")
    ap.add_argument("--show", help="print every item in this plugin URL and exit")
    ap.add_argument("--screenshot", help="save a screenshot of the recent list here")
    args = ap.parse_args()
    if not args.host.strip():
        sys.exit("no Kodi host: pass --host or set KODI_HOST (~/.config/epgstation-kodi/local.env)")
    if args.screenshot:
        screenshot_target()  # refuse before the smoke runs

    if args.show:
        for f in listdir(args.host, args.show):
            print(json.dumps(f, ensure_ascii=False, indent=1))
        return 0

    failures = []
    epgs = args.epgs or f"http://{args.host}:8888"
    started = time.time()  # review M1: the access log / kodi.log write checks cover [started, end]
    kodi_offset = vm_file_size(args.host, KODI_LOG)
    cnts_start = epgs_get(epgs, "/reserves/cnts")  # print only
    print(f"reserves/cnts at start: {cnts_start}")
    root = listdir(args.host, PLUGIN)
    print(f"root: {[f['label'] for f in root]}")
    actions = [urllib.parse.parse_qs(urllib.parse.urlparse(f["file"]).query).get("action", [""])[0] for f in root]
    if actions != ROOT_ACTIONS:
        failures.append(f"root actions {actions}, expected {ROOT_ACTIONS}")

    recent = listdir(args.host, PLUGIN + "?action=list")
    videos = [f for f in recent if f["filetype"] == "file"]
    print(f"recent: {len(videos)} recordings")
    for f in videos[:5]:
        print(f"  {f['label'][:40]:40}  {', '.join(f.get('genre') or [])[:24]:24}  "
              f"{(f.get('studio') or [''])[0][:14]:14} {f.get('runtime', 0) // 60:4} min")
    def check_recent_gui():
        listed = listdir(args.host, PLUGIN + "?action=list")
        gui = gui_labels(args.host, PLUGIN + "?action=list")
        marked = [f["label"] for f in listed if "[Partial]" in f["label"] or "●" in f["label"]]
        print(f"gui: {len(gui)} labels shown, {len(marked)} with [Partial]/● markers")
        return [f"GUI does not show the label {f['label']!r} (shows the title instead?)"
                for f in listed if f["label"] not in gui]

    failures += retry_once("recent gui", check_recent_gui)

    def check_recent():
        records = epgs_records(epgs)
        listed = listdir(args.host, PLUGIN + "?action=list") + listdir(args.host, PLUGIN + "?action=recording")
        meta_failures, checked = check_metadata(listed, records)
        kinds = {}
        for f in listed:
            kinds[f.get("type")] = kinds.get(f.get("type"), 0) + 1
        print(f"metadata: {checked} recordings checked against epgmeta.metadata() ({kinds}), "
              f"{len(meta_failures)} differ")
        return meta_failures

    failures += retry_once("recent/recording metadata", check_recent)
    if not videos:
        failures.append("no recordings listed")
    elif not any(f.get("plot") for f in videos):
        failures.append("no recording has a plot")

    failures += retry_once("details", lambda: check_details(args.host, epgs))  # full detail view (002)
    failures += retry_once("art", lambda: check_art(args.host, epgs))  # artwork (003)
    failures += retry_once("movies", lambda: check_movies(args.host, epgs))
    failures += retry_once("tvshows", lambda: check_shows(args.host, epgs))
    for action in ("movies", "tvshows"):
        failures += retry_once(f"gui {action}", lambda action=action: check_gui(args.host, f"{PLUGIN}?action={action}"))
    shows = listdir(args.host, PLUGIN + "?action=tvshows")
    if shows:  # the show with the most episodes: its folder labels and window header (the show title)
        show = max(shows, key=lambda f: int(f["label"].rsplit("(", 1)[1].rstrip(")")))
        failures += retry_once("gui show folder", lambda: check_gui(args.host, show["file"], show["title"]))
    failures += retry_once("genres", lambda: check_genres(args.host, epgs))  # subgenres (009)

    browse = listdir(args.host, PLUGIN + "?action=browse")
    print(f"browse: {[f['label'] for f in browse]}")
    if len(browse) != 3:
        failures.append(f"browse has {len(browse)} items, expected 3")
    for f in browse:
        action = urllib.parse.parse_qs(urllib.parse.urlparse(f["file"]).query)["action"][0]
        sub = listdir(args.host, f["file"])
        print(f"{action}: {len(sub)} folders")
        if not sub:
            failures.append(f"{action} has no folders")
            continue
        if action == "rules":
            empty = [x["label"] for x in sub if x["label"].endswith("(0)")]
            if empty:
                failures.append(f"rules with no recordings listed: {empty}")
        inner = listdir(args.host, sub[0]["file"])
        if not inner:
            failures.append(f"first {action} folder is empty")

    # live TV (012): listings only, never a stream; EPGStation must have no stream before and after
    failures += streams_failures(epgs, "before the live checks")
    failures += retry_once("live", lambda: check_live(args.host, epgs))
    failures += retry_once("gui live", lambda: check_live_gui(args.host, epgs))
    failures += retry_once("guide", lambda: check_guide(args.host, epgs))  # program guide (013)
    failures += retry_once("gui guide", lambda: check_gui(args.host, PLUGIN + "?action=guide_genres",
                                                          GUIDE_HEADERS["genres"]))
    clear_guide_cache(args.host)  # leave no guide cache behind
    failures += retry_once("reserves", lambda: check_reserves(args.host, epgs))  # reservations (007)
    failures += retry_once("gui reserves", lambda: check_gui(args.host, PLUGIN + "?action=reserves",
                                                             RESERVE_HEADERS[None]))
    failures += streams_failures(epgs, "after the live checks")

    print(f"reserves/cnts at end: {epgs_get(epgs, '/reserves/cnts')} (start {cnts_start}; print only)")
    failures += write_failures(args.host, started, time.time(), kodi_offset)

    if args.screenshot:
        screenshot(args.host, PLUGIN + "?action=list", args.screenshot)
    failures += streams_failures(epgs, "at the end of the smoke")  # transcoded playback (011)

    for f in failures:
        print("FAIL:", f, file=sys.stderr)
    print("SMOKE OK" if not failures else "SMOKE FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
