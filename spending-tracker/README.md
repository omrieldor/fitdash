# SpendTrack

A personal spending tracker, separate from the fitness app in this repo. Flask +
SQLite, installed to the iPhone Home Screen as a PWA. All amounts in shekels.

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
| Food | Groceries, Restaurants, Cafés, Food Delivery |
| Car | Fuel, Car Insurance, Car Maintenance, Parking, Tolls, Car Payment |
| Home | Rent / Mortgage, Utilities, Internet & TV, Home Maintenance, Furniture & Appliances, Home Insurance |
| Cash | Cash Withdrawal |
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

## Enabling the monthly reminder

Push **requires HTTPS** — iOS only delivers it to a Home Screen PWA over TLS.

1. `bash deploy/add-ssl.sh spend.example.com`
2. Open the HTTPS URL in Safari → Share → **Add to Home Screen**
3. Launch from the Home Screen icon (not from Safari) and tap 🔔

A systemd timer ticks hourly; `send_reminders.py` fires on the 8th at 20:00
Israel time, catching up through the 10th if the server was down, and records
each send so it never double-notifies. DST is handled via `Asia/Jerusalem`.

Send one on demand: `venv/bin/python send_reminders.py --force`

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
