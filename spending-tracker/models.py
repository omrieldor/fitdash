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


class Account(db.Model):
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
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(60), nullable=False)
    icon = db.Column(db.String(10))
    color = db.Column(db.String(20))
    is_system = db.Column(db.Boolean, default=False)

    __table_args__ = (db.UniqueConstraint('user_id', 'name', name='uq_category_user_name'),)


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    import_id = db.Column(db.Integer, db.ForeignKey('import_batch.id'))
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))

    date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)  # positive = spend, negative = refund/credit
    merchant_raw = db.Column(db.String(300), nullable=False)
    merchant_normalized = db.Column(db.String(150), nullable=False, index=True)
    category_source = db.Column(db.String(20), default='default')  # rule, manual, default
    dedup_hash = db.Column(db.String(64), nullable=False, index=True)
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


DEFAULT_CATEGORIES = [
    ('Groceries', '🛒', '#5a9a75'),
    ('Dining', '🍔', '#c9a84c'),
    ('Transport', '🚗', '#1478ff'),
    ('Shopping', '🛍️', '#8a5565'),
    ('Bills & Utilities', '💡', '#a05555'),
    ('Entertainment', '🎬', '#7a5ac9'),
    ('Health', '💊', '#4ca6a8'),
    ('Travel', '✈️', '#c97a4c'),
    ('Subscriptions', '🔁', '#6a7fc9'),
    ('Uncategorized', '❔', '#5a6070'),
]


def ensure_default_categories(user):
    existing = {c.name for c in Category.query.filter_by(user_id=user.id).all()}
    created_any = False
    for name, icon, color in DEFAULT_CATEGORIES:
        if name not in existing:
            db.session.add(Category(
                user_id=user.id, name=name, icon=icon, color=color,
                is_system=(name == 'Uncategorized'),
            ))
            created_any = True
    if created_any:
        db.session.commit()


def get_uncategorized_category(user):
    ensure_default_categories(user)
    return Category.query.filter_by(user_id=user.id, name='Uncategorized').first()
