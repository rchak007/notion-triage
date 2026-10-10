#!/usr/bin/env python3
"""
notion_walker.py — read-only Notion tree walker + deterministic triage engine.

READ-ONLY BY DESIGN: this module only ever issues HTTP GET against the Notion
API (blocks/{id}/children). It contains no create/update/delete calls. It cannot
edit, move, or delete anything in your Notion, and it cannot alter the tree
structure. The worst it can do is read.

What it produces (all deterministic, $0, no model):
  • overdue / due-soon   — the date that follows a due-signal word ("due",
                           "deadline", "eod"); falls back to the first date
  • dated (unconfirmed)  — a date appears but with no due-signal (likely a
                           created/logged date, not a deadline)
  • priorities           — weighted tiers (default A1000 > B1000 > C1000)
  • lingering            — open tagged/dated items with no confirmed due date and
                           no edits anywhere in their subtree for >= STALE_DAYS

Done = a checked to-do, or text marked DONE / Completed / ✅ (but not "once
DONE"); everything under a done item is done too. Items under REFERENCE_NODES
(default "INFO") are kept for the Ask panel but left out of triage.

Importable by app.py (Streamlit), and runnable standalone for pi2 cron later.

Env / .env keys:
  NOTION_TOKEN            token for single-source CLI runs
  NOTION_ROOT_PAGE_ID     page id for single-source CLI runs
  For --source WORKLL it reads NOTION_TOKEN_WORKLL / NOTION_PAGE_WORKLL, etc.
  PRIORITY_TIERS          "A1000:3,B1000:2,C1000:1"  (label:weight, highest wins)
  DUE_SOON_DAYS           look-ahead window, default 7
  STALE_DAYS              lingering threshold, default 14
  SKIP_NODES              node titles to prune, default "Archive,Archived"
  REFERENCE_NODES         node titles whose subtree is notes, not tasks, default "INFO"

CLI:
  python3 notion_walker.py --source WORKLL
  python3 notion_walker.py --page <id>            # uses NOTION_TOKEN
  python3 notion_walker.py --selftest             # offline, no network
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date

# ─── .env loader (stdlib, no python-dotenv) ──────────────────────────────────

def load_dotenv(path=None):
    """Load KEY=VALUE lines from a .env into os.environ. Absent file = no-op.
    Existing env vars win over .env. Supports `export `, # comments, quotes."""
    path = path or os.environ.get("DOTENV_PATH", ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val

load_dotenv()

# ─── Config ──────────────────────────────────────────────────────────────────

NOTION_VERSION = os.environ.get("NOTION_VERSION", "2022-06-28")
DUE_SOON_DAYS = int(os.environ.get("DUE_SOON_DAYS", "7"))
STALE_DAYS = int(os.environ.get("STALE_DAYS", "14"))
def _name_set(key, default):
    return {s.strip().lower() for s in os.environ.get(key, default).split(",") if s.strip()}

SKIP_NODES = _name_set("SKIP_NODES", "Archive,Archived")
REFERENCE_NODES = _name_set("REFERENCE_NODES", "INFO")
REQUEST_DELAY = float(os.environ.get("NOTION_REQUEST_DELAY", "0.34"))  # ~3 req/s ceiling

def parse_tiers(spec):
    tiers = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        label, _, w = part.partition(":")
        try:
            tiers.append((label.strip(), int(w) if w else 1))
        except ValueError:
            tiers.append((label.strip(), 1))
    return sorted(tiers, key=lambda t: -t[1])  # highest weight first

TIERS = parse_tiers(os.environ.get("PRIORITY_TIERS", "A1000:3,B1000:2,C1000:1"))

DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
# No leading \b so run-together text like "systemdue 8/24/26" still counts.
DUE_SIGNAL_RE = re.compile(r"(?:due|deadline|eod)\b", re.I)
# The date right after a due-signal ("Due 8/25/26", "Due date 9/29/26", "due by 8/1/26").
DUE_DATE_RE = re.compile(r"(?:due|deadline|eod)\b[^0-9\n]{0,15}?(\d{1,2})/(\d{1,2})/(\d{2,4})\b", re.I)
DONE_RE = re.compile(r"\b(?:done|completed)\b", re.I)
DONE_MARKS = ("✅", "✔")
# "once DONE", "when done", "to be done" describe future work, not a finished task.
NOT_DONE_PREV = {"once", "when", "until", "after", "if", "before", "be", "get", "not"}

# ─── HTTP helper (stdlib only) ───────────────────────────────────────────────

def http_json(method, url, headers, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {url}\n{e.read().decode(errors='replace')}") from None

# ─── Notion client (GET only) ────────────────────────────────────────────────

class NotionClient:
    def __init__(self, token):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def get_children(self, block_id):
        cursor = None
        while True:
            url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
            if cursor:
                url += f"&start_cursor={cursor}"
            time.sleep(REQUEST_DELAY)
            payload = http_json("GET", url, self.headers)
            for block in payload.get("results", []):
                yield block
            if not payload.get("has_more"):
                break
            cursor = payload.get("next_cursor")


class MockClient:
    """Offline client for --selftest. Mirrors Notion block JSON shape."""
    def __init__(self, tree):
        self.tree = tree

    def get_children(self, block_id):
        yield from self.tree.get(block_id, [])

# ─── Block parsing ───────────────────────────────────────────────────────────

def block_text(block):
    btype = block.get("type", "")
    if btype == "child_page":
        return block.get("child_page", {}).get("title", "").strip()
    payload = block.get(btype, {})
    return "".join(rt.get("plain_text", "") for rt in payload.get("rich_text", [])).strip()

def is_checked(block):
    if block.get("type") == "to_do":
        return bool(block.get("to_do", {}).get("checked"))
    return None

def iso_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])  # day granularity; ignore tz/time
    except ValueError:
        return None

def node_is_skipped(text):
    return text.strip().lower() in SKIP_NODES

def node_is_reference(text):
    return text.strip().lower() in REFERENCE_NODES

def text_done(text):
    if any(mark in text for mark in DONE_MARKS):
        return True
    for m in DONE_RE.finditer(text):
        prev = text[:m.start()].split()
        if not prev or prev[-1].lower().strip(".,;:-") not in NOT_DONE_PREV:
            return True
    return False

def _latest(*dates):
    dates = [d for d in dates if d]
    return max(dates) if dates else None

# ─── Tree walk (read-only) ───────────────────────────────────────────────────

def find_child(client, page_id, title):
    """Id of the page's top-level block titled `title` (case-insensitive), or
    None. Reads only the top-level blocks, so walking from the result never
    touches sibling sections."""
    want = title.strip().lower()
    for block in client.get_children(page_id):
        if block_text(block).strip().lower() == want:
            return block["id"]
    return None

def walk(client, block_id, path, out, done=False, reference=False):
    """Append one item per non-empty block. Returns the latest edit date in this
    subtree, so a parent task counts as active when anything under it changes."""
    latest = None
    for block in client.get_children(block_id):
        text = block_text(block)
        if text and node_is_skipped(text):
            continue  # prune Archive and its whole subtree
        edited = iso_date(block.get("last_edited_time"))
        item_done = done or bool(is_checked(block)) or (bool(text) and text_done(text))
        item_ref = reference or (bool(text) and node_is_reference(text))
        item = None
        if text:
            item = {
                "path": list(path),
                "text": text,
                "type": block.get("type", ""),
                "checked": is_checked(block),
                "done": item_done,
                "reference": item_ref,
                "created": iso_date(block.get("created_time")),
                "last_edited": edited,
            }
            out.append(item)
        sub = None
        if block.get("has_children"):
            sub = walk(client, block["id"], path + [text] if text else path, out, item_done, item_ref)
        active = _latest(edited, sub)
        if item:
            item["last_active"] = active
        latest = _latest(latest, active)
    return latest

# ─── Deterministic analysis ──────────────────────────────────────────────────

def _to_date(m):
    mm, dd, yy = (int(g) for g in m.groups())
    if yy < 100:
        yy += 2000
    try:
        return date(yy, mm, dd)
    except ValueError:
        return None

def parse_due(text):
    """Returns (date, confirmed). Prefers the date right after a due-signal word;
    otherwise the first date, confirmed only if a due-signal appears anywhere."""
    m = DUE_DATE_RE.search(text)
    if m and (d := _to_date(m)):
        return d, True
    m = DATE_RE.search(text)
    if not m:
        return None, False
    d = _to_date(m)
    return d, bool(d) and bool(DUE_SIGNAL_RE.search(text))

def match_priority(low):
    for label, weight in TIERS:  # already highest-weight-first
        if label.lower() in low:
            return (label, weight)
    return None

def analyze(items, today=None):
    today = today or date.today()
    soon_ord = today.toordinal() + DUE_SOON_DAYS
    for it in items:
        low = it["text"].lower()
        it["due"], it["due_confirmed"] = parse_due(it["text"])
        it["priority"] = match_priority(low)
        act = it.get("last_active") or it.get("last_edited")
        it["age_days"] = (today - act).days if act else None

    overdue, due_soon, dated, lingering = [], [], [], []
    priorities = {label: [] for label, _ in TIERS}
    for it in items:
        if it.get("done", it["checked"]) or it.get("reference"):
            continue
        d = it["due"]
        if d and it["due_confirmed"]:
            if d < today:
                overdue.append(it)
            elif d.toordinal() <= soon_ord:
                due_soon.append(it)
        elif d:  # date present but no due-signal word → likely a logged/created date
            dated.append(it)
        if it["priority"]:
            priorities[it["priority"][0]].append(it)
        # Tasks are the tagged/dated items; untagged notes and section headings
        # aren't. Confirmed-due items already have their own buckets.
        is_task = it["priority"] or d
        if is_task and not it["due_confirmed"] and it["age_days"] is not None \
                and it["age_days"] >= STALE_DAYS:
            lingering.append(it)

    overdue.sort(key=lambda i: i["due"])
    due_soon.sort(key=lambda i: i["due"])
    lingering.sort(key=lambda i: -(i["age_days"] or 0))
    for k in priorities:
        priorities[k].sort(key=lambda i: (i["due"] or date.max))
    return {
        "overdue": overdue, "due_soon": due_soon, "dated": dated,
        "priorities": priorities, "lingering": lingering,
        "total": len(items), "today": today,
    }

# ─── Text report (for CLI / pi2 cron) ────────────────────────────────────────

def crumb(it):
    return " › ".join(it["path"]) or "(root)"

def build_report(a):
    L = [f"=== Notion Triage — {a['today'].isoformat()} ===",
         f"scanned {a['total']} items · skipped: {', '.join(sorted(SKIP_NODES)) or 'none'}"
         f" · reference: {', '.join(sorted(REFERENCE_NODES)) or 'none'}", ""]

    def row(it, extra=""):
        return f"  • {it['text']}\n      [{crumb(it)}]{extra}"

    L.append(f"🔴 OVERDUE ({len(a['overdue'])})")
    for it in a["overdue"]:
        L.append(row(it, f"  (due {it['due']}, {(a['today']-it['due']).days}d overdue)"))
    L.append("  — none" if not a["overdue"] else "")

    L.append(f"🟡 DUE SOON · next {DUE_SOON_DAYS}d ({len(a['due_soon'])})")
    for it in a["due_soon"]:
        L.append(row(it, f"  (due {it['due']}, in {(it['due']-a['today']).days}d)"))
    L.append("  — none" if not a["due_soon"] else "")

    for label, _ in TIERS:
        items = a["priorities"].get(label, [])
        L.append(f"⭐ {label} ({len(items)})")
        for it in items:
            L.append(row(it, f"  (due {it['due']})" if it["due"] else ""))
        if not items:
            L.append("  — none")

    L.append(f"\n🐌 LINGERING · tagged/dated, no edits in subtree ≥{STALE_DAYS}d ({len(a['lingering'])})")
    for it in a["lingering"]:
        L.append(row(it, f"  ({it['age_days']}d idle)"))
    if not a["lingering"]:
        L.append("  — none")

    if a["dated"]:
        L.append(f"\n📅 DATED (unconfirmed — no 'due' word) ({len(a['dated'])})")
        for it in a["dated"]:
            L.append(row(it, f"  ({it['due']})"))
    return "\n".join(L)

# ─── Self-test (offline) ─────────────────────────────────────────────────────

def _b(bid, btype, text, has_children=False, edited="2026-07-25", created="2026-06-01"):
    block = {"id": bid, "type": btype, "has_children": has_children,
             "created_time": created + "T12:00:00.000Z",
             "last_edited_time": edited + "T12:00:00.000Z"}
    if btype == "child_page":
        block["child_page"] = {"title": text}
        return block
    block[btype] = {"rich_text": [{"plain_text": text}] if text else []}
    return block

def selftest():
    tree = {
        "root": [
            _b("u", "heading_2", "Union contracts", has_children=True),
            _b("redwood", "toggle", "Redwood -"),
        ],
        "u": [
            _b("a1sheet", "toggle", "A1000 Schema co-ordination sheet"),
            _b("prem", "toggle", "B1000 Premiums number changes"),
            _b("pymod", "toggle", "6/21/26 - PY Mod 39_Create Parental Leave Unpaid Absence Code PLUP"),
            _b("r11949", "toggle", "R11949 - Teamsters - Units D, S, A, and H Implementation", has_children=True),
            _b("r11959", "toggle", "R11959 - SEIU Implementation Memo and MOUs", has_children=True),
            _b("r11969", "toggle", "R11969 - UTLA 2025-27 Salary Raise other", has_children=True),
            _b("budget", "toggle", "C1000 Budget Memo - email Angela 6/22/26", edited="2026-05-10"),
            _b("r11975", "toggle", "R11975 - Unit S A H - Vacation accruals", edited="2026-05-01"),
            _b("archive", "toggle", "Archive", has_children=True),
            _b("archived", "toggle", "Archived", has_children=True),
            _b("twodates", "toggle", "Config change from vendor 6/22/26 - A1000 - paperwork due 8/10/26"),
            _b("nospace", "toggle", "Portal systemdue 8/24/26 - A1000"),
            _b("once", "toggle", "DO FULL DOCUMENTATION UPDATE once DONE A1000 Due 10/1/26"),
            _b("donepar", "toggle", "Configure this Z1000 - Done;", has_children=True),
            _b("busy", "toggle", "MOU review - A1000", has_children=True, edited="2026-05-01"),
            _b("info", "heading_1", "INFO", has_children=True),
        ],
        "r11949": [_b("c1", "paragraph", "A1000 -review again to make sure all is covered.")],
        "r11959": [_b("unitg", "toggle", "Unit G - A1000")],
        "r11969": [_b("parental", "toggle", "Parental Leave changes A1000 - Due 7/1/26")],
        "archive": [_b("secret", "toggle", "A1000 OLD archived - Due 1/1/26 - must NOT appear")],
        "archived": [_b("secret2", "toggle", "Old event B1000 - due 9/30/26 - must NOT appear")],
        "donepar": [_b("under", "toggle", "A1000 child of done - Due 7/1/26")],
        "busy": [_b("fresh", "paragraph", "note added recently", edited=date.today().isoformat())],
        "info": [_b("ref", "paragraph", "A1000 send forms to payroll 6/1/26 - Due 6/2/26")],
    }
    items = []
    walk(MockClient(tree), "root", [], items)
    a = analyze(items)
    print(build_report(a))
    by = {i["text"]: i for i in items}
    texts = list(by)
    assert not any("must NOT appear" in t for t in texts), "Archive/Archived not pruned!"
    assert any(i["due_confirmed"] for i in items), "no confirmed due found!"
    assert not any(i["text"].startswith("6/21") and i["due_confirmed"] for i in items), \
        "bare created-date wrongly confirmed as due!"
    two = by["Config change from vendor 6/22/26 - A1000 - paperwork due 8/10/26"]
    assert two["due"] == date(2026, 8, 10), f"took the wrong date: {two['due']}"
    assert by["Portal systemdue 8/24/26 - A1000"]["due_confirmed"], "'systemdue' not confirmed"
    assert not by["DO FULL DOCUMENTATION UPDATE once DONE A1000 Due 10/1/26"]["done"], \
        "'once DONE' wrongly treated as done"
    assert by["A1000 child of done - Due 7/1/26"]["done"], "child of a done item not done"
    open_items = a["overdue"] + a["due_soon"] + a["lingering"] + [i for v in a["priorities"].values() for i in v]
    assert not any(i["done"] or i["reference"] for i in open_items), "done/reference item in triage!"
    assert by["MOU review - A1000"] not in a["lingering"], "recent child edit ignored for lingering"
    assert by["Union contracts"] not in a["lingering"], "untagged heading counted as lingering"
    print("\n[selftest] OK — Archive/Archived pruned, due date after 'due' wins, done markers "
          "honored, INFO kept out of triage, lingering uses subtree activity.")

# ─── CLI (single source; for pi2 later) ──────────────────────────────────────

def resolve_source(args):
    if args.source:
        s = args.source.upper()
        return os.environ.get(f"NOTION_TOKEN_{s}"), (args.page or os.environ.get(f"NOTION_PAGE_{s}"))
    return os.environ.get("NOTION_TOKEN"), (args.page or os.environ.get("NOTION_ROOT_PAGE_ID"))

def main():
    ap = argparse.ArgumentParser(description="Read-only Notion triage (single source).")
    ap.add_argument("--source", help="named source, e.g. WORKLL (reads NOTION_TOKEN_WORKLL / NOTION_PAGE_WORKLL)")
    ap.add_argument("--page", help="page id (overrides source's page)")
    ap.add_argument("--selftest", action="store_true", help="offline demo, no network")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    token, page = resolve_source(args)
    if not token or not page:
        ap.error("need a token and a page (via --source NAME, or NOTION_TOKEN + --page)")
    items = []
    walk(NotionClient(token), page, [], items)
    print(build_report(analyze(items)))

if __name__ == "__main__":
    main()