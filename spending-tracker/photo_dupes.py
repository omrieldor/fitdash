"""Find and repair photo-import entries that duplicate statement transactions.

Photo-import entries land in the auto-created "Photo Imports" account whenever
the payload omits `account` (see inbox/README.md). The dedup hash is computed
from account_id, so those entries can never collide with the same charges
imported from a statement into the real card account -- they import a second
time, silently, and category totals double while each account's own total still
looks right.

For every transaction in the Photo Imports account, look for a twin elsewhere --
same date, same amount, same direction:

  twin found  -> the photo copy is a duplicate, delete it
  no twin     -> the photo copy is the only record, keep it, and move it to the
                 real account for its card

Moving matters as much as deleting: an entry left in Photo Imports duplicates
all over again the next time a statement covering it is imported.

Twins are matched on date+amount+direction rather than on the merchant string,
because a merchant name transcribed from a phone screenshot is often truncated
and would not match what the statement's CSV recorded. That is deliberately
loose -- two genuinely separate charges of the same amount, at the same
merchant, on the same day look like a duplicate -- so every caller shows the
plan before applying it.

Shared by tools/fix_photo_import_dupes.py (terminal) and the /maintenance
endpoints (phone). One implementation, so the two can never disagree.
"""

from collections import Counter, defaultdict

from models import db, Account, Transaction
from spending_import import compute_dedup_hash

PHOTO_ACCOUNT_NAME = 'Photo Imports'


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


def build_plan(user, overrides=None):
    """Work out what to delete and what to move. Returns None if there is no
    Photo Imports account, otherwise a dict describing the whole plan."""
    photo = Account.query.filter_by(user_id=user.id, name=PHOTO_ACCOUNT_NAME).first()
    if photo is None:
        return None

    accounts = {a.id: a for a in Account.query.filter_by(user_id=user.id) if a.id != photo.id}
    by_name = {a.name.lower(): a.id for a in accounts.values()}

    photo_txns = Transaction.query.filter_by(account_id=photo.id).order_by(Transaction.date).all()
    inferred = infer_card_accounts(user, photo, photo_txns)
    for card, name in (overrides or {}).items():
        if name.lower() not in by_name:
            raise ValueError(f'no account named {name!r}')
        inferred[card] = by_name[name.lower()]

    deletes, moves, stays = [], [], []
    for txn in photo_txns:
        twin = find_twin(user.id, photo.id, txn)
        if twin is not None:
            deletes.append((txn, accounts.get(twin.account_id)))
            continue
        target = accounts.get(inferred.get(txn.card_label)) if txn.card_label else None
        (moves if target is not None else stays).append((txn, target))

    return {
        'photo': photo,
        'accounts': accounts,
        'inferred': inferred,
        'deletes': deletes,
        'moves': moves,
        'stays': stays,
    }


def apply_plan(plan):
    """Delete the duplicates and re-home the survivors. Returns a count summary."""
    for txn, _ in plan['deletes']:
        db.session.delete(txn)
    for txn, target in plan['moves']:
        txn.account_id = target.id
        # The hash is account-scoped, so a moved row must be re-keyed or it
        # would not dedup against future imports into its new home either.
        signed = -txn.amount if txn.txn_type == 'income' else txn.amount
        txn.dedup_hash = compute_dedup_hash(target.id, txn.date, signed, txn.merchant_raw)
    db.session.commit()
    return {
        'deleted': len(plan['deletes']),
        'moved': len(plan['moves']),
        'left': len(plan['stays']),
    }


def plan_as_dict(plan):
    """The plan as plain JSON-able data, for the maintenance endpoints."""
    def row(txn, extra):
        return {
            'id': txn.id,
            'date': txn.date.isoformat(),
            'amount': txn.amount,
            'merchant': txn.merchant_raw,
            'card': txn.card_label,
            'account': extra.name if extra is not None else None,
        }

    return {
        'accounts': [
            {'name': a.name,
             'rows': Transaction.query.filter_by(account_id=a.id).count()}
            for a in [plan['photo']] + sorted(plan['accounts'].values(), key=lambda a: a.name)
        ],
        'mapping': {card: plan['accounts'][account_id].name
                    for card, account_id in plan['inferred'].items()
                    if account_id in plan['accounts']},
        'deletes': [row(t, a) for t, a in plan['deletes']],
        'moves': [row(t, a) for t, a in plan['moves']],
        'stays': [row(t, a) for t, a in plan['stays']],
        'totals': {
            'deletes': sum(t.amount for t, _ in plan['deletes']),
            'moves': sum(t.amount for t, _ in plan['moves']),
            'stays': sum(t.amount for t, _ in plan['stays']),
        },
    }
