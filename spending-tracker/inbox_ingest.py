"""Server-side ingest of the photo-import inbox.

The assistant (in Claude chat) reads a photo the user posts, extracts entries,
encrypts them with inbox/public_key.pem and pushes the .enc file to the repo.
The deploy webhook pulls and restarts; this module runs at boot, decrypts with
INBOX_PRIVATE_KEY from the environment, and files the entries as transactions.

ProcessedInbox records every filename ingested, so repeated pulls and restarts
never double-import — and the transaction dedup hash protects a second time.
"""

import base64
import json
import os
from datetime import datetime

from models import (db, User, Account, Category, Transaction, ProcessedInbox,
                    ArchivedDedup, get_uncategorized_category,
                    get_default_income_category, get_category)

INBOX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'inbox')
PHOTO_ACCOUNT_NAME = 'Photo Imports'


def _private_key():
    raw = os.environ.get('INBOX_PRIVATE_KEY', '').strip()
    if not raw:
        return None
    from cryptography.hazmat.primitives import serialization
    return serialization.load_der_private_key(base64.b64decode(raw), password=None)


def _decrypt(envelope_text, private_key):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    env = json.loads(envelope_text)
    aes_key = private_key.decrypt(base64.b64decode(env['key']), padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()),
        algorithm=hashes.SHA256(), label=None))
    plaintext = AESGCM(aes_key).decrypt(base64.b64decode(env['nonce']),
                                        base64.b64decode(env['data']), None)
    return json.loads(plaintext)


def _photo_account(user):
    for account in Account.query.filter_by(user_id=user.id).all():
        if account.name == PHOTO_ACCOUNT_NAME:
            return account
    account = Account(user_id=user.id, name=PHOTO_ACCOUNT_NAME, account_type='other')
    db.session.add(account)
    db.session.flush()
    return account


def _import_entries(user, payload):
    from spending_import import (normalize_merchant, compute_dedup_hash, categorize,
                                 normalize_card, seed_default_rules)

    seed_default_rules(user)
    uncategorized = get_uncategorized_category(user)
    default_income = get_default_income_category(user)

    accounts_by_name = {a.name.lower(): a for a in Account.query.filter_by(user_id=user.id)}
    imported = 0

    for entry in payload.get('entries', []):
        txn_date = datetime.strptime(entry['date'], '%Y-%m-%d').date()
        amount = abs(float(entry['amount']))
        # Normalise rather than trust: this value is authored by the assistant
        # reading a photo, and every aggregate keys off `txn_type == 'expense'`
        # as an exact string. Anything unexpected ('Expense', None, 'purchase')
        # would still list and render, but silently vanish from SPENT and the
        # donut -- a discrepancy with no visible cause.
        _raw_type = str(entry.get('type') or 'expense').strip().lower()
        txn_type = 'income' if _raw_type in ('income', 'credit', 'in', 'refund') else 'expense'
        merchant_raw = str(entry.get('merchant', '')).strip() or 'Photo entry'
        merchant_normalized = normalize_merchant(merchant_raw)

        account = accounts_by_name.get(str(entry.get('account', '')).lower()) or _photo_account(user)

        signed = -amount if txn_type == 'income' else amount
        dedup_hash = compute_dedup_hash(account.id, txn_date, signed, merchant_raw)
        if (Transaction.query.filter_by(account_id=account.id, dedup_hash=dedup_hash).first()
                or ArchivedDedup.query.filter_by(account_id=account.id, dedup_hash=dedup_hash).first()):
            continue

        # A category named by the assistant (which saw the photo) is trusted the
        # same way a hand correction is; otherwise the keyword rules decide.
        category_id, source = None, 'default'
        hint = entry.get('category')
        if hint:
            hinted = get_category(user, hint)
            if hinted and hinted.parent_id is not None:
                category_id, source = hinted.id, 'manual'
        if category_id is None:
            category_id, source = categorize(merchant_normalized, user.id, txn_type)
        if category_id is None:
            category_id = default_income.id if txn_type == 'income' else uncategorized.id

        card = entry.get('card')
        card_label = (normalize_card(card) or card) if card else None

        db.session.add(Transaction(
            user_id=user.id, account_id=account.id, category_id=category_id,
            date=txn_date, amount=amount, txn_type=txn_type,
            merchant_raw=merchant_raw, merchant_normalized=merchant_normalized,
            category_source=source, dedup_hash=dedup_hash,
            card_label=(card_label or '')[:60] or None,
            notes=payload.get('note'),
        ))
        imported += 1

    return imported


def ingest_inbox():
    """Scan inbox/*.enc and import anything new. Safe to call on every boot."""
    private_key = _private_key()
    if private_key is None:
        return 0
    if not os.path.isdir(INBOX_DIR):
        return 0
    user = User.query.first()
    if user is None:
        return 0

    total = 0
    for name in sorted(os.listdir(INBOX_DIR)):
        if not name.endswith('.enc'):
            continue
        if ProcessedInbox.query.filter_by(filename=name).first():
            continue
        try:
            payload = _decrypt(open(os.path.join(INBOX_DIR, name)).read(), private_key)
            count = _import_entries(user, payload)
        except Exception as e:
            print(f'inbox: skipping {name}: {e}')
            continue
        db.session.add(ProcessedInbox(filename=name, entry_count=count))
        db.session.commit()
        total += count
        print(f'inbox: imported {count} entries from {name}')
    return total
