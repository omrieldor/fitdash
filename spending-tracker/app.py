import json
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import func
from werkzeug.security import generate_password_hash, check_password_hash

from models import (db, User, Account, Category, Transaction, CategorizationRule, Import,
                    NetWorthItem, NetWorthEntry, PortfolioSnapshot, Holding,
                    PushSubscription, ensure_default_categories)

app = Flask(__name__)
_fallback_key = os.environ.get('SECRET_KEY')
if not _fallback_key:
    import warnings
    warnings.warn('SECRET_KEY not set — using insecure dev-only default. Set SECRET_KEY env var in production.')
    _fallback_key = 'spendtrack-dev-insecure-key-change-me'
app.secret_key = _fallback_key
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
    })
    add_missing_columns('portfolio_snapshot', {
        'total_value': 'FLOAT DEFAULT 0',
    })
    db.session.commit()


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


# --- Helpers ---

def _month_bounds(today=None):
    """(this month start, this month end, last month start, last month end).

    The current month runs to its calendar end rather than today, so a statement
    containing charges dated later in the month still counts toward this month.
    """
    today = today or date.today()
    start = today.replace(day=1)
    this_end = (date(start.year + 1, 1, 1) if start.month == 12
                else date(start.year, start.month + 1, 1)) - timedelta(days=1)
    prev_end = start - timedelta(days=1)
    return start, this_end, prev_end.replace(day=1), prev_end


def _month_range(months_back):
    """List of (year, month) tuples ending with the current month."""
    today = date.today()
    out = []
    y, m = today.year, today.month
    for i in range(months_back - 1, -1, -1):
        yy, mm = y, m - i
        while mm <= 0:
            mm += 12
            yy -= 1
        out.append((yy, mm))
    return out


def _month_span(year, month):
    start = date(year, month, 1)
    end = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - timedelta(days=1)
    return start, end


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
        return jsonify(sniff_columns(file.read()))
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
    }


@app.route('/api/transactions')
@login_required
def api_transactions():
    days = request.args.get('days', 365, type=int)
    since = date.today() - timedelta(days=days)
    txns = (Transaction.query
            .filter(Transaction.user_id == current_user.id, Transaction.date >= since)
            .order_by(Transaction.date.desc()).all())
    return jsonify([_txn_json(t) for t in txns])


@app.route('/transactions/<int:transaction_id>', methods=['PUT'])
@login_required
def edit_transaction(transaction_id):
    from spending_import import apply_correction, ImportError_
    txn = Transaction.query.filter_by(id=transaction_id, user_id=current_user.id).first_or_404()
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
    db.session.delete(txn)
    db.session.commit()
    return jsonify({'status': 'ok'})


# --- Spending summary ---

@app.route('/api/summary')
@login_required
def api_summary():
    month_start, month_end, last_start, last_end = _month_bounds()

    income_this = _sum_transactions('income', month_start, month_end)
    expense_this = _sum_transactions('expense', month_start, month_end)
    expense_last = _sum_transactions('expense', last_start, last_end)
    income_last = _sum_transactions('income', last_start, last_end)

    pct_change = None
    if expense_last > 0:
        pct_change = round((expense_this - expense_last) / expense_last * 100, 1)

    # Spending grouped by top-level category, with children nested underneath.
    Child = db.aliased(Category)
    Parent = db.aliased(Category)
    rows = (db.session.query(Child.name, Child.icon, Parent.name, Parent.icon, Parent.color,
                             func.sum(Transaction.amount))
            .join(Transaction, Transaction.category_id == Child.id)
            .outerjoin(Parent, Child.parent_id == Parent.id)
            .filter(Transaction.user_id == current_user.id,
                    Transaction.txn_type == 'expense',
                    Transaction.date >= month_start, Transaction.date <= month_end)
            .group_by(Child.id).all())

    groups = {}
    for child_name, child_icon, parent_name, parent_icon, parent_color, total in rows:
        gname = parent_name or child_name
        gicon = parent_icon or child_icon
        gcolor = parent_color or '#5a6070'
        g = groups.setdefault(gname, {'name': gname, 'icon': gicon, 'color': gcolor,
                                      'total': 0.0, 'children': []})
        g['total'] += float(total)
        g['children'].append({'name': child_name, 'icon': child_icon, 'total': round(float(total), 2)})

    by_group = sorted(groups.values(), key=lambda g: g['total'], reverse=True)
    for g in by_group:
        g['total'] = round(g['total'], 2)
        g['children'].sort(key=lambda c: c['total'], reverse=True)

    # Income broken down by source (Salary, Bonus, ...).
    income_rows = (db.session.query(Category.name, Category.icon, func.sum(Transaction.amount))
                   .join(Transaction, Transaction.category_id == Category.id)
                   .filter(Transaction.user_id == current_user.id,
                           Transaction.txn_type == 'income',
                           Transaction.date >= month_start, Transaction.date <= month_end)
                   .group_by(Category.id)
                   .order_by(func.sum(Transaction.amount).desc()).all())
    by_income_source = [{'name': n, 'icon': i, 'total': round(float(t), 2)} for n, i, t in income_rows]

    top_rows = (db.session.query(Transaction.merchant_normalized,
                                 func.sum(Transaction.amount), func.count(Transaction.id))
                .filter(Transaction.user_id == current_user.id,
                        Transaction.txn_type == 'expense',
                        Transaction.date >= month_start, Transaction.date <= month_end)
                .group_by(Transaction.merchant_normalized)
                .order_by(func.sum(Transaction.amount).desc()).limit(10).all())
    top_merchants = [{'merchant': m, 'total': round(float(t), 2), 'count': c} for m, t, c in top_rows]

    trend = []
    for y, m in _month_range(6):
        s, e = _month_span(y, m)
        trend.append({
            'month': f'{y}-{m:02d}',
            'expense': round(_sum_transactions('expense', s, e), 2),
            'income': round(_sum_transactions('income', s, e), 2),
        })

    cash_cat = Category.query.filter_by(user_id=current_user.id, name='Cash Withdrawal').first()
    cash_withdrawn = 0.0
    if cash_cat:
        cash_withdrawn = float(db.session.query(func.coalesce(func.sum(Transaction.amount), 0.0))
                               .filter(Transaction.user_id == current_user.id,
                                       Transaction.category_id == cash_cat.id,
                                       Transaction.date >= month_start,
                                       Transaction.date <= month_end).scalar() or 0.0)

    uncat = Category.query.filter_by(user_id=current_user.id, name='Uncategorized').first()
    uncategorized_count = (Transaction.query.filter_by(user_id=current_user.id,
                                                       category_id=uncat.id).count() if uncat else 0)

    return jsonify({
        'income_this_month': round(income_this, 2),
        'expense_this_month': round(expense_this, 2),
        'net_this_month': round(income_this - expense_this, 2),
        'income_last_month': round(income_last, 2),
        'expense_last_month': round(expense_last, 2),
        'pct_change': pct_change,
        'by_group': by_group,
        'by_income_source': by_income_source,
        'top_merchants': top_merchants,
        'monthly_trend': trend,
        'cash_withdrawn': round(cash_withdrawn, 2),
        'uncategorized_count': uncategorized_count,
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


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
