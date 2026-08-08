# fitdash — repo guide for Claude Code

One public repo, **two fully independent apps**, one Oracle Cloud VM.

| | Fitness app ("Path to Eldorado") | Spending app ("SpendTrack") |
|---|---|---|
| Code | repo root (`app.py`, `models.py`, …) | `spending-tracker/` |
| systemd service | `eldorado` | `spendtrack` |
| gunicorn port | 127.0.0.1:5000 | 127.0.0.1:5001 |
| Database | `instance/dashboard.db` | `spending-tracker/instance/spending.db` |
| Public URL | server IP via nginx | https://financecop.duckdns.org |

**Hard rule: a change to one app must never touch the other's files.** Every PR
so far has verified its diff stays inside its own directory — keep it that way.

## Telling the two apps apart (read this before editing)

The fitness app is at the **repo root**, so bare paths like `app.py`,
`models.py`, or `templates/index.html` **silently resolve to it**. These 15
files exist at the same relative path in *both* apps:

`app.py` · `models.py` · `requirements.txt` · `.gitignore` ·
`templates/index.html` · `templates/login.html` · `static/style.css` ·
`static/sw.js` · `static/manifest.json` · `static/icon-{180,512}.png` ·
`deploy/{setup-server,update-app,backup-db,add-ssl}.sh`

A repo-wide search for something like `def login` returns `./app.py` **and**
`./spending-tracker/app.py`. Always check which path a result came from, and
write `spending-tracker/...` explicitly when you mean that app.

Two guardrails exist:

```bash
bash tools/which-app.sh     # which app does my diff touch? where does it deploy?
```

and a **pre-commit hook that blocks any commit spanning both apps**. Enable it
once per clone (it is not active by default in a fresh clone):

```bash
git config core.hooksPath .githooks
```

`spending-tracker/CLAUDE.md` carries the same warning for sessions that start
inside that directory.

## Where the real documentation lives

- `spending-tracker/README.md` — SpendTrack's full docs, including the
  **iOS push-notification checklist** (six conditions that must all hold; the
  failure mode of getting any wrong is a silently dead 🔔 button) and the
  billing-cycle / Library design.
- `spending-tracker/inbox/README.md` — the **photo-import protocol**. If the
  user posts a photo of a receipt / salary slip / bank screenshot and asks to
  log it, follow that file exactly. Never commit readable financial data —
  the repo is public; entries travel only as encrypted `.enc` files.

## SpendTrack module map

- `app.py` — routes, auth, billing-cycle window (`_visible_window`), lazy
  archiver, Library endpoints, deploy webhook (`/deploy`), boot-time inbox ingest
- `models.py` — schema + seeded category tree (`CATEGORY_TREE`); no-Alembic
  migrations: new tables via `create_all()`, new columns via
  `add_missing_columns` in `app.py`
- `spending_import.py` — CSV/XLS/XLSX/HTML-as-XLS statement parsing, Hebrew
  handling, merchant normalisation, keyword rules, card detection
- `market_data.py` — Twelve Data quotes/FX, cached in `PriceCache`
- `push.py`, `send_reminders.py` — web push + the 8th-at-20:00 reminder timer
- `inbox_ingest.py`, `tools/pack_inbox.py` — photo-import decrypt/encrypt ends

## Domain facts that keep biting

- **Billing cycle is the 8th → 7th**, not calendar months. The homepage window
  spans the **two** most recent cycles; older ones archive into `CycleSummary`
  rows for the Library. Transaction detail is **retained** — summaries are
  recomputed from the live rows on every `/api/summary`, so they are a cache,
  not the only copy. (This is a reversal: archiving used to delete the rows.
  Summaries predating the change may cover cycles whose detail is genuinely
  gone, and the archiver skips those rather than recomputing them to zero.)
- **Hebrew statements** embed bidi control marks inside date/amount cells and
  use two-digit years (`02.07.25`). `_clean()` and the `DATE_FORMATS` ordering
  in `spending_import.py` exist because of real zero-row import bugs — don't
  simplify them away.
- Isracard exports put junk rows above the real header and split cards/foreign
  currency into sections and sheets; header detection is score-based, card
  attribution is sticky per section.

## Deployment

- Server: `/home/ubuntu/fitdash` on an Oracle Always-Free VM, nginx in front.
- **Push to master auto-deploys SpendTrack** via its `/deploy` webhook
  (HMAC-signed; secret in the server's `.env`). Manual fallback:
  `bash spending-tracker/deploy/update-app.sh` on the server.
- The server's `spending-tracker/.env` holds `SECRET_KEY`, `VAPID_*` (push),
  `INBOX_PRIVATE_KEY` (photo import), `MARKET_DATA_API_KEY`, `DEPLOY_SECRET`.
  Never committed. Never regenerate VAPID or inbox keys casually — that breaks
  the phone's push subscription / undecryptable inbox files respectively.

## Conventions

- Branch → PR → **squash-merge to master** (master is what deploys).
- Bump the service-worker cache name and `style.css?v=` on any UI change, or
  the installed iPhone PWA keeps serving stale assets.
- Single-user app: registration locks after the first account; keep it that way.
- All amounts ILS; `Transaction.amount` is a magnitude, direction in `txn_type`.
