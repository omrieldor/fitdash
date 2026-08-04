import json
import os
import time
from collections import defaultdict
from datetime import date, timedelta

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import func
from werkzeug.security import generate_password_hash, check_password_hash

from models import db, User, Account, Category, Transaction, CategorizationRule, Import, ensure_default_categories

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
    return User.query.get(int(user_id))


with app.app_context():
    db.create_all()
    db.session.execute(db.text('PRAGMA journal_mode=WAL'))
    db.session.commit()


# --- Rate limiting (mirrors fitdash's simple in-memory limiter) ---

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
        username = request.form['username'].strip()
        email = request.form['email'].strip()
        password = request.form['password']
        if len(password) < 8:
            flash('Password must be at least 8 characters.')
            return redirect(url_for('register'))
        user = User(
            username=username,
            email=email,
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
        username = request.form['username'].strip()
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if not user or not check_password_hash(user.password_hash, password):
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


# --- Page ---

@app.route('/')
@login_required
def index():
    ensure_default_categories(current_user)
    return render_template('index.html')


# --- Categories ---

@app.route('/categories')
@login_required
def list_categories():
    ensure_default_categories(current_user)
    cats = Category.query.filter_by(user_id=current_user.id).order_by(Category.is_system, Category.name).all()
    return jsonify([{'id': c.id, 'name': c.name, 'icon': c.icon, 'color': c.color, 'is_system': c.is_system} for c in cats])


@app.route('/categories', methods=['POST'])
@login_required
def create_category():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    if Category.query.filter_by(user_id=current_user.id, name=name).first():
        return jsonify({'error': 'Category already exists'}), 400
    cat = Category(user_id=current_user.id, name=name, icon=data.get('icon') or '📌', color=data.get('color') or '#5a6070')
    db.session.add(cat)
    db.session.commit()
    return jsonify({'id': cat.id, 'name': cat.name, 'icon': cat.icon, 'color': cat.color, 'is_system': cat.is_system})


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
        user_id=current_user.id,
        name=name,
        institution=(data.get('institution') or '').strip() or None,
        account_type=data.get('account_type') or 'credit_card',
    )
    db.session.add(acct)
    db.session.commit()
    return jsonify({'id': acct.id, 'name': acct.name, 'institution': acct.institution,
                     'account_type': acct.account_type, 'has_mapping': False})


@app.route('/accounts/<int:account_id>', methods=['PUT'])
@login_required
def edit_account(account_id):
    acct = Account.query.filter_by(id=account_id, user_id=current_user.id).first_or_404()
    data = request.get_json(force=True)
    if 'name' in data:
        acct.name = data['name'].strip()
    if 'institution' in data:
        acct.institution = (data['institution'] or '').strip() or None
    if 'account_type' in data:
        acct.account_type = data['account_type']
    if 'column_mapping' in data:
        acct.column_mapping = json.dumps(data['column_mapping'])
    db.session.commit()
    return jsonify({'status': 'ok'})


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
        result = sniff_columns(file.read())
    except ImportError_ as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(result)


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
        import_record = parse_and_import(file.read(), acct, column_mapping, current_user, filename=file.filename)
    except ImportError_ as e:
        return jsonify({'error': str(e)}), 400

    return jsonify({
        'imported_count': import_record.imported_count,
        'duplicate_count': import_record.duplicate_count,
        'row_count': import_record.row_count,
        'date_range_start': import_record.date_range_start.isoformat() if import_record.date_range_start else None,
        'date_range_end': import_record.date_range_end.isoformat() if import_record.date_range_end else None,
    })


@app.route('/imports')
@login_required
def list_imports():
    imports = (Import.query.filter_by(user_id=current_user.id)
               .order_by(Import.created_at.desc()).limit(50).all())
    return jsonify([{
        'id': i.id, 'account_id': i.account_id, 'filename': i.filename,
        'row_count': i.row_count, 'imported_count': i.imported_count,
        'duplicate_count': i.duplicate_count,
        'date_range_start': i.date_range_start.isoformat() if i.date_range_start else None,
        'date_range_end': i.date_range_end.isoformat() if i.date_range_end else None,
        'created_at': i.created_at.isoformat(),
    } for i in imports])


# --- Transactions ---

@app.route('/api/transactions')
@login_required
def api_transactions():
    days = request.args.get('days', 365, type=int)
    since = date.today() - timedelta(days=days)
    txns = (Transaction.query
            .filter(Transaction.user_id == current_user.id, Transaction.date >= since)
            .order_by(Transaction.date.desc())
            .all())
    return jsonify([_txn_json(t) for t in txns])


def _txn_json(t):
    cat = t.category
    return {
        'id': t.id, 'account_id': t.account_id, 'date': t.date.isoformat(),
        'amount': t.amount, 'merchant': t.merchant_normalized, 'merchant_raw': t.merchant_raw,
        'category_id': t.category_id, 'category_name': cat.name if cat else None,
        'category_icon': cat.icon if cat else None, 'category_color': cat.color if cat else None,
        'category_source': t.category_source, 'notes': t.notes,
    }


@app.route('/transactions/<int:transaction_id>', methods=['PUT'])
@login_required
def edit_transaction(transaction_id):
    from spending_import import apply_correction

    txn = Transaction.query.filter_by(id=transaction_id, user_id=current_user.id).first_or_404()
    data = request.get_json(force=True)
    if 'category_id' in data:
        apply_correction(txn, data['category_id'], current_user, retroactive=data.get('retroactive', True))
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


# --- Summary / insights ---

@app.route('/api/summary')
@login_required
def api_summary():
    today = date.today()
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    def spend_total(start, end):
        q = (db.session.query(func.coalesce(func.sum(Transaction.amount), 0.0))
             .filter(Transaction.user_id == current_user.id,
                     Transaction.amount > 0,
                     Transaction.date >= start, Transaction.date <= end))
        return float(q.scalar() or 0.0)

    total_this_month = spend_total(month_start, today)
    total_last_month = spend_total(last_month_start, last_month_end)
    pct_change = None
    if total_last_month > 0:
        pct_change = round((total_this_month - total_last_month) / total_last_month * 100, 1)

    by_category_rows = (db.session.query(Category.name, Category.icon, Category.color,
                                          func.sum(Transaction.amount))
                         .join(Transaction, Transaction.category_id == Category.id)
                         .filter(Transaction.user_id == current_user.id, Transaction.amount > 0,
                                 Transaction.date >= month_start, Transaction.date <= today)
                         .group_by(Category.id)
                         .order_by(func.sum(Transaction.amount).desc())
                         .all())
    by_category = [{'name': n, 'icon': i, 'color': c, 'total': round(float(t), 2)} for n, i, c, t in by_category_rows]

    top_merchants_rows = (db.session.query(Transaction.merchant_normalized,
                                            func.sum(Transaction.amount), func.count(Transaction.id))
                           .filter(Transaction.user_id == current_user.id, Transaction.amount > 0,
                                   Transaction.date >= month_start, Transaction.date <= today)
                           .group_by(Transaction.merchant_normalized)
                           .order_by(func.sum(Transaction.amount).desc())
                           .limit(10)
                           .all())
    top_merchants = [{'merchant': m, 'total': round(float(t), 2), 'count': c} for m, t, c in top_merchants_rows]

    trend = []
    year, month = month_start.year, month_start.month
    for i in range(5, -1, -1):
        y, m = year, month - i
        while m <= 0:
            m += 12
            y -= 1
        m_start = date(y, m, 1)
        next_m = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        m_end = min(next_m - timedelta(days=1), today)
        trend.append({'month': m_start.strftime('%Y-%m'), 'total': round(spend_total(m_start, m_end), 2)})

    uncategorized_cat = Category.query.filter_by(user_id=current_user.id, name='Uncategorized').first()
    uncategorized_count = 0
    if uncategorized_cat:
        uncategorized_count = (Transaction.query
                                .filter_by(user_id=current_user.id, category_id=uncategorized_cat.id)
                                .count())

    return jsonify({
        'total_this_month': round(total_this_month, 2),
        'total_last_month': round(total_last_month, 2),
        'pct_change': pct_change,
        'by_category': by_category,
        'top_merchants': top_merchants,
        'monthly_trend': trend,
        'uncategorized_count': uncategorized_count,
    })


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
