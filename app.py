"""
app.py — Streamlit dashboard over Notion task trees (read-only).

Two sources (WORKLL, OPA), each its own tab: a deterministic triage dashboard
(due dates, weighted priorities, lingering items) plus an "Ask" panel that lets
an LLM reason over that source's live tree.

Credentials come from Streamlit secrets in the cloud, or .env / env vars locally.
Set in .streamlit/secrets.toml (see secrets.toml.example) or the Streamlit Cloud
"Secrets" UI:

  NOTION_TOKEN_WORKLL, NOTION_PAGE_WORKLL
  NOTION_TOKEN_OPA,   NOTION_PAGE_OPA
  ANTHROPIC_API_KEY            # enables the Ask panel
  ASK_MODEL                    # optional, defaults below

Nothing here writes to Notion — the walker is GET-only.
"""

import os
import streamlit as st

from notion_walker import NotionClient, walk, analyze, crumb, TIERS, DUE_SOON_DAYS, STALE_DAYS

# ─── Sources ─────────────────────────────────────────────────────────────────

SOURCES = [
    {"name": "WORKLL", "token_key": "NOTION_TOKEN_WORKLL", "page_key": "NOTION_PAGE_WORKLL"},
    {"name": "OPA",   "token_key": "NOTION_TOKEN_OPA",   "page_key": "NOTION_PAGE_OPA"},
]

DEFAULT_ASK_MODEL = "claude-sonnet-4-6"  # bump to a higher tier (e.g. an Opus) anytime

# ─── Secret/env resolution ───────────────────────────────────────────────────

def secret(key, default=None):
    """Streamlit secrets first (cloud), then env / .env (local)."""
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.environ.get(key, default)

# ─── Data loading (cached network walk) ──────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def load_items(name, token, page_id):
    """Cached raw walk. Keyed by (name, token, page_id); TTL 5 min so the
    dashboard doesn't re-hit Notion on every widget interaction."""
    items = []
    walk(NotionClient(token), page_id, [], items)
    return items

# ─── Ask panel (LLM over the tree) ───────────────────────────────────────────

def tree_digest(items, cap=500):
    """Compact, model-friendly rendering of the tree for the Ask panel."""
    lines = []
    for it in items[:cap]:
        tags = []
        if it.get("priority"):
            tags.append(it["priority"][0])
        if it.get("due"):
            tags.append(("DUE " if it.get("due_confirmed") else "dated ") + str(it["due"]))
        if it.get("age_days") is not None:
            tags.append(f"{it['age_days']}d idle")
        if it.get("checked"):
            tags.append("done")
        meta = f" ({', '.join(tags)})" if tags else ""
        lines.append(f"[{crumb(it)}] {it['text']}{meta}")
    if len(items) > cap:
        lines.append(f"... (+{len(items) - cap} more items truncated)")
    return "\n".join(lines)

def ask_llm(question, digest, source_name, history, model, api_key):
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)
    system = (
        f"You are a task-triage analyst for the '{source_name}' Notion workspace. "
        "Answer ONLY from the task list below; if something isn't in it, say so. "
        "Be concise and cite the tree path in brackets when you reference a task. "
        "When asked what to prioritize, reason over confirmed due dates, the priority "
        f"tiers ({' > '.join(l for l, _ in TIERS)}, first is highest), and how long "
        "items have sat idle. You are READ-ONLY: never claim to have changed anything "
        f"in Notion.\n\nCURRENT TASK LIST for {source_name}:\n{digest}"
    )
    msgs = [{"role": m["role"], "content": m["content"]} for m in history]
    msgs.append({"role": "user", "content": question})
    resp = client.messages.create(model=model, max_tokens=1200, system=system, messages=msgs)
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

# ─── Rendering ───────────────────────────────────────────────────────────────

def rows(items, extra):
    return [{"Task": it["text"], "Where": crumb(it), "": extra(it)} for it in items]

def dashboard(name, a):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Overdue", len(a["overdue"]))
    c2.metric(f"Due ≤{DUE_SOON_DAYS}d", len(a["due_soon"]))
    c3.metric("Lingering", len(a["lingering"]))
    c4.metric("Scanned", a["total"])

    today = a["today"]
    if a["overdue"]:
        st.markdown("#### 🔴 Overdue")
        st.dataframe(rows(a["overdue"], lambda it: f"{(today-it['due']).days}d overdue · {it['due']}"),
                     use_container_width=True, hide_index=True)
    if a["due_soon"]:
        st.markdown(f"#### 🟡 Due soon (next {DUE_SOON_DAYS}d)")
        st.dataframe(rows(a["due_soon"], lambda it: f"in {(it['due']-today).days}d · {it['due']}"),
                     use_container_width=True, hide_index=True)

    st.markdown("#### ⭐ Priorities")
    ptabs = st.tabs([f"{label} ({len(a['priorities'].get(label, []))})" for label, _ in TIERS])
    for (label, _), t in zip(TIERS, ptabs):
        items = a["priorities"].get(label, [])
        with t:
            if items:
                st.dataframe(rows(items, lambda it: str(it["due"]) if it["due"] else "—"),
                             use_container_width=True, hide_index=True)
            else:
                st.caption("Nothing in this tier.")

    if a["lingering"]:
        st.markdown(f"#### 🐌 Lingering (untouched ≥ {STALE_DAYS}d)")
        st.dataframe(rows(a["lingering"], lambda it: f"{it['age_days']}d idle"),
                     use_container_width=True, hide_index=True)

    if a["dated"]:
        with st.expander(f"📅 Dated but unconfirmed — no “due” word ({len(a['dated'])})"):
            st.caption("A date appears in the text but without a due-signal, so it's likely a "
                       "created/logged date rather than a deadline. Shown separately on purpose.")
            st.dataframe(rows(a["dated"], lambda it: str(it["due"])),
                         use_container_width=True, hide_index=True)

def ask_panel(name, items):
    st.markdown("#### 💬 Ask")
    api_key = secret("ANTHROPIC_API_KEY")
    model = secret("ASK_MODEL", DEFAULT_ASK_MODEL)
    if not api_key:
        st.info("Set `ANTHROPIC_API_KEY` in secrets to enable the Ask panel.")
        return

    key = f"msgs_{name}"
    history = st.session_state.setdefault(key, [])
    cols = st.columns([1, 5])
    if cols[0].button("Clear", key=f"clear_{name}"):
        st.session_state[key] = []
        st.rerun()
    cols[1].caption(f"Reasoning with **{model}** over {len(items)} live {name} items · read-only")

    for m in history:
        st.chat_message(m["role"]).markdown(m["content"])

    if q := st.chat_input(f"Ask about {name} — e.g. “what should I focus on before Friday?”",
                          key=f"in_{name}"):
        history.append({"role": "user", "content": q})
        st.chat_message("user").markdown(q)
        with st.chat_message("assistant"):
            with st.spinner("Reasoning over the tree…"):
                try:
                    ans = ask_llm(q, tree_digest(items), name, history[:-1], model, api_key)
                except Exception as e:
                    ans = f"⚠️ Ask failed: {e}"
                st.markdown(ans)
        history.append({"role": "assistant", "content": ans})

# ─── App ─────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Notion Triage", page_icon="🌳", layout="wide")
st.title("🌳 Notion Triage")
if st.button("↻ Refresh from Notion"):
    st.cache_data.clear()
    st.rerun()

tabs = st.tabs([s["name"] for s in SOURCES])
for src, tab in zip(SOURCES, tabs):
    with tab:
        token = secret(src["token_key"])
        page = secret(src["page_key"])
        if not token or not page:
            st.warning(f"Missing `{src['token_key']}` and/or `{src['page_key']}` in secrets. "
                       f"Add them (and share the {src['name']} page with that Notion connection) "
                       "to activate this tab.")
            continue
        try:
            items = load_items(src["name"], token, page)
        except Exception as e:
            st.error(f"Couldn't read {src['name']} from Notion: {e}\n\n"
                     "Common cause: the page isn't shared with this connection "
                     "(••• → Connections → Add), or the token/page id is wrong.")
            continue
        dashboard(src["name"], analyze(items))
        st.divider()
        ask_panel(src["name"], items)