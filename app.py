import os
import re

import time
from datetime import date, timedelta
from collections import defaultdict
from sqlalchemy import func
from sqlalchemy.orm import joinedload
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_wtf.csrf import CSRFProtect
from models import db, User, Workout, WorkoutExercise, Weight, Sleep, Nutrition, WeeklyPlan

app = Flask(__name__)
_fallback_key = os.environ.get('SECRET_KEY')
if not _fallback_key:
    import warnings
    warnings.warn('SECRET_KEY not set — using insecure dev-only default. Set SECRET_KEY env var in production.')
    _fallback_key = 'fitdash-dev-insecure-key-change-me'
app.secret_key = _fallback_key
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///dashboard.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


with app.app_context():
    db.create_all()


# --- Rate Limiting ---

_login_attempts = defaultdict(list)
_commander_attempts = defaultdict(list)

def _is_rate_limited(store, key, max_attempts=5, window=30):
    now = time.time()
    store[key] = [t for t in store[key] if now - t < window]
    if not store[key]:
        del store[key]
        return False
    return len(store[key]) >= max_attempts

def _record_attempt(store, key):
    store[key].append(time.time())

def _evict_stale(store, window=60):
    now = time.time()
    stale = [k for k, ts in store.items() if all(now - t >= window for t in ts)]
    for k in stale:
        del store[k]


# --- Commander ---

COMMANDER_PASSWORD = os.environ.get('COMMANDER_PASSWORD')
if not COMMANDER_PASSWORD:
    import warnings
    warnings.warn('COMMANDER_PASSWORD not set — commander mode disabled until env var is configured.')
COMMANDER_USERNAMES = {'omri', 'omri22', 'omri2202'}
COMMANDER_EMAILS = {'omrieldor@gmail.com', 'omrieldor@yahoo.com'}


def can_be_commander(user):
    return (user.username.lower() in COMMANDER_USERNAMES or
            user.email.lower() in COMMANDER_EMAILS)


def is_commander():
    if not session.get('commander', False):
        return False
    return can_be_commander(current_user)


# --- Helpers ---

def _safe_int(val, lo, hi):
    if val is None or val == '':
        return None
    try:
        v = int(val)
        return v if lo <= v <= hi else None
    except (TypeError, ValueError):
        return None

def _safe_float(val, lo, hi):
    if val is None or val == '':
        return None
    try:
        v = float(val)
        return v if lo <= v <= hi else None
    except (TypeError, ValueError):
        return None

def _parse_mmss(val):
    if not val or str(val).strip() == '':
        return None
    val = str(val).strip()
    if ':' in val:
        parts = val.split(':')
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            return None
    try:
        return int(float(val) * 60)
    except (TypeError, ValueError):
        return None

ALLOWED_WORKOUT_TYPES = {'power', 'hypertrophy', 'hiit', 'run', 'other'}
ALLOWED_PHASES = {'bulk', 'cut'}
_TIME_RE = re.compile(r'^\d{1,2}:\d{2}$')

def _safe_time(val):
    if not val or not isinstance(val, str):
        return ''
    val = val.strip()[:5]
    return val if _TIME_RE.match(val) else ''

def _safe_date(val):
    if not val:
        return date.today()
    try:
        return date.fromisoformat(val)
    except (ValueError, TypeError):
        return None

def _safe_workout_type(val):
    return val if val in ALLOWED_WORKOUT_TYPES else 'other'

def _clean_speed(val):
    if not val or str(val).strip() == '':
        return None
    val = str(val).strip()[:10]
    if ':' in val:
        parts = val.split(':')
        try:
            int(parts[0]); int(parts[1])
            return val
        except (ValueError, IndexError):
            return None
    return None


# --- Auth ---

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        email = request.form['email'].strip()
        password = request.form['password']
        if len(password) < 8:
            flash('Password must be at least 8 characters.')
            return redirect(url_for('register'))
        if not re.search(r'\d', password):
            flash('Password must contain at least one number.')
            return redirect(url_for('register'))
        if User.query.filter_by(username=username).first():
            flash('Username already taken.')
            return redirect(url_for('register'))
        if User.query.filter_by(email=email).first():
            flash('Email already registered.')
            return redirect(url_for('register'))
        user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password)
        )
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for('index'))
    return render_template('login.html', mode='register')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.remote_addr or 'unknown'
        _evict_stale(_login_attempts, window=30)
        if _is_rate_limited(_login_attempts, ip, max_attempts=5, window=30):
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


# --- Commander ---

@app.route('/commander/activate', methods=['POST'])
@login_required
def commander_activate():
    if not COMMANDER_PASSWORD:
        return jsonify({'status': 'commander mode not configured'}), 403
    if not can_be_commander(current_user):
        return jsonify({'status': 'forbidden'}), 403
    key = str(current_user.id)
    _evict_stale(_commander_attempts, window=60)
    if _is_rate_limited(_commander_attempts, key, max_attempts=3, window=60):
        return jsonify({'status': 'too many attempts, try again later'}), 429
    data = request.get_json()
    if data.get('password') == COMMANDER_PASSWORD:
        session['commander'] = True
        _commander_attempts.pop(key, None)
        return jsonify({'status': 'ok'})
    _record_attempt(_commander_attempts, key)
    return jsonify({'status': 'wrong password'}), 403


@app.route('/commander/deactivate', methods=['POST'])
@login_required
def commander_deactivate():
    session.pop('commander', None)
    return jsonify({'status': 'ok'})


@app.route('/commander/users')
@login_required
def commander_users():
    if not is_commander():
        return jsonify({'status': 'forbidden'}), 403
    users = User.query.all()
    return jsonify([{'id': u.id, 'username': u.username, 'email': u.email,
                     'phase': u.current_phase, 'weight_target_kg': u.weight_target_kg}
                    for u in users])


@app.route('/commander/user/<int:user_id>', methods=['DELETE'])
@login_required
def commander_delete_user(user_id):
    if not is_commander():
        return jsonify({'status': 'forbidden'}), 403
    if user_id == current_user.id:
        return jsonify({'status': 'cannot delete yourself'}), 400
    user = User.query.get(user_id)
    if not user:
        return jsonify({'status': 'not found'}), 404
    db.session.delete(user)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/commander/user/<int:user_id>', methods=['POST'])
@login_required
def commander_edit_user(user_id):
    if not is_commander():
        return jsonify({'status': 'forbidden'}), 403
    user = User.query.get(user_id)
    if not user:
        return jsonify({'status': 'not found'}), 404
    data = request.get_json()
    if 'username' in data:
        new_name = str(data['username']).strip()[:80]
        existing = User.query.filter(User.username == new_name, User.id != user_id).first()
        if existing:
            return jsonify({'status': 'username already taken'}), 409
        user.username = new_name
    if 'email' in data:
        new_email = str(data['email']).strip()[:120]
        existing = User.query.filter(User.email == new_email, User.id != user_id).first()
        if existing:
            return jsonify({'status': 'email already taken'}), 409
        user.email = new_email
    if 'phase' in data and data['phase'] in ALLOWED_PHASES:
        user.current_phase = data['phase']
    if 'weight_target_kg' in data:
        user.weight_target_kg = _safe_float(data['weight_target_kg'], 0, 500)
    db.session.commit()
    return jsonify({'status': 'ok'})


# --- Dashboard ---

@app.route('/')
@login_required
def index():
    return render_template('index.html', user=current_user,
                           can_commander=can_be_commander(current_user))


# --- Data API ---

@app.route('/data')
@login_required
def get_data():
    target_user = current_user
    uid = request.args.get('user_id')
    if uid and is_commander():
        target_user = User.query.get(int(uid)) or current_user

    days = _safe_int(request.args.get('days'), 1, 3650) or 90
    cutoff = date.today() - timedelta(days=days)
    tid = target_user.id

    wo_query = Workout.query.options(joinedload(Workout.exercises))\
        .filter(Workout.user_id == tid, Workout.date >= cutoff).all()
    wt_query = Weight.query.filter(Weight.user_id == tid, Weight.date >= cutoff).all()
    sl_query = Sleep.query.filter(Sleep.user_id == tid, Sleep.date >= cutoff).all()
    nt_query = Nutrition.query.filter(Nutrition.user_id == tid, Nutrition.date >= cutoff).all()

    workouts = [{
        'id': w.id, 'type': w.type, 'duration': w.duration, 'notes': w.notes,
        'date': str(w.date),
        'exercises': [{
            'name': e.exercise_name, 'sets': e.sets, 'reps': e.reps,
            'weight_kg': e.weight_kg, 'duration_seconds': e.duration_seconds,
            'rest_seconds': e.rest_seconds, 'distance_km': e.distance_km,
            'avg_heart_rate': e.avg_heart_rate, 'avg_speed': e.avg_speed_min_per_km,
            'elevation_m': e.elevation_m
        } for e in w.exercises]
    } for w in wo_query]
    weights = [{'id': w.id, 'value_kg': w.value_kg, 'date': str(w.date)} for w in wt_query]
    sleeps = [{'id': s.id, 'hours': s.hours, 'bedtime': s.bedtime, 'wake_time': s.wake_time,
               'date': str(s.date)} for s in sl_query]
    nutritions = [{'id': n.id, 'calories': n.calories, 'protein': n.protein, 'carbs': n.carbs,
                   'creatine': n.creatine, 'water_ml': n.water_ml, 'date': str(n.date)}
                  for n in nt_query]
    plan = WeeklyPlan.query.filter_by(user_id=tid)\
                           .order_by(WeeklyPlan.week_start.desc()).first()
    return jsonify({
        'workouts': workouts,
        'weights': weights,
        'sleeps': sleeps,
        'nutritions': nutritions,
        'phase': target_user.current_phase,
        'weight_target_kg': target_user.weight_target_kg,
        'weekly_plan': plan.plan_text if plan else ''
    })


# --- Log endpoints ---

@app.route('/log/workout', methods=['POST'])
@login_required
def log_workout():
    data = request.get_json()
    parsed_date = _safe_date(data.get('date'))
    if parsed_date is None:
        return jsonify({'status': 'invalid date format'}), 400
    entry = Workout(
        user_id=current_user.id,
        type=_safe_workout_type(data.get('type', 'other')),
        duration=_safe_int(data.get('duration'), 1, 600),
        notes=data.get('notes', '')[:500],
        date=parsed_date
    )
    db.session.add(entry)
    db.session.flush()

    for i, ex in enumerate(data.get('exercises', [])):
        name = str(ex.get('name', '')).strip()[:100]
        if not name:
            continue
        db.session.add(WorkoutExercise(
            workout_id=entry.id,
            order=i,
            exercise_name=name,
            sets=_safe_int(ex.get('sets'), 1, 20),
            reps=_safe_int(ex.get('reps'), 1, 200),
            weight_kg=_safe_float(ex.get('weight_kg'), 0, 500),
            duration_seconds=_parse_mmss(ex.get('time')),
            rest_seconds=_parse_mmss(ex.get('rest')),
            distance_km=_safe_float(ex.get('distance_km'), 0.01, 200),
            avg_heart_rate=_safe_int(ex.get('avg_heart_rate'), 40, 230),
            avg_speed_min_per_km=_clean_speed(ex.get('avg_speed')),
            elevation_m=_safe_float(ex.get('elevation_m'), -5000, 9000)
        ))

    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/log/weight', methods=['POST'])
@login_required
def log_weight():
    data = request.get_json()
    parsed_date = _safe_date(data.get('date'))
    if parsed_date is None:
        return jsonify({'status': 'invalid date format'}), 400
    entry = Weight(
        user_id=current_user.id,
        value_kg=_safe_float(data.get('value_kg'), 0, 500),
        date=parsed_date
    )
    db.session.add(entry)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/log/sleep', methods=['POST'])
@login_required
def log_sleep():
    data = request.get_json()
    parsed_date = _safe_date(data.get('date'))
    if parsed_date is None:
        return jsonify({'status': 'invalid date format'}), 400
    entry = Sleep(
        user_id=current_user.id,
        hours=_safe_float(data.get('hours'), 0, 24) or 0,
        bedtime=_safe_time(data.get('bedtime')),
        wake_time=_safe_time(data.get('wake_time')),
        date=parsed_date
    )
    db.session.add(entry)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/log/nutrition', methods=['POST'])
@login_required
def log_nutrition():
    data = request.get_json()
    parsed_date = _safe_date(data.get('date'))
    if parsed_date is None:
        return jsonify({'status': 'invalid date format'}), 400
    entry = Nutrition(
        user_id=current_user.id,
        calories=_safe_int(data.get('calories'), 0, 50000),
        protein=_safe_float(data.get('protein'), 0, 5000),
        carbs=_safe_float(data.get('carbs'), 0, 50000),
        creatine=_safe_float(data.get('creatine'), 0, 100),
        water_ml=_safe_int(data.get('water_ml'), 0, 20000),
        date=parsed_date
    )
    db.session.add(entry)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/log/plan', methods=['POST'])
@login_required
def log_plan():
    data = request.get_json()
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    plan_text = str(data.get('plan_text', ''))[:5000]
    plan = WeeklyPlan.query.filter_by(user_id=current_user.id, week_start=week_start).first()
    if plan:
        plan.plan_text = plan_text
    else:
        plan = WeeklyPlan(user_id=current_user.id, week_start=week_start, plan_text=plan_text)
        db.session.add(plan)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/delete/workout/<int:workout_id>', methods=['DELETE'])
@login_required
def delete_workout(workout_id):
    if is_commander():
        workout = Workout.query.get(workout_id)
    else:
        workout = Workout.query.filter_by(id=workout_id, user_id=current_user.id).first()
    if not workout:
        return jsonify({'status': 'not found'}), 404
    db.session.delete(workout)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/delete/weight/<int:weight_id>', methods=['DELETE'])
@login_required
def delete_weight(weight_id):
    if is_commander():
        entry = Weight.query.get(weight_id)
    else:
        entry = Weight.query.filter_by(id=weight_id, user_id=current_user.id).first()
    if not entry:
        return jsonify({'status': 'not found'}), 404
    db.session.delete(entry)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/delete/sleep/<int:sleep_id>', methods=['DELETE'])
@login_required
def delete_sleep(sleep_id):
    if is_commander():
        entry = Sleep.query.get(sleep_id)
    else:
        entry = Sleep.query.filter_by(id=sleep_id, user_id=current_user.id).first()
    if not entry:
        return jsonify({'status': 'not found'}), 404
    db.session.delete(entry)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/delete/nutrition/<int:nutrition_id>', methods=['DELETE'])
@login_required
def delete_nutrition(nutrition_id):
    if is_commander():
        entry = Nutrition.query.get(nutrition_id)
    else:
        entry = Nutrition.query.filter_by(id=nutrition_id, user_id=current_user.id).first()
    if not entry:
        return jsonify({'status': 'not found'}), 404
    db.session.delete(entry)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/edit/weight/<int:weight_id>', methods=['PUT'])
@login_required
def edit_weight(weight_id):
    if is_commander():
        entry = Weight.query.get(weight_id)
    else:
        entry = Weight.query.filter_by(id=weight_id, user_id=current_user.id).first()
    if not entry:
        return jsonify({'status': 'not found'}), 404
    data = request.get_json()
    if 'value_kg' in data:
        entry.value_kg = _safe_float(data['value_kg'], 0, 500)
    if 'date' in data:
        d = _safe_date(data['date'])
        if d is None:
            return jsonify({'status': 'invalid date format'}), 400
        entry.date = d
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/edit/sleep/<int:sleep_id>', methods=['PUT'])
@login_required
def edit_sleep(sleep_id):
    if is_commander():
        entry = Sleep.query.get(sleep_id)
    else:
        entry = Sleep.query.filter_by(id=sleep_id, user_id=current_user.id).first()
    if not entry:
        return jsonify({'status': 'not found'}), 404
    data = request.get_json()
    if 'bedtime' in data:
        entry.bedtime = _safe_time(data['bedtime'])
    if 'wake_time' in data:
        entry.wake_time = _safe_time(data['wake_time'])
    if 'bedtime' in data or 'wake_time' in data:
        b = entry.bedtime or '00:00'
        w = entry.wake_time or '00:00'
        if _TIME_RE.match(b) and _TIME_RE.match(w):
            bh, bm = map(int, b.split(':'))
            wh, wm = map(int, w.split(':'))
            bed_mins = bh * 60 + bm
            wake_mins = wh * 60 + wm
            if wake_mins <= bed_mins:
                wake_mins += 24 * 60
            entry.hours = round((wake_mins - bed_mins) / 60, 1)
    if 'date' in data:
        d = _safe_date(data['date'])
        if d is None:
            return jsonify({'status': 'invalid date format'}), 400
        entry.date = d
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/edit/nutrition/<int:nutrition_id>', methods=['PUT'])
@login_required
def edit_nutrition(nutrition_id):
    if is_commander():
        entry = Nutrition.query.get(nutrition_id)
    else:
        entry = Nutrition.query.filter_by(id=nutrition_id, user_id=current_user.id).first()
    if not entry:
        return jsonify({'status': 'not found'}), 404
    data = request.get_json()
    if 'calories' in data:
        entry.calories = _safe_int(data['calories'], 0, 50000)
    if 'protein' in data:
        entry.protein = _safe_float(data['protein'], 0, 5000)
    if 'carbs' in data:
        entry.carbs = _safe_float(data['carbs'], 0, 50000)
    if 'creatine' in data:
        entry.creatine = _safe_float(data['creatine'], 0, 100)
    if 'water_ml' in data:
        entry.water_ml = _safe_int(data['water_ml'], 0, 20000)
    if 'date' in data:
        d = _safe_date(data['date'])
        if d is None:
            return jsonify({'status': 'invalid date format'}), 400
        entry.date = d
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/edit/workout/<int:workout_id>', methods=['PUT'])
@login_required
def edit_workout(workout_id):
    if is_commander():
        entry = Workout.query.get(workout_id)
    else:
        entry = Workout.query.filter_by(id=workout_id, user_id=current_user.id).first()
    if not entry:
        return jsonify({'status': 'not found'}), 404
    data = request.get_json()
    if 'type' in data:
        entry.type = _safe_workout_type(data['type'])
    if 'duration' in data:
        entry.duration = _safe_int(data['duration'], 1, 600)
    if 'notes' in data:
        entry.notes = str(data['notes'])[:500]
    if 'date' in data:
        d = _safe_date(data['date'])
        if d is None:
            return jsonify({'status': 'invalid date format'}), 400
        entry.date = d
    if 'exercises' in data:
        WorkoutExercise.query.filter_by(workout_id=entry.id).delete()
        for i, ex in enumerate(data['exercises']):
            name = str(ex.get('name', '')).strip()[:100]
            if not name:
                continue
            db.session.add(WorkoutExercise(
                workout_id=entry.id,
                order=i,
                exercise_name=name,
                sets=_safe_int(ex.get('sets'), 1, 20),
                reps=_safe_int(ex.get('reps'), 1, 200),
                weight_kg=_safe_float(ex.get('weight_kg'), 0, 500),
                duration_seconds=_parse_mmss(ex.get('time')),
                rest_seconds=_parse_mmss(ex.get('rest')),
                distance_km=_safe_float(ex.get('distance_km'), 0.01, 200),
                avg_heart_rate=_safe_int(ex.get('avg_heart_rate'), 40, 230),
                avg_speed_min_per_km=_clean_speed(ex.get('avg_speed')),
                elevation_m=_safe_float(ex.get('elevation_m'), -5000, 9000)
            ))
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/settings', methods=['POST'])
@login_required
def update_settings():
    data = request.get_json()
    if 'phase' in data and data['phase'] in ALLOWED_PHASES:
        current_user.current_phase = data['phase']
    if 'weight_target_kg' in data:
        current_user.weight_target_kg = _safe_float(data['weight_target_kg'], 0, 500)
    db.session.commit()
    return jsonify({'status': 'ok'})


_lb_cache = {'data': None, 'expires': 0}

@app.route('/leaderboard')
@login_required
def leaderboard():
    now = time.time()
    if _lb_cache['data'] and now < _lb_cache['expires']:
        return jsonify(_lb_cache['data'])

    today = date.today()
    month_start = today.replace(day=1)
    if today.month == 12:
        month_end = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        month_end = today.replace(month=today.month + 1, day=1) - timedelta(days=1)

    date_filter = lambda model: (model.date >= month_start, model.date <= month_end)

    weight_stats = db.session.query(
        User.username, func.round(func.avg(Weight.value_kg), 1)
    ).join(Weight).filter(*date_filter(Weight)).group_by(User.id).all()

    carb_stats = db.session.query(
        User.username, func.round(func.avg(func.coalesce(Nutrition.carbs, 0)), 1)
    ).join(Nutrition).filter(*date_filter(Nutrition)).group_by(User.id).all()

    protein_stats = db.session.query(
        User.username, func.round(func.avg(func.coalesce(Nutrition.protein, 0)), 1)
    ).join(Nutrition).filter(*date_filter(Nutrition)).group_by(User.id).all()

    sleep_stats = db.session.query(
        User.username, func.round(func.sum(func.coalesce(Sleep.hours, 0)), 1)
    ).join(Sleep).filter(*date_filter(Sleep)).group_by(User.id).all()

    workout_stats = db.session.query(
        User.username, func.count(Workout.id)
    ).join(Workout).filter(*date_filter(Workout)).group_by(User.id).all()

    def to_ranking(stats):
        ranked = sorted(stats, key=lambda x: x[1] or 0, reverse=True)
        return [{'username': r[0], 'value': float(r[1] or 0)} for r in ranked if r[1] and r[1] > 0][:20]

    categories = [
        {'name': 'Queen of the Month', 'subtitle': 'Highest avg weight (kg)', 'ranking': to_ranking(weight_stats)},
        {'name': 'The Vacuum', 'subtitle': 'Highest avg carbs (g)', 'ranking': to_ranking(carb_stats)},
        {'name': 'Bob the Builder', 'subtitle': 'Highest avg protein (g)', 'ranking': to_ranking(protein_stats)},
        {'name': 'Lazy Smurf', 'subtitle': 'Most total sleep (h)', 'ranking': to_ranking(sleep_stats)},
        {'name': 'The Machine', 'subtitle': 'Most workouts', 'ranking': to_ranking(workout_stats)},
    ]

    month_label = today.strftime('%B %Y')

    result = {'month': month_label, 'categories': categories}
    _lb_cache['data'] = result
    _lb_cache['expires'] = now + 300
    return jsonify(result)


@app.after_request
def set_security_headers(response):
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'"
    )
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    return response


if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true')
