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
  python3 measure.py stats               # the Measuring table
  python3 measure.py day [--date D]      # one day, vertical
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
        return cat["name"], cat.get("default_sub")
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
    """{date: score} for a score category. Several logs in a day (e.g. lunch
    and dinner) are averaged. Days without a log are simply absent: 'not
    reported', never 0."""
    per_day = {}
    for e in entries:
        d = date.fromisoformat(e["date"])
        if e["category"] == name and start <= d <= end and e.get("score") is not None:
            per_day.setdefault(d, []).append(e["score"])
    return {d: sum(v) / len(v) for d, v in per_day.items()}


def _hours(cfg, name, h):
    """' · 3.5h' when any hours were logged for the category; nothing otherwise."""
    return f" · {round(h, 2):g}h" if h else ""


def _detail(sub_counts):
    """' (AI-TPM 3, myTrading 2)' — subcategory breakdown, biggest first."""
    return f" ({', '.join(f'{s} {k}' for s, k in sub_counts.most_common())})" if sub_counts else ""


def _period(cfg, entries, frm, to):
    """{category: {"n", "h", "subs": {sub: [n, h]}, "score": (avg, days) | None}}"""
    scored = _score_cats(cfg)
    out = {}
    for c in cfg["categories"]:
        name = c["name"]
        rows = [e for e in entries if e["category"] == name
                and frm <= date.fromisoformat(e["date"]) <= to]
        subs = {}
        for e in rows:
            if e.get("sub"):
                t = subs.setdefault(e["sub"], [0, 0.0])
                t[0] += 1
                t[1] += e.get("hours") or 0
        score = None
        if name in scored:
            sc = day_scores(entries, name, frm, to)
            score = (sum(sc.values()) / len(sc), len(sc)) if sc else None
        out[name] = {"n": len(rows), "h": sum(e.get("hours") or 0 for e in rows),
                     "subs": subs, "score": score}
    return out


def _cell(n, h, days=None):
    s = f"{n}/{days}" if days else f"{n}"
    return s + (f" · {round(h, 2):g}h" if h else "")


def table(today=None):
    """Measuring table: {"headers": [...3 column titles], "rows": [(label, is_sub, y, m, a)]}.
    Rows are categories, with indented subcategory rows under any that have
    activity. Returns None when measurements aren't set up."""
    if not CONFIG.exists():
        return None
    cfg, entries = load_config(), load_entries()
    today = today or date.today()
    start = date.fromisoformat(cfg["start"])
    y = today - timedelta(days=1)
    month_from = max(today.replace(day=1), start)
    mdays, adays = (today - month_from).days + 1, (today - start).days + 1
    have_y = y >= start
    P = {"y": _period(cfg, entries, y, y) if have_y else None,
         "m": _period(cfg, entries, month_from, today),
         "a": _period(cfg, entries, start, today)}
    headers = [f"Yesterday ({y:%a %b %-d})" if have_y else f"Yesterday (tracking starts {start:%b %-d})",
               f"{calendar.month_name[today.month]} so far ({mdays}d)",
               f"All time since {start:%b %-d} ({adays}d)"]
    rows = []
    for c in cfg["categories"]:
        name, label = c["name"], c.get("short", c["name"])
        if c.get("type") == "score":
            def sc(per, days, single=False):
                if per is None:
                    return "—"
                v = per[name]["score"]
                if not v:
                    return "not reported"
                return f"{v[0]:.2f}" if single else f"avg {v[0]:.2f} ({v[1]}/{days}d)"
            rows.append((label, False, sc(P["y"], 1, True), sc(P["m"], mdays), sc(P["a"], adays)))
            continue
        yc = _cell(P["y"][name]["n"], P["y"][name]["h"]) if have_y else "—"
        rows.append((label, False, yc, _cell(P["m"][name]["n"], P["m"][name]["h"], mdays),
                     _cell(P["a"][name]["n"], P["a"][name]["h"], adays)))
        order = [x["name"] for x in c.get("subs", [])]
        for sub in sorted(P["a"][name]["subs"], key=lambda k: order.index(k) if k in order else 99):
            def sv(per, days=None):
                if per is None:
                    return "—"
                n, h = per[name]["subs"].get(sub, [0, 0])
                return _cell(n, h, days)
            rows.append((sub, True, sv(P["y"]), sv(P["m"], mdays), sv(P["a"], adays)))
    return {"headers": headers, "rows": rows}


def text_table(t):
    """Fixed-width plain-text rendering of table()."""
    labels = [("  › " + r[0]) if r[1] else r[0] for r in t["rows"]]
    cols = [["Category"] + labels] + [[t["headers"][i]] + [r[2 + i] for r in t["rows"]] for i in range(3)]
    w = [max(len(x) for x in col) for col in cols]
    line = lambda cells: "  ".join(c.ljust(w[i]) for i, c in enumerate(cells)).rstrip()
    out = [line([col[0] for col in cols]), "  ".join("─" * x for x in w)]
    out += [line([col[k] for col in cols]) for k in range(1, len(cols[0]))]
    return out


def day_rows(d):
    """Vertical one-day view: [(label, is_sub, cell, notes)] for every category,
    with subcategory rows for those used that day. Notes are that row's entry
    descriptions (on the sub rows when a category has subs)."""
    cfg, entries = load_config(), load_entries()
    P = _period(cfg, entries, d, d)
    day = [e for e in entries if e["date"] == d.isoformat()]

    def notes(name, sub=None, any_sub=False):
        rows = [e for e in day if e["category"] == name
                and (any_sub or e.get("sub") == sub) and e.get("desc")]
        return " • ".join(e["desc"] for e in rows)

    rows = []
    for c in cfg["categories"]:
        name, label, p = c["name"], c.get("short", c["name"]), P[c["name"]]
        if c.get("type") == "score":
            rows.append((label, False, f"{p['score'][0]:.2f}" if p["score"] else "not reported",
                         notes(name, any_sub=True)))
            continue
        rows.append((label, False, _cell(p["n"], p["h"]),
                     notes(name, None) if p["subs"] else notes(name, any_sub=True)))
        order = [x["name"] for x in c.get("subs", [])]
        for sub, (n, h) in sorted(p["subs"].items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99):
            rows.append((sub, True, _cell(n, h), notes(name, sub)))
    n = sum(P[c["name"]]["n"] for c in cfg["categories"] if c.get("type") != "score")
    h = sum(P[c["name"]]["h"] for c in cfg["categories"])
    rows.append(("Total", False, _cell(n, h), ""))
    return rows


def summary(today=None):
    """Plain-text lines of the Measuring table (CLI `stats`)."""
    t = table(today)
    return text_table(t) if t else []


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
    p = sub.add_parser("day"); p.add_argument("--date", help="YYYY-MM-DD, default today")
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
        if args.hours and any(c["name"] == cat and c.get("no_hours") for c in cfg["categories"]):
            print(f"note: {cat} counts items only; hours not recorded")
            args.hours = None
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
    elif args.cmd == "day":
        d = date.fromisoformat(args.date) if args.date else date.today()
        rows = day_rows(d)
        w = max(len(("  › " if sub else "") + l) for l, sub, _, _ in rows)
        cw = max(len(c) for _, _, c, _ in rows + [("", False, f"{d:%a %b %-d}", "")])
        print(f"{'Category':<{w}}  {format(d, '%a %b %-d'):<{cw}}  Notes")
        for l, sub, cell, note in rows:
            print(f"{(('  › ' if sub else '') + l):<{w}}  {cell:<{cw}}  {note}".rstrip())
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
