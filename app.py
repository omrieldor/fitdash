import os
import json
from datetime import date, timedelta
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User, Workout, WorkoutExercise, Weight, Sleep, Nutrition, WeeklyPlan

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-prod')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///dashboard.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


with app.app_context():
    db.create_all()


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
        username = request.form['username'].strip()
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash('Invalid username or password.')
            return redirect(url_for('login'))
        login_user(user)
        return redirect(url_for('index'))
    return render_template('login.html', mode='login')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# --- Dashboard ---

@app.route('/')
@login_required
def index():
    return render_template('index.html', user=current_user)


# --- Data API ---

@app.route('/data')
@login_required
def get_data():
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
    } for w in current_user.workouts]
    weights = [{'value_kg': w.value_kg, 'date': str(w.date)} for w in current_user.weights]
    sleeps = [{'hours': s.hours, 'bedtime': s.bedtime, 'wake_time': s.wake_time,
               'date': str(s.date)} for s in current_user.sleeps]
    nutritions = [{'calories': n.calories, 'protein': n.protein, 'carbs': n.carbs,
                   'fat': n.fat, 'water_ml': n.water_ml, 'date': str(n.date)}
                  for n in current_user.nutritions]
    plan = WeeklyPlan.query.filter_by(user_id=current_user.id)\
                           .order_by(WeeklyPlan.week_start.desc()).first()
    return jsonify({
        'workouts': workouts,
        'weights': weights,
        'sleeps': sleeps,
        'nutritions': nutritions,
        'phase': current_user.current_phase,
        'weight_target_kg': current_user.weight_target_kg,
        'weekly_plan': plan.plan_text if plan else ''
    })


# --- Log endpoints ---

@app.route('/log/workout', methods=['POST'])
@login_required
def log_workout():
    data = request.get_json()
    entry = Workout(
        user_id=current_user.id,
        type=data.get('type', 'other'),
        duration=_safe_int(data.get('duration'), 1, 600),
        notes=data.get('notes', '')[:500],
        date=date.fromisoformat(data['date']) if data.get('date') else date.today()
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
    entry = Weight(
        user_id=current_user.id,
        value_kg=data['value_kg'],
        date=date.fromisoformat(data['date']) if data.get('date') else date.today()
    )
    db.session.add(entry)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/log/sleep', methods=['POST'])
@login_required
def log_sleep():
    data = request.get_json()
    entry = Sleep(
        user_id=current_user.id,
        hours=data['hours'],
        bedtime=data.get('bedtime', ''),
        wake_time=data.get('wake_time', ''),
        date=date.fromisoformat(data['date']) if data.get('date') else date.today()
    )
    db.session.add(entry)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/log/nutrition', methods=['POST'])
@login_required
def log_nutrition():
    data = request.get_json()
    entry = Nutrition(
        user_id=current_user.id,
        calories=data.get('calories'),
        protein=data.get('protein'),
        carbs=data.get('carbs'),
        fat=data.get('fat'),
        water_ml=data.get('water_ml'),
        date=date.fromisoformat(data['date']) if data.get('date') else date.today()
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
    plan = WeeklyPlan.query.filter_by(user_id=current_user.id, week_start=week_start).first()
    if plan:
        plan.plan_text = data['plan_text']
    else:
        plan = WeeklyPlan(user_id=current_user.id, week_start=week_start, plan_text=data['plan_text'])
        db.session.add(plan)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/settings', methods=['POST'])
@login_required
def update_settings():
    data = request.get_json()
    if 'phase' in data:
        current_user.current_phase = data['phase']
    if 'weight_target_kg' in data:
        current_user.weight_target_kg = float(data['weight_target_kg'])
    db.session.commit()
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(debug=True)
