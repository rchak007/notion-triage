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
from pathlib import Path

import inbox
import mailer
import measure
from notion_walker import (NotionClient, walk, analyze, crumb, find_child,
                           resolve_source, DUE_SOON_DAYS)

A_TIER_RE = re.compile(r"\bA\d{4}\b")
URGENT_RE = re.compile(r"\burgent\b", re.I)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
EVERY_RE = re.compile(r"\bevery\s+(day|weekday|" + "|".join(WEEKDAYS) + r")s?\b", re.I)


def read_open_items(path):
    """Unchecked '- [ ]' items from a markdown checklist, grouped by '## '
    heading. Each item is its text (wrapped lines joined) up to the first full
    sentence after any bold title, capped at ~180 characters."""
    groups, heading, cur = [], "", None

    def flush():
        if cur is None:
            return
        text = re.sub(r"[*`]", "", " ".join(cur)).strip()
        m = re.match(r"(.{20,}?[.?!])(\s|$)", text)
        text = m.group(1) if m else text
        if len(text) > 180:
            text = text[:177].rsplit(" ", 1)[0] + "…"
        if not groups or groups[-1][0] != heading:
            groups.append((heading, []))
        groups[-1][1].append(text)

    for line in Path(path).read_text().splitlines():
        if line.startswith("- [ ] "):
            flush(); cur = [line[6:].strip()]
        elif cur is not None and line.startswith("  ") and line.strip():
            cur.append(line.strip())
        else:
            flush(); cur = None
            if line.startswith("## "):
                heading = line[3:].strip()
    flush()
    return groups


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


def build(name, a, items, a_only=False, title=None, group=False, depth=0, inbox_items=(), measuring=None, open_items=()):
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
             lambda i: f"due {i['due']:%a %b %-d} · " + (
                 "today" if i["due"] == today else f"in {(i['due'] - today).days}d")),
        ]
    def a_note(i):
        if not (i["due"] and i["due_confirmed"]):
            return ""
        late = (today - i["due"]).days
        return f"due {i['due']:%b %-d}" + (f" · {late}d overdue" if late > 0 else "")

    sections.append(("⭐ A-items" if a_only else "⭐ Other open A-items", a_items, a_note))

    # "Urgent" anywhere in the line: listed up top with the full branch, and
    # still shown in its normal section below.
    urgent = sorted((i for i in live if URGENT_RE.search(i["text"])),
                    key=lambda i: (i["due"] or date.max, crumb(i)))

    if title:
        subject = f"{title} · {today:%a %b %-d}"
    else:
        subject = (f"{name} triage · {today:%a %b %-d} · {len(overdue)} overdue · "
                   f"{len(soon)} due soon · {len(a_items)} A-items")
    if urgent:
        subject += f" · 🚨 {len(urgent)} urgent"

    header = f"{title or name} — {today:%A, %B %-d, %Y}"
    text = [header, ""]
    parts = [f"<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
             f"font-size:14px;line-height:1.4;color:#222\">"
             f"<p style=\"color:#666;margin:0 0 12px\">{html.escape(header)}</p>"]
    n_open = sum(len(g[1]) for g in open_items)
    if n_open:
        note = f"👇 {n_open} open items — see the Open items section at the bottom."
        text += [note, ""]
        parts.append(f"<p style=\"margin:0 0 12px;color:#555\"><i>{html.escape(note)}</i></p>")
    if inbox_items:
        # Inbox lives outside Notion (inbox.json); shown first, as a plain list.
        text.append(f"📥 INBOX ({len(inbox_items)})")
        parts.append("<div style=\"border-left:4px solid #36c;padding:2px 0 2px 12px;margin-bottom:8px\">"
                     f"<h3 style=\"margin:14px 0 6px\">📥 INBOX ({len(inbox_items)})</h3>"
                     "<ul style=\"margin:0;padding-left:18px\">")
        for it in inbox_items:
            text.append(f"  • {it['text']}")
            parts.append(f"<li style=\"margin-bottom:6px\">{html.escape(it['text'])}</li>")
        text.append("")
        parts.append("</ul></div>")
    if measuring:
        text += ["📊 MEASURING"] + [f"  {l}" for l in measure.text_table(measuring)] + [""]
        td = "padding:4px 8px;border:1px solid #ddd;white-space:nowrap"
        hd = measuring["headers"]
        head = "".join(f"<th style=\"{td};background:#f3f3f3;text-align:left\">{html.escape(h)}</th>"
                       for h in ["Category", hd[0], "Yesterday's notes", hd[1], hd[2]])
        note_td = ("padding:4px 8px;border:1px solid #ddd;font-size:12px;color:#444;"
                   "min-width:200px;max-width:360px;white-space:normal")
        body = ""
        for label, is_sub, y, m, a, note in measuring["rows"]:
            style = f"{td};color:#666;font-size:12px" if is_sub else f"{td};font-weight:600"
            dim = ";color:#666;font-size:12px" if is_sub else ""
            name = ("&nbsp;&nbsp;› " if is_sub else "") + html.escape(label)
            body += (f"<tr{' style=\"background:#fafafa\"' if is_sub else ''}><td style=\"{style}\">{name}</td>"
                     f"<td style=\"{td}{dim}\">{html.escape(y)}</td>"
                     f"<td style=\"{note_td}\">{html.escape(note)}</td>"
                     f"<td style=\"{td}{dim}\">{html.escape(m)}</td>"
                     f"<td style=\"{td}{dim}\">{html.escape(a)}</td></tr>")
        parts.append("<div style=\"border-left:4px solid #2a8;padding:2px 0 2px 12px;margin-bottom:8px\">"
                     "<h3 style=\"margin:14px 0 6px\">📊 MEASURING</h3>"
                     "<div style=\"overflow-x:auto\"><table style=\"border-collapse:collapse;font-size:13px\">"
                     f"<tr>{head}</tr>{body}</table></div></div>")
    if urgent:
        parts.append("<div style=\"border-left:4px solid #c00;padding:2px 0 2px 12px;margin-bottom:8px\">")
        render_section("🚨 URGENT", urgent, a_note, crumb, text, parts)
        parts.append("</div>")
    if group:
        render_grouped(sections, items, depth, text, parts)
    else:
        for sec_title, sec_items, note in sections:
            render_section(sec_title, sec_items, note, crumb, text, parts, show_empty=True)
    if n_open:
        text += ["━" * 40, f"📋 OPEN ITEMS ({n_open})", "━" * 40]
        parts.append(f"<h2 style=\"margin:30px 0 4px;padding-bottom:4px;border-bottom:2px solid #ddd;"
                     f"font-size:18px\">📋 Open items ({n_open})</h2>")
        for heading, titles in open_items:
            text.append(f"  {heading}")
            parts.append(f"<h4 style=\"margin:12px 0 4px;color:#555\">{html.escape(heading)}</h4>"
                         "<ul style=\"margin:0;padding-left:18px\">")
            for t in titles:
                text.append(f"    • {t}")
                parts.append(f"<li style=\"margin-bottom:4px\">{html.escape(t)}</li>")
            text.append("")
            parts.append("</ul>")
    parts.append("<p style=\"color:#aaa;font-size:11px;margin-top:20px\">"
                 "notion-triage · read-only</p></div>")
    return subject, "\n".join(text), "".join(parts)


def render_section(sec_title, sec_items, note, where, text, parts, show_empty=False, indent=""):
    if not sec_items and not show_empty:
        return
    text.append(f"{indent}{sec_title} ({len(sec_items)})")
    tag = "h4" if indent else "h3"
    parts.append(f"<{tag} style=\"margin:14px 0 6px\">{html.escape(sec_title)} ({len(sec_items)})</{tag}>")
    if not sec_items:
        text.append(f"{indent}  — none")
        parts.append("<p style=\"color:#888;margin:0\">None</p>")
    else:
        parts.append("<ul style=\"margin:0;padding-left:18px\">")
        for i in sec_items:
            extra, loc = note(i), where(i)
            text.append(f"{indent}  • {i['text']}")
            meta = " · ".join(x for x in (loc, extra) if x)
            if meta:
                text.append(f"{indent}      " + "  ".join(x for x in (loc, f"({extra})" if extra else "") if x))
            parts.append(
                f"<li style=\"margin-bottom:8px\">{html.escape(i['text'])}"
                + (f"<br><span style=\"color:#888;font-size:12px\">{html.escape(loc)}" if meta else "")
                + (f"{' · ' if loc else ''}<b style=\"color:#a33\">{html.escape(extra)}</b>" if extra else "")
                + ("</span>" if meta else "") + "</li>")
        parts.append("</ul>")
    text.append("")


SHORT = {"🔁": "recurring today", "🔴": "overdue", "🟡": "due soon", "⭐": "A-items"}


def render_grouped(sections, items, depth, text, parts):
    """One block per top-level node (in page order), each with its own
    Overdue / Due soon / A-items lists. Empty groups and lists are skipped."""
    def key(i):
        p = i["path"][depth:]
        return p[0] if p else i["text"]

    order = {}
    for n, i in enumerate(items):
        order.setdefault(key(i), n)
    groups = sorted({key(i) for _, lst, _ in sections for i in lst}, key=lambda k: order.get(k, 1e9))
    if not groups:
        text.append("Nothing overdue, due soon, or A-tagged. 🎉")
        parts.append("<p>Nothing overdue, due soon, or A-tagged. 🎉</p>")
        return

    def where(i):  # breadcrumb below the group heading
        return " › ".join(i["path"][depth + 1:])

    for g in groups:
        counts = [f"{len([i for i in lst if key(i) == g])} {SHORT[t[0]]}"
                  for t, lst, _ in sections if any(key(i) == g for i in lst)]
        text += ["━" * 40, f"{g.upper()}  ·  " + " · ".join(counts), "━" * 40]
        parts.append(f"<h2 style=\"margin:26px 0 4px;padding-bottom:4px;border-bottom:2px solid #ddd;"
                     f"font-size:18px\">{html.escape(g)}</h2>"
                     f"<p style=\"color:#888;font-size:12px;margin:0\">{html.escape(' · '.join(counts))}</p>")
        for sec_title, lst, note in sections:
            render_section(sec_title, [i for i in lst if key(i) == g], note, where, text, parts, indent="  ")


def main():
    ap = argparse.ArgumentParser(description="Email digest for one Notion source.")
    ap.add_argument("--source", required=True, help="e.g. OPA, WORKLL, STRADA")
    ap.add_argument("--page", help="page id (overrides the source's page)")
    ap.add_argument("--token-from", help="reuse another source's token, e.g. OPA when the page "
                                         "is shared with that same connection")
    ap.add_argument("--root", help="only walk the subtree under this top-level node title")
    ap.add_argument("--a-only", action="store_true", help="only A-tier items (+ recurring today)")
    ap.add_argument("--title", help="email title, e.g. 'STRADA TO DO'")
    ap.add_argument("--group", action="store_true", help="section the email by top-level node")
    ap.add_argument("--open-items", metavar="PATH",
                    help="append unchecked '- [ ]' items from this markdown file")
    ap.add_argument("--measure", action="store_true", help="include the Measuring section")
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
    subject, body, html_body = build(src, a, items, a_only=args.a_only, title=args.title,
                                     group=args.group, depth=len(path),
                                     inbox_items=inbox.items_for(src),
                                     measuring=measure.table() if args.measure else None,
                                     open_items=read_open_items(args.open_items)
                                     if args.open_items and Path(args.open_items).exists() else ())
    print(subject, "\n", body, sep="")
    if args.send and not mailer.send(subject, body, html=html_body):
        sys.exit(1)


if __name__ == "__main__":
    main()
