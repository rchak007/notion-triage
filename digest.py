#!/usr/bin/env python3
"""
digest.py — short email digest for one Notion source: overdue, due soon, and
open A-tier items (A1000, A1010, …). Lingering, dated-only and B/C items are
left out on purpose; the dashboard has the full picture.

  python3 digest.py --source OPA            # print only
  python3 digest.py --source OPA --send     # print and email (DIGEST_TO, default you)

Exit code 0 on success, 1 if the email didn't go out (retry next run).
"""

import argparse
import html
import re
import sys

import mailer
from notion_walker import NotionClient, walk, analyze, crumb, resolve_source, DUE_SOON_DAYS

A_TIER_RE = re.compile(r"\bA\d{4}\b")


def build(name, a):
    today = a["today"]
    overdue, soon = a["overdue"], a["due_soon"]
    shown = {id(i) for i in overdue + soon}
    a_items = [i for i in a["items"]
               if A_TIER_RE.search(i["text"]) and not i["done"] and not i["reference"]
               and id(i) not in shown]

    sections = [
        ("🔴 Overdue", overdue, lambda i: f"due {i['due']:%b %-d} · {(today - i['due']).days}d overdue"),
        (f"🟡 Due in the next {DUE_SOON_DAYS} days", soon,
         lambda i: f"due {i['due']:%a %b %-d} · in {(i['due'] - today).days}d"),
        ("⭐ Other open A-items", a_items, lambda i: ""),
    ]
    subject = (f"{name} triage · {today:%a %b %-d} · {len(overdue)} overdue · "
               f"{len(soon)} due soon · {len(a_items)} A-items")

    text = [f"{name} — {today:%A, %B %-d, %Y}", ""]
    parts = [f"<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
             f"font-size:14px;line-height:1.4;color:#222\">"
             f"<p style=\"color:#666;margin:0 0 12px\">{html.escape(text[0])}</p>"]
    for title, items, note in sections:
        text.append(f"{title} ({len(items)})")
        parts.append(f"<h3 style=\"margin:18px 0 6px\">{html.escape(title)} ({len(items)})</h3>")
        if not items:
            text.append("  — none")
            parts.append("<p style=\"color:#888;margin:0\">None</p>")
        else:
            parts.append("<ul style=\"margin:0;padding-left:18px\">")
            for i in items:
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
    ap.add_argument("--source", required=True, help="e.g. OPA or WORKLL")
    ap.add_argument("--page", help="page id (overrides the source's page)")
    ap.add_argument("--send", action="store_true", help="email it (default: print only)")
    args = ap.parse_args()

    token, page = resolve_source(args)
    if not token or not page:
        ap.error(f"missing NOTION_TOKEN_{args.source.upper()} / NOTION_PAGE_{args.source.upper()}")
    items = []
    walk(NotionClient(token), page, [], items)
    a = analyze(items)
    a["items"] = items
    subject, body, html_body = build(args.source.upper(), a)
    print(subject, "\n", body, sep="")
    if args.send and not mailer.send(subject, body, html=html_body):
        sys.exit(1)


if __name__ == "__main__":
    main()
