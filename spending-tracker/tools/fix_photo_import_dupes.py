"""Terminal front end for the photo-import duplicate repair.

The logic lives in photo_dupes.py, shared with the /maintenance endpoints so
the phone and the terminal can never disagree about what needs repairing. This
adds the report and the --apply gate.

Which account a card belongs to is worked out from the data -- no need to know
or type any account names. A card the data cannot speak to is left alone rather
than guessed at; --map overrides or supplies one.

Dry run by default; nothing is written without --apply.

    venv/bin/python tools/fix_photo_import_dupes.py
    venv/bin/python tools/fix_photo_import_dupes.py --apply
    venv/bin/python tools/fix_photo_import_dupes.py --map "Amex •8444=Amex" --apply
"""

import argparse
import os
import sys

from flask import Flask

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from models import db, User, Transaction  # noqa: E402
from photo_dupes import PHOTO_ACCOUNT_NAME, build_plan, apply_plan  # noqa: E402


def make_app():
    app = Flask(__name__, instance_path=os.path.join(ROOT, 'instance'))
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///spending.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


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

        try:
            plan = build_plan(user, overrides)
        except ValueError as e:
            sys.exit(f'--map: {e}')
        if plan is None:
            sys.exit(f'no "{PHOTO_ACCOUNT_NAME}" account - nothing to repair')

        print('ACCOUNTS:')
        for acct in [plan['photo']] + sorted(plan['accounts'].values(), key=lambda a: a.name):
            n = Transaction.query.filter_by(account_id=acct.id).count()
            print(f'  {acct.name:24s} {n:4d} rows')
        print()

        print('CARD -> ACCOUNT (inferred from the data):')
        for card, account_id in sorted(plan['inferred'].items()):
            print(f'  {card:24s} -> {plan["accounts"][account_id].name}')
        for card in sorted({t.card_label or '(no card)' for t, _ in plan['stays']}):
            print(f'  {card:24s} -> (none found, left in {PHOTO_ACCOUNT_NAME})')
        print()

        show('DUPLICATES to delete', plan['deletes'],
             lambda t, a: f'{t.date}  {t.amount:>10,.2f}  {t.merchant_raw[:32]:32s} '
                          f'(twin in {a.name if a else "?"})')
        show('KEEP and move', plan['moves'],
             lambda t, a: f'{t.date}  {t.amount:>10,.2f}  {t.merchant_raw[:32]:32s} -> {a.name}')
        show('KEEP where they are', plan['stays'],
             lambda t, _: f'{t.date}  {t.amount:>10,.2f}  {t.merchant_raw[:32]:32s} '
                          f'[{t.card_label or "no card"}]')

        if not args.apply:
            print('DRY RUN - nothing written. Re-run with --apply to commit.')
            return

        result = apply_plan(plan)
        print(f'APPLIED: deleted {result["deleted"]}, moved {result["moved"]}, '
              f'left {result["left"]}.')


if __name__ == '__main__':
    main()
