import csv
import hashlib
import io
import json
import re
from datetime import datetime

from models import db, Transaction, Import, CategorizationRule, get_uncategorized_category

MAX_FILE_BYTES = 2 * 1024 * 1024  # 2MB
MAX_ROWS = 10000

DATE_FORMATS = ['%Y-%m-%d', '%m/%d/%Y', '%d/%m/%Y', '%m-%d-%Y', '%d-%m-%Y', '%Y/%m/%d']

_PROCESSOR_PREFIX = re.compile(r'^(SQ|TST|PAYPAL|PY|SP|IN)\s*\*\s*', re.IGNORECASE)
_CITY_STATE_SUFFIX = re.compile(r'\s+(?:[A-Z]+\s+){1,2}[A-Z]{2}$')  # e.g. " SEATTLE WA" / " SAN FRANCISCO CA"
_STORE_NUMBER = re.compile(r'#?\d{3,}\b')  # store numbers / long digit runs
_EMBEDDED_DATE = re.compile(r'\b\d{2}/\d{2}\b')  # embedded dates like 08/04


class ImportError_(Exception):
    pass


def sniff_columns(file_bytes):
    """Return headers + sample rows + a best-guess column mapping, without touching the DB."""
    text = file_bytes.decode('utf-8-sig', errors='replace')
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ImportError_('Empty file')
    headers = rows[0]
    sample = rows[1:6]

    guess = {'date': None, 'amount': None, 'merchant': None}
    for h in headers:
        low = h.lower()
        if guess['date'] is None and 'date' in low:
            guess['date'] = h
        if guess['amount'] is None and any(k in low for k in ('amount', 'debit', 'credit', 'charge')):
            guess['amount'] = h
        if guess['merchant'] is None and any(k in low for k in ('description', 'merchant', 'payee', 'name', 'details')):
            guess['merchant'] = h
    # fall back to positional guesses if nothing matched
    if guess['date'] is None and headers:
        guess['date'] = headers[0]
    if guess['merchant'] is None and len(headers) > 1:
        guess['merchant'] = headers[1]
    if guess['amount'] is None and len(headers) > 2:
        guess['amount'] = headers[-1]

    return {
        'headers': headers,
        'sample_rows': sample,
        'guess': guess,
    }


def normalize_merchant(raw):
    s = (raw or '').strip().upper()
    s = _PROCESSOR_PREFIX.sub('', s)
    s = _CITY_STATE_SUFFIX.sub('', s)
    s = _STORE_NUMBER.sub(' ', s)
    s = _EMBEDDED_DATE.sub(' ', s)
    s = s.replace('*', ' ')
    s = re.sub(r'\s{2,}', ' ', s).strip()
    return s.title() if s else 'Unknown'


def compute_dedup_hash(account_id, txn_date, amount, merchant_raw):
    key = f"{account_id}|{txn_date.isoformat()}|{amount:.2f}|{(merchant_raw or '').strip().lower()}"
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def _parse_date(value, known_format=None):
    value = (value or '').strip()
    if not value:
        return None
    formats = ([known_format] if known_format else []) + DATE_FORMATS
    for fmt in formats:
        if not fmt:
            continue
        try:
            return datetime.strptime(value, fmt).date(), fmt
        except ValueError:
            continue
    return None, None


def _parse_amount(value, amount_sign):
    value = (value or '').strip().replace(',', '').replace('$', '')
    if not value:
        return None
    negative = value.startswith('(') and value.endswith(')')
    if negative:
        value = value[1:-1]
    try:
        amount = float(value)
    except ValueError:
        return None
    if negative:
        amount = -amount
    # amount_sign tells us how the source file encodes spend:
    #   'positive_is_debit' -> spend already positive, keep as-is
    #   'negative_is_debit' -> spend is negative in the file, flip sign
    if amount_sign == 'negative_is_debit':
        amount = -amount
    return amount


def categorize(merchant_normalized, user_id):
    rules = (CategorizationRule.query
             .filter_by(user_id=user_id)
             .order_by(CategorizationRule.priority.desc())
             .all())
    rules.sort(key=lambda r: len(r.match_pattern), reverse=True)
    haystack = merchant_normalized.lower()
    for rule in rules:
        pattern = rule.match_pattern.lower()
        matched = (haystack == pattern) if rule.match_type == 'exact' else (pattern in haystack)
        if matched:
            rule.hit_count = (rule.hit_count or 0) + 1
            return rule.category_id, 'rule'
    return None, 'default'


def parse_and_import(file_bytes, account, column_mapping, user, filename=None):
    if len(file_bytes) > MAX_FILE_BYTES:
        raise ImportError_('File too large (max 2MB)')

    text = file_bytes.decode('utf-8-sig', errors='replace')
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if len(rows) > MAX_ROWS:
        raise ImportError_(f'Too many rows (max {MAX_ROWS})')

    date_col = column_mapping['date']
    amount_col = column_mapping['amount']
    merchant_col = column_mapping['merchant']
    date_format = column_mapping.get('date_format')
    amount_sign = column_mapping.get('amount_sign', 'positive_is_debit')

    uncategorized = get_uncategorized_category(user)

    imported = 0
    duplicates = 0
    min_date, max_date = None, None
    new_transactions = []
    resolved_date_format = date_format

    for row in rows:
        raw_date = row.get(date_col, '')
        raw_amount = row.get(amount_col, '')
        raw_merchant = row.get(merchant_col, '')

        txn_date, used_format = _parse_date(raw_date, resolved_date_format)
        if txn_date is None:
            continue
        if resolved_date_format is None:
            resolved_date_format = used_format

        amount = _parse_amount(raw_amount, amount_sign)
        if amount is None:
            continue

        merchant_normalized = normalize_merchant(raw_merchant)
        dedup_hash = compute_dedup_hash(account.id, txn_date, amount, raw_merchant)

        existing = Transaction.query.filter_by(account_id=account.id, dedup_hash=dedup_hash).first()
        if existing:
            duplicates += 1
            continue

        category_id, source = categorize(merchant_normalized, user.id)
        if category_id is None:
            category_id = uncategorized.id

        txn = Transaction(
            user_id=user.id,
            account_id=account.id,
            category_id=category_id,
            date=txn_date,
            amount=amount,
            merchant_raw=raw_merchant.strip(),
            merchant_normalized=merchant_normalized,
            category_source=source,
            dedup_hash=dedup_hash,
        )
        new_transactions.append(txn)
        imported += 1

        if min_date is None or txn_date < min_date:
            min_date = txn_date
        if max_date is None or txn_date > max_date:
            max_date = txn_date

    import_record = Import(
        user_id=user.id,
        account_id=account.id,
        filename=filename,
        row_count=len(rows),
        imported_count=imported,
        duplicate_count=duplicates,
        date_range_start=min_date,
        date_range_end=max_date,
    )
    db.session.add(import_record)
    db.session.flush()

    for txn in new_transactions:
        txn.import_id = import_record.id
        db.session.add(txn)

    # remember the resolved mapping (including learned date format) on the account
    saved_mapping = dict(column_mapping)
    saved_mapping['date_format'] = resolved_date_format
    account.column_mapping = json.dumps(saved_mapping)

    db.session.commit()
    return import_record


def apply_correction(transaction, new_category_id, user, retroactive=True):
    transaction.category_id = new_category_id
    transaction.category_source = 'manual'

    pattern = transaction.merchant_normalized
    rule = CategorizationRule.query.filter_by(user_id=user.id, match_pattern=pattern).first()
    if rule:
        rule.category_id = new_category_id
    else:
        rule = CategorizationRule(
            user_id=user.id,
            category_id=new_category_id,
            match_pattern=pattern,
            match_type='contains',
        )
        db.session.add(rule)

    if retroactive:
        (Transaction.query
         .filter(Transaction.user_id == user.id,
                 Transaction.merchant_normalized == pattern,
                 Transaction.category_source != 'manual',
                 Transaction.id != transaction.id)
         .update({'category_id': new_category_id, 'category_source': 'rule'}, synchronize_session=False))

    db.session.commit()
