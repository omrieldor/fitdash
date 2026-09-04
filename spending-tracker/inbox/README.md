# Photo-import inbox — protocol notes

This directory is the transport for the "post a photo in Claude chat and it
shows up in the app" feature. **Any Claude session working on this repo should
follow this protocol when the user posts a photo of a receipt, salary slip,
bank screenshot or similar and asks to log it.**

## Why it exists

The assistant's sandbox cannot reach the user's server directly, but it can
push to this repository, and the server's deploy webhook pulls and restarts the
service on every push to master. On boot, `inbox_ingest.py` decrypts and files
anything new in here. The repository is public, so payloads are encrypted —
committing readable financial data here is never acceptable.

## Assistant-side steps (per photo)

1. Read the photo. Extract entries; amounts are positive magnitudes, direction
   goes in `type`. Dates `YYYY-MM-DD` — if the photo shows no year, assume the
   most recent plausible one; if no date at all, use today. Write
   `entries.json`:

   ```json
   {
     "note": "what the photo was",
     "entries": [
       {"date": "2026-08-01", "merchant": "משכורת אוגוסט", "amount": 28500,
        "type": "income", "card": null, "account": null, "category": "Salary"},
       {"date": "2026-08-03", "merchant": "ארומה", "amount": 38,
        "type": "expense", "card": "Visa •1234", "account": "Isracard",
        "category": null}
     ]
   }
   ```

   Set `category` only when confident (it must name an existing child category
   — see `CATEGORY_TREE` in `models.py`); it is trusted like a manual
   correction. Leave it `null` to let the server's keyword rules decide.

   **Always set `account`, and ask the user for the name if you don't know it.**
   This is the field that decides whether duplicate protection works at all, and
   getting it wrong is silent — see the warning below.

### `account` decides whether dedup can see anything

`compute_dedup_hash` takes `account_id` as its **first component**, and the
ingest only looks for a collision *within the same account*:

```python
dedup_hash = compute_dedup_hash(account.id, txn_date, signed, merchant_raw)
Transaction.query.filter_by(account_id=account.id, dedup_hash=dedup_hash)
```

`account` is matched case-insensitively against the user's account names, and
anything that doesn't match — **including omitting the field** — falls back to
an auto-created account called "Photo Imports". Entries parked there can never
collide with the same charges imported from a statement, because those live in
the user's real card account and therefore hash differently. Dedup does not
fail loudly in that case; it simply never fires, and the user ends up with two
of every charge, split across two accounts, with correct-looking per-account
totals and doubled category totals.

This has actually happened: a session logged a card's whole billing cycle from
screenshots without setting `account`, and every charge the user had already
imported from that cycle's statement was silently duplicated.

So: name the account the way the user's app does, matching the card the charges
were made on. `card` is only a display label on the transaction — it plays no
part in dedup and does not route the entry anywhere.

Even with the right account, dedup still needs `merchant` to match the stored
string exactly (compared stripped and lowercased). Card apps truncate long
merchant names on screen, so a name copied from a phone screenshot may not match
what the statement's CSV recorded. Treat dedup as a backstop, not a guarantee:
when charges may already have been imported from a statement, ask the user
rather than re-sending the period and relying on the hash to sort it out.

2. `cryptography` may be broken in the system Python of the sandbox — use a
   venv: `python3 -m venv venv && venv/bin/pip install cryptography`.

3. `venv/bin/python tools/pack_inbox.py entries.json` → writes `inbox/<ts>.enc`.

4. Commit **only the .enc file** (never entries.json) and push to master.

5. The webhook deploys; entries appear in the app in about a minute. If the
   user hasn't registered the webhook, they land on the next update/restart —
   tell the user which of the two applies.

## Server side

- `INBOX_PRIVATE_KEY` (base64 PKCS8 DER) must be in `.env`. Without it, ingest
  is silently skipped.
- `ProcessedInbox` records ingested filenames; re-pulls never double-import,
  and the standard dedup hash guards a second time.
- Entries with no matching `account` name go to an auto-created account named
  "Photo Imports".

## Key management

`public_key.pem` here encrypts; the private key lives only in the server's
`.env` and the user's own records. Rotating the pair means regenerating both
halves together — old .enc files become undecryptable, so delete them first
(they'll already be ingested).
