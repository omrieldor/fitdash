import hashlib
import hmac
import json
import os
import subprocess
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, send_from_directory
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import func
from werkzeug.security import generate_password_hash, check_password_hash

from models import (db, User, Account, Category, Transaction, CategorizationRule, Import,
                    NetWorthItem, NetWorthEntry, PortfolioSnapshot, Holding,
                    PushSubscription, CycleSummary, ArchivedDedup, ProcessedInbox,
                    ensure_default_categories)

app = Flask(__name__)
_fallback_key = os.environ.get('SECRET_KEY')
if not _fallback_key:
    import warnings
    warnings.warn('SECRET_KEY not set — using insecure dev-only default. Set SECRET_KEY env var in production.')
    _fallback_key = 'spendtrack-dev-insecure-key-change-me'
app.secret_key = _fallback_key
# Distinct cookie names: the fitness app shares this host, and cookies are scoped
# by host and NOT by port (RFC 6265). With both apps issuing a cookie called
# 'session', each overwrote the other's and the mismatched SECRET_KEY then failed
# verification — silently logging you out of whichever app you hadn't just used.
app.config['SESSION_COOKIE_NAME'] = 'spendtrack_session'
app.config['REMEMBER_COOKIE_NAME'] = 'spendtrack_remember'
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///spending.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 3 * 1024 * 1024  # 3MB upload cap

db.init_app(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


with app.app_context():
    db.create_all()
    db.session.execute(db.text('PRAGMA journal_mode=WAL'))
    db.session.commit()

    # Additive migrations, matching the app's no-Alembic approach: new tables come
    # from create_all() above, new columns on existing tables are added here.
    inspector = db.inspect(db.engine)
    existing_tables = set(inspector.get_table_names())

    def add_missing_columns(table, columns):
        if table not in existing_tables:
            return
        present = {c['name'] for c in inspector.get_columns(table)}
        for name, ddl in columns.items():
            if name not in present:
                db.session.execute(db.text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))

    add_missing_columns('category', {
        'parent_id': 'INTEGER',
        'kind': "VARCHAR(10) DEFAULT 'expense'",
        'sort_order': 'INTEGER DEFAULT 0',
    })
    add_missing_columns('transaction', {
        'txn_type': "VARCHAR(10) DEFAULT 'expense'",
        'card_label': 'VARCHAR(60)',
    })
    add_missing_columns('portfolio_snapshot', {
        'total_value': 'FLOAT DEFAULT 0',
    })
    db.session.commit()

    # Photo-import inbox: entries the assistant pushed via the repo get filed
    # here on every boot (the deploy webhook restarts the service after a push,
    # so this is what makes "post a photo in chat" end up in the app).
    try:
        from inbox_ingest import ingest_inbox
        ingest_inbox()
    except Exception as _inbox_err:      # never let inbox trouble block startup
        print(f'inbox ingest skipped: {_inbox_err}')


# --- Rate limiting ---

_login_attempts = defaultdict(list)


def _is_rate_limited(store, key, max_attempts=5, window=30):
    now = time.time()
    store[key] = [t for t in store[key] if now - t < window]
    if not store[key]:
        del store[key]
        return False
    return len(store[key]) >= max_attempts


def _record_attempt(store, key):
    store[key].append(time.time())


# --- Auth ---

@app.route('/register', methods=['GET', 'POST'])
def register():
    if User.query.count() > 0:
        flash('Registration is closed — this is a single-user app.')
        return redirect(url_for('login'))
    if request.method == 'POST':
        password = request.form['password']
        if len(password) < 8:
            flash('Password must be at least 8 characters.')
            return redirect(url_for('register'))
        user = User(
            username=request.form['username'].strip(),
            email=request.form['email'].strip(),
            password_hash=generate_password_hash(password),
        )
        db.session.add(user)
        db.session.commit()
        ensure_default_categories(user)
        login_user(user)
        return redirect(url_for('index'))
    return render_template('login.html', mode='register')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.remote_addr or 'unknown'
        if _is_rate_limited(_login_attempts, ip):
            flash('Too many login attempts. Please wait 30 seconds.')
            return redirect(url_for('login'))
        user = User.query.filter_by(username=request.form['username'].strip()).first()
        if not user or not check_password_hash(user.password_hash, request.form['password']):
            _record_attempt(_login_attempts, ip)
            flash('Invalid username or password.')
            return redirect(url_for('login'))
        _login_attempts.pop(ip, None)
        login_user(user)
        return redirect(url_for('index'))
    return render_template('login.html', mode='login')


@app.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    ensure_default_categories(current_user)
    from spending_import import seed_default_rules
    seed_default_rules(current_user)
    return render_template('index.html')


@app.route('/sw.js')
def service_worker():
    """Serve the worker from the root so its scope covers the whole app.

    At /static/sw.js the scope would be /static/, leaving the dashboard at /
    uncontrolled — navigator.serviceWorker.ready would then never resolve and
    push registration would hang silently.
    """
    resp = send_from_directory(app.static_folder, 'sw.js')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


# --- Billing-cycle helpers ---
#
# The app runs on the card's billing cycle, not calendar months: a cycle runs
# from the 8th of one month through the 7th of the next. The homepage shows the
# window that opens with the most recently *completed* cycle — so a statement
# uploaded on the 8th stays on the homepage until the following 7th, exactly as
# the user asked. Everything older is archived into the Library.

def _shift_month(year, month, delta):
    month += delta
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return year, month


def _cycle_start_for(d):
    """Start (an 8th) of the cycle containing date d."""
    if d.day >= 8:
        return date(d.year, d.month, 8)
    y, m = _shift_month(d.year, d.month, -1)
    return date(y, m, 8)


def _cycle_end(cycle_start):
    y, m = _shift_month(cycle_start.year, cycle_start.month, 1)
    return date(y, m, 7)


def _prev_cycle_start(cycle_start):
    y, m = _shift_month(cycle_start.year, cycle_start.month, -1)
    return date(y, m, 8)


def _visible_window(today=None):
    """(window_start, today). Homepage shows this; older data lives in the Library.

    The boundary is the most recent 7th ≤ today; the window opens on the 8th one
    month before it. On Sep 7 the window becomes Aug 8 → today, and the Aug 8–Sep 7
    statement uploaded on Sep 8 stays visible until Oct 7.
    """
    today = today or date.today()
    if today.day >= 7:
        boundary = date(today.year, today.month, 7)
    else:
        y, m = _shift_month(today.year, today.month, -1)
        boundary = date(y, m, 7)
    y, m = _shift_month(boundary.year, boundary.month, -1)
    return date(y, m, 8), today


def _cycle_label(cycle_start):
    end = _cycle_end(cycle_start)
    return f"{cycle_start.day} {cycle_start.strftime('%b')} – {end.day} {end.strftime('%b %Y')}"


# Calendar-month helpers — still used by the net-worth history, which tracks
# balances by month rather than by billing cycle.

def _month_range(months_back):
    today = date.today()
    out = []
    y, m = today.year, today.month
    for i in range(months_back - 1, -1, -1):
        yy, mm = _shift_month(y, m, -i)
        out.append((yy, mm))
    return out


def _month_span(year, month):
    start = date(year, month, 1)
    y, m = _shift_month(year, month, 1)
    return start, date(y, m, 1) - timedelta(days=1)


def _sum_transactions(txn_type, start, end):
    total = (db.session.query(func.coalesce(func.sum(Transaction.amount), 0.0))
             .filter(Transaction.user_id == current_user.id,
                     Transaction.txn_type == txn_type,
                     Transaction.date >= start, Transaction.date <= end)
             .scalar())
    return float(total or 0.0)


# --- Categories ---

@app.route('/categories')
@login_required
def list_categories():
    ensure_default_categories(current_user)
    cats = (Category.query.filter_by(user_id=current_user.id)
            .order_by(Category.sort_order, Category.name).all())
    by_id = {c.id: c for c in cats}
    return jsonify([{
        'id': c.id, 'name': c.name, 'icon': c.icon, 'color': c.color,
        'kind': c.kind, 'parent_id': c.parent_id, 'is_system': c.is_system,
        'group': by_id[c.parent_id].name if c.parent_id and c.parent_id in by_id else None,
        'is_group': c.parent_id is None,
    } for c in cats])


@app.route('/categories', methods=['POST'])
@login_required
def create_category():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    if Category.query.filter_by(user_id=current_user.id, name=name).first():
        return jsonify({'error': 'Category already exists'}), 400
    parent_id = data.get('parent_id')
    parent = Category.query.filter_by(id=parent_id, user_id=current_user.id).first() if parent_id else None
    cat = Category(
        user_id=current_user.id, name=name, parent_id=parent.id if parent else None,
        icon=data.get('icon') or '📌', color=(parent.color if parent else data.get('color')) or '#5a6070',
        kind=(parent.kind if parent else data.get('kind')) or 'expense',
    )
    db.session.add(cat)
    db.session.commit()
    return jsonify({'id': cat.id, 'name': cat.name, 'icon': cat.icon,
                    'color': cat.color, 'kind': cat.kind, 'parent_id': cat.parent_id})


# --- Accounts ---

@app.route('/accounts')
@login_required
def list_accounts():
    accts = Account.query.filter_by(user_id=current_user.id).order_by(Account.created_at).all()
    return jsonify([{
        'id': a.id, 'name': a.name, 'institution': a.institution,
        'account_type': a.account_type, 'has_mapping': bool(a.column_mapping),
    } for a in accts])


@app.route('/accounts', methods=['POST'])
@login_required
def create_account():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    acct = Account(
        user_id=current_user.id, name=name,
        institution=(data.get('institution') or '').strip() or None,
        account_type=data.get('account_type') or 'credit_card',
    )
    db.session.add(acct)
    db.session.commit()
    return jsonify({'id': acct.id, 'name': acct.name, 'institution': acct.institution,
                    'account_type': acct.account_type, 'has_mapping': False})


@app.route('/accounts/<int:account_id>', methods=['DELETE'])
@login_required
def delete_account(account_id):
    acct = Account.query.filter_by(id=account_id, user_id=current_user.id).first_or_404()
    db.session.delete(acct)
    db.session.commit()
    return jsonify({'status': 'ok'})


# --- Upload / import ---

@app.route('/upload/sniff', methods=['POST'])
@login_required
def upload_sniff():
    from spending_import import sniff_columns, ImportError_
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'No file uploaded'}), 400
    try:
        return jsonify(sniff_columns(file.read(), filename=file.filename))
    except ImportError_ as e:
        return jsonify({'error': str(e)}), 400


@app.route('/upload', methods=['POST'])
@login_required
def upload():
    from spending_import import parse_and_import, ImportError_

    account_id = request.form.get('account_id', type=int)
    file = request.files.get('file')
    if not account_id or not file:
        return jsonify({'error': 'account_id and file required'}), 400

    acct = Account.query.filter_by(id=account_id, user_id=current_user.id).first_or_404()
    mapping_raw = request.form.get('column_mapping')
    if mapping_raw:
        column_mapping = json.loads(mapping_raw)
    elif acct.column_mapping:
        column_mapping = json.loads(acct.column_mapping)
    else:
        return jsonify({'error': 'column_mapping required for first upload on this account'}), 400

    try:
        rec = parse_and_import(file.read(), acct, column_mapping, current_user, filename=file.filename)
    except ImportError_ as e:
        return jsonify({'error': str(e)}), 400

    return jsonify({
        'imported_count': rec.imported_count,
        'duplicate_count': rec.duplicate_count,
        'row_count': rec.row_count,
        'date_range_start': rec.date_range_start.isoformat() if rec.date_range_start else None,
        'date_range_end': rec.date_range_end.isoformat() if rec.date_range_end else None,
    })


@app.route('/imports')
@login_required
def list_imports():
    imports = (Import.query.filter_by(user_id=current_user.id)
               .order_by(Import.created_at.desc()).limit(50).all())
    return jsonify([{
        'id': i.id, 'filename': i.filename, 'row_count': i.row_count,
        'imported_count': i.imported_count, 'duplicate_count': i.duplicate_count,
        'created_at': i.created_at.isoformat(),
    } for i in imports])


# --- Transactions ---

def _txn_json(t):
    cat = t.category
    return {
        'id': t.id, 'account_id': t.account_id, 'date': t.date.isoformat(),
        'amount': t.amount, 'txn_type': t.txn_type,
        'merchant': t.merchant_normalized, 'merchant_raw': t.merchant_raw,
        'category_id': t.category_id, 'category_name': cat.name if cat else None,
        'category_icon': cat.icon if cat else None, 'category_color': cat.color if cat else None,
        'category_source': t.category_source, 'notes': t.notes,
        'card_label': t.card_label,
    }


@app.route('/api/transactions')
@login_required
def api_transactions():
    # Only the visible billing window — older cycles live in the Library.
    window_start, _today = _visible_window()
    txns = (Transaction.query
            .filter(Transaction.user_id == current_user.id,
                    Transaction.date >= window_start)
            .order_by(Transaction.date.desc()).all())
    return jsonify([_txn_json(t) for t in txns])


@app.route('/transactions/<int:transaction_id>', methods=['PUT'])
@login_required
def edit_transaction(transaction_id):
    from spending_import import apply_correction, ImportError_
    txn = Transaction.query.filter_by(id=transaction_id, user_id=current_user.id).first_or_404()
    if txn.date < _visible_window()[0]:
        return jsonify({'error': "This month is archived and can't be edited"}), 403
    data = request.get_json(force=True)
    if 'category_id' in data:
        try:
            apply_correction(txn, data['category_id'], current_user, retroactive=data.get('retroactive', True))
        except ImportError_ as e:
            return jsonify({'error': str(e)}), 400
    if 'notes' in data:
        txn.notes = data['notes']
        db.session.commit()
    return jsonify(_txn_json(txn))


@app.route('/transactions/<int:transaction_id>', methods=['DELETE'])
@login_required
def delete_transaction(transaction_id):
    txn = Transaction.query.filter_by(id=transaction_id, user_id=current_user.id).first_or_404()
    if txn.date < _visible_window()[0]:
        return jsonify({'error': "This month is archived and can't be edited"}), 403
    db.session.delete(txn)
    db.session.commit()
    return jsonify({'status': 'ok'})


# --- Spending summary, cycle archiving, Library ---

def _aggregate_window(user_id, start, end):
    """Everything the dashboard (or a Library card) shows for one date range.

    This single function feeds both the live homepage and the archived
    CycleSummary rows, so the two can never disagree about the same data.
    """
    def total(txn_type):
        return float(db.session.query(func.coalesce(func.sum(Transaction.amount), 0.0))
                     .filter(Transaction.user_id == user_id,
                             Transaction.txn_type == txn_type,
                             Transaction.date >= start, Transaction.date <= end)
                     .scalar() or 0.0)

    Child = db.aliased(Category)
    Parent = db.aliased(Category)
    rows = (db.session.query(Child.name, Child.icon, Parent.name, Parent.icon, Parent.color,
                             func.sum(Transaction.amount))
            .join(Transaction, Transaction.category_id == Child.id)
            .outerjoin(Parent, Child.parent_id == Parent.id)
            .filter(Transaction.user_id == user_id,
                    Transaction.txn_type == 'expense',
                    Transaction.date >= start, Transaction.date <= end)
            .group_by(Child.id).all())

    groups = {}
    for child_name, child_icon, parent_name, parent_icon, parent_color, amount in rows:
        gname = parent_name or child_name
        g = groups.setdefault(gname, {'name': gname, 'icon': parent_icon or child_icon,
                                      'color': parent_color or '#5a6070',
                                      'total': 0.0, 'children': []})
        g['total'] += float(amount)
        g['children'].append({'name': child_name, 'icon': child_icon,
                              'total': round(float(amount), 2)})
    by_group = sorted(groups.values(), key=lambda g: g['total'], reverse=True)
    for g in by_group:
        g['total'] = round(g['total'], 2)
        g['children'].sort(key=lambda c: c['total'], reverse=True)

    income_rows = (db.session.query(Category.name, Category.icon, func.sum(Transaction.amount))
                   .join(Transaction, Transaction.category_id == Category.id)
                   .filter(Transaction.user_id == user_id,
                           Transaction.txn_type == 'income',
                           Transaction.date >= start, Transaction.date <= end)
                   .group_by(Category.id)
                   .order_by(func.sum(Transaction.amount).desc()).all())

    top_rows = (db.session.query(Transaction.merchant_normalized,
                                 func.sum(Transaction.amount), func.count(Transaction.id))
                .filter(Transaction.user_id == user_id,
                        Transaction.txn_type == 'expense',
                        Transaction.date >= start, Transaction.date <= end)
                .group_by(Transaction.merchant_normalized)
                .order_by(func.sum(Transaction.amount).desc()).limit(10).all())

    txn_count = (Transaction.query
                 .filter(Transaction.user_id == user_id,
                         Transaction.date >= start, Transaction.date <= end).count())

    return {
        'income': round(total('income'), 2),
        'expense': round(total('expense'), 2),
        'by_group': by_group,
        'by_income_source': [{'name': n, 'icon': i, 'total': round(float(t), 2)}
                             for n, i, t in income_rows],
        'top_merchants': [{'merchant': m, 'total': round(float(t), 2), 'count': c}
                          for m, t, c in top_rows],
        'txn_count': txn_count,
    }


def _merge_summary(stored, fresh):
    """Fold late-arriving rows into an existing cycle summary.

    Happens when an old statement is re-uploaded after its cycle was archived
    and contains rows that weren't in the original import.
    """
    stored['income'] = round(stored.get('income', 0) + fresh['income'], 2)
    stored['expense'] = round(stored.get('expense', 0) + fresh['expense'], 2)
    stored['txn_count'] = stored.get('txn_count', 0) + fresh['txn_count']

    def merge_named(key, name_field, extra=()):
        combined = {item[name_field]: dict(item) for item in stored.get(key, [])}
        for item in fresh[key]:
            if item[name_field] in combined:
                combined[item[name_field]]['total'] = round(
                    combined[item[name_field]]['total'] + item['total'], 2)
                for f in extra:
                    combined[item[name_field]][f] = combined[item[name_field]].get(f, 0) + item.get(f, 0)
            else:
                combined[item[name_field]] = dict(item)
        return sorted(combined.values(), key=lambda x: x['total'], reverse=True)

    # Group children aren't merged in detail — collapse to totals-only entries.
    stored['by_group'] = merge_named('by_group', 'name')
    stored['by_income_source'] = merge_named('by_income_source', 'name')
    stored['top_merchants'] = merge_named('top_merchants', 'merchant', extra=('count',))[:10]
    return stored


def _archive_completed_cycles(user):
    """Move everything older than the visible window into the Library.

    Per the user's explicit choice, transaction detail is DELETED after the
    summary is stored; only (account_id, dedup_hash) survives so re-uploading
    an old statement can't re-import the erased rows.
    """
    window_start, _today = _visible_window()
    archived_any = False

    while True:
        oldest = (Transaction.query
                  .filter(Transaction.user_id == user.id, Transaction.date < window_start)
                  .order_by(Transaction.date).first())
        if not oldest:
            break

        cycle_start = _cycle_start_for(oldest.date)
        cycle_end = _cycle_end(cycle_start)
        agg = _aggregate_window(user.id, cycle_start, cycle_end)

        summary = CycleSummary.query.filter_by(user_id=user.id, cycle_start=cycle_start).first()
        if summary:
            merged = _merge_summary({
                'income': summary.income_total, 'expense': summary.expense_total,
                'txn_count': summary.txn_count,
                'by_group': json.loads(summary.by_group or '[]'),
                'by_income_source': json.loads(summary.by_income_source or '[]'),
                'top_merchants': json.loads(summary.top_merchants or '[]'),
            }, agg)
            summary.income_total = merged['income']
            summary.expense_total = merged['expense']
            summary.txn_count = merged['txn_count']
            summary.by_group = json.dumps(merged['by_group'])
            summary.by_income_source = json.dumps(merged['by_income_source'])
            summary.top_merchants = json.dumps(merged['top_merchants'])
        else:
            db.session.add(CycleSummary(
                user_id=user.id, cycle_start=cycle_start, cycle_end=cycle_end,
                income_total=agg['income'], expense_total=agg['expense'],
                txn_count=agg['txn_count'],
                by_group=json.dumps(agg['by_group']),
                by_income_source=json.dumps(agg['by_income_source']),
                top_merchants=json.dumps(agg['top_merchants']),
            ))

        doomed = (Transaction.query
                  .filter(Transaction.user_id == user.id,
                          Transaction.date >= cycle_start, Transaction.date <= cycle_end)
                  .all())
        for txn in doomed:
            if not ArchivedDedup.query.filter_by(account_id=txn.account_id,
                                                 dedup_hash=txn.dedup_hash).first():
                db.session.add(ArchivedDedup(account_id=txn.account_id,
                                             dedup_hash=txn.dedup_hash))
            db.session.delete(txn)

        db.session.commit()
        archived_any = True

    return archived_any


def _summary_row_json(s):
    return {
        'id': s.id,
        'label': _cycle_label(s.cycle_start),
        'cycle_start': s.cycle_start.isoformat(),
        'cycle_end': s.cycle_end.isoformat(),
        'income': round(s.income_total or 0, 2),
        'expense': round(s.expense_total or 0, 2),
        'net': round((s.income_total or 0) - (s.expense_total or 0), 2),
        'txn_count': s.txn_count or 0,
    }


@app.route('/api/summary')
@login_required
def api_summary():
    _archive_completed_cycles(current_user)
    window_start, today = _visible_window()

    agg = _aggregate_window(current_user.id, window_start, today)

    # Compare against the most recently archived cycle.
    prev = (CycleSummary.query.filter_by(user_id=current_user.id)
            .order_by(CycleSummary.cycle_start.desc()).first())
    expense_last = prev.expense_total if prev else 0.0
    pct_change = (round((agg['expense'] - expense_last) / expense_last * 100, 1)
                  if expense_last else None)

    # Trend: the last six cycles — archived ones from their stored summaries,
    # the visible window computed live.
    trend = []
    summaries = {s.cycle_start: s for s in CycleSummary.query.filter_by(user_id=current_user.id)}
    cursor = _cycle_start_for(today)
    starts = []
    for _ in range(6):
        starts.append(cursor)
        cursor = _prev_cycle_start(cursor)
    for cs in reversed(starts):
        label = f"{cs.strftime('%b')}–{_cycle_end(cs).strftime('%b')}"
        if cs in summaries:
            s = summaries[cs]
            trend.append({'month': label, 'expense': round(s.expense_total or 0, 2),
                          'income': round(s.income_total or 0, 2)})
        else:
            ce = _cycle_end(cs)
            trend.append({'month': label,
                          'expense': round(_sum_transactions('expense', cs, ce), 2),
                          'income': round(_sum_transactions('income', cs, ce), 2)})

    cash_cat = Category.query.filter_by(user_id=current_user.id, name='Cash Withdrawal').first()
    cash_withdrawn = 0.0
    if cash_cat:
        cash_withdrawn = float(db.session.query(func.coalesce(func.sum(Transaction.amount), 0.0))
                               .filter(Transaction.user_id == current_user.id,
                                       Transaction.category_id == cash_cat.id,
                                       Transaction.date >= window_start,
                                       Transaction.date <= today).scalar() or 0.0)

    uncat = Category.query.filter_by(user_id=current_user.id, name='Uncategorized').first()
    uncategorized_count = (Transaction.query.filter_by(user_id=current_user.id,
                                                       category_id=uncat.id).count() if uncat else 0)

    return jsonify({
        'window_label': f"{window_start.day} {window_start.strftime('%b')} – today",
        'window_start': window_start.isoformat(),
        'income_this_month': agg['income'],
        'expense_this_month': agg['expense'],
        'net_this_month': round(agg['income'] - agg['expense'], 2),
        'expense_last_month': round(expense_last, 2),
        'pct_change': pct_change,
        'by_group': agg['by_group'],
        'by_income_source': agg['by_income_source'],
        'top_merchants': agg['top_merchants'],
        'monthly_trend': trend,
        'cash_withdrawn': round(cash_withdrawn, 2),
        'uncategorized_count': uncategorized_count,
    })


@app.route('/api/library')
@login_required
def api_library():
    _archive_completed_cycles(current_user)
    summaries = (CycleSummary.query.filter_by(user_id=current_user.id)
                 .order_by(CycleSummary.cycle_start.desc()).all())
    return jsonify([_summary_row_json(s) for s in summaries])


@app.route('/api/library/<int:summary_id>')
@login_required
def api_library_detail(summary_id):
    s = CycleSummary.query.filter_by(id=summary_id, user_id=current_user.id).first_or_404()
    out = _summary_row_json(s)
    out['by_group'] = json.loads(s.by_group or '[]')
    out['by_income_source'] = json.loads(s.by_income_source or '[]')
    out['top_merchants'] = json.loads(s.top_merchants or '[]')
    return jsonify(out)


# --- TEMPORARY diagnostic (remove once the missing-transactions bug is fixed) ---
#
# Read-only. Deliberately does NOT call _archive_completed_cycles(), so opening
# this page can never destroy the evidence it is meant to capture. Exists to
# answer one question with real data: why does a transaction appear in the list
# but not in the SPENT total? The list filters on date only, while SPENT also
# requires txn_type == 'expense' exactly, and the donut additionally INNER JOINs
# the category — so a row can pass one filter and fail another.

@app.route('/debug/diag')
@login_required
def debug_diag():
    from collections import Counter
    window_start, today = _visible_window()
    rows = (Transaction.query.filter(Transaction.user_id == current_user.id)
            .order_by(Transaction.date.desc()).all())

    out = []
    for t in rows:
        in_list = t.date >= window_start
        in_agg = (t.txn_type == 'expense' and window_start <= t.date <= today)
        out.append({
            'id': t.id,
            'date': str(t.date),
            'date_pytype': type(t.date).__name__,
            'txn_type_repr': repr(t.txn_type),
            'amount': t.amount,
            'category_id': t.category_id,
            'category_name': t.category.name if t.category else None,
            'category_kind': t.category.kind if t.category else None,
            'category_source': t.category_source,
            'merchant': t.merchant_normalized,
            'in_list_filter': in_list,
            'in_expense_aggregate': in_agg,
            'LIST_BUT_NOT_SPENT': in_list and not in_agg,
        })

    agg = _aggregate_window(current_user.id, window_start, today)

    return jsonify({
        'server_now_local': datetime.now().isoformat(),
        'date_today': date.today().isoformat(),
        'tz_name': time.tzname,
        'tz_env': os.environ.get('TZ'),
        'window_start': window_start.isoformat(),
        'total_rows_all_time': len(rows),
        'rows_in_list_window': sum(1 for r in out if r['in_list_filter']),
        'rows_in_expense_agg': sum(1 for r in out if r['in_expense_aggregate']),
        'rows_LIST_BUT_NOT_SPENT': sum(1 for r in out if r['LIST_BUT_NOT_SPENT']),
        'txn_type_histogram': dict(Counter(repr(t.txn_type) for t in rows)),
        'null_category_in_window': sum(1 for t in rows
                                       if t.date >= window_start and t.category_id is None),
        'aggregate_expense': agg['expense'],
        'aggregate_income': agg['income'],
        'donut_group_total': round(sum(g['total'] for g in agg['by_group']), 2),
        'cycle_summaries': [{'cycle_start': s.cycle_start.isoformat(),
                             'cycle_end': s.cycle_end.isoformat(),
                             'expense_total': s.expense_total,
                             'income_total': s.income_total,
                             'txn_count': s.txn_count}
                            for s in (CycleSummary.query
                                      .filter_by(user_id=current_user.id)
                                      .order_by(CycleSummary.cycle_start).all())],
        'rows': out,
    })


# --- Portfolio ---

def _latest_snapshot():
    return (PortfolioSnapshot.query.filter_by(user_id=current_user.id)
            .order_by(PortfolioSnapshot.as_of_date.desc(), PortfolioSnapshot.id.desc()).first())


def _snapshot_json(s):
    return {
        'id': s.id, 'as_of_date': s.as_of_date.isoformat(),
        'contribution': s.contribution or 0.0, 'cash_value': s.cash_value or 0.0,
        'total_value': round(s.total_value or 0.0, 2), 'note': s.note,
        'holdings': [{'id': h.id, 'ticker': h.ticker, 'label': h.label,
                      'quantity': h.quantity, 'manual_value': h.manual_value,
                      'currency': h.currency} for h in s.holdings],
    }


@app.route('/api/portfolio')
@login_required
def api_portfolio():
    import market_data

    snapshots = (PortfolioSnapshot.query.filter_by(user_id=current_user.id)
                 .order_by(PortfolioSnapshot.as_of_date).all())
    latest = snapshots[-1] if snapshots else None

    holdings_rows, live_total, live_count = [], 0.0, 0
    if latest:
        holdings_rows, live_total, live_count = market_data.value_holdings(latest.holdings)
        live_total += latest.cash_value or 0.0

    current_value = round(live_total, 2) if latest else 0.0

    # Value history. When market data is available every snapshot is re-priced from
    # the closes for its own date, so the series is on one consistent basis instead
    # of mixing manually-entered values with live ones.
    historical_closes = {}
    if market_data.is_configured() and snapshots:
        for ticker in {h.ticker for s in snapshots for h in s.holdings if h.ticker}:
            historical_closes[ticker] = market_data.get_closes(
                ticker, snapshots[0].as_of_date, date.today())

    def value_at(snapshot):
        """(value in ILS, whether every holding was priced from market data)."""
        total = 0.0
        fully_priced = True
        for h in snapshot.holdings:
            close = market_data.close_on_or_before(historical_closes.get(h.ticker, {}),
                                                   snapshot.as_of_date)
            if close is not None:
                native = close * (h.quantity or 0)
            else:
                native = h.manual_value or 0.0
                fully_priced = False
            rate = market_data.get_fx_rate(h.currency or 'USD') or 1.0
            total += native * rate
        return total + (snapshot.cash_value or 0.0), fully_priced

    priced_values = {}
    consistent = bool(snapshots)
    for s in snapshots:
        value, fully_priced = value_at(s)
        priced_values[s.id] = value
        if not fully_priced:
            consistent = False

    # Only use market-priced values if *every* snapshot could be priced that way —
    # otherwise the chart would compare live prices against hand-entered figures.
    if consistent:
        snapshot_values = priced_values
        if latest and live_count:
            snapshot_values[latest.id] = current_value
    else:
        snapshot_values = {s.id: (s.total_value or 0.0) for s in snapshots}
        if latest:
            current_value = round(snapshot_values[latest.id], 2)

    history = [{'date': s.as_of_date.isoformat(), 'value': round(snapshot_values[s.id], 2)}
               for s in snapshots]

    # Performance vs benchmark, chain-linked Modified Dietz so deposits don't
    # masquerade as returns.
    performance = []
    if len(snapshots) >= 2:
        bench_closes = {}
        if market_data.is_configured():
            bench_closes = market_data.get_closes(
                market_data.BENCHMARK_SYMBOL, snapshots[0].as_of_date, date.today())

        port_index = 100.0
        bench_index = 100.0
        base_close = market_data.close_on_or_before(bench_closes, snapshots[0].as_of_date)
        performance.append({'date': snapshots[0].as_of_date.isoformat(),
                            'portfolio': 100.0,
                            'benchmark': 100.0 if base_close else None})

        for prev, cur in zip(snapshots, snapshots[1:]):
            start_value = snapshot_values.get(prev.id, prev.total_value or 0.0)
            end_value = snapshot_values.get(cur.id, cur.total_value or 0.0)
            flow = cur.contribution or 0.0
            denominator = start_value + 0.5 * flow
            period_return = ((end_value - start_value - flow) / denominator) if denominator > 0 else 0.0
            port_index *= (1 + period_return)

            bench_value = None
            if base_close:
                prev_close = market_data.close_on_or_before(bench_closes, prev.as_of_date)
                cur_close = market_data.close_on_or_before(bench_closes, cur.as_of_date)
                if prev_close and cur_close and prev_close > 0:
                    bench_index *= (cur_close / prev_close)
                    bench_value = round(bench_index, 2)

            performance.append({'date': cur.as_of_date.isoformat(),
                                'portfolio': round(port_index, 2),
                                'benchmark': bench_value})

    return jsonify({
        'current_value': current_value,
        'holdings': holdings_rows,
        'live_prices': bool(live_count) and consistent,
        'market_data_configured': market_data.is_configured(),
        'benchmark_label': market_data.BENCHMARK_LABEL,
        'latest_snapshot': _snapshot_json(latest) if latest else None,
        'history': history,
        'performance': performance,
        'snapshot_count': len(snapshots),
    })


@app.route('/portfolio/snapshots', methods=['POST'])
@login_required
def create_snapshot():
    import market_data

    data = request.get_json(force=True)
    try:
        as_of = datetime.strptime(data['as_of_date'], '%Y-%m-%d').date()
    except (KeyError, ValueError):
        as_of = date.today()

    snapshot = PortfolioSnapshot(
        user_id=current_user.id, as_of_date=as_of,
        contribution=float(data.get('contribution') or 0),
        cash_value=float(data.get('cash_value') or 0),
        note=(data.get('note') or '').strip() or None,
    )
    db.session.add(snapshot)
    db.session.flush()

    holdings = []
    for h in data.get('holdings', []):
        ticker = (h.get('ticker') or '').strip().upper()
        if not ticker:
            continue
        holding = Holding(
            user_id=current_user.id, snapshot_id=snapshot.id, ticker=ticker,
            label=(h.get('label') or '').strip() or None,
            quantity=float(h.get('quantity') or 0),
            manual_value=float(h['manual_value']) if h.get('manual_value') not in (None, '') else None,
            currency=(h.get('currency') or 'USD').upper(),
        )
        db.session.add(holding)
        holdings.append(holding)

    db.session.flush()
    _, total, _live = market_data.value_holdings(holdings)
    snapshot.total_value = round(total + (snapshot.cash_value or 0.0), 2)
    db.session.commit()

    return jsonify(_snapshot_json(snapshot))


@app.route('/portfolio/snapshots')
@login_required
def list_snapshots():
    snaps = (PortfolioSnapshot.query.filter_by(user_id=current_user.id)
             .order_by(PortfolioSnapshot.as_of_date.desc()).all())
    return jsonify([_snapshot_json(s) for s in snaps])


@app.route('/portfolio/snapshots/<int:snapshot_id>', methods=['DELETE'])
@login_required
def delete_snapshot(snapshot_id):
    snap = PortfolioSnapshot.query.filter_by(id=snapshot_id, user_id=current_user.id).first_or_404()
    db.session.delete(snap)
    db.session.commit()
    return jsonify({'status': 'ok'})


# --- Net worth ---

def _latest_balance(item):
    entry = (NetWorthEntry.query.filter_by(item_id=item.id)
             .order_by(NetWorthEntry.as_of_date.desc(), NetWorthEntry.id.desc()).first())
    return (entry.amount if entry else 0.0), (entry.as_of_date.isoformat() if entry else None)


@app.route('/api/networth')
@login_required
def api_networth():
    import market_data

    items = (NetWorthItem.query.filter_by(user_id=current_user.id)
             .order_by(NetWorthItem.sort_order, NetWorthItem.id).all())

    assets_total = 0.0
    liabilities_total = 0.0
    item_rows = []
    emergency_fund = None

    for item in items:
        amount, as_of = _latest_balance(item)
        if item.kind == 'liability':
            liabilities_total += amount
        else:
            assets_total += amount
        row = {'id': item.id, 'name': item.name, 'kind': item.kind, 'icon': item.icon,
               'amount': round(amount, 2), 'as_of': as_of, 'target_amount': item.target_amount}
        item_rows.append(row)
        if item.kind == 'emergency_fund' and emergency_fund is None:
            progress = None
            if item.target_amount:
                progress = round(min(amount / item.target_amount * 100, 100), 1)
            emergency_fund = dict(row, progress=progress)

    latest_snap = _latest_snapshot()
    portfolio_value = 0.0
    if latest_snap:
        _rows, live_total, live_count = market_data.value_holdings(latest_snap.holdings)
        portfolio_value = round(live_total + (latest_snap.cash_value or 0.0), 2) if live_count \
            else round(latest_snap.total_value or 0.0, 2)

    net_worth = round(assets_total + portfolio_value - liabilities_total, 2)

    # 12-month history: each item's most recent balance as of that month end.
    history = []
    for y, m in _month_range(12):
        _s, month_end = _month_span(y, m)
        assets = liabilities = 0.0
        for item in items:
            entry = (NetWorthEntry.query
                     .filter(NetWorthEntry.item_id == item.id, NetWorthEntry.as_of_date <= month_end)
                     .order_by(NetWorthEntry.as_of_date.desc(), NetWorthEntry.id.desc()).first())
            if not entry:
                continue
            if item.kind == 'liability':
                liabilities += entry.amount
            else:
                assets += entry.amount
        snap = (PortfolioSnapshot.query
                .filter(PortfolioSnapshot.user_id == current_user.id,
                        PortfolioSnapshot.as_of_date <= month_end)
                .order_by(PortfolioSnapshot.as_of_date.desc()).first())
        invest = (snap.total_value or 0.0) if snap else 0.0
        history.append({'month': f'{y}-{m:02d}', 'value': round(assets + invest - liabilities, 2)})

    return jsonify({
        'net_worth': net_worth,
        'assets_total': round(assets_total, 2),
        'liabilities_total': round(liabilities_total, 2),
        'portfolio_value': portfolio_value,
        'items': item_rows,
        'emergency_fund': emergency_fund,
        'history': history,
    })


@app.route('/networth/items', methods=['POST'])
@login_required
def create_networth_item():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    item = NetWorthItem(
        user_id=current_user.id, name=name,
        kind=data.get('kind') or 'bank', icon=data.get('icon') or '🏦',
        target_amount=float(data['target_amount']) if data.get('target_amount') not in (None, '') else None,
    )
    db.session.add(item)
    db.session.commit()

    if data.get('amount') not in (None, ''):
        db.session.add(NetWorthEntry(user_id=current_user.id, item_id=item.id,
                                     as_of_date=date.today(), amount=float(data['amount'])))
        db.session.commit()
    return jsonify({'id': item.id, 'name': item.name, 'kind': item.kind})


@app.route('/networth/items/<int:item_id>', methods=['PUT'])
@login_required
def update_networth_item(item_id):
    item = NetWorthItem.query.filter_by(id=item_id, user_id=current_user.id).first_or_404()
    data = request.get_json(force=True)
    if 'name' in data:
        item.name = data['name'].strip()
    if 'target_amount' in data:
        item.target_amount = float(data['target_amount']) if data['target_amount'] not in (None, '') else None
    if data.get('amount') not in (None, ''):
        as_of = date.today()
        existing = NetWorthEntry.query.filter_by(item_id=item.id, as_of_date=as_of).first()
        if existing:
            existing.amount = float(data['amount'])
        else:
            db.session.add(NetWorthEntry(user_id=current_user.id, item_id=item.id,
                                         as_of_date=as_of, amount=float(data['amount'])))
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/networth/items/<int:item_id>', methods=['DELETE'])
@login_required
def delete_networth_item(item_id):
    item = NetWorthItem.query.filter_by(id=item_id, user_id=current_user.id).first_or_404()
    db.session.delete(item)
    db.session.commit()
    return jsonify({'status': 'ok'})


# --- Push notifications ---

@app.route('/push/public-key')
@login_required
def push_public_key():
    import push
    return jsonify({'public_key': push.public_key(), 'configured': push.is_configured()})


@app.route('/push/subscribe', methods=['POST'])
@login_required
def push_subscribe():
    data = request.get_json(force=True)
    endpoint = data.get('endpoint')
    keys = data.get('keys') or {}
    if not endpoint or not keys.get('p256dh') or not keys.get('auth'):
        return jsonify({'error': 'Invalid subscription'}), 400

    existing = PushSubscription.query.filter_by(user_id=current_user.id, endpoint=endpoint).first()
    if existing:
        existing.p256dh = keys['p256dh']
        existing.auth = keys['auth']
    else:
        db.session.add(PushSubscription(user_id=current_user.id, endpoint=endpoint,
                                        p256dh=keys['p256dh'], auth=keys['auth']))
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/push/test', methods=['POST'])
@login_required
def push_test():
    import push
    sent, failed = push.send_to_user(
        current_user, 'SpendTrack', 'Test notification — reminders are working.', '/')
    return jsonify({'sent': sent, 'failed': failed})


# --- Auto-deploy webhook ---

DEPLOY_SECRET = os.environ.get('DEPLOY_SECRET', '')
REPO_DIR = os.environ.get('REPO_DIR', '/home/ubuntu/fitdash')
APP_DIR = os.path.join(REPO_DIR, 'spending-tracker')


@app.route('/deploy', methods=['POST'])
@csrf.exempt
def github_webhook():
    """Pull and restart when GitHub reports a push to the default branch.

    Disabled unless DEPLOY_SECRET is set, and every request must carry a valid
    HMAC signature computed with it — the same scheme the fitness app uses.
    """
    if not DEPLOY_SECRET:
        return jsonify({'status': 'not configured'}), 403

    signature = request.headers.get('X-Hub-Signature-256', '')
    expected = 'sha256=' + hmac.new(DEPLOY_SECRET.encode(), request.data, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return jsonify({'status': 'forbidden'}), 403

    # Only act on pushes to the default branch, so feature branches don't deploy.
    payload = request.get_json(silent=True) or {}
    ref = payload.get('ref')
    if ref and ref not in ('refs/heads/master', 'refs/heads/main'):
        return jsonify({'status': f'ignored {ref}'}), 200

    subprocess.Popen(
        ['bash', '-c',
         f'cd {REPO_DIR} && git pull && '
         f'{APP_DIR}/venv/bin/pip install -q -r {APP_DIR}/requirements.txt && '
         f'sudo systemctl restart spendtrack'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonify({'status': 'deploying'})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
