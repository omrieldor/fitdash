"""Assistant-side packer for the photo-import inbox.

Takes a JSON file of entries extracted from a photo, hybrid-encrypts it
(RSA-OAEP wrapping an AES-256-GCM key) with the repo's public key, and writes
spending-tracker/inbox/<utc-timestamp>.enc ready to commit. The repo is public,
so nothing readable may land in it.

    python tools/pack_inbox.py entries.json

entries.json format:
{
  "note": "salary slip photo, August",
  "entries": [
    {"date": "2026-08-01", "merchant": "משכורת", "amount": 28500.0,
     "type": "income", "card": null, "category": "Salary"},
    {"date": "2026-08-03", "merchant": "ארומה", "amount": 38.0,
     "type": "expense", "card": "Visa •1234", "category": null}
  ]
}

"category" is optional — when present it must name an existing child category
and is treated as a manual (trusted) classification; when null the server's
keyword rules decide.
"""

import base64
import json
import os
import sys
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HERE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(HERE, '..', 'inbox')
PUBLIC_KEY = os.path.join(INBOX, 'public_key.pem')


def pack(payload: dict) -> str:
    for e in payload.get('entries', []):
        datetime.strptime(e['date'], '%Y-%m-%d')
        assert e['type'] in ('expense', 'income'), e
        assert float(e['amount']) > 0, 'amounts are magnitudes; use type for direction'

    public_key = serialization.load_pem_public_key(open(PUBLIC_KEY, 'rb').read())
    aes_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ciphertext = AESGCM(aes_key).encrypt(nonce, json.dumps(payload).encode(), None)
    wrapped = public_key.encrypt(aes_key, padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()),
        algorithm=hashes.SHA256(), label=None))

    envelope = json.dumps({
        'v': 1,
        'key': base64.b64encode(wrapped).decode(),
        'nonce': base64.b64encode(nonce).decode(),
        'data': base64.b64encode(ciphertext).decode(),
    })
    name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.enc'
    path = os.path.join(INBOX, name)
    with open(path, 'w') as f:
        f.write(envelope)
    return path


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit('usage: python tools/pack_inbox.py entries.json')
    # Read as UTF-8 explicitly. Merchant names here are usually Hebrew, and
    # open() without an encoding uses the platform default -- cp1255 on a
    # Windows box -- which raises UnicodeDecodeError on the very payloads this
    # tool exists to pack, including the Hebrew example in the docstring above.
    with open(sys.argv[1], encoding='utf-8') as f:
        payload = json.load(f)
    print('wrote', pack(payload))
