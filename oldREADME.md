# notion-triage

Turns a Notion task tree into a prioritized action surface. A read-only walker
reads the page tree and surfaces due dates and weighted priorities; an LLM Ask
panel reasons over the live tree. Served as a Streamlit dashboard, with two
sources (WORKLL, OPA) as tabs.

**Read-only by design.** `notion_walker.py` only ever issues HTTP GET. It cannot
edit, move, or delete anything in Notion, and cannot change the tree structure.

## Files
- `notion_walker.py` — stdlib engine: walk + deterministic triage. Importable, and
  runnable standalone (`--source WORKLL`, `--selftest`) for pi2 cron later.
- `app.py` — Streamlit UI: per-source dashboard + Ask panel.
- `.streamlit/secrets.toml.example` — copy to `secrets.toml` (gitignored) or paste
  into Streamlit Cloud's Secrets box.

## Local run
```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then fill it in
streamlit run app.py
```
(Locally you can also use a `.env` instead of secrets.toml — the walker auto-loads it.)

## Two Notion accounts
WORKLL and OPA are different Notion accounts. Each needs its **own** internal
connection created *in that account's workspace*, with the target page shared to
it (page ••• → Connections → Add). Then put both tokens + page ids in secrets.

## Quick engine check (no network)
```bash
python3 notion_walker.py --selftest
```