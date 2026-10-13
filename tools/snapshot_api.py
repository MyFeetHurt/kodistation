#!/usr/bin/env python3
"""Capture EPGStation API responses as JSON fixtures.

READ-ONLY by construction: this tool can only issue HTTP GET requests.

Free-channel test data only (backlog 020): fixtures in the repository come from the dev EPGStation's free channels.
    --dev           required to write into the repository work tree; refused when the base URL is the production
                    one (--prod-url, default $EPGS_PROD_URL, compared by URL and by host:port). With --dev every
                    capture keeps only free channels (is_free_channel: GR, or BS in FREE_BS_SERVICES; never CS,
                    never BS10プレミアム): channels.json, recorded/recording/reserves (other items are skipped and
                    counted), schedules, live, guide, broadcasting, schedule texts and drop logs.
    without --dev   a private capture (e.g. `make snapshot-prod`, GET only): out must be outside every git work
                    tree, written 0700/0600; nothing is filtered. --fingerprints FILE (refused inside a work tree)
                    then also writes the private deny list for the public export audit (019): prod programIds,
                    storage totals, rule directories and the title/people tokens of the captured texts, except the
                    ones a frozen allow list names (tools/fingerprint_allow.txt: generic words, committed;
                    --allow-private: reviewed public names, private); every exclusion is printed with its reason.
                    --openapi PATH is the one write of a private capture allowed into the tree (generic spec).
    --prune-free DIR      apply the free-channel rule to an existing fixture set or live directory (no request)
    --check-fingerprints FILE DIR   report every fingerprint found in DIR's files (exit 1 on a hit; no request)

    tools/snapshot_api.py --dev http://epgs-dev.local:8888 tests/fixtures/dev --only-ids-of tests/fixtures/dev \
        --exclude-ids-of tests/fixtures/dev2   (refresh in place; files of recordings no longer in the set go)
    tools/snapshot_api.py --dev http://epgs-dev.local:8888 tests/fixtures/dev2 --exclude-ids-of tests/fixtures/dev \
        --newest 100 --reserves-too
    tools/snapshot_api.py http://epgstation.local:8888 ~/.cache/epgstation-kodi/prod-snapshot --fingerprints FILE

Output layout (paths mirror the API so tools/fake_epgstation.py can serve them):
    <out>/api/version.json
    <out>/api/recorded.json               (all records, isHalfWidth=false, like the addon asks)
    <out>/api/recorded/<id>.json          (isHalfWidth=false)
    <out>/api/recording.json              (isHalfWidth=false)
    <out>/api/videos/<id>/duration.json   (real file length, one per videoFile)
    <out>/api/thumbnails/<id>.jpg         (first N only, to keep the repo small)
    <out>/api/dropLogs/<id>.txt           (backlog 002/020: /dropLogs/<id>?maxsize=512 of up to DROPLOGS recordings:
                                          ones with a count > 0 first (drops/scrambling before errors), else ones
                                          with any dropLogFile; free channels only with --dev)
    ...
    <out>/halfwidth.json                  ({id: {name, description, extended, rawExtended}} of the same
                                          recordings with isHalfWidth=true: proves items.to_half/raw_extended)
    <out>/schedule_texts.json             (--schedules DAYS: unique guide EPG texts, isHalfWidth=false, GR + BS)
    --reserves (reservations, backlog 007; one run, so "recording now" lines up with capturedAt):
    <out>/api/reserves.json               (/reserves?type=all&isHalfWidth=false&limit=1000 trimmed to <= RESERVES_MAX:
                                          up to 10 of each non-normal state, the ones matching a /recording item,
                                          then the earliest normal ones; extended cut except on the first 3;
                                          compact, one reservation per line; budget RESERVES_BUDGET bytes)
    <out>/api/reserves/cnts.json          (/reserves/cnts as captured)
    <out>/api/recording.json              (/recording?isHalfWidth=false, same run)
    <out>/reserves_meta.json              ({"capturedAt": ms}; tests fix the plugin clock to it)
    <out>/schedule_details.json           (--schedule-details ID...: {id: {name, extended (isHalfWidth=false),
                                          rawExtended (isHalfWidth=true)}} of these guide programs; added to the file)
    --live (live TV, backlog 012; out is tests/fixtures/live, one copy for every fixture set since dev and
    dev2 come from the same server; never requests a stream endpoint):
    <out>/schedules.json                  (now..now+6h, GR/BS, CS without --dev, isHalfWidth=false, the first
                                          LIVE_PROGRAMS programs per channel: now and next)
    <out>/broadcasting.json               (/schedules/broadcasting reduced to channel and program ids)
    <out>/live_meta.json                  ({"capturedAt": ms}; the fake server shifts schedule times by
                                          now - capturedAt)
    <out>/guide.json                      (program guide, backlog 013; same run and capturedAt: every GR TV
                                          channel's programs starting within GUIDE_HOURS h, plus NHK総合1
                                          days=8 from local midnight, the first GUIDE_DAY_PROGRAMS per day; slim
                                          programs, compact JSON; the fake server merges it into schedules.json)

The addon and the parser (epgmeta.py) read the full-width texts, like EPGStation passes them to encode
commands; items.to_half turns them into what isHalfWidth=true would show. With isHalfWidth=false EPGStation
leaves out rawExtended, so the full-width fixtures have none (tools/fake_epgstation.py adds it back for
isHalfWidth=true requests).

    tools/snapshot_api.py --dev http://epgs-dev.local:8888 tests/fixtures/dev --schedules-only 4
    tools/snapshot_api.py --dev http://epgs-dev.local:8888 tests/fixtures/live --live
    tools/snapshot_api.py --dev http://epgs-dev.local:8888 tests/fixtures/dev2 --reserves
"""
import argparse
import json
import os
import socket
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin.video.epgstation"))
from resources.lib.epgstation import epgmeta  # noqa: E402  fingerprints (020)
from resources.lib.epgstation import reserves as reserves_mod  # noqa: E402  reservations (007): match_recording

sys.path.insert(0, os.path.join(ROOT, "tools"))
import local_env  # noqa: E402  (019: $EPGS_PROD_URL from local.env)

# Free channels (backlog 020, user decision U5): every GR service, and these BS services (NHK BS, BS日テレ, BS朝日,
# BS-TBS, BSテレ東, BSフジ, BS10, BS11, BS12, 放送大学, J:COM BS, BSよしもと; every program on them isFree over 24 h).
# CS is never free (also the shopping channels); BS10プレミアム (201, data 800) is paid although its EPG says isFree.
FREE_BS_SERVICES = frozenset([101, 102, 141, 142, 143, 151, 152, 153, 161, 162, 163, 171, 172, 173, 181, 182, 183,
                              200, 211, 222, 231, 232, 260, 265])


def is_free_channel(channel):
    """True for a channel (an /api/channels item or a schedule's channel) the fixtures may contain."""
    kind = channel.get("channelType")
    return kind == "GR" or (kind == "BS" and channel.get("serviceId") in FREE_BS_SERVICES)


def free_channel_ids(channels):
    return {c["id"] for c in channels if is_free_channel(c)}


class RefusedError(Exception):
    """A capture the free-test-data rules forbid (backlog 020); raised before any request."""


def _inside_git_tree(path):
    """True when path (existing or not) is inside a git work tree (a directory or file `.git` up the chain)."""
    d = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


def _host(url):
    return (urllib.parse.urlsplit(url if "//" in url else "http://" + url).hostname or "").lower()


def _addresses(host):
    """Every IP address host resolves to (the host itself when it does not resolve)."""
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, None)} | {host}
    except OSError:
        return {host}


def check_capture(base, out, dev, prod_url):
    """Raise RefusedError unless this capture is allowed: writing into a git work tree (the repository) needs --dev,
    and --dev never talks to the production host (prod_url: same host name or any shared address, whatever the
    port or alias)."""
    root = os.path.realpath(ROOT)
    target = os.path.realpath(os.path.abspath(os.path.expanduser(out)))
    in_tree = target == root or target.startswith(root + os.sep) or _inside_git_tree(target)
    if not dev:
        if in_tree:
            raise RefusedError(f"{out} is inside a git work tree: only --dev captures of free channels may go there "
                          "(private captures belong outside, e.g. ~/.cache/epgstation-kodi/prod-snapshot)")
        return
    if not prod_url:
        raise RefusedError("--dev needs --prod-url or $EPGS_PROD_URL, so it can refuse the production EPGStation")
    if _host(base) == _host(prod_url) or _addresses(_host(base)) & _addresses(_host(prod_url)):
        raise RefusedError(f"--dev refuses {base}: that is the production host")


def check_private_path(path, what):
    """Raise RefusedError when a private output (fingerprints) would land inside a git work tree."""
    target = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    root = os.path.realpath(ROOT)
    if target == root or target.startswith(root + os.sep) or _inside_git_tree(os.path.dirname(target)):
        raise RefusedError(f"{what} {path} is inside a git work tree: it is private")

LIST_ENDPOINTS = [
    ("/api/version", {}),
    ("/api/config", {}),
    ("/api/channels", {}),
    ("/api/encode", {"isHalfWidth": "true"}),
    ("/api/rules", {"type": "all", "limit": 1000}),
    ("/api/recorded/options", {}),
    ("/api/recording", {"isHalfWidth": "false", "limit": 1000}),
]
PRIVATE_ENDPOINTS = [("/api/storages", {})]  # private captures only (020: the disk size stays out of the repository)


def get(base, path, params=None):
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def write(out, path, data):
    full = os.path.join(out, path.lstrip("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)


def write_json(out, path, obj):
    write(out, path + ".json", json.dumps(obj, ensure_ascii=False, indent=1).encode())


# EPG text fields the parser reads (kept in schedule_texts.json).
GENRE_KEYS = ("genre1", "subGenre1", "genre2", "subGenre2", "genre3", "subGenre3")
HALF_WIDTH_KEYS = ("name", "description", "extended", "rawExtended")


def text_fields(item):
    out = {k: item[k] for k in ("name", "description", "extended") if item.get(k) is not None}
    out.update({k: item[k] for k in GENRE_KEYS if item.get(k) is not None})
    return out


def all_recorded(base, half_width):
    records, offset = [], 0
    while True:
        body, _ = get(base, "/api/recorded", {"isHalfWidth": half_width, "offset": offset, "limit": 100})
        page = json.loads(body)
        records += page["records"]
        offset += 100
        if offset >= page["total"]:
            return records


def snapshot_halfwidth(base, out, ids=None):
    """halfwidth.json: {id: {name, description, extended, rawExtended}} of the recordings (ids), isHalfWidth=true."""
    texts = {str(r["id"]): {k: r[k] for k in HALF_WIDTH_KEYS if r.get(k) is not None}
             for r in all_recorded(base, "true") if ids is None or r["id"] in ids}
    with open(os.path.join(out, "halfwidth.json"), "w", encoding="utf-8") as f:
        json.dump(texts, f, ensure_ascii=False, indent=1, sort_keys=True)
    return len(texts)


def snapshot_schedules(base, out, days, free_only=False):
    """schedule_texts.json: unique {name, description, extended, genres} of the guide for the next DAYS days
    (free_only: GR and the free BS channels; the file has no channel field, so it is filtered here)."""
    now = int(time.time() * 1000)
    seen = {}
    for kind in ("GR", "BS") if free_only else ("GR", "BS", "CS"):
        params = {"startAt": now, "endAt": now + int(days * 86400000), "isHalfWidth": "false",
                  "GR": "false", "BS": "false", "CS": "false", "SKY": "false"}
        params[kind] = "true"
        body, _ = get(base, "/api/schedules", params)
        for channel in json.loads(body):
            if free_only and not is_free_channel(channel["channel"]):
                continue
            for prog in channel["programs"]:
                entry = text_fields(prog)
                seen[json.dumps(entry, ensure_ascii=False, sort_keys=True)] = entry
    entries = sorted(seen.values(), key=lambda e: json.dumps(e, ensure_ascii=False, sort_keys=True))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "schedule_texts.json"), "w", encoding="utf-8") as f:
        f.write("[\n" + ",\n".join(json.dumps(e, ensure_ascii=False, sort_keys=True) for e in entries) + "\n]\n")
    return len(entries)


def snapshot_schedule_details(base, out, ids, free_ids=None):
    """schedule_details.json: full-width extended + half-width rawExtended of these guide programs (kept, added to;
    with free_ids a program on another channel is refused)."""
    path = os.path.join(out, "schedule_details.json")
    texts = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            texts = json.load(f)
    for program_id in ids:
        full = json.loads(get(base, f"/api/schedules/detail/{program_id}", {"isHalfWidth": "false"})[0])
        if free_ids is not None and full.get("channelId") not in free_ids:
            raise RefusedError(f"program {program_id} is not on a free channel")
        half = json.loads(get(base, f"/api/schedules/detail/{program_id}", {"isHalfWidth": "true"})[0])
        texts[str(program_id)] = {"name": full["name"], "extended": full.get("extended"),
                                  "rawExtended": half.get("rawExtended")}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(texts, f, ensure_ascii=False, indent=1, sort_keys=True)
    return len(ids)


# live TV (012): programs kept per channel in schedules.json (now and next) to keep it small.
LIVE_PROGRAMS = 2
LIVE_WINDOW_MS = 6 * 3600 * 1000


def snapshot_live(base, out, free_only=False):
    """Live TV fixtures (GET only): /schedules for now..now+6h and the ids of /schedules/broadcasting."""
    now = int(time.time() * 1000)
    params = {"startAt": now, "endAt": now + LIVE_WINDOW_MS, "isHalfWidth": "false",
              "GR": "true", "BS": "true", "CS": "false" if free_only else "true", "SKY": "false"}
    schedules = [s for s in json.loads(get(base, "/api/schedules", params)[0])
                 if not free_only or is_free_channel(s["channel"])]
    for channel in schedules:
        channel["programs"] = channel["programs"][:LIVE_PROGRAMS]
    keep = {s["channel"]["id"] for s in schedules}
    broadcasting = [{"channel": {"id": s["channel"]["id"]}, "programs": [{"id": p["id"]} for p in s["programs"]]}
                    for s in json.loads(get(base, "/api/schedules/broadcasting", {"isHalfWidth": "false"})[0])
                    if not free_only or s["channel"]["id"] in keep]
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "schedules.json"), "w", encoding="utf-8") as f:
        # one channel per line (013: keeps tests/fixtures/live within its 330 KB budget)
        f.write("[\n" + ",\n".join(json.dumps(s, ensure_ascii=False, separators=(",", ":")) for s in schedules)
                + "\n]\n")
    with open(os.path.join(out, "broadcasting.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(broadcasting, separators=(",", ":")) + "\n")
    with open(os.path.join(out, "live_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"capturedAt": now}, f, indent=1)
    snapshot_guide(base, out, now, free_only)  # program guide (013)
    return len(schedules), len(broadcasting)


# program guide (013): guide.json limits (budget <= 100 KB)
GUIDE_HOURS = 12
GUIDE_CHANNEL = 3211240960  # NHK総合1・大阪
GUIDE_DAYS = 8
GUIDE_DAY_PROGRAMS = 6
GUIDE_EXTENDED = 2  # programs per channel that keep `extended` (epgmeta's cast path)
GUIDE_DESCRIPTION = 24
GUIDE_EXTENDED_CHARS = 300  # the credits come first
GUIDE_KEYS = ("id", "startAt", "endAt", "isFree", "name", "description", "extended") + GENRE_KEYS
TV_TYPES = {0x01, 0xA1, 0xA5, 0xAD}  # live.LIVE_SERVICE_TYPES


def _slim(program, keep_extended):
    out = {k: program[k] for k in GUIDE_KEYS if program.get(k) is not None}
    if out.get("description"):
        out["description"] = out["description"][:GUIDE_DESCRIPTION]
    if keep_extended and out.get("extended"):
        out["extended"] = out["extended"][:GUIDE_EXTENDED_CHARS]
    else:
        out.pop("extended", None)
    return out


def snapshot_guide(base, out, now, free_only=False):
    """guide.json (GET only, no stream endpoint): [{channel, programs}] to merge into schedules.json by channel.

    Programs are slim: no channelId (the fake server adds it from the channel), description cut to
    GUIDE_DESCRIPTION chars, extended (cut to GUIDE_EXTENDED_CHARS) only on the first GUIDE_EXTENDED per channel."""
    params = {"startAt": now, "endAt": now + GUIDE_HOURS * 3600 * 1000, "isHalfWidth": "false",
              "GR": "true", "BS": "false", "CS": "false", "SKY": "false"}
    guide = {}
    for sched in json.loads(get(base, "/api/schedules", params)[0]):
        channel = sched["channel"]
        if channel.get("type") is not None and channel["type"] not in TV_TYPES:
            continue
        if free_only and not is_free_channel(channel):
            continue
        progs = [p for p in sched["programs"] if p["startAt"] < params["endAt"]]
        guide[channel["id"]] = {"channel": channel,
                                "programs": [_slim(p, i < GUIDE_EXTENDED) for i, p in enumerate(progs)]}
    lt = time.localtime(now / 1000)
    midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) * 1000)
    days = json.loads(get(base, f"/api/schedules/{GUIDE_CHANNEL}",
                          {"startAt": midnight, "days": GUIDE_DAYS, "isHalfWidth": "false"})[0])
    if days:
        entry = guide.setdefault(GUIDE_CHANNEL, {"channel": days[0]["channel"], "programs": []})
        seen = {p["id"] for p in entry["programs"]}
        for day in days:
            for p in day["programs"][:GUIDE_DAY_PROGRAMS]:
                if p["id"] not in seen:
                    seen.add(p["id"])
                    entry["programs"].append(_slim(p, False))
        entry["programs"].sort(key=lambda p: p["startAt"])
    with open(os.path.join(out, "guide.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(list(guide.values()), ensure_ascii=False, separators=(",", ":")) + "\n")


# reservations (007): reserves.json limits
RESERVES_MAX = 40
RESERVES_PER_STATE = 10
RESERVES_FULL = 3  # reservations that keep their whole `extended` (epgmeta's cast path)
RESERVES_EXTENDED_CHARS = 300
RESERVES_BUDGET = 60_000


def _reserves_body(chosen):
    return ('{"reserves": [\n' + ",\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in chosen)
            + f'\n], "total": {len(chosen)}}}\n').encode()


def trim_reserves(all_reserves, recording):
    """<= RESERVES_MAX reservations (startAt order) for the fixture: up to RESERVES_PER_STATE per non-normal state,
    every one matching a /recording item, then the earliest normal ones; extended cut to RESERVES_EXTENDED_CHARS
    except on the first RESERVES_FULL, rawExtended dropped (the fake rebuilds it); normal ones are dropped from
    the end until the file fits RESERVES_BUDGET."""
    keep = {}
    for state in reserves_mod.STATES:
        for r in [r for r in all_reserves if reserves_mod.classify(r) == state][:RESERVES_PER_STATE]:
            keep[r["id"]] = r
    for rec in recording:
        r = reserves_mod.match_recording(all_reserves, rec)
        if r is not None:
            keep[r["id"]] = r
    must = set(keep)
    for r in all_reserves:
        if len(keep) >= RESERVES_MAX:
            break
        keep.setdefault(r["id"], r)
    chosen = sorted(keep.values(), key=lambda r: (r["startAt"], r["id"]))
    out = []
    for i, r in enumerate(chosen):
        r = {k: v for k, v in r.items() if k != "rawExtended"}
        if i >= RESERVES_FULL and r.get("extended"):
            r["extended"] = r["extended"][:RESERVES_EXTENDED_CHARS]
        out.append(r)
    while len(_reserves_body(out)) > RESERVES_BUDGET:
        drop = next((i for i in range(len(out) - 1, -1, -1) if out[i]["id"] not in must), None)
        if drop is None:
            break
        out.pop(drop)
    return out


def snapshot_reserves(base, out, free_ids=None):
    """Reservation fixtures (GET only): reserves.json, reserves/cnts.json, recording.json, reserves_meta.json.
    free_ids: keep only reservations and recordings on these channels (cnts is then counted from what is kept)."""
    now = int(time.time() * 1000)
    all_reserves, offset = [], 0
    while True:
        page = json.loads(get(base, "/api/reserves", {"type": "all", "isHalfWidth": "false", "offset": offset,
                                                      "limit": reserves_mod.PAGE})[0])
        all_reserves += page["reserves"]
        offset += reserves_mod.PAGE
        if offset >= page["total"]:
            break
    cnts = json.loads(get(base, "/api/reserves/cnts")[0])
    recording = json.loads(get(base, "/api/recording", {"isHalfWidth": "false", "limit": 1000})[0])
    skipped = 0
    if free_ids is not None:
        n = len(all_reserves) + len(recording["records"])
        all_reserves = [r for r in all_reserves if r["channelId"] in free_ids]
        recording = {"records": [r for r in recording["records"] if r["channelId"] in free_ids]}
        recording["total"] = len(recording["records"])
        skipped = n - len(all_reserves) - recording["total"]
        cnts = reserve_cnts(all_reserves)
    chosen = trim_reserves(all_reserves, recording["records"])
    if free_ids is None:  # a private capture keeps the whole list for the fingerprints (020)
        write(out, "/reserves_all.json", _reserves_body(all_reserves))
    write(out, "/api/reserves.json", _reserves_body(chosen))
    write_json(out, "/api/reserves/cnts", cnts)
    write_json(out, "/api/recording", recording)
    with open(os.path.join(out, "reserves_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"capturedAt": now}, f, indent=1)
    return len(all_reserves), len(chosen), len(recording["records"]), skipped


def reserve_cnts(all_reserves):
    """/reserves/cnts of these reservations (EPGStation's getCnts: conflict > skip > overlap > normal)."""
    cnts = {"normal": 0, "conflicts": 0, "skips": 0, "overlaps": 0}
    for r in all_reserves:
        if r.get("isConflict"):
            cnts["conflicts"] += 1
        elif r.get("isSkip"):
            cnts["skips"] += 1
        elif r.get("isOverlap"):
            cnts["overlaps"] += 1
        else:
            cnts["normal"] += 1
    return cnts


# drop logs (002, 020): each is a few KB
DROPLOGS = 3
DROPLOG_MAX_KB = 512


def pick_droplogs(records, free_ids=None, n=DROPLOGS):
    """dropLogFile ids of up to n recordings (on free_ids channels when given): ones with a count > 0 first (the ones
    the addon offers ドロップログ for; drops or scrambling before errors only), then any with a dropLogFile;
    newest first within each (records come newest first)."""
    logs = [r for r in records if isinstance(r.get("dropLogFile"), dict) and r["dropLogFile"].get("id") is not None
            and (free_ids is None or r["channelId"] in free_ids)]

    def rank(r):
        d = r["dropLogFile"]
        return (0 if d.get("dropCnt") or d.get("scramblingCnt") else 1 if d.get("errorCnt") else 2)
    logs.sort(key=rank)  # stable: keeps the newest-first order
    return [r["dropLogFile"]["id"] for r in logs[:n]]


def snapshot_droplogs(base, out, records, free_ids=None):
    """api/dropLogs/<id>.txt (GET only) for pick_droplogs(records); a log that fails (404, 416) is skipped."""
    saved = 0
    for log_id in pick_droplogs(records, free_ids):
        try:
            data, _ = get(base, f"/api/dropLogs/{log_id}", {"maxsize": DROPLOG_MAX_KB})
        except Exception as e:  # noqa: BLE001 - optional, keep going
            print(f"skip drop log {log_id}: {e}", file=sys.stderr)
            continue
        write(out, f"/api/dropLogs/{log_id}.txt", data)
        saved += 1
    return saved


def recorded_options(records):
    """/recorded/options of these records (EPGStation: count per channelId, and per genre1 when set)."""
    channels, genres = {}, {}
    for r in records:
        channels[r["channelId"]] = channels.get(r["channelId"], 0) + 1
        if r.get("genre1") is not None:
            genres[r["genre1"]] = genres.get(r["genre1"], 0) + 1
    return {"channels": [{"cnt": channels[c], "channelId": c} for c in sorted(channels)],
            "genres": [{"cnt": genres[g], "genre": g} for g in sorted(genres)]}


# --prune-free (020): apply the free-channel rule to fixtures captured before it existed (no request)
def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _dump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def _rewrite_lines(path, items, compact_ascii=False):
    """A one-item-per-line JSON list like snapshot_live writes it."""
    with open(path, "w", encoding="utf-8") as f:
        if compact_ascii:
            f.write(json.dumps(items, separators=(",", ":")) + "\n")
        else:
            f.write("[\n" + ",\n".join(json.dumps(s, ensure_ascii=False, separators=(",", ":")) for s in items)
                    + "\n]\n")


def prune_free(d):
    """Drop everything not on a free channel from fixture directory d; returns a summary dict.

    A fixture set (d/api/channels.json): recordings on other channels with their recorded/<id>.json, durations,
    thumbnails, drop logs and halfwidth.json entries; recorded/options counts; recording.json and reserves.json
    items; channels.json; storages.json is deleted. A live directory (d/schedules.json): schedules.json,
    guide.json, broadcasting.json lose the other channels."""
    summary = {}
    api = os.path.join(d, "api")
    if os.path.isfile(os.path.join(api, "channels.json")):
        channels = _load(os.path.join(api, "channels.json"))
        free = free_channel_ids(channels)
        _dump(os.path.join(api, "channels.json"), [c for c in channels if c["id"] in free])
        summary["channels"] = len(channels) - len(free)
        recorded = _load(os.path.join(api, "recorded.json"))
        gone = [r for r in recorded["records"] if r["channelId"] not in free]
        if gone:
            recorded["records"] = [r for r in recorded["records"] if r["channelId"] in free]
            recorded["total"] = len(recorded["records"])
            _dump(os.path.join(api, "recorded.json"), recorded)
        for r in gone:
            for path in [f"recorded/{r['id']}.json"] + [f"videos/{v['id']}/duration.json"
                                                        for v in r.get("videoFiles") or []] + \
                    [f"thumbnails/{t}.jpg" for t in r.get("thumbnails") or []] + \
                    ([f"dropLogs/{r['dropLogFile']['id']}.txt"] if isinstance(r.get("dropLogFile"), dict) else []):
                full = os.path.join(api, path)
                if os.path.isfile(full):
                    os.remove(full)
                    if path.startswith("videos/") and not os.listdir(os.path.dirname(full)):
                        os.rmdir(os.path.dirname(full))
        summary["recorded"] = len(gone)
        options = os.path.join(api, "recorded", "options.json")
        if gone and os.path.isfile(options):
            opts = _load(options)
            for r in gone:
                for key, field, value in (("channels", "channelId", r["channelId"]),
                                          ("genres", "genre", r.get("genre1"))):
                    for o in opts.get(key, []):
                        if value is not None and o[field] == value:
                            o["cnt"] -= 1
            for key in ("channels", "genres"):
                opts[key] = [o for o in opts.get(key, []) if o["cnt"] > 0]
            _dump(options, opts)
        half = os.path.join(d, "halfwidth.json")
        if gone and os.path.isfile(half):
            texts = _load(half)
            for r in gone:
                texts.pop(str(r["id"]), None)
            with open(half, "w", encoding="utf-8") as f:
                json.dump(texts, f, ensure_ascii=False, indent=1, sort_keys=True)
        rec_path = os.path.join(api, "recording.json")
        if os.path.isfile(rec_path):
            recording = _load(rec_path)
            keep = [r for r in recording["records"] if r["channelId"] in free]
            if len(keep) != len(recording["records"]):
                _dump(rec_path, {"records": keep, "total": len(keep)})
            summary["recording"] = len(recording["records"]) - len(keep)
        res_path = os.path.join(api, "reserves.json")
        if os.path.isfile(res_path):
            reserves = _load(res_path)["reserves"]
            keep = [r for r in reserves if r["channelId"] in free]
            if len(keep) != len(reserves):
                write(d, "/api/reserves.json", _reserves_body(keep))
                cnts_path = os.path.join(api, "reserves", "cnts.json")
                if os.path.isfile(cnts_path):  # server totals: take the dropped ones off
                    cnts = _load(cnts_path)
                    for key, n in reserve_cnts([r for r in reserves if r["channelId"] not in free]).items():
                        cnts[key] = max(0, cnts.get(key, 0) - n)
                    write_json(d, "/api/reserves/cnts", cnts)
            summary["reserves"] = len(reserves) - len(keep)
        storages = os.path.join(api, "storages.json")
        if os.path.isfile(storages):
            os.remove(storages)
            summary["storages"] = 1
    sched_path = os.path.join(d, "schedules.json")
    if os.path.isfile(sched_path):
        schedules = _load(sched_path)
        keep = [s for s in schedules if is_free_channel(s["channel"])]
        free = {s["channel"]["id"] for s in keep}
        _rewrite_lines(sched_path, keep)
        summary["schedules"] = len(schedules) - len(keep)
        guide_path = os.path.join(d, "guide.json")
        if os.path.isfile(guide_path):
            guide = _load(guide_path)
            kept = [g for g in guide if is_free_channel(g["channel"])]
            if len(kept) != len(guide):
                with open(guide_path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(kept, ensure_ascii=False, separators=(",", ":")) + "\n")
            summary["guide"] = len(guide) - len(kept)
        b_path = os.path.join(d, "broadcasting.json")
        if os.path.isfile(b_path):
            broadcasting = _load(b_path)
            kept = [b for b in broadcasting if b["channel"]["id"] in free]
            _rewrite_lines(b_path, kept, compact_ascii=True)
            summary["broadcasting"] = len(broadcasting) - len(kept)
    return summary


# fingerprints (020): private deny list for the public export audit (019's public_audit.py)
FINGERPRINT_MIN_CHARS = 3
# Frozen, reviewed allow lists ("token<TAB># reason" per line). A token of the private capture becomes a fingerprint
# unless one of them names it, so a leak that already sits in the fixtures is still caught. The committed one holds
# only generic words (shared programme names, numbers); the private one (never committed: it would tell which free
# programmes and people the user's production recordings share) holds reviewed public names that also occur in the
# free fixtures. build_fingerprints reports every exclusion with its reason.
FINGERPRINT_ALLOW = os.path.join(ROOT, "tools", "fingerprint_allow.txt")
FINGERPRINT_ALLOW_PRIVATE = os.path.expanduser("~/.config/epgstation-kodi/fingerprint_allow_private.txt")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
FIXTURE_DIRS = ("dev", "dev2", "live", "synthetic")  # the repository's free fixture sets (test_fixture_policy)


def _nfkc(text):
    return unicodedata.normalize("NFKC", text)


def _text_tokens(item):
    """Distinctive tokens of one EPG text: the parsed show / subtitle and every credited name (NFKC)."""
    meta = epgmeta.metadata(item) or {}
    tokens = [meta.get("show"), meta.get("subtitle")]
    for value in (meta.get("cast") or []) + (meta.get("directors") or []) + (meta.get("writers") or []):
        tokens.append(value[0] if isinstance(value, (list, tuple)) else value)
    return [_nfkc(t).strip() for t in tokens if isinstance(t, str) and t.strip()]


def _fixture_text(fixtures):
    parts = []
    for name in FIXTURE_DIRS:
        for dirpath, _, files in os.walk(os.path.join(fixtures, name)):
            for fn in files:
                if fn.endswith((".json", ".txt")):
                    with open(os.path.join(dirpath, fn), encoding="utf-8", errors="replace") as f:
                        parts.append(_nfkc(f.read()))
    return "\n".join(parts)


def load_allow(path):
    """{token: reason} of an allow list ("token<TAB># reason"; blank lines and lines starting with # skipped)."""
    out = {}
    if path and os.path.isfile(os.path.expanduser(path)):
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip() or line.startswith("#"):
                    continue
                token, _, reason = line.partition("\t")
                out[_nfkc(token.strip())] = reason.lstrip("# ").strip() or "no reason given"
    return out


def build_fingerprints(snapshot, allow_files=(FINGERPRINT_ALLOW, FINGERPRINT_ALLOW_PRIVATE), fixtures=FIXTURES):
    """(sorted fingerprints, {excluded token: reason}, [tokens in the free fixtures that no allow list names]) of a
    private capture: programIds of recordings / in-progress recordings / reservations, storage totals, rule
    directories, and the title / people tokens of their EPG texts, except the ones an allow list names."""
    def load(rel, default):
        path = os.path.join(snapshot, rel)
        return _load(path) if os.path.isfile(path) else default
    items = load("api/recorded.json", {}).get("records", []) + load("api/recording.json", {}).get("records", []) + \
        (load("reserves_all.json", {}) or load("api/reserves.json", {})).get("reserves", [])
    out = {str(i["programId"]) for i in items if i.get("programId")}
    out |= {str(s["total"]) for s in load("api/storages.json", {}).get("items", []) if s.get("total")}
    for rule in load("api/rules.json", {}).get("rules", []):
        directory = ((rule.get("saveOption") or {}).get("directory") or "").strip().strip("/")
        if directory:
            out.add(_nfkc(os.path.basename(directory)))
    allow = {}
    for path in allow_files:
        for token, reason in load_allow(path).items():
            allow.setdefault(token, f"{reason} ({os.path.basename(path)})")
    excluded = {}
    for item in items:
        for token in _text_tokens(item):
            if len(token) < FINGERPRINT_MIN_CHARS:
                excluded[token] = f"shorter than {FINGERPRINT_MIN_CHARS} characters"
            elif token in allow:
                excluded[token] = allow[token]
            else:
                out.add(token)
    free_text = _fixture_text(fixtures)
    in_fixtures = sorted(t for t in out if t in free_text)
    return sorted(out), excluded, in_fixtures


def write_fingerprints(path, fingerprints):
    """The private fingerprint file, created 0600 (never world-readable, not even for a moment)."""
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# private: production fingerprints for the public export audit (never commit)\n")
        f.write("\n".join(fingerprints) + "\n")


def check_fingerprints(fp_file, d):
    """[(relative path, fingerprint)] for every fingerprint found in a file under d (raw or NFKC text)."""
    with open(os.path.expanduser(fp_file), encoding="utf-8") as f:
        prints = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    hits = []
    for dirpath, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x != ".git"]
        for fn in files:
            path = os.path.join(dirpath, fn)
            with open(path, "rb") as f:
                raw = f.read().decode("utf-8", errors="ignore")
            text = raw + "\n" + _nfkc(raw)
            hits += [(os.path.relpath(path, d), p) for p in prints if p in text]
    return hits


def capture(args, free_ids):
    """The whole fixture set (recordings, lists, durations, thumbnails, drop logs, half-width texts)."""
    endpoints = LIST_ENDPOINTS + ([] if args.dev else PRIVATE_ENDPOINTS)
    for path, params in endpoints:
        try:
            body, _ = get(args.base, path, params)
            data = json.loads(body)
            if free_ids is not None and path == "/api/channels":
                data = [c for c in data if c["id"] in free_ids]
            if free_ids is not None and path == "/api/recording":
                data["records"] = [r for r in data["records"] if r["channelId"] in free_ids]
                data["total"] = len(data["records"])
            write_json(args.out, path, data)
        except Exception as e:  # noqa: BLE001 - keep going, report at the end
            print(f"skip {path}: {e}", file=sys.stderr)

    records = all_recorded(args.base, "false")
    n_all = len(records)
    if free_ids is not None:
        records = [r for r in records if r["channelId"] in free_ids]
    skipped = n_all - len(records)
    subset = free_ids is not None  # the server's counts include paid recordings, also in-progress ones
    if args.exclude_ids_of:
        exclude = {r["id"] for r in _load(os.path.join(args.exclude_ids_of, "api", "recorded.json"))["records"]}
        records = [r for r in records if r["id"] not in exclude]
        subset = True
    if args.only_ids_of:  # refresh a set in place: the recordings it already has (the ids its tests pin)
        keep = {r["id"] for r in _load(os.path.join(args.only_ids_of, "api", "recorded.json"))["records"]}
        records = [r for r in records if r["id"] in keep]
        subset = True
    if args.newest:
        records = sorted(records, key=lambda r: (r["startAt"], r["id"]), reverse=True)[:args.newest]
        subset = True
    write_json(args.out, "/api/recorded", {"records": records, "total": len(records)})
    if subset:  # the server's counts would include recordings this set leaves out
        recording = _load(os.path.join(args.out, "api", "recording.json"))["records"]
        write_json(args.out, "/api/recorded/options", recorded_options(records + recording))

    thumbs = durations = 0
    for rec in records:
        body, _ = get(args.base, f"/api/recorded/{rec['id']}", {"isHalfWidth": "false"})
        detail = json.loads(body)
        write_json(args.out, f"/api/recorded/{rec['id']}", detail)
        for vf in detail.get("videoFiles") or []:
            try:
                body, _ = get(args.base, f"/api/videos/{vf['id']}/duration")
                write_json(args.out, f"/api/videos/{vf['id']}/duration", json.loads(body))
                durations += 1
            except Exception as e:  # noqa: BLE001 - optional, keep going
                print(f"skip duration of video {vf['id']}: {e}", file=sys.stderr)
        for tid in rec.get("thumbnails") or []:
            if thumbs < args.thumbs:
                data, _ = get(args.base, f"/api/thumbnails/{tid}")
                write(args.out, f"/api/thumbnails/{tid}.jpg", data)
                thumbs += 1

    logs = snapshot_droplogs(args.base, args.out, records, free_ids)  # drop logs (002, 020)
    if args.openapi:
        body, _ = get(args.base, "/api/docs")
        os.makedirs(os.path.dirname(args.openapi), exist_ok=True)
        with open(args.openapi, "w") as f:
            json.dump(json.loads(body), f, ensure_ascii=False, indent=1)

    texts = snapshot_halfwidth(args.base, args.out, {r["id"] for r in records})
    stale = remove_stale(args.out)
    if args.schedules:
        print(f"saved {snapshot_schedules(args.base, args.out, args.schedules, args.dev)} schedule texts")
    print(f"saved {len(records)} recordings ({texts} half-width texts), {durations} durations, {thumbs} thumbnails,"
          f" {logs} drop logs to {args.out}" + (f"; skipped {skipped} on other channels" if skipped else "")
          + (f"; removed {stale} stale files" if stale else ""))


def remove_stale(out):
    """Delete recorded/<id>.json, videos/<id>/, thumbnails and drop logs that belong to no recording of the set
    (recorded.json + recording.json) any more; returns how many files went."""
    api = os.path.join(out, "api")
    recs = _load(os.path.join(api, "recorded.json"))["records"]
    rec_path = os.path.join(api, "recording.json")
    recs += _load(rec_path)["records"] if os.path.isfile(rec_path) else []
    keep = {"recorded": {str(r["id"]) for r in recs} | {"options"},
            "videos": {str(v["id"]) for r in recs for v in r.get("videoFiles") or []},
            "thumbnails": {str(t) for r in recs for t in r.get("thumbnails") or []},
            "dropLogs": {str(r["dropLogFile"]["id"]) for r in recs if isinstance(r.get("dropLogFile"), dict)}}
    removed = 0
    for sub, ids in keep.items():
        d = os.path.join(api, sub)
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if os.path.splitext(name)[0] in ids:
                continue
            path = os.path.join(d, name)
            if os.path.isdir(path):
                for f in os.listdir(path):
                    os.remove(os.path.join(path, f))
                    removed += 1
                os.rmdir(path)
            else:
                os.remove(path)
                removed += 1
    return removed


def _run(args):
    """The capture itself (after check_capture)."""
    free_ids = free_channel_ids(json.loads(get(args.base, "/api/channels")[0])) if args.dev else None
    if args.reserves:
        n, kept, rec, skipped = snapshot_reserves(args.base, args.out, free_ids)
        print(f"saved reservation fixtures: {kept} of {n} reservations, {rec} recording now"
              + (f"; skipped {skipped} on other channels" if skipped else ""))
        return 0
    if args.live:
        n, on_air = snapshot_live(args.base, args.out, args.dev)
        print(f"saved live fixtures: {n} schedule channels, {on_air} on air")
        return 0
    if args.schedule_details:
        n = snapshot_schedule_details(args.base, args.out, args.schedule_details, free_ids)
        print(f"saved {n} schedule details")
        return 0
    if args.schedules_only:
        print(f"saved {snapshot_schedules(args.base, args.out, args.schedules_only, args.dev)} schedule texts")
        return 0

    capture(args, free_ids)
    if args.reserves_too:
        n, kept, rec, skipped = snapshot_reserves(args.base, args.out, free_ids)
        print(f"saved reservation fixtures: {kept} of {n} reservations, {rec} recording now"
              + (f"; skipped {skipped} on other channels" if skipped else ""))
    if args.fingerprints:
        prints, excluded, in_fixtures = build_fingerprints(args.out, (FINGERPRINT_ALLOW, args.allow_private))
        reasons = {}
        for token, reason in sorted(excluded.items()):
            if args.show_tokens:
                print(f"not a fingerprint: {token}  ({reason})")
            reasons[reason] = reasons.get(reason, 0) + 1
        if not args.show_tokens:
            for reason, n in sorted(reasons.items()):
                print(f"not a fingerprint: {n} tokens ({reason})")
        for token in in_fixtures:
            print(f"WARNING: fingerprint {token if args.show_tokens else '(hidden)'} occurs in the free fixtures: "
                  "--check-fingerprints will report it; remove it from the fixtures, or review it and add it to an "
                  "allow list (--show-tokens shows it, in a private terminal only)", file=sys.stderr)
        write_fingerprints(args.fingerprints, prints)
        print(f"wrote {len(prints)} fingerprints to {args.fingerprints} ({len(excluded)} tokens excluded)")
    return 0


def main(argv=None):
    local_env.load()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base", nargs="?", help="EPGStation base URL, e.g. http://epgs-dev.local:8888")
    ap.add_argument("out", nargs="?", help="output directory")
    ap.add_argument("--dev", action="store_true",
                    help="a free-channel capture of the dev EPGStation (needed to write into the repository)")
    ap.add_argument("--prod-url", default=os.environ.get("EPGS_PROD_URL", ""),
                    help="the production EPGStation, which --dev refuses (default $EPGS_PROD_URL)")
    ap.add_argument("--thumbs", type=int, default=10, help="number of thumbnails to save")
    ap.add_argument("--openapi", help="also save /api/docs to this path: the one write from a private capture that "
                    "may go into the tree (EPGStation's generic 2.10.0 spec, no user data)")
    ap.add_argument("--schedules", type=float, metavar="DAYS", help="also write schedule_texts.json for DAYS days")
    ap.add_argument("--schedules-only", type=float, metavar="DAYS", help="only write schedule_texts.json")
    ap.add_argument("--schedule-details", type=int, nargs="+", metavar="ID",
                    help="only add these guide programs to schedule_details.json")
    ap.add_argument("--live", action="store_true", help="only write the live TV fixtures (schedules) into out")
    ap.add_argument("--reserves", action="store_true", help="only write the reservation fixtures (backlog 007)")
    ap.add_argument("--reserves-too", action="store_true", help="the whole set and then the reservation fixtures")
    ap.add_argument("--exclude-ids-of", metavar="DIR", help="leave out the recordings of this fixture set")
    ap.add_argument("--newest", type=int, metavar="N", help="keep only the newest N recordings")
    ap.add_argument("--only-ids-of", metavar="DIR", help="keep only the recordings this fixture set already has")
    ap.add_argument("--fingerprints", metavar="FILE",
                    help="private capture: also write the fingerprint list (refused inside a git work tree)")
    ap.add_argument("--allow-private", metavar="FILE", default=FINGERPRINT_ALLOW_PRIVATE,
                    help="the private reviewed allow list (default " + FINGERPRINT_ALLOW_PRIVATE + ")")
    ap.add_argument("--prune-free", metavar="DIR", help="only apply the free-channel rule to this fixture dir")
    ap.add_argument("--check-fingerprints", nargs=2, metavar=("FILE", "DIR"), help="only scan DIR for fingerprints")
    ap.add_argument("--show-tokens", action="store_true",
                    help="print the private fingerprint tokens themselves (a private terminal only; default: counts)")
    args = ap.parse_args(argv)

    if args.check_fingerprints:
        hits = check_fingerprints(*args.check_fingerprints)
        per_file = {}
        for path, token in hits:
            if args.show_tokens:
                print(f"{path}: {token}")
            per_file[path] = per_file.get(path, 0) + 1
        if not args.show_tokens:  # 019 L1: only the place and a count, never the private token
            for path, n in sorted(per_file.items()):
                print(f"{path}: {n} fingerprint(s)")
        print(f"{len(hits)} fingerprint hits")
        return 1 if hits else 0
    if args.prune_free:
        print(f"pruned {args.prune_free}: {prune_free(args.prune_free)}")
        return 0
    if not args.base or not args.out:
        ap.error("base and out are required")
    try:
        check_capture(args.base, args.out, args.dev, args.prod_url)
        if args.fingerprints:
            check_private_path(args.fingerprints, "--fingerprints")
    except RefusedError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2

    if args.dev:
        return _run(args)
    old = os.umask(0o077)  # a private capture: directories 0700, files 0600
    try:
        os.makedirs(args.out, mode=0o700, exist_ok=True)
        os.chmod(args.out, 0o700)
        return _run(args)
    finally:
        os.umask(old)


if __name__ == "__main__":
    sys.exit(main())
