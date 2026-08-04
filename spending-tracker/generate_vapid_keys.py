"""Generate the VAPID keypair used for web push.

Run once, then put the two printed lines into the systemd service Environment=
entries (or your .env). Regenerating the keys invalidates existing subscriptions,
so keep them stable once devices have subscribed.

    python3 generate_vapid_keys.py
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def main():
    key = ec.generate_private_key(ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, 'big')
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )

    print('VAPID_PUBLIC_KEY=' + b64url(public_raw))
    print('VAPID_PRIVATE_KEY=' + b64url(private_raw))


if __name__ == '__main__':
    main()
