# Email setup

How notion-triage sends its digest email. Gmail SMTP, stdlib only.

The master reference for Gmail on this machine is
`/home/chakravarti/agents/market-tracker/EMAIL-SETUP.md`. This file only covers
how this project uses it.

---

## Credentials: read in place, never copied

`GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` live in **one** file:

```
/home/chakravarti/agents/market-tracker/.env
```

`mailer.py` reads that file directly. **Don't put Gmail values in this
project's `.env` or `secrets.toml`.** That way there's one place to update when
the app password rotates, and only one place that can go stale.

`mailer.py` checks these locations in order and uses the first one that exists
*and* contains `GMAIL_APP_PASSWORD`:

1. `$GMAIL_ENV_FILE`, if set
2. `~/agents/market-tracker/.env` (works on both Pis)
3. `/home/chakravarti/agents/market-tracker/.env` (Pi 2)
4. `/home/rchak007/agents/market-tracker/.env` (Pi 1)

## Recipients

Set `DIGEST_TO` (comma-separated) in this project's `.streamlit/secrets.toml`
(gitignored; `.env` links to it), so addresses stay out of the public repo.

## Sending the digest

```bash
python3 digest.py --source OPA           # print only, no email
python3 digest.py --source OPA --send    # print and email
python3 digest.py --source WORKLL --send
```

The digest contains only:
- 🔴 **Overdue:** confirmed due date in the past
- 🟡 **Due soon:** confirmed due date in the next `DUE_SOON_DAYS` (default 7)
- ⭐ **Other open A-items:** anything tagged `A` + 4 digits (`A1000`, `A1010`, …)
  that isn't already listed above

Done items (DONE / ✅) and reference sections (INFO) are left out. Lingering,
dated-only and B/C items stay on the dashboard only.

Exit code is `1` if the email didn't go out, so a scheduler can retry.

---

## Gotchas (each one cost real debugging time)

1. **Strip spaces from the app password.** Google shows it as four groups of
   four, but the credential is the 16 characters with no spaces. Sending it
   with spaces gives a `535` that looks identical to a wrong password.
   `mailer.py` strips them.
2. **The file wins over `os.environ`.** A stale `GMAIL_APP_PASSWORD` exported in
   a shell would otherwise shadow the right value and give the same baffling
   `535`. `mailer.py` reads the file first, then fills gaps from the environment.
3. **`534` ≠ `535`.** `535` is a wrong credential. `534 5.7.14` is a
   suspicious-sign-in flag: the credential is fine, and signing in to the Google
   account in a browser on the same network clears it.
4. **`send()` never raises.** Gmail SMTP from a residential IP on a schedule gets
   blocked now and then. Treat `False` as "retry next run", and only mark work
   done after a successful send.
5. **Pace multiple recipients.** `mailer.py` waits 2 seconds between recipients;
   a burst counts against you in Gmail's abuse checks.

## Schedule

Scheduled in the Pi 2 crontab (`crontab -l`). The Pi is on America/Los_Angeles,
so times follow daylight saving. A failed send retries once 15 minutes later.

| Email | When | Command | Log |
|---|---|---|---|
| OPA triage | daily 9:30 AM | `digest.py --source OPA --send` | `~/.local/state/notion_digest.log` |
| STRADA TO DO | weekdays 8:00 AM | `digest.py --source STRADA --token-from OPA --page <id> --root "TO DO" --a-only --title "STRADA TO DO" --send` | `~/.local/state/notion_strada.log` |

STRADA reuses the OPA connection (`--token-from OPA`); the page just has to be
shared with it (page ••• → Connections → OPA). The page id lives only in the
local crontab.

STRADA reads **only** the subtree under the top-level "TO DO" node: the walker
lists the page's top-level blocks to find it, then never opens the other
sections. It sends A-items (`A0000`–`A9999`) plus recurring items whose weekday
is today, e.g. `due every Friday - Timesheets` under `TO DO › Recurring`.

Cron runs whatever is checked out in this folder, so keep the working copy on a
branch that has `digest.py` and `mailer.py`.

Notes:

- `cd` into the repo first, because the Notion secrets load from `./.env`
  (a link to `.streamlit/secrets.toml`):
  `cd /home/chakravarti/github/notion-triage && python3 digest.py --source OPA --send`
- Cron has almost no environment, so gotcha #2 can't happen there. It happens
  when testing in a shell with old exports.
- `today` is the machine's local date, so the Pi's timezone decides what counts
  as overdue.

## Files

| File | Purpose |
|---|---|
| `mailer.py` | Gmail SMTP sender: credential lookup, never raises |
| `digest.py` | Builds the digest (plain text + HTML) for one source and sends it |
