"""All recordings, grouped by the metadata parser, from a disk cache (stdlib only, no Kodi imports).

Movies, TV shows and the rule counts need every recording, parsed (epgmeta.metadata, ~0.33 ms each).
Two files in the addon profile hold the last full fetch:
    library-records.json  {"h": header, "records": [RecordedItem, ...], "meta": {id: epgmeta.metadata()}}
                          (records full-width, as the API sends them; meta so lists need not parse again)
    library-index.json    {"h": header, "entries": [slim(...), ...], "channels": {id: name},
                           "logos": [channel ids with hasLogoData]}
                          (slim "genres": [[genre, subGenre], ...] of genre1-3)
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

A deletion changes the signature, a protect toggle does not: after any write the addon calls invalidate(), which
removes the index. The next open then fetches everything again, but parses only what the records file does not have
(same id and name).
"""
import hashlib
import json
import os
import time

from . import epgmeta, items, store
from .api import ApiError
from .genres import genre_pairs
from .items import show_key, to_half

FORMAT = 5  # 4 "logos", slim "thumb"; 5 slim "genres" = genres.genre_pairs
TTL = 900
DEADLINE = 15.0
STALE_DEADLINE = 5.0  # when an old cache can stand in, do not keep the user waiting the full DEADLINE
STALE_MAX_AGE = 3600  # ...unless it is this old: then wait the full DEADLINE so a slow server can still refresh
PAGE = 500
WORKERS = 4
INDEX_FILE = "library-index.json"
RECORDS_FILE = "library-records.json"


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
            "genres": genre_pairs(rec)}


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
    """[[genre, subGenre|None], ...]"""
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


def invalidate(directory, server_url):
    """Make the next open refresh (after a write: a protect toggle keeps the signature). Removes the index of this
    server's cache; the records file stays for its parsed metadata. False when the index could not be removed."""
    path = os.path.join(directory, INDEX_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            if json.load(f)["h"]["server"] != server_url:
                return True
    except (OSError, ValueError, TypeError, KeyError):
        pass  # no readable index: remove whatever is there
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


class Snapshot:
    """records: {id: RecordedItem} (finished ones only when asked for, in-progress ones always);
    entries: slim entries of all of them, newest first; channels: {channelId: name};
    stale: the server could not be asked, this is the old cache; meta: parsed metadata per record id;
    logos: channel ids with hasLogoData."""

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
        if not _ints(data.get("logos")):
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

    def _save(self, header, records, meta, entries, channels, logos):
        """Records first, index last: an index on disk always has its records file."""
        body = {"h": header, "records": records, "meta": {str(k): v for k, v in meta.items()}}
        if store.atomic_write_json(self._path(RECORDS_FILE), body):
            store.atomic_write_json(self._path(INDEX_FILE), {"h": header, "entries": entries, "channels": channels,
                                                             "logos": sorted(logos)})

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
        cache = self.load()
        short = cache is not None and 0 <= self.now() - cache[0]["t"] < STALE_MAX_AGE
        end = time.monotonic() + (min(self.deadline, self.stale_deadline) if short else self.deadline)

        def from_cache():
            got = self.load_records(cache[0]) if with_records else ({}, {})
            return None if got is None else (got[0], got[1], list(cache[1]), cache[2], cache[3])

        def stale(err, live=()):
            got = from_cache() if cache is not None else None
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
                    and set(cache[0]["live"]) <= live_now):
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
        # the records file of the last refresh (also after invalidate()) has the metadata parsed before
        got = self._read_records()
        old_names = {r["id"]: r.get("name") or "" for r in got[0]["records"]} if got else {}
        old_meta = got[1] if got else {}
        entries, meta = [], {}
        for rec in fetched:
            rid = rec["id"]
            if rid in old_meta and old_names.get(rid) == (rec.get("name") or ""):
                meta[rid] = old_meta[rid]
            else:
                meta[rid] = self._parse(rec, on_error)
            entries.append(slim(rec, meta[rid]))
        entries = _newest_first(entries)
        header = {"v": FORMAT, "server": self.server, "code": CODE_SHA1, "sig": stored_sig, "t": self.now(),
                  "live": sorted(live_now)}
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


def _newest_first_records(records):
    return sorted(records, key=lambda r: (r["startAt"], r["id"]), reverse=True)
