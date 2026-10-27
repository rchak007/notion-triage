#!/usr/bin/env python3
"""
measure.py — count personal activities per category, for the "Measuring"
section of a digest email.

Data and categories are local only (excluded from git):
  measurements.jsonl     one line per item: date, category, sub, desc
  measure_config.json    start date, categories, subcategories, aliases

Each log is one item for that category on that date. Categories listed under
"day_based" (e.g. Meditation) count days done rather than items.
Names match loosely: case, spaces and punctuation are ignored, and each
category/sub has aliases ("AI TPM", "AITPM", "TPM" → AI-TPM).

  python3 measure.py log Work "finished payroll mod review"
  python3 measure.py log MyDev --sub "ai tpm" "zone price analysis"
  python3 measure.py log Meditation --date 2026-10-24 --hours 0.33 "20 min"
  python3 measure.py log Diet --score 0.5 "ok lunch, junk dinner"
  python3 measure.py list [--days 7]
  python3 measure.py undo                # remove the most recent entry
  python3 measure.py stats               # the 3 summary lines
  python3 measure.py cats                # categories, subs, aliases
"""

import argparse
import calendar
import difflib
import json
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "measurements.jsonl"
CONFIG = HERE / "measure_config.json"


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def load_config():
    return json.loads(CONFIG.read_text())


def load_entries():
    if not DATA.exists():
        return []
    return [json.loads(l) for l in DATA.read_text().splitlines() if l.strip()]


def _match(name, options):
    """options: list of {"name", "aliases"}; exact match on normalized name/alias."""
    want = norm(name)
    for o in options:
        if want in {norm(o["name"]), *(norm(a) for a in o.get("aliases", []))}:
            return o
    return None


def _suggest(name, options):
    """Closest option names for a typo, never applied automatically."""
    keys = {}
    for o in options:
        for k in (o["name"], *o.get("aliases", [])):
            keys[norm(k)] = o["name"]
    hits = difflib.get_close_matches(norm(name), list(keys), n=3, cutoff=0.6)
    return list(dict.fromkeys(keys[h] for h in hits))


def _unknown(kind, name, options):
    guess = _suggest(name, options)
    known = ", ".join(o["name"] for o in options) or "none"
    raise SystemExit(f"⚠️ unknown {kind} '{name}'"
                     + (f" — did you mean {' or '.join(guess)}?" if guess else "")
                     + f"\n   Known: {known}\n   Nothing was logged.")


def resolve(cfg, category, sub=None):
    cat = _match(category, cfg["categories"])
    if not cat:
        _unknown("category", category, cfg["categories"])
    if not sub:
        return cat["name"], None
    s = _match(sub, cat.get("subs", []))
    if not s:
        _unknown(f"{cat['name']} subcategory", sub, cat.get("subs", []))
    return cat["name"], s["name"]


def counts(cfg, entries, start, end):
    """Per category for start..end inclusive: items, or distinct days if day-based."""
    day_based = set(cfg.get("day_based", []))
    out, subs, hours = {}, {}, {}
    for c in cfg["categories"]:
        rows = [e for e in entries if e["category"] == c["name"]
                and start <= date.fromisoformat(e["date"]) <= end]
        out[c["name"]] = len({e["date"] for e in rows}) if c["name"] in day_based else len(rows)
        subs[c["name"]] = Counter(e["sub"] for e in rows if e.get("sub"))
        hours[c["name"]] = sum(e.get("hours") or 0 for e in rows)
    return out, subs, hours


def _score_cats(cfg):
    return {c["name"] for c in cfg["categories"] if c.get("type") == "score"}


def day_scores(entries, name, start, end):
    """{date: score} for a score category; the latest log of a day wins.
    Days without a log are simply absent: 'not reported', never 0."""
    out = {}
    for e in entries:
        d = date.fromisoformat(e["date"])
        if e["category"] == name and start <= d <= end and e.get("score") is not None:
            out[d] = e["score"]
    return out


def _hours(cfg, name, h):
    """' · 3.5h' when any hours were logged for the category; nothing otherwise."""
    return f" · {round(h, 2):g}h" if h else ""


def _detail(sub_counts):
    """' (AI-TPM 3, myTrading 2)' — subcategory breakdown, biggest first."""
    return f" ({', '.join(f'{s} {k}' for s, k in sub_counts.most_common())})" if sub_counts else ""


def summary(today=None):
    """The 3 Measuring lines: yesterday, this month, all time."""
    if not CONFIG.exists():
        return []
    cfg, entries = load_config(), load_entries()
    today = today or date.today()
    start = date.fromisoformat(cfg["start"])
    short = {c["name"]: c.get("short", c["name"]) for c in cfg["categories"]}
    lines = []

    y = today - timedelta(days=1)
    if y < start:
        lines.append(f"Yesterday ({y:%a %b %-d}): — tracking starts {start:%b %-d}")
    else:
        c, subs, hrs = counts(cfg, entries, y, y)
        scored = _score_cats(cfg)

        def ybit(n, k):
            if n in scored:
                sc = day_scores(entries, n, y, y).get(y)
                return f"{short[n]} {sc:g}" if sc is not None else f"{short[n]} not reported"
            return f"{short[n]} {k}" + _hours(cfg, n, hrs[n]) + _detail(subs[n])
        lines.append(f"Yesterday ({y:%a %b %-d}): " + " | ".join(ybit(n, k) for n, k in c.items()))

    def ratio_line(label, frm):
        days = (today - frm).days + 1
        c, subs, hrs = counts(cfg, entries, frm, today)
        scored = _score_cats(cfg)

        def rbit(n, k):
            if n in scored:
                sc = day_scores(entries, n, frm, today)
                if not sc:
                    return f"{short[n]} not reported"
                return f"{short[n]} avg {sum(sc.values()) / len(sc):.2f} ({len(sc)}/{days} days reported)"
            return f"{short[n]} {k}/{days}" + _hours(cfg, n, hrs[n]) + _detail(subs[n])
        return (f"{label} ({days} day{'s' if days != 1 else ''}): "
                + " | ".join(rbit(n, k) for n, k in c.items()))

    month_from = max(today.replace(day=1), start)
    lines.append(ratio_line(f"{calendar.month_name[today.month]} so far", month_from))
    lines.append(ratio_line(f"All time since {start:%b %-d}", start))
    return lines


def main():
    ap = argparse.ArgumentParser(description="Personal activity counts (local only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("log")
    p.add_argument("category"); p.add_argument("desc", nargs="?", default="")
    p.add_argument("--sub"); p.add_argument("--date")
    p.add_argument("--hours", type=float, help="time spent, in hours (e.g. 1.5)")
    p.add_argument("--score", type=float, help="0–1 for score categories like Diet (1 = perfect)")
    p = sub.add_parser("list"); p.add_argument("--days", type=int, default=7)
    sub.add_parser("undo"); sub.add_parser("stats"); sub.add_parser("cats")
    args = ap.parse_args()
    cfg = load_config()

    if args.cmd == "log":
        cat, s = resolve(cfg, args.category, args.sub)
        d = args.date or date.today().isoformat()
        date.fromisoformat(d)
        is_score = cat in _score_cats(cfg)
        if is_score and args.score is None:
            raise SystemExit(f"⚠️ {cat} needs --score between 0 and 1 (1 = perfect). Nothing was logged.")
        if args.score is not None and not (is_score and 0 <= args.score <= 1):
            raise SystemExit("⚠️ --score must be 0–1 and only for score categories. Nothing was logged.")
        entry = {"date": d, "category": cat, "sub": s, "desc": args.desc}
        if is_score:
            entry["score"] = args.score
        if args.hours:
            entry["hours"] = round(args.hours, 2)
        with open(DATA, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"logged {d}: {cat}" + (f" › {s}" if s else "") + (f" · {args.hours:g}h" if args.hours else "")
              + (f" · score {args.score:g}" if is_score else "")
              + (f" — {args.desc}" if args.desc else ""))
    elif args.cmd == "list":
        since = date.today() - timedelta(days=args.days - 1)
        for e in load_entries():
            if date.fromisoformat(e["date"]) >= since:
                print(f"{e['date']}  {e['category']}" + (f" › {e['sub']}" if e.get("sub") else "")
                      + (f" · {e['hours']:g}h" if e.get("hours") else "")
                      + (f" · score {e['score']:g}" if e.get("score") is not None else "")
                      + (f" — {e['desc']}" if e.get("desc") else ""))
    elif args.cmd == "undo":
        entries = load_entries()
        if not entries:
            print("nothing to undo"); return 1
        last = entries.pop()
        DATA.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))
        print(f"removed: {last}")
    elif args.cmd == "stats":
        print("\n".join(summary()))
    elif args.cmd == "cats":
        for c in cfg["categories"]:
            tag = " (counts days)" if c["name"] in cfg.get("day_based", []) else ""
            print(f"{c['name']}{tag}  aliases: {', '.join(c.get('aliases', []))}")
            for s in c.get("subs", []):
                print(f"   › {s['name']}  aliases: {', '.join(s.get('aliases', []))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
