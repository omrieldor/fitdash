import os
import logging
from datetime import datetime, date, timedelta
from cryptography.fernet import Fernet
from garminconnect import Garmin

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

GARMIN_ENCRYPT_KEY = os.environ.get('GARMIN_ENCRYPT_KEY', '')

ACTIVITY_TYPE_MAP = {
    'running': 'run', 'trail_running': 'run', 'treadmill_running': 'run',
    'strength_training': 'hypertrophy',
    'hiit': 'hiit', 'cardio': 'hiit', 'indoor_cardio': 'hiit',
}


def _fernet():
    return Fernet(GARMIN_ENCRYPT_KEY.encode())


def encrypt_password(plain):
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_password(enc):
    return _fernet().decrypt(enc.encode()).decode()


def _clean_exercise_name(raw):
    if not raw or raw == 'UNKNOWN':
        return None
    return raw.replace('_', ' ').title()


def _map_type(type_key):
    return ACTIVITY_TYPE_MAP.get(type_key, 'other')


def _pace_from_speed(avg_speed_mps):
    if not avg_speed_mps or avg_speed_mps <= 0:
        return None
    mins_per_km = 1000 / (avg_speed_mps * 60)
    m = int(mins_per_km)
    s = int((mins_per_km - m) * 60)
    return f'{m}:{s:02d}'


def garmin_login(user):
    if user.garmin_token_store:
        try:
            api = Garmin(user.garmin_email, decrypt_password(user.garmin_password_enc))
            api.login(tokenstore=user.garmin_token_store)
            return api
        except Exception:
            pass
    password = decrypt_password(user.garmin_password_enc)
    api = Garmin(user.garmin_email, password)
    api.login()
    user.garmin_token_store = api.client.dumps()
    return api


def sync_user(user, db_session):
    from models import Workout, WorkoutExercise

    try:
        api = garmin_login(user)
    except Exception as e:
        log.error(f'Auth failed for user {user.id}: {e}')
        user.garmin_sync_status = 'auth_failed'
        db_session.commit()
        return

    try:
        activities = api.get_activities(0, 20)
    except Exception as e:
        log.error(f'Failed to fetch activities for user {user.id}: {e}')
        user.garmin_sync_status = 'error'
        db_session.commit()
        return

    new_count = 0
    for act in activities:
        act_id = str(act.get('activityId', ''))
        if not act_id:
            continue
        existing = Workout.query.filter_by(user_id=user.id, garmin_activity_id=act_id).first()
        if existing:
            continue

        type_key = act.get('activityType', {}).get('typeKey', 'other')
        mapped_type = _map_type(type_key)
        act_name = act.get('activityName', '')
        duration_secs = act.get('duration', 0)
        duration_mins = round(duration_secs / 60) if duration_secs else None
        avg_hr = act.get('averageHR')
        if avg_hr:
            avg_hr = int(avg_hr)

        start_local = act.get('startTimeLocal', '')
        try:
            act_date = datetime.strptime(start_local[:10], '%Y-%m-%d').date()
        except (ValueError, TypeError):
            act_date = date.today()

        if mapped_type == 'other':
            type_label = type_key.replace('_', ' ').title()
            notes = f'{type_label}: {act_name}' if act_name else type_label
        else:
            notes = act_name or ''

        workout = Workout(
            user_id=user.id,
            type=mapped_type,
            duration=duration_mins,
            notes=notes[:500],
            date=act_date,
            garmin_activity_id=act_id
        )
        db_session.add(workout)
        db_session.flush()

        if mapped_type == 'run':
            distance = act.get('distance')
            distance_km = round(distance / 1000, 2) if distance else None
            avg_speed = act.get('averageSpeed')
            pace = _pace_from_speed(avg_speed)
            elevation = act.get('elevationGain')

            db_session.add(WorkoutExercise(
                workout_id=workout.id, order=0, exercise_name='Run',
                distance_km=distance_km, avg_heart_rate=avg_hr,
                avg_speed_min_per_km=pace,
                elevation_m=round(elevation, 1) if elevation else None
            ))

        elif type_key == 'strength_training':
            try:
                sets_data = api.get_activity_exercise_sets(act_id)
                exercises = sets_data.get('exerciseSets', [])
                ex_order = 0
                current_exercise = None
                set_count = 0
                total_reps = 0
                weight_kg = None
                rest_secs = None
                duration_s = None

                for s in exercises:
                    set_type = s.get('setType')
                    ex_name_raw = s.get('exerciseName') or s.get('exerciseCategory')
                    ex_name = _clean_exercise_name(ex_name_raw)

                    if set_type == 'REST':
                        rest_secs = s.get('duration')
                        if rest_secs:
                            rest_secs = int(rest_secs / 1000)
                        continue

                    if ex_name and ex_name != current_exercise and current_exercise is not None:
                        db_session.add(WorkoutExercise(
                            workout_id=workout.id, order=ex_order,
                            exercise_name=current_exercise,
                            sets=set_count, reps=total_reps // max(set_count, 1),
                            weight_kg=weight_kg, rest_seconds=rest_secs,
                            duration_seconds=duration_s, avg_heart_rate=avg_hr
                        ))
                        ex_order += 1
                        set_count = 0
                        total_reps = 0
                        weight_kg = None
                        rest_secs = None
                        duration_s = None

                    if ex_name:
                        current_exercise = ex_name
                    elif current_exercise is None:
                        current_exercise = f'Exercise {ex_order + 1}'

                    set_count += 1
                    reps = s.get('repetitionCount', 0)
                    total_reps += reps or 0
                    w = s.get('weight')
                    if w:
                        weight_kg = round(w / 1000, 1)
                    d = s.get('duration')
                    if d:
                        duration_s = int(d / 1000)

                if current_exercise:
                    db_session.add(WorkoutExercise(
                        workout_id=workout.id, order=ex_order,
                        exercise_name=current_exercise,
                        sets=set_count, reps=total_reps // max(set_count, 1),
                        weight_kg=weight_kg, rest_seconds=rest_secs,
                        duration_seconds=duration_s, avg_heart_rate=avg_hr
                    ))

            except Exception as e:
                log.warning(f'Could not get exercise sets for activity {act_id}: {e}')
                db_session.add(WorkoutExercise(
                    workout_id=workout.id, order=0,
                    exercise_name=act_name or 'Strength Training',
                    avg_heart_rate=avg_hr
                ))

        else:
            distance = act.get('distance')
            distance_km = round(distance / 1000, 2) if distance else None
            elevation = act.get('elevationGain')

            db_session.add(WorkoutExercise(
                workout_id=workout.id, order=0,
                exercise_name=act_name or type_key.replace('_', ' ').title(),
                avg_heart_rate=avg_hr,
                distance_km=distance_km,
                elevation_m=round(elevation, 1) if elevation else None
            ))

        new_count += 1

    # --- Sync Sleep ---
    sleep_count = 0
    try:
        from models import Sleep
        today = date.today()
        for days_ago in range(14):
            d = today - timedelta(days=days_ago)
            date_str = d.isoformat()
            existing_sleep = Sleep.query.filter_by(user_id=user.id, garmin_sleep_id=date_str).first()
            if existing_sleep:
                continue
            try:
                sleep_data = api.get_sleep_data(date_str)
                dto = sleep_data.get('dailySleepDTO', {})
                if not dto or not dto.get('sleepTimeSeconds'):
                    continue
                total_hours = round(dto['sleepTimeSeconds'] / 3600, 1)
                bed_ms = dto.get('sleepStartTimestampLocal')
                wake_ms = dto.get('sleepEndTimestampLocal')
                bedtime = None
                wake_time = None
                if bed_ms:
                    bed_dt = datetime.fromtimestamp(bed_ms / 1000)
                    bedtime = bed_dt.strftime('%H:%M')
                if wake_ms:
                    wake_dt = datetime.fromtimestamp(wake_ms / 1000)
                    wake_time = wake_dt.strftime('%H:%M')
                db_session.add(Sleep(
                    user_id=user.id, hours=total_hours,
                    bedtime=bedtime, wake_time=wake_time,
                    garmin_sleep_id=date_str, date=d
                ))
                sleep_count += 1
            except Exception as e:
                log.debug(f'No sleep data for {date_str}: {e}')
    except Exception as e:
        log.warning(f'Sleep sync failed for user {user.id}: {e}')

    # --- Sync Weight ---
    weight_count = 0
    try:
        from models import Weight
        today = date.today()
        start = today - timedelta(days=30)
        try:
            weigh_ins = api.get_weigh_ins(start.isoformat(), today.isoformat())
            for entry in weigh_ins.get('dateWeightList', []):
                sample_pk = str(entry.get('samplePk', ''))
                if not sample_pk:
                    continue
                existing_w = Weight.query.filter_by(user_id=user.id, garmin_weight_id=sample_pk).first()
                if existing_w:
                    continue
                weight_grams = entry.get('weight')
                if not weight_grams:
                    continue
                weight_kg = round(weight_grams / 1000, 1)
                cal_date = entry.get('calendarDate', '')
                try:
                    w_date = datetime.strptime(cal_date, '%Y-%m-%d').date()
                except (ValueError, TypeError):
                    w_date = today
                db_session.add(Weight(
                    user_id=user.id, value_kg=weight_kg,
                    garmin_weight_id=sample_pk, date=w_date
                ))
                weight_count += 1
        except Exception as e:
            log.warning(f'Weight fetch failed for user {user.id}: {e}')
    except Exception as e:
        log.warning(f'Weight sync failed for user {user.id}: {e}')

    user.garmin_last_sync = datetime.utcnow()
    user.garmin_sync_status = 'ok'
    try:
        user.garmin_token_store = api.client.dumps()
    except Exception:
        pass
    db_session.commit()
    total = new_count + sleep_count + weight_count
    if total:
        log.info(f'User {user.id}: synced {new_count} workouts, {sleep_count} sleep, {weight_count} weight')


def main():
    from app import app
    from models import db, User

    with app.app_context():
        users = User.query.filter_by(garmin_linked=True).all()
        if not users:
            return
        log.info(f'Syncing {len(users)} Garmin-linked users')
        for user in users:
            try:
                sync_user(user, db.session)
            except Exception as e:
                log.error(f'Sync failed for user {user.id}: {e}')
                try:
                    user.garmin_sync_status = 'error'
                    db.session.commit()
                except Exception:
                    db.session.rollback()


if __name__ == '__main__':
    main()
