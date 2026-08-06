from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import date, datetime

db = SQLAlchemy()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

    accounts = db.relationship('Account', backref='user', lazy=True, cascade='all, delete-orphan')
    transactions = db.relationship('Transaction', backref='user', lazy=True, cascade='all, delete-orphan')
    categories = db.relationship('Category', backref='user', lazy=True, cascade='all, delete-orphan')
    rules = db.relationship('CategorizationRule', backref='user', lazy=True, cascade='all, delete-orphan')
    imports = db.relationship('Import', backref='user', lazy=True, cascade='all, delete-orphan')
    networth_items = db.relationship('NetWorthItem', backref='user', lazy=True, cascade='all, delete-orphan')
    portfolio_snapshots = db.relationship('PortfolioSnapshot', backref='user', lazy=True, cascade='all, delete-orphan')
    push_subscriptions = db.relationship('PushSubscription', backref='user', lazy=True, cascade='all, delete-orphan')


class Account(db.Model):
    """A bank/card account that statements are imported for."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    institution = db.Column(db.String(100))
    account_type = db.Column(db.String(20), default='credit_card')  # credit_card, debit, checking, savings, other
    column_mapping = db.Column(db.Text)  # JSON: {date, amount, merchant, date_format, amount_sign}
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    transactions = db.relationship('Transaction', backref='account', lazy=True, cascade='all, delete-orphan')
    imports = db.relationship('Import', backref='account', lazy=True, cascade='all, delete-orphan')


class Category(db.Model):
    """Two-level category tree: top-level groups (parent_id NULL) and their children."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    name = db.Column(db.String(60), nullable=False)
    icon = db.Column(db.String(10))
    color = db.Column(db.String(20))
    kind = db.Column(db.String(10), default='expense')  # expense | income
    is_system = db.Column(db.Boolean, default=False)
    sort_order = db.Column(db.Integer, default=0)

    children = db.relationship('Category', backref=db.backref('parent', remote_side=[id]), lazy=True)

    __table_args__ = (db.UniqueConstraint('user_id', 'name', name='uq_category_user_name'),)


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    import_id = db.Column(db.Integer, db.ForeignKey('import_batch.id'))
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))

    date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)  # always a magnitude; direction lives in txn_type
    txn_type = db.Column(db.String(10), default='expense')  # expense | income
    merchant_raw = db.Column(db.String(300), nullable=False)
    merchant_normalized = db.Column(db.String(150), nullable=False, index=True)
    category_source = db.Column(db.String(20), default='default')  # rule, manual, default
    dedup_hash = db.Column(db.String(64), nullable=False, index=True)
    card_label = db.Column(db.String(60))  # e.g. Visa •1234, חבר של קבע — auto-detected at import
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    category = db.relationship('Category')

    __table_args__ = (db.UniqueConstraint('account_id', 'dedup_hash', name='uq_txn_account_dedup'),)


class CategorizationRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    match_pattern = db.Column(db.String(200), nullable=False)
    match_type = db.Column(db.String(20), default='contains')  # contains, exact
    priority = db.Column(db.Integer, default=0)
    hit_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    category = db.relationship('Category')

    __table_args__ = (db.UniqueConstraint('user_id', 'match_pattern', name='uq_rule_user_pattern'),)


class Import(db.Model):
    __tablename__ = 'import_batch'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    filename = db.Column(db.String(255))
    row_count = db.Column(db.Integer, default=0)
    imported_count = db.Column(db.Integer, default=0)
    duplicate_count = db.Column(db.Integer, default=0)
    date_range_start = db.Column(db.Date)
    date_range_end = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    transactions = db.relationship('Transaction', backref='import_batch', lazy=True)


# --- Billing-cycle archive (the Library) ---

class CycleSummary(db.Model):
    """A completed billing cycle (8th → 7th), rendered for the Library.

    Transaction rows are RETAINED when a cycle is archived, so this is a cached
    view of them rather than the only surviving copy -- it is recomputed from
    the live rows on every /api/summary, which is what lets an edit to an older
    transaction flow through into its cycle's totals.

    Summaries written before that change may cover cycles whose detail was
    already deleted; those are deliberately left untouched, since recomputing
    them from zero surviving rows would replace a real total with 0.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    cycle_start = db.Column(db.Date, nullable=False)
    cycle_end = db.Column(db.Date, nullable=False)
    income_total = db.Column(db.Float, default=0.0)
    expense_total = db.Column(db.Float, default=0.0)
    by_group = db.Column(db.Text)       # JSON: [{name, icon, color, total}]
    top_merchants = db.Column(db.Text)  # JSON: [{merchant, total, count}]
    by_income_source = db.Column(db.Text)  # JSON: [{name, icon, total}]
    txn_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('user_id', 'cycle_start', name='uq_cycle_user_start'),)


class ArchivedDedup(db.Model):
    """Dedup hashes of deleted (archived) transactions.

    Keeps re-uploads of old statements from re-importing rows whose detail was
    erased at archive time — the hash alone reveals nothing about the purchase.
    """
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    dedup_hash = db.Column(db.String(64), nullable=False, index=True)

    __table_args__ = (db.UniqueConstraint('account_id', 'dedup_hash', name='uq_arch_account_dedup'),)


class ProcessedInbox(db.Model):
    """Inbox files already ingested, so repeated git pulls never double-import."""
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), unique=True, nullable=False)
    entry_count = db.Column(db.Integer, default=0)
    processed_at = db.Column(db.DateTime, default=datetime.utcnow)


# --- Net worth ---

class NetWorthItem(db.Model):
    """A named balance you update manually: bank accounts, emergency fund, loans, etc."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    kind = db.Column(db.String(20), default='bank')  # bank | emergency_fund | other_asset | liability
    icon = db.Column(db.String(10))
    target_amount = db.Column(db.Float)  # used by emergency_fund
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    entries = db.relationship('NetWorthEntry', backref='item', lazy=True,
                              cascade='all, delete-orphan',
                              order_by='NetWorthEntry.as_of_date.desc()')


class NetWorthEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('net_worth_item.id'), nullable=False)
    as_of_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)  # always positive; liabilities subtract by kind
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# --- Investments ---

class PortfolioSnapshot(db.Model):
    """One manual portfolio update, normally logged once a month."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    as_of_date = db.Column(db.Date, nullable=False)
    contribution = db.Column(db.Float, default=0.0)  # money added since previous snapshot
    cash_value = db.Column(db.Float, default=0.0)    # uninvested cash sitting in the brokerage
    total_value = db.Column(db.Float, default=0.0)   # ILS value at the time it was logged
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    holdings = db.relationship('Holding', backref='snapshot', lazy=True, cascade='all, delete-orphan')


class Holding(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    snapshot_id = db.Column(db.Integer, db.ForeignKey('portfolio_snapshot.id'), nullable=False)
    ticker = db.Column(db.String(20), nullable=False)
    label = db.Column(db.String(100))
    quantity = db.Column(db.Float, nullable=False)
    manual_value = db.Column(db.Float)  # fallback total value when no live price is available
    currency = db.Column(db.String(10), default='USD')


class PriceCache(db.Model):
    """Cached quotes and historical closes so we don't re-hit the market data API."""
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), nullable=False, index=True)
    as_of_date = db.Column(db.Date, nullable=False)
    close = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), default='USD')
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('symbol', 'as_of_date', name='uq_price_symbol_date'),)


# --- Push notifications ---

class PushSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    endpoint = db.Column(db.Text, nullable=False)
    p256dh = db.Column(db.String(200), nullable=False)
    auth = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ReminderLog(db.Model):
    """One row per reminder actually delivered, so an hourly scheduler can't double-send."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    kind = db.Column(db.String(40), default='monthly_log')
    period = db.Column(db.String(10), nullable=False)  # YYYY-MM
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('user_id', 'kind', 'period', name='uq_reminder_user_kind_period'),)


# --- Seed data ---

# (group name, icon, color, kind, [(child name, child icon), ...])
CATEGORY_TREE = [
    ('Income', '💰', '#5a9a75', 'income', [
        ('Salary', '🧾'), ('Bonus', '🎁'), ('Freelance', '💻'), ('Dividends', '📈'),
        ('Interest', '🏦'), ('Refunds', '↩️'), ('Other Income', '➕'),
    ]),
    ('Food', '🍽️', '#c9a84c', 'expense', [
        ('Groceries', '🛒'), ('Restaurants', '🍴'), ('Cafés', '☕'), ('Food Delivery', '🛵'),
        ('Snacks', '🍫'), ('Bars', '🍺'),
    ]),
    ('Friends', '👥', '#b8917a', 'expense', [
        ('Gifts', '🎁'), ('Money Lent', '🤝'), ('Money Owed', '💸'),
        ('Bill Split', '🧾'),
    ]),
    ('Car', '🚗', '#1478ff', 'expense', [
        ('Fuel', '⛽'), ('Car Insurance', '🛡️'), ('Car Maintenance', '🔧'),
        ('Parking', '🅿️'), ('Tolls', '🛣️'), ('Car Payment', '🚙'),
    ]),
    ('Home', '🏠', '#a05555', 'expense', [
        ('Rent / Mortgage', '🔑'), ('Utilities', '💡'), ('Internet & TV', '📶'),
        ('Home Maintenance', '🧰'), ('Furniture & Appliances', '🛋️'), ('Home Insurance', '📋'),
    ]),
    ('Cash', '💵', '#7a8a5a', 'expense', [
        ('Cash Withdrawal', '🏧'),
    ]),
    ('Transport', '🚌', '#6a7fc9', 'expense', [
        ('Public Transport', '🚇'), ('Taxi & Rideshare', '🚕'),
    ]),
    ('Shopping', '🛍️', '#8a5565', 'expense', [
        ('Clothing', '👕'), ('Electronics', '📱'), ('General Shopping', '🏬'),
    ]),
    ('Health', '💊', '#4ca6a8', 'expense', [
        ('Pharmacy', '💊'), ('Doctor & Dental', '🩺'), ('Gym & Fitness', '🏋️'), ('Health Insurance', '🏥'),
    ]),
    ('Entertainment', '🎬', '#7a5ac9', 'expense', [
        ('Streaming & Subscriptions', '🔁'), ('Events & Nightlife', '🎟️'), ('Hobbies', '🎨'),
    ]),
    ('Travel', '✈️', '#c97a4c', 'expense', [
        ('Flights', '🛫'), ('Hotels', '🏨'), ('Vacation', '🏝️'),
    ]),
    ('Financial', '🏦', '#5f6b8a', 'expense', [
        ('Bank Fees', '💳'), ('Taxes', '📑'), ('Loan Payment', '📉'),
    ]),
    ('Other', '📦', '#5a6070', 'expense', [
        ('Uncategorized', '❔'), ('Misc', '📦'),
    ]),
]

# Categories that must always exist because code falls back to them.
SYSTEM_CATEGORY_NAMES = {'Uncategorized', 'Other Income', 'Cash Withdrawal', 'Salary'}


def ensure_default_categories(user):
    existing = {c.name: c for c in Category.query.filter_by(user_id=user.id).all()}
    created_any = False
    order = 0

    for group_name, group_icon, color, kind, children in CATEGORY_TREE:
        group = existing.get(group_name)
        if group is None:
            group = Category(user_id=user.id, name=group_name, icon=group_icon, color=color,
                             kind=kind, is_system=True, sort_order=order)
            db.session.add(group)
            db.session.flush()
            existing[group_name] = group
            created_any = True
        order += 1

        for child_name, child_icon in children:
            if child_name not in existing:
                db.session.add(Category(
                    user_id=user.id, parent_id=group.id, name=child_name, icon=child_icon,
                    color=color, kind=kind, sort_order=order,
                    is_system=(child_name in SYSTEM_CATEGORY_NAMES),
                ))
                created_any = True
            order += 1

    if created_any:
        db.session.commit()


def get_category(user, name):
    return Category.query.filter_by(user_id=user.id, name=name).first()


def get_uncategorized_category(user):
    ensure_default_categories(user)
    return get_category(user, 'Uncategorized')


def get_default_income_category(user):
    ensure_default_categories(user)
    return get_category(user, 'Other Income')
