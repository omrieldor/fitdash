"""Repair photo-import entries that duplicate statement-imported transactions.

Photo-import entries land in the auto-created "Photo Imports" account whenever
the payload omits `account` (see inbox/README.md). The dedup hash is computed
from account_id, so those entries can never collide with the same charges
imported from a statement into the real card account -- they import a second
time, silently, and category totals double while each account's own total still
looks right.

This finds the damage and optionally repairs it. For every transaction in the
Photo Imports account it looks for a twin in the user's other accounts -- same
date, same amount, same direction:

  twin found     -> the photo copy is a duplicate, delete it
  no twin        -> the photo copy is the only record, keep it, and move it to
                    the real account when --map says where it belongs

Moving matters as much as deleting: an entry left in Photo Imports duplicates
all over again the next time a statement covering it is imported.

Dry run by default; nothing is written without --apply.

    venv/bin/python tools/fix_photo_import_dupes.py
    venv/bin/python tools/fix_photo_import_dupes.py --map "Mastercard •7322=Isracard"
    venv/bin/python tools/fix_photo_import_dupes.py --map "Mastercard •7322=Isracard" --apply

Twins are matched on date+amount+direction rather than on the merchant string,
because a merchant name transcribed from a phone screenshot is often truncated
("תיירות מרום גולן בע"") and would not match what the statement's CSV recorded.
That is deliberately loose: two genuinely separate charges of the same amount,
at the same merchant, on the same day would look like a duplicate. Read the dry
run before applying.
"""

import argparse
import os
import sys

from flask import Flask

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from models import db, User, Account, Transaction  # noqa: E402
from spending_import import compute_dedup_hash  # noqa: E402

PHOTO_ACCOUNT_NAME = 'Photo Imports'


def make_app():
    app = Flask(__name__, instance_path=os.path.join(ROOT, 'instance'))
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///spending.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def plan(user, mapping):
    """Work out what to delete and what to move. Returns (deletes, moves, stays)."""
    photo = Account.query.filter_by(user_id=user.id, name=PHOTO_ACCOUNT_NAME).first()
    if photo is None:
        return None, None, None

    others = {a.id: a for a in Account.query.filter_by(user_id=user.id) if a.id != photo.id}
    by_name = {a.name.lower(): a for a in others.values()}

    deletes, moves, stays = [], [], []
    for txn in Transaction.query.filter_by(account_id=photo.id).order_by(Transaction.date).all():
        twin = (Transaction.query
                .filter(Transaction.user_id == user.id,
                        Transaction.account_id != photo.id,
                        Transaction.date == txn.date,
                        Transaction.amount == txn.amount,
                        Transaction.txn_type == txn.txn_type)
                .first())
        if twin is not None:
            deletes.append((txn, others.get(twin.account_id)))
            continue
        target = by_name.get((mapping.get(txn.card_label) or '').lower()) if txn.card_label else None
        (moves if target is not None else stays).append((txn, target))
    return deletes, moves, stays


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='write the changes (default: dry run)')
    ap.add_argument('--map', action='append', default=[], metavar='CARD=ACCOUNT',
                    help='move surviving entries with this card_label into this account')
    args = ap.parse_args()

    mapping = {}
    for item in args.map:
        if '=' not in item:
            sys.exit(f'--map needs CARD=ACCOUNT, got {item!r}')
        card, account = item.split('=', 1)
        mapping[card.strip()] = account.strip()

    app = make_app()
    with app.app_context():
        print(f'database: {db.engine.url}\n')
        user = User.query.first()
        if user is None:
            sys.exit('no user registered')

        deletes, moves, stays = plan(user, mapping)
        if deletes is None:
            sys.exit(f'no "{PHOTO_ACCOUNT_NAME}" account — nothing to repair')

        print(f'DUPLICATES to delete ({len(deletes)}):')
        for txn, twin_account in deletes:
            where = twin_account.name if twin_account else '?'
            print(f'  {txn.date}  {txn.amount:>10,.2f}  {txn.merchant_raw[:32]:32s} (twin in {where})')
        print(f'  subtotal: {sum(t.amount for t, _ in deletes):,.2f}\n')

        print(f'KEEP and move ({len(moves)}):')
        for txn, target in moves:
            print(f'  {txn.date}  {txn.amount:>10,.2f}  {txn.merchant_raw[:32]:32s} -> {target.name}')
        print(f'  subtotal: {sum(t.amount for t, _ in moves):,.2f}\n')

        print(f'KEEP where they are ({len(stays)}):')
        for txn, _ in stays:
            label = txn.card_label or '(no card)'
            print(f'  {txn.date}  {txn.amount:>10,.2f}  {txn.merchant_raw[:32]:32s} [{label}]')
        print(f'  subtotal: {sum(t.amount for t, _ in stays):,.2f}\n')

        if not args.apply:
            print('DRY RUN — nothing written. Re-run with --apply to commit.')
            return

        for txn, _ in deletes:
            db.session.delete(txn)
        for txn, target in moves:
            txn.account_id = target.id
            # The hash is account-scoped, so a moved row must be re-keyed or it
            # would not dedup against future imports into its new home either.
            signed = -txn.amount if txn.txn_type == 'income' else txn.amount
            txn.dedup_hash = compute_dedup_hash(target.id, txn.date, signed, txn.merchant_raw)
        db.session.commit()
        print(f'APPLIED: deleted {len(deletes)}, moved {len(moves)}, left {len(stays)}.')


if __name__ == '__main__':
    main()
