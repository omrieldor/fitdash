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
        "type": "income", "card": null, "category": "Salary"},
       {"date": "2026-08-03", "merchant": "ארומה", "amount": 38,
        "type": "expense", "card": "Visa •1234", "category": null}
     ]
   }
   ```

   Set `category` only when confident (it must name an existing child category
   — see `CATEGORY_TREE` in `models.py`); it is trusted like a manual
   correction. Leave it `null` to let the server's keyword rules decide.

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
