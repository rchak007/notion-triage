#!/usr/bin/env python3
"""
digest.py — short email digest for one Notion source.

Default: overdue, due soon, and open A-tier items (A0000–A9999). Lingering,
dated-only and B/C items are left out on purpose; the dashboard has the full
picture.

--a-only: just the open A-tier items (e.g. STRADA).
--root "TO DO": walk only the subtree under that top-level node; nothing else
on the page is read.

Either way, items like "due every Friday - Timesheets" are recurring: they're
listed at the top on their weekday and left out of the other sections.

  python3 digest.py --source OPA                       # print only
  python3 digest.py --source OPA --send                # print and email (DIGEST_TO)
  python3 digest.py --source STRADA --root "TO DO" --a-only --title "STRADA TO DO" --send

Exit code 0 on success, 1 if the email didn't go out (retry next run).
"""

import argparse
import html
import os
import re
import sys
from datetime import date

import mailer
from notion_walker import (NotionClient, walk, analyze, crumb, find_child,
                           resolve_source, DUE_SOON_DAYS)

A_TIER_RE = re.compile(r"\bA\d{4}\b")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
EVERY_RE = re.compile(r"\bevery\s+(day|weekday|" + "|".join(WEEKDAYS) + r")s?\b", re.I)


def recurs_on(text, today):
    """True/False if `text` is a recurring item ("every Friday"), None if not."""
    m = EVERY_RE.search(text)
    if not m:
        return None
    when = m.group(1).lower()
    if when == "day":
        return True
    if when == "weekday":
        return today.weekday() < 5
    return WEEKDAYS.index(when) == today.weekday()


def build(name, a, items, a_only=False, title=None):
    today = a["today"]
    live = [i for i in items if not i["done"] and not i["reference"]]
    recurring = {id(i) for i in live if recurs_on(i["text"], today) is not None}
    rec_today = [i for i in live if recurs_on(i["text"], today)]

    def keep(lst):
        return [i for i in lst if id(i) not in recurring]

    overdue, soon = ([], []) if a_only else (keep(a["overdue"]), keep(a["due_soon"]))
    shown = {id(i) for i in overdue + soon} | recurring
    a_items = sorted((i for i in live if A_TIER_RE.search(i["text"]) and id(i) not in shown),
                     key=lambda i: (i["due"] or date.max, crumb(i)))

    sections = []
    if rec_today:
        sections.append((f"🔁 Recurring today ({today:%A})", rec_today, lambda i: ""))
    if not a_only:
        sections += [
            ("🔴 Overdue", overdue, lambda i: f"due {i['due']:%b %-d} · {(today - i['due']).days}d overdue"),
            (f"🟡 Due in the next {DUE_SOON_DAYS} days", soon,
             lambda i: f"due {i['due']:%a %b %-d} · in {(i['due'] - today).days}d"),
        ]
    def a_note(i):
        if not (i["due"] and i["due_confirmed"]):
            return ""
        late = (today - i["due"]).days
        return f"due {i['due']:%b %-d}" + (f" · {late}d overdue" if late > 0 else "")

    sections.append(("⭐ A-items" if a_only else "⭐ Other open A-items", a_items, a_note))

    if title:
        subject = f"{title} · {today:%a %b %-d}"
    else:
        subject = (f"{name} triage · {today:%a %b %-d} · {len(overdue)} overdue · "
                   f"{len(soon)} due soon · {len(a_items)} A-items")

    header = f"{title or name} — {today:%A, %B %-d, %Y}"
    text = [header, ""]
    parts = [f"<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
             f"font-size:14px;line-height:1.4;color:#222\">"
             f"<p style=\"color:#666;margin:0 0 12px\">{html.escape(header)}</p>"]
    for sec_title, sec_items, note in sections:
        text.append(f"{sec_title} ({len(sec_items)})")
        parts.append(f"<h3 style=\"margin:18px 0 6px\">{html.escape(sec_title)} ({len(sec_items)})</h3>")
        if not sec_items:
            text.append("  — none")
            parts.append("<p style=\"color:#888;margin:0\">None</p>")
        else:
            parts.append("<ul style=\"margin:0;padding-left:18px\">")
            for i in sec_items:
                extra = note(i)
                text.append(f"  • {i['text']}")
                text.append(f"      {crumb(i)}" + (f"  ({extra})" if extra else ""))
                parts.append(
                    f"<li style=\"margin-bottom:8px\">{html.escape(i['text'])}<br>"
                    f"<span style=\"color:#888;font-size:12px\">{html.escape(crumb(i))}"
                    + (f" · <b style=\"color:#a33\">{html.escape(extra)}</b>" if extra else "")
                    + "</span></li>")
            parts.append("</ul>")
        text.append("")
    parts.append("<p style=\"color:#aaa;font-size:11px;margin-top:20px\">"
                 "notion-triage · read-only</p></div>")
    return subject, "\n".join(text), "".join(parts)


def main():
    ap = argparse.ArgumentParser(description="Email digest for one Notion source.")
    ap.add_argument("--source", required=True, help="e.g. OPA, WORKLL, STRADA")
    ap.add_argument("--page", help="page id (overrides the source's page)")
    ap.add_argument("--token-from", help="reuse another source's token, e.g. OPA when the page "
                                         "is shared with that same connection")
    ap.add_argument("--root", help="only walk the subtree under this top-level node title")
    ap.add_argument("--a-only", action="store_true", help="only A-tier items (+ recurring today)")
    ap.add_argument("--title", help="email title, e.g. 'STRADA TO DO'")
    ap.add_argument("--send", action="store_true", help="email it (default: print only)")
    args = ap.parse_args()

    src = args.source.upper()
    token, page = resolve_source(args)
    if args.token_from:
        token = os.environ.get(f"NOTION_TOKEN_{args.token_from.upper()}")
    if not token or not page:
        ap.error(f"missing NOTION_TOKEN_{src} / NOTION_PAGE_{src}")
    client = NotionClient(token)
    start, path = page, []
    items = []
    try:
        if args.root:
            start = find_child(client, page, args.root)
            if not start:
                sys.exit(f"no top-level node titled '{args.root}' on the {src} page")
            path = [args.root]
        walk(client, start, path, items)
    except RuntimeError as e:
        hint = (" — page not shared with this connection (page ••• → Connections → add it)"
                if "HTTP 404" in str(e) else "")
        sys.exit(f"Couldn't read {src} from Notion: {str(e).splitlines()[0]}{hint}")
    a = analyze(items)
    subject, body, html_body = build(src, a, items, a_only=args.a_only, title=args.title)
    print(subject, "\n", body, sep="")
    if args.send and not mailer.send(subject, body, html=html_body):
        sys.exit(1)


if __name__ == "__main__":
    main()
