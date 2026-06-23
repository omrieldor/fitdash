from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import date

db = SQLAlchemy()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    current_phase = db.Column(db.String(10), default='bulk')  # 'bulk' or 'cut'
    weight_target_kg = db.Column(db.Float, default=83.0)

    workouts = db.relationship('Workout', backref='user', lazy=True, cascade='all, delete-orphan')
    weights = db.relationship('Weight', backref='user', lazy=True, cascade='all, delete-orphan')
    sleeps = db.relationship('Sleep', backref='user', lazy=True, cascade='all, delete-orphan')
    nutritions = db.relationship('Nutrition', backref='user', lazy=True, cascade='all, delete-orphan')
    plans = db.relationship('WeeklyPlan', backref='user', lazy=True, cascade='all, delete-orphan')


class Workout(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    type = db.Column(db.String(50), nullable=False)  # power, hypertrophy, hiit, run, other
    duration = db.Column(db.Integer)  # minutes
    notes = db.Column(db.Text)
    date = db.Column(db.Date, default=date.today)
    exercises = db.relationship('WorkoutExercise', backref='workout', lazy=True,
                                cascade='all, delete-orphan',
                                order_by='WorkoutExercise.order')


class WorkoutExercise(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workout_id = db.Column(db.Integer, db.ForeignKey('workout.id'), nullable=False)
    order = db.Column(db.Integer, default=0)
    exercise_name = db.Column(db.String(100), nullable=False)
    # Strength fields
    sets = db.Column(db.Integer)
    reps = db.Column(db.Integer)
    weight_kg = db.Column(db.Float)
    duration_seconds = db.Column(db.Integer)
    rest_seconds = db.Column(db.Integer)
    # Run fields
    distance_km = db.Column(db.Float)
    avg_heart_rate = db.Column(db.Integer)
    avg_speed_min_per_km = db.Column(db.String(10))
    elevation_m = db.Column(db.Float)


class Weight(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    value_kg = db.Column(db.Float, nullable=False)
    date = db.Column(db.Date, default=date.today)


class Sleep(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    hours = db.Column(db.Float, nullable=False)
    bedtime = db.Column(db.String(10))   # HH:MM
    wake_time = db.Column(db.String(10)) # HH:MM
    date = db.Column(db.Date, default=date.today)


class Nutrition(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    calories = db.Column(db.Integer)
    protein = db.Column(db.Float)  # grams
    carbs = db.Column(db.Float)
    fat = db.Column(db.Float)
    creatine = db.Column(db.Float)  # grams
    water_ml = db.Column(db.Integer)
    date = db.Column(db.Date, default=date.today)


class WeeklyPlan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    week_start = db.Column(db.Date, nullable=False)
    plan_text = db.Column(db.Text)
