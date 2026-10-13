"""All recordings, grouped by the metadata parser, from a disk cache (stdlib only, no Kodi imports).

Movies, TV shows and the rule counts need every recording, parsed (epgmeta.metadata, ~0.33 ms each).
Two files in the addon profile hold the last full fetch:
    library-records.json  {"h": header, "records": [RecordedItem, ...], "meta": {id: epgmeta.metadata()}}
                          (records full-width, as the API sends them; meta so lists need not parse again)
    library-index.json    {"h": header, "entries": [slim(...), ...], "channels": {id: name},
                           "logos": [channel ids with hasLogoData]}  (artwork (003), FORMAT 4;
                           slim "genres": [[genre, subGenre], ...] of genre1-3, subgenres (009), FORMAT 5)
header = {"v": FORMAT, "server": url, "code": CODE_SHA1, "sig": signature, "t": unix time of the fetch,
          "live": ids in progress at the fetch}.
CODE_SHA1 hashes epgmeta.py, items.py and library.py: the parsed fields (kind, show, key, ...) depend on all
three, so any upgrade that touches them re-parses everything. Both files are written atomically, records
first and index last; a header that does not match (format, server, code, or index vs records) or an
unreadable file means no cache.

Every open probes GET /recorded?limit=1 and ?isReverse=true&limit=1 (a few ms each). The cache is used while
the signature [total, newest id, newest file count, newest thumbnail count, oldest id] matches, it is younger
than TTL and every recording that was in progress at the fetch still is (/recording); otherwise
every page is fetched again (in parallel, overall DEADLINE), and only recordings that are new or renamed
are parsed. In-progress recordings come from /recording on every open and are never cached. When the
server cannot be reached the old cache is used (stale), and with no cache the ApiError goes up.

protect / delete (006): a protect toggle keeps the signature, so plugin.do_recorded_action calls mark_changed, which
writes library-changed.json {"server", "t", "deleted": [ids], "protect": {id: bool}}. The cache header's "covered" is
the time its refresh began: a marker with t < covered is in the cache, any other one (same server) makes the cache not
warm (full refresh, parsed metadata reused) and, when a stale cache stands in, has its deletions and protect flags
applied. A save removes the marker only when the cache covers it (compare-and-delete under the marker's lock), and never
replaces a cache from a refresh that began later.
"""
import hashlib
import json
import os
import time

from . import epgmeta, items, store
from .api import ApiError
from .genres import genre_pairs
from .items import show_key, to_half

FORMAT = 5  # 4 artwork (003): "logos", slim "thumb"; 5 subgenres (009): slim "genres" = genres.genre_pairs
TTL = 900
DEADLINE = 15.0
STALE_DEADLINE = 5.0  # when an old cache can stand in, do not keep the user waiting the full DEADLINE
STALE_MAX_AGE = 3600  # ...unless it is this old: then wait the full DEADLINE so a slow server can still refresh
PAGE = 500
WORKERS = 4
INDEX_FILE = "library-index.json"
RECORDS_FILE = "library-records.json"
CHANGED_FILE = "library-changed.json"  # protect / delete (006)


def code_sha1(paths):
    """sha1 over the bytes of these files (a missing file counts as a marker, so it changes the hash too)."""
    digest = hashlib.sha1()  # noqa: S324 - a change detector, not security
    for path in paths:
        try:
            with open(path, "rb") as f:
                digest.update(f.read())
        except OSError:
            digest.update(b"\0missing:" + os.path.basename(path).encode())
    return digest.hexdigest()


CODE_FILES = (epgmeta.__file__, items.__file__, __file__, os.path.join(os.path.dirname(__file__), "genres.py"))
CODE_SHA1 = code_sha1(CODE_FILES)

_PARSED = ("kind", "show", "key", "season", "episode", "version", "year")


def _parsed(rec, meta):
    """The fields that come from the parser (reused while the recording's name is unchanged)."""
    if meta is None:
        return {"kind": "tv", "show": to_half(rec.get("name") or ""), "key": show_key(None, rec),
                "season": None, "episode": None, "version": "", "year": None}
    return {"kind": meta["kind"], "show": to_half(meta["show"]) or to_half(rec.get("name") or ""),
            "key": show_key(meta, rec), "season": meta["season"], "episode": meta["episode"],
            "version": meta["version"] or "", "year": meta["year"]}


def _rec_fields(rec):
    return {"id": rec["id"], "name": rec.get("name") or "", "startAt": rec["startAt"], "ruleId": rec.get("ruleId"),
            "channelId": rec["channelId"], "thumb": items.newest_thumb(rec), "rec": bool(rec.get("isRecording")),
            "genres": genre_pairs(rec)}  # subgenres (009)


def slim(rec, meta):
    """Index entry of one recording: what grouping, counting and sorting need, without the EPG texts."""
    out = _rec_fields(rec)
    out.update(_parsed(rec, meta))
    return {k: out[k] for k in ("id", "name", "kind", "show", "key", "season", "episode", "version", "year",
                                "startAt", "ruleId", "channelId", "thumb", "rec", "genres")}


def signature(newest, oldest=None):
    """[total, newest id, its file count, its thumbnail count, oldest id] from a /recorded response (newest first)
    and one with isReverse=true (oldest first; None: unknown). The oldest id catches a deletion of the oldest
    recording (dev guard, auto-delete) at the same time as a new one finishes, which keeps total and newest."""
    recs = newest.get("records") or []
    r = recs[0] if recs else {}
    old = (oldest or {}).get("records") or []
    return [newest.get("total", 0), r.get("id"), len(r.get("videoFiles") or []), len(r.get("thumbnails") or []),
            old[0]["id"] if old else None]


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _ints(v):
    return isinstance(v, list) and all(_is_int(x) for x in v)


def _is_sig(sig):
    return isinstance(sig, list) and len(sig) == 5


_ENTRY_INTS = ("id", "startAt", "channelId")
_ENTRY_STRS = ("name", "kind", "show", "key", "version")
_ENTRY_KEYS = ("season", "episode", "year", "ruleId", "thumb", "rec")


def _valid_pairs(v):
    """subgenres (009): [[genre, subGenre|None], ...]"""
    return isinstance(v, list) and all(isinstance(p, list) and len(p) == 2 and _is_int(p[0])
                                       and (p[1] is None or _is_int(p[1])) for p in v)


def _valid_entry(e):
    return (isinstance(e, dict) and all(_is_int(e.get(k)) for k in _ENTRY_INTS)
            and all(isinstance(e.get(k), str) for k in _ENTRY_STRS) and all(k in e for k in _ENTRY_KEYS)
            and _valid_pairs(e.get("genres")))


_META_KEYS = frozenset(("kind", "show", "episode", "episode_end", "season", "subtitle", "year", "edition", "version",
                        "tags", "cast", "directors", "writers", "plot"))


def _valid_meta(m):
    return m is None or (isinstance(m, dict) and _META_KEYS <= m.keys())


def _valid_record(r):
    """The fields items.recorded_item() indexes directly."""
    return (isinstance(r, dict) and all(_is_int(r.get(k)) for k in ("id", "startAt", "endAt", "channelId"))
            and isinstance(r.get("name"), str))


def _newest_first(entries):
    return sorted(entries, key=lambda e: (e["startAt"], e["id"]), reverse=True)


# --- protect / delete (006): changed-marker ---------------------------------------------------------------------

def _read_changed(directory):
    """The changed-marker {"server", "t", "deleted": [ids], "protect": {str id: bool}}, or None when missing or bad."""
    try:
        with open(os.path.join(directory, CHANGED_FILE), encoding="utf-8") as f:
            data = json.load(f)
        if not (isinstance(data, dict) and isinstance(data.get("server"), str)
                and isinstance(data.get("t"), (int, float)) and not isinstance(data["t"], bool)
                and _ints(data.get("deleted", [])) and isinstance(data.get("protect", {}), dict)
                and all(isinstance(v, bool) and str(k).isdigit() for k, v in data.get("protect", {}).items())):
            return None
        return {"server": data["server"], "t": data["t"], "deleted": list(data.get("deleted", [])),
                "protect": {str(k): v for k, v in data.get("protect", {}).items()}}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


LOCK_FILE = "library-changed.lock"  # protect / delete (006) review L2: serialises the marker's read-merge-write
LOCK_WAIT = 1.0  # seconds to wait for the lock, then go on without it (a plugin run must never hang on it)
LOCK_STALE = 10.0  # a lock file older than this was left by a crashed run: taken over


class _changed_lock:  # noqa: N801 - used as a context manager
    """Lock file (O_EXCL) around the changed-marker. Best effort: after LOCK_WAIT the caller goes on unlocked."""

    def __init__(self, directory):
        self.path = os.path.join(directory, LOCK_FILE)
        self.held = False

    def __enter__(self):
        end = time.monotonic() + LOCK_WAIT
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                self.held = True
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > LOCK_STALE:
                        os.remove(self.path)  # a crashed run's lock: try again at once
                        continue
                except OSError:
                    pass  # released meanwhile, or cannot be removed: wait like any held lock (review N2)
            except OSError:
                return self  # cannot create files here: unlocked (the write will fail the same way)
            if time.monotonic() >= end:  # every path that does not hold the lock reaches the deadline
                return self
            time.sleep(0.02)

    def __exit__(self, *exc):
        if self.held:
            try:
                os.remove(self.path)
            except OSError:
                pass
        return False


def mark_changed(directory, server_url, recorded_id, op, now=time.time):
    """Note a write on recording recorded_id (op protect / unprotect / delete, or None: just refresh) so the next
    library open refreshes and a stale cache never shows it as before. Merged with an existing marker of the same
    server, under the lock file (concurrent runs lose nothing); written atomically. Returns False if it could not
    write."""
    with _changed_lock(directory):
        old = _read_changed(directory)
        data = old if old is not None and old["server"] == server_url else {"server": server_url, "deleted": [],
                                                                            "protect": {}}
        rid = int(recorded_id)
        if op == "delete" and rid not in data["deleted"]:
            data["deleted"].append(rid)
        elif op in ("protect", "unprotect"):
            data["protect"][str(rid)] = op == "protect"
        data["t"] = now()
        return store.atomic_write_json(os.path.join(directory, CHANGED_FILE),
                                       {"server": data["server"], "t": data["t"], "deleted": data["deleted"],
                                        "protect": data["protect"]})


def covered(header):
    """When the refresh that wrote this cache began (0 for a cache from before "covered": no marker is in it)."""
    value = header.get("covered")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _drop_changed_before(directory, started):
    """Remove the marker if it is older than `started` (a refresh that began then covers it). Call under the lock,
    so a marker written meanwhile is never lost."""
    changed = _read_changed(directory)
    if changed is not None and changed["t"] < started:
        try:
            os.remove(os.path.join(directory, CHANGED_FILE))
        except OSError:
            pass


def _apply_changed(changed, records, meta, entries):
    """The marker's deletions and protect flags applied to a stale snapshot's parts."""
    gone = set(changed["deleted"])
    flags = {int(k): v for k, v in changed["protect"].items()}
    records = {k: (dict(r, isProtected=flags[k]) if k in flags else r) for k, r in records.items() if k not in gone}
    meta = {k: v for k, v in meta.items() if k not in gone}
    entries = [e for e in entries if e["id"] not in gone]
    return records, meta, entries


class Snapshot:
    """records: {id: RecordedItem} (finished ones only when asked for, in-progress ones always);
    entries: slim entries of all of them, newest first; channels: {channelId: name};
    stale: the server could not be asked, this is the old cache; meta: parsed metadata per record id;
    logos: channel ids with hasLogoData (artwork (003))."""

    def __init__(self, records, entries, channels, stale=False, meta=None, logos=None):
        self.records, self.entries, self.channels, self.stale = records, entries, channels, stale
        self.meta = meta or {}  # {id: epgmeta.metadata() result} for the records above (cached or parsed now)
        self.logos = set(logos or ())


class Library:
    ttl, deadline, stale_deadline, page, workers = TTL, DEADLINE, STALE_DEADLINE, PAGE, WORKERS

    def __init__(self, directory, server_url, now=time.time):
        self.dir = directory
        self.server = server_url
        self.now = now
        self.warnings = []  # why the stale cache was used (plugin.py logs them)

    # --- files -----------------------------------------------------------------------------------------------

    def _path(self, name):
        return os.path.join(self.dir, name)

    def _read(self, name, body, valid):
        """The file's data if its header matches and every item of data[body] passes valid(item), else None."""
        try:
            with open(self._path(name), encoding="utf-8") as f:
                data = json.load(f)
            h = data["h"]
            if not (isinstance(h, dict) and (h.get("v"), h.get("server"), h.get("code")) == (FORMAT, self.server,
                                                                                           CODE_SHA1)
                    and (h.get("sig") is None or _is_sig(h["sig"])) and "sig" in h
                    and isinstance(h.get("t"), (int, float)) and _ints(h.get("live"))
                    and isinstance(data[body], list) and all(valid(x) for x in data[body])):
                return None
            return data
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    def load(self):
        """(header, entries, channels, logos) of a valid index, or None."""
        data = self._read(INDEX_FILE, "entries", _valid_entry)
        if data is None:
            return None
        try:
            channels = {int(k): str(v) for k, v in (data.get("channels") or {}).items()}
        except (TypeError, ValueError, AttributeError):
            return None
        if not _ints(data.get("logos")):  # artwork (003)
            return None
        return data["h"], data["entries"], channels, set(data["logos"])

    def _read_records(self):
        data = self._read(RECORDS_FILE, "records", _valid_record)
        if data is None:
            return None
        try:
            meta = {int(k): v for k, v in data["meta"].items()}
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        if not all(_valid_meta(m) for m in meta.values()):
            return None
        return data, meta

    def load_records(self, header):
        """({id: RecordedItem}, {id: metadata}) written with this index header, or None."""
        got = self._read_records()
        if got is None or got[0]["h"] != header:
            return None
        return {r["id"]: r for r in got[0]["records"]}, got[1]

    def _changed(self):  # protect / delete (006): this server's changed-marker, or None
        changed = _read_changed(self.dir)
        return changed if changed is not None and changed["server"] == self.server else None

    def _save(self, header, records, meta, entries, channels, logos):
        """Under the changed-marker's lock (protect / delete, 006): skipped when the cache on disk comes from a refresh
        that began later (two overlapping refreshes finishing out of order, review N1); after the save the marker
        is removed if this refresh covers it."""
        with _changed_lock(self.dir):
            on_disk = self.load()
            if on_disk is not None and covered(on_disk[0]) > header["covered"]:
                return
            body = {"h": header, "records": records, "meta": {str(k): v for k, v in meta.items()}}
            if store.atomic_write_json(self._path(RECORDS_FILE), body) and store.atomic_write_json(
                    self._path(INDEX_FILE), {"h": header, "entries": entries, "channels": channels,
                                             "logos": sorted(logos)}):
                _drop_changed_before(self.dir, header["covered"])

    # --- fetching ----------------------------------------------------------------------------------------------

    @staticmethod
    def _timeout(api, end):
        return max(0.1, min(api.timeout, end - time.monotonic()))

    def _probe(self, api, end):
        newest = api.recorded(offset=0, limit=1, timeout=self._timeout(api, end))
        oldest = api.recorded(offset=0, limit=1, reverse=True, timeout=self._timeout(api, end))
        return signature(newest, oldest)

    def _fetch(self, api, sig, end):
        """All finished recordings: (records, sig to store). Probes again and retries once when the pages disagree
        with the probe; if they still do, keeps what came back and stores sig None (the next open refreshes)."""
        recs = []
        for attempt in range(2):
            if attempt:
                sig = self._probe(api, end)
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise ApiError("/recorded: deadline passed")
            pages = api.recorded_pages(sig[0], self.page, self.workers, remaining)
            seen = {}
            for p in pages:
                for r in p.get("records") or []:
                    seen.setdefault(r["id"], r)
            recs = list(seen.values())
            if (signature(pages[0])[:4] == sig[:4] and len(recs) == sig[0]
                    and (sig[4] is None or sig[4] in seen)):
                return recs, sig
        return recs, None

    def _channels(self, api, end, old, old_logos):
        """({channelId: name}, channel ids with hasLogoData); the old ones when /channels fails."""
        try:
            got = api.channels(timeout=self._timeout(api, end))
        except ApiError:
            return old, old_logos
        return ({c["id"]: c.get("halfWidthName") or c["name"] for c in got},
                {c["id"] for c in got if c.get("hasLogoData")})

    def current(self, api, on_error=None, with_records=True):
        """Snapshot of all recordings (see Snapshot). on_error(rec, exc): metadata parser failures."""
        started = self.now()  # protect / delete (006): changed-markers from here on are not covered by this refresh
        changed = self._changed()
        cache = self.load()
        short = cache is not None and 0 <= self.now() - cache[0]["t"] < STALE_MAX_AGE
        end = time.monotonic() + (min(self.deadline, self.stale_deadline) if short else self.deadline)

        def from_cache():
            got = self.load_records(cache[0]) if with_records else ({}, {})
            return None if got is None else (got[0], got[1], list(cache[1]), cache[2], cache[3])

        def stale_cache():
            got = from_cache()
            if got is None or changed is None or changed["t"] < covered(cache[0]):  # the cache has it already
                return got
            return _apply_changed(changed, got[0], got[1], got[2]) + (got[3], got[4])  # protect / delete (006)

        def stale(err, live=()):
            got = stale_cache() if cache is not None else None
            if got is None:
                raise err
            self.warnings.append(f"using the cached library: {err}")
            return self._merge(*got, live, on_error, stale=True)

        try:
            sig = self._probe(api, end)
            live = api.recording(timeout=self._timeout(api, end))["records"]
        except ApiError as e:
            return stale(e)

        # Warm while the signature matches, the cache is young, and every recording that was in progress at
        # the last refresh still is (one that finished may hide behind an unchanged signature).
        live_now = {r["id"] for r in live}
        try:
            if (cache is not None and cache[0]["sig"] == sig and 0 <= self.now() - cache[0]["t"] < self.ttl
                    and set(cache[0]["live"]) <= live_now
                    and (changed is None or changed["t"] < covered(cache[0]))):  # protect / delete (006)
                got = from_cache()
                if got is not None:
                    return self._merge(*got, live, on_error)
        except (KeyError, TypeError, ValueError, AttributeError):  # a cache load() let through: no cache
            cache = None
        try:
            fetched, stored_sig = self._fetch(api, sig, end)
        except ApiError as e:
            return stale(e, live)
        channels, logos = self._channels(api, end, cache[2] if cache else {}, cache[3] if cache else set())
        old = {e["id"]: e for e in cache[1]} if cache else {}
        old_meta = {}
        if old:  # the old records file has the full metadata of the recordings parsed before
            got = self._read_records()
            old_meta = got[1] if got else {}
        entries, meta = [], {}
        for rec in fetched:
            prev = old.get(rec["id"])
            if prev is not None and prev.get("name") == (rec.get("name") or "") and rec["id"] in old_meta:
                e = {k: prev[k] for k in _PARSED}
                e.update(_rec_fields(rec))
                entries.append(slim_order(e))
                meta[rec["id"]] = old_meta[rec["id"]]
            else:
                meta[rec["id"]] = self._parse(rec, on_error)
                entries.append(slim(rec, meta[rec["id"]]))
        entries = _newest_first(entries)
        header = {"v": FORMAT, "server": self.server, "code": CODE_SHA1, "sig": stored_sig, "t": self.now(),
                  "live": sorted(live_now), "covered": started}  # protect / delete (006): covered
        self._save(header, _newest_first_records(fetched), meta, entries, {str(k): v for k, v in channels.items()},
                   logos)
        # what the file holds (tuples became lists), so a fresh and a cached open build the same items
        meta = json.loads(json.dumps(meta))
        meta = {int(k): v for k, v in meta.items()}
        return self._merge({r["id"]: r for r in fetched}, meta, entries, channels, logos, live, on_error)

    def _merge(self, records, meta, entries, channels, logos, live, on_error, stale=False):
        """Add the in-progress recordings (they win over a finished copy of the same id)."""
        live_ids = {r["id"] for r in live}
        records = {k: v for k, v in records.items() if k not in live_ids}
        records.update({r["id"]: r for r in live})
        meta = {k: v for k, v in meta.items() if k not in live_ids}
        for r in live:
            meta[r["id"]] = self._parse(r, on_error)
        merged = [e for e in entries if e["id"] not in live_ids] + [slim(r, meta[r["id"]]) for r in live]
        return Snapshot(records, _newest_first(merged), channels, stale, meta, logos)

    @staticmethod
    def _parse(rec, on_error):
        return epgmeta.metadata(rec, on_error=(lambda e: on_error(rec, e)) if on_error else None)


def slim_order(entry):
    """entry with slim()'s key order (keeps the JSON files and comparisons tidy)."""
    return {k: entry[k] for k in ("id", "name", "kind", "show", "key", "season", "episode", "version", "year",
                                  "startAt", "ruleId", "channelId", "thumb", "rec", "genres")}


def _newest_first_records(records):
    return sorted(records, key=lambda r: (r["startAt"], r["id"]), reverse=True)
