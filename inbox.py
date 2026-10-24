#!/usr/bin/env python3
"""
inbox.py — per-source inbox items that live outside Notion. digest.py shows
them as the first section of that source's email. The data file, inbox.json,
is local only (excluded from git).

  python3 inbox.py list [SOURCE]
  python3 inbox.py add SOURCE "send Srinu Thanksgiving gift"
  python3 inbox.py remove SOURCE <number | text fragment>

Removed items aren't deleted: they're appended, with the date, to
archiveInbox/<SOURCE>.jsonl (local only, never shown in emails).
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

DATA = Path(__file__).resolve().parent / "inbox.json"
ARCHIVE_DIR = Path(__file__).resolve().parent / "archiveInbox"
# Friendly names → the digest's --source name.
ALIASES = {"LAUSD": "WORKLL", "TASKLISTS": "WORKLL", "REAL ESTATE": "REALESTATE",
           "REAL_ESTATE": "REALESTATE"}


def norm(source):
    s = source.strip().upper()
    return ALIASES.get(s, ALIASES.get(s.replace(" ", ""), s.replace(" ", "")))


def load():
    return json.loads(DATA.read_text()) if DATA.exists() else {}


def save(data):
    DATA.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def archive(source, item):
    ARCHIVE_DIR.mkdir(exist_ok=True)
    with open(ARCHIVE_DIR / f"{source}.jsonl", "a") as f:
        f.write(json.dumps({**item, "done": date.today().isoformat()}, ensure_ascii=False) + "\n")


def items_for(source):
    return load().get(norm(source), [])


def main():
    ap = argparse.ArgumentParser(description="Local inbox items per source.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("source", nargs="?")
    p = sub.add_parser("add"); p.add_argument("source"); p.add_argument("text")
    p = sub.add_parser("remove"); p.add_argument("source"); p.add_argument("which")
    args = ap.parse_args()
    data = load()

    if args.cmd == "list":
        for src in ([norm(args.source)] if args.source else sorted(data)):
            print(f"{src}:")
            for n, it in enumerate(data.get(src, []), 1):
                print(f"  {n}. {it['text']}  (added {it['added']})")
            if not data.get(src):
                print("  — empty")
        return 0

    src = norm(args.source)
    lst = data.setdefault(src, [])
    if args.cmd == "add":
        lst.append({"text": args.text, "added": date.today().isoformat()})
        save(data)
        print(f"added to {src} inbox: {args.text}")
        return 0

    # remove: by 1-based number, or a unique case-insensitive text match
    if args.which.isdigit() and 1 <= int(args.which) <= len(lst):
        hits = [lst[int(args.which) - 1]]
    else:
        hits = [it for it in lst if args.which.lower() in it["text"].lower()]
    if len(hits) != 1:
        print(f"{'no' if not hits else 'several'} {src} inbox items match '{args.which}'")
        return 1
    archive(src, hits[0])
    lst.remove(hits[0])
    save(data)
    print(f"done: moved to archiveInbox/{src}.jsonl: {hits[0]['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
