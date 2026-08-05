# SpendTrack

A personal spending tracker, separate from the fitness app in this repo. Flask +
SQLite, installed to the iPhone Home Screen as a PWA. All amounts in shekels.

## Billing cycles and the Library

The app runs on the card's billing cycle — the 8th of one month through the 7th
of the next — not calendar months. The homepage shows the window opening at the
most recently completed cycle, so the statement uploaded on the 8th stays front
and centre until the following 7th. Everything older is archived automatically
(lazily, on the first request after a 7th passes) into the **Library**: a
read-only record per month with income vs spending, the category breakdown and
top merchants. Per the owner's choice, archived transaction detail is
permanently deleted — only the summary and the dedup hashes survive, so
re-uploading an old statement still imports nothing.

## Photo import ("post a photo in Claude chat")

Post a photo of a salary slip, receipt or bank screenshot in a Claude chat
session on this repo and ask to log it. The assistant extracts the entries,
encrypts them with `inbox/public_key.pem` and pushes an `.enc` file; the deploy
webhook restarts the service, which decrypts with `INBOX_PRIVATE_KEY` from
`.env` and files the entries. Protocol details: `inbox/README.md`.

## Card attribution

Each imported transaction records which card paid for it (Visa •1234, Amex,
חבר טעמים, חבר של קבע…), detected automatically from a card column, the
statement's per-card section headings, or the account name — shown as a chip
in All Transactions.

## What it does

- **Statement import.** Upload a CSV export from your bank or card. The first
  upload per account asks which columns are date / amount / merchant; after that
  it remembers. Re-uploading an overlapping date range is safe — duplicates are
  detected and skipped.
- **Auto-categorisation.** Merchants are matched against keyword rules (seeded
  with common Israeli chains — Shufersal, Paz, Wolt, Super Pharm, Bezeq, …) and
  sorted into a two-level category tree. Correcting one transaction creates a
  rule and retroactively fixes every other transaction from that merchant, so
  each month needs less cleanup than the last.
- **Income tracking.** Credits on the statement are recorded as income and split
  by source (Salary, Bonus, Freelance, Dividends, …).
- **Net worth.** Manually-updated balances for bank accounts, emergency fund and
  debts, plus the live portfolio value.
- **Portfolio.** Log holdings once a month; prices refresh automatically between
  logs. Performance is compared against the S&P 500 using chain-linked Modified
  Dietz, so deposits don't get counted as returns.
- **Monthly reminder.** Push notification on the 8th at 20:00 Israel time.

## Categories

Two levels — a group and its children:

| Group | Children |
|---|---|
| Income | Salary, Bonus, Freelance, Dividends, Interest, Refunds, Other Income |
| Food | Groceries, Restaurants, Cafés, Food Delivery, Snacks, Bars |
| Car | Fuel, Car Insurance, Car Maintenance, Parking, Tolls, Car Payment |
| Home | Rent / Mortgage, Utilities, Internet & TV, Home Maintenance, Furniture & Appliances, Home Insurance |
| Cash | Cash Withdrawal |
| Friends | Gifts, Money Lent, Bill Split |
| Transport | Public Transport, Taxi & Rideshare |
| Shopping | Clothing, Electronics, General Shopping |
| Health | Pharmacy, Doctor & Dental, Gym & Fitness, Health Insurance |
| Entertainment | Streaming & Subscriptions, Events & Nightlife, Hobbies |
| Travel | Flights, Hotels, Vacation |
| Financial | Bank Fees, Taxes, Loan Payment |
| Other | Uncategorized, Misc |

Add your own children under any group from the API (`POST /categories` with a
`parent_id`).

## Deploying

Runs as its own systemd service on the same VM as the fitness app, on port 5001
behind nginx.

```bash
bash deploy/setup-server.sh              # first time
bash deploy/add-ssl.sh spend.example.com # then add a domain + HTTPS
bash deploy/update-app.sh                # after each git push
bash deploy/backup-db.sh                 # timestamped DB copy
```

`setup-server.sh` writes `.env` with a generated `SECRET_KEY` and VAPID keypair.
It never overwrites an existing `.env` — regenerating the VAPID keys would
invalidate the subscription already on your phone.

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Flask session signing. Generated at setup. |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | Web push. Generated at setup; keep stable. |
| `VAPID_SUBJECT` | Contact `mailto:` for push services. |
| `MARKET_DATA_API_KEY` | [Twelve Data](https://twelvedata.com) key for live prices. Optional — without it the portfolio falls back to the values you typed. |
| `INBOX_PRIVATE_KEY` | Decrypts photo-import inbox files (base64 PKCS8 DER). Without it, photo import is silently skipped. |

## Enabling the monthly reminder

**This is the configuration that actually works on iOS.** It took several
attempts; the failure mode of every wrong variant was the 🔔 button doing
absolutely nothing — no prompt, no error — so the notes below record what the
working setup is and why each piece is required.

### The six things that must all be true

1. **Served over HTTPS.** iOS only delivers web push to a PWA over TLS.
   `bash deploy/add-ssl.sh yourdomain` (a free DuckDNS subdomain is fine).
   Point the domain at the server's **public IP** — pointing it at the laptop's
   IP makes certbot fail with "Timeout during connect".
2. **Installed to the Home Screen.** Open the HTTPS URL in **Safari** →
   Share → Add to Home Screen. Push does not work from a Safari tab; the
   `PushManager` API is absent there.
3. **Launched from the Home Screen icon**, not from Safari. Subscribing from a
   browser tab silently produces nothing.
4. **The service worker must be served from `/sw.js`, not `/static/sw.js`.**
   This was the actual bug. A worker at `/static/sw.js` gets scope `/static/`,
   which does not cover the dashboard at `/`, so it never controls the page and
   `navigator.serviceWorker.ready` never resolves — the handler hangs forever
   with no output. `app.py` serves it from the root with a
   `Service-Worker-Allowed: /` header.
5. **`Notification.requestPermission()` must be called before any `await`.**
   Safari discards the tap's user activation across an async call and then
   refuses to show the prompt. Request permission first, then fetch the key.
6. **VAPID keys in `.env`, loaded by systemd.** `setup-server.sh` generates them
   with the **venv** python (the system python may lack a working `cryptography`)
   and the service unit loads them via `EnvironmentFile=`.

### What success looks like

Tapping 🔔 shows an iOS permission prompt, then this toast:

```
Reminders on — 8th of each month, 20:00
```

Confirm the subscription reached the server:

```bash
cd /home/ubuntu/fitdash/spending-tracker
venv/bin/python3 -c "from app import app; from models import PushSubscription; \
  app.app_context().push(); print(PushSubscription.query.count())"
```

`1` (or more) means it registered. `0` means it didn't — recheck items 3–5.

### Sending a test notification

`send_reminders.py` reads VAPID keys from the environment, so `.env` has to be
sourced first. Without it the script prints *"VAPID keys not configured"* even
though the keys exist:

```bash
cd /home/ubuntu/fitdash/spending-tracker && set -a && source .env && set +a && \
  venv/bin/python send_reminders.py --force
```

Expected output: `Reminders sent: 1`, and the phone buzzes. The scheduled run
does not need this because systemd supplies the environment.

### How the schedule works

A systemd timer ticks hourly; `send_reminders.py` fires on the 8th at 20:00
Israel time, catching up through the 10th if the server was down, and records
each send in `ReminderLog` so it never double-notifies. DST is handled via
`Asia/Jerusalem` — 17:00 UTC in summer, 18:00 in winter.

## Local development

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
SECRET_KEY=dev python3 app.py            # http://127.0.0.1:5001
SECRET_KEY=dev python3 run_dev_stubbed.py # same, with fake market data
```

`run_dev_stubbed.py` serves synthetic quotes so the portfolio and benchmark
charts can be exercised without an API key. Development only — production runs
gunicorn against `app:app`.

## Layout

| File | Role |
|---|---|
| `app.py` | Routes, auth, summary/net-worth/portfolio aggregation |
| `models.py` | Schema and the seeded category tree |
| `spending_import.py` | CSV parsing, merchant normalisation, dedup, categorisation rules |
| `market_data.py` | Quotes, historical closes, FX, price cache |
| `push.py` / `send_reminders.py` | Web push and the monthly schedule |
| `templates/`, `static/` | Dashboard, styles, service worker, PWA manifest |

Schema changes follow the same no-Alembic approach as the fitness app: new
tables come from `db.create_all()`, new columns from the `add_missing_columns`
block at the top of `app.py`.
