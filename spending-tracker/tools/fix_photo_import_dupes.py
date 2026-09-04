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
                    the real account for its card

Moving matters as much as deleting: an entry left in Photo Imports duplicates
all over again the next time a statement covering it is imported.

Which account a card belongs to is worked out from the data -- no need to know
or type any account names. Two signals, strongest first:

  1. the account holding the twins of that card's duplicates
  2. the account already holding other transactions with the same card_label

A card matched by neither is left alone rather than guessed at. Use --map to
override or to supply one the data cannot show.

Dry run by default; nothing is written without --apply.

    venv/bin/python tools/fix_photo_import_dupes.py
    venv/bin/python tools/fix_photo_import_dupes.py --apply
    venv/bin/python tools/fix_photo_import_dupes.py --map "Amex •8444=Amex" --apply

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
from collections import Counter, defaultdict

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


def find_twin(user_id, photo_id, txn):
    return (Transaction.query
            .filter(Transaction.user_id == user_id,
                    Transaction.account_id != photo_id,
                    Transaction.date == txn.date,
                    Transaction.amount == txn.amount,
                    Transaction.txn_type == txn.txn_type)
            .first())


def infer_card_accounts(user, photo, photo_txns):
    """Decide which account each card_label belongs to, using the data alone.

    A duplicate's twin is the strongest evidence available: it is the very same
    charge, already filed where it belongs. Weighted above a shared card_label
    so a stray mislabelled row cannot outvote it.
    """
    votes = defaultdict(Counter)

    for txn in photo_txns:
        if not txn.card_label:
            continue
        twin = find_twin(user.id, photo.id, txn)
        if twin is not None:
            votes[txn.card_label][twin.account_id] += 10

    for txn in (Transaction.query
                .filter(Transaction.user_id == user.id,
                        Transaction.account_id != photo.id)
                .all()):
        if txn.card_label:
            votes[txn.card_label][txn.account_id] += 1

    return {card: counter.most_common(1)[0][0] for card, counter in votes.items() if counter}


def plan(user, overrides):
    photo = Account.query.filter_by(user_id=user.id, name=PHOTO_ACCOUNT_NAME).first()
    if photo is None:
        return None

    accounts = {a.id: a for a in Account.query.filter_by(user_id=user.id) if a.id != photo.id}
    by_name = {a.name.lower(): a.id for a in accounts.values()}

    photo_txns = Transaction.query.filter_by(account_id=photo.id).order_by(Transaction.date).all()
    inferred = infer_card_accounts(user, photo, photo_txns)
    for card, name in overrides.items():
        if name.lower() not in by_name:
            sys.exit(f'--map: no account named {name!r} (have: '
                     + ', '.join(sorted(a.name for a in accounts.values())) + ')')
        inferred[card] = by_name[name.lower()]

    deletes, moves, stays = [], [], []
    for txn in photo_txns:
        twin = find_twin(user.id, photo.id, txn)
        if twin is not None:
            deletes.append((txn, accounts.get(twin.account_id)))
            continue
        target_id = inferred.get(txn.card_label) if txn.card_label else None
        target = accounts.get(target_id) if target_id else None
        (moves if target is not None else stays).append((txn, target))

    return photo, accounts, inferred, deletes, moves, stays


def show(title, rows, render):
    print(f'{title} ({len(rows)}):')
    for txn, extra in rows:
        print('  ' + render(txn, extra))
    print(f'  subtotal: {sum(t.amount for t, _ in rows):,.2f}\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='write the changes (default: dry run)')
    ap.add_argument('--map', action='append', default=[], metavar='CARD=ACCOUNT',
                    help='override the inferred account for a card_label')
    args = ap.parse_args()

    overrides = {}
    for item in args.map:
        if '=' not in item:
            sys.exit(f'--map needs CARD=ACCOUNT, got {item!r}')
        card, account = item.split('=', 1)
        overrides[card.strip()] = account.strip()

    with make_app().app_context():
        print(f'database: {db.engine.url}\n')
        user = User.query.first()
        if user is None:
            sys.exit('no user registered')

        result = plan(user, overrides)
        if result is None:
            sys.exit(f'no "{PHOTO_ACCOUNT_NAME}" account - nothing to repair')
        photo, accounts, inferred, deletes, moves, stays = result

        print('ACCOUNTS:')
        for acct in [photo] + sorted(accounts.values(), key=lambda a: a.name):
            n = Transaction.query.filter_by(account_id=acct.id).count()
            print(f'  {acct.name:24s} {n:4d} rows')
        print()

        print('CARD -> ACCOUNT (inferred from the data):')
        for card, account_id in sorted(inferred.items()):
            print(f'  {card:24s} -> {accounts[account_id].name}')
        unmapped = sorted({t.card_label or '(no card)' for t, _ in stays})
        for card in unmapped:
            print(f'  {card:24s} -> (none found, left in {PHOTO_ACCOUNT_NAME})')
        print()

        show('DUPLICATES to delete', deletes,
             lambda t, a: f'{t.date}  {t.amount:>10,.2f}  {t.merchant_raw[:32]:32s} '
                          f'(twin in {a.name if a else "?"})')
        show('KEEP and move', moves,
             lambda t, a: f'{t.date}  {t.amount:>10,.2f}  {t.merchant_raw[:32]:32s} -> {a.name}')
        show('KEEP where they are', stays,
             lambda t, _: f'{t.date}  {t.amount:>10,.2f}  {t.merchant_raw[:32]:32s} '
                          f'[{t.card_label or "no card"}]')

        if not args.apply:
            print('DRY RUN - nothing written. Re-run with --apply to commit.')
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
