#!/usr/bin/env python3
"""tests/fixtures/epgmeta_expected.json: what epgmeta.py makes of every captured EPG text.

The file is KODIstation's regression fixture for the parser (tests/test_epgmeta.py compares against it).
After a deliberate parser change, rewrite it from the Python output and review the diff:

    uv run python tools/epgmeta_expected.py --write
    git diff --word-diff tests/fixtures/epgmeta_expected.json

Cases, one JSON object per line, keys sorted:
    dev/<id>, dev2/<id>   recordings (<set>/api/recorded.json, full-width): name, parse, plot, people, flag_tags,
                          genre_tags, movie_year
    <set>/<id>/dir        the same recording parsed with its rule's directory as `dir` (parse only)
    synthetic/<key>       hand-written EPG texts (synthetic/epg_texts.json: shapes the free channels lack), like
                          a recording
    schedule/<n>          dev guide entries (dev/schedule_texts.json), plot as plot_hash (sha256, 16 hex) to stay small
The initial file came from the user's epgXport.js (tools/epgxport_baseline.js, run once).

Review by behaviour class rather than per program (backlog 020): `--classes [FILE ...]` prints how many cases of
each file (default: the expected file) fall into each class (classes()), one column per file; a class with no case
in the last file is marked LOST.

    uv run python tools/epgmeta_expected.py --classes old.json tests/fixtures/epgmeta_expected.json
"""
import argparse
import hashlib
import json
import os
import sys
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
EXPECTED = os.path.join(FIXTURES, "epgmeta_expected.json")
sys.path.insert(0, os.path.join(ROOT, "plugin.video.epgstation"))

from resources.lib.epgstation import epgmeta  # noqa: E402


def _json(rel):
    with open(os.path.join(FIXTURES, rel), encoding="utf-8") as f:
        return json.load(f)


def _drop_none(d):
    return {k: v for k, v in d.items() if v is not None}


def _parsed(item, dir=""):
    if not epgmeta.norm(item.get("name")):
        return None
    return _drop_none(epgmeta.parse_title(item["name"], epgmeta.genres_of(item), dir))


def text_case(case_id, item, plot_hash):
    desc, ext = item.get("description") or "", item.get("extended") or ""
    ext_result = epgmeta.parse_extended(desc, ext, epgmeta.is_dubbed(item.get("name")))
    fields = epgmeta.genre_fields(epgmeta.genres_of(item), epgmeta.norm(item.get("name")))
    case = {"id": case_id, "parse": _parsed(item), "people": ext_result["people"],
            "flag_tags": fields["flag_tags"], "genre_tags": fields["genre_tags"],
            "movie_year": epgmeta.movie_year(desc, ext)}
    if plot_hash:
        case["plot_hash"] = hashlib.sha256(ext_result["plot"].encode("utf-8")).hexdigest()[:16]
    else:
        case["name"] = item.get("name")
        case["plot"] = ext_result["plot"]
    return case


SETS = ("dev", "dev2")


def inputs():
    """[(case_id, kind, item, dir)] in file order: recordings per set, their dir variants, synthetic texts,
    schedule entries."""
    out = []
    for fixture_set in SETS:
        texts = {str(r["id"]): r for r in _json(fixture_set + "/api/recorded.json")["records"]}
        try:
            rules = {r["id"]: r for r in _json(fixture_set + "/api/rules.json")["rules"]}
        except OSError:
            rules = {}
        ids = sorted(texts, key=int)
        out += [(f"{fixture_set}/{i}", "recording", texts[i], "") for i in ids]
        for i in ids:
            directory = ((rules.get(texts[i].get("ruleId")) or {}).get("saveOption") or {}).get("directory")
            if directory:
                d = unicodedata.normalize("NFKC", os.path.basename(directory.strip()))
                out.append((f"{fixture_set}/{i}/dir", "dir", texts[i], d))
    out += [(f"synthetic/{item['key']}", "recording", item, "") for item in _json("synthetic/epg_texts.json")["items"]]
    out += [(f"schedule/{n}", "schedule", item, "") for n, item in enumerate(_json("dev/schedule_texts.json"))]
    return out


def classes(case):
    """Behaviour classes of one expected case: parse kind / version / edition and the presence of season,
    episode_end, year; people tags and dub / voice roles; every flag and genre tag; movie_year; dir parses."""
    out = []
    if case["id"].endswith("/dir"):
        return ["dir"] if case.get("parse") else []
    parse = case.get("parse") or {}
    if parse.get("kind"):
        out.append("parse.kind=" + parse["kind"])
    for field in ("version", "edition"):
        if parse.get(field):
            out.append(f"parse.{field}=" + parse[field])
    out += ["parse." + field for field in ("season", "episode_end", "year") if parse.get(field) is not None]
    for tag, value in case.get("people") or []:
        out.append("people." + tag)
        if tag == "actor":
            role = value[1] or ""
            if role.startswith(epgmeta.DUB_ROLE):
                out.append("role=吹替")
            elif "声" in role:
                out.append("role=声")
    out += ["flag=" + str(t) for t in case.get("flag_tags") or []]
    out += ["genre=" + str(t) for t in case.get("genre_tags") or []]
    if case.get("movie_year"):
        out.append("movie_year")
    return list(dict.fromkeys(out))


def class_table(paths):
    """Lines "class<TAB>count per file", LOST when the last file has none of a class the others have."""
    counts = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            c = {}
            for case in json.load(f):
                for k in classes(case):
                    c[k] = c.get(k, 0) + 1
            counts.append(c)
    names = sorted(set().union(*counts))
    lines = ["class\t" + "\t".join(os.path.basename(p) for p in paths)]
    for k in names:
        row = [str(c.get(k, 0)) for c in counts]
        lost = len(paths) > 1 and not counts[-1].get(k)
        lines.append(f"{k}\t" + "\t".join(row) + ("\tLOST" if lost else ""))
    lines.append(f"{len(names)} classes; " + ", ".join(f"{os.path.basename(p)}: {len(c)}"
                                                        for p, c in zip(paths, counts)))
    return lines


def build_case(case_id, kind, item, dir):
    if kind == "dir":
        return {"id": case_id, "dir": dir, "parse": _parsed(item, dir)}
    return text_case(case_id, item, kind == "schedule")


def dumps(case):
    return json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="rewrite " + os.path.relpath(EXPECTED, ROOT))
    ap.add_argument("--classes", nargs="*", metavar="FILE", help="print the behaviour class table of these files")
    args = ap.parse_args()
    if args.classes is not None:
        print("\n".join(class_table(args.classes or [EXPECTED])))
        return
    lines = [dumps(build_case(*i)) for i in inputs()]
    text = "[\n" + ",\n".join(lines) + "\n]\n"
    if args.write:
        with open(EXPECTED, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {len(lines)} cases to {EXPECTED}")
    else:
        with open(EXPECTED, encoding="utf-8") as f:
            same = f.read() == text
        print(f"{len(lines)} cases,", "identical" if same else "DIFFERENT (run with --write and review)")
        sys.exit(0 if same else 1)


if __name__ == "__main__":
    main()
