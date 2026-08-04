"""Monthly "log your spending" reminder.

Run hourly from a systemd timer. Fires on the 8th of the month at 20:00 Israel
time, and catches up on the 9th/10th if the server happened to be down. A
ReminderLog row per (user, month) makes repeat runs a no-op.

    python3 send_reminders.py          # normal scheduled run
    python3 send_reminders.py --force  # send now, ignoring the schedule
"""

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from app import app
from models import db, User, ReminderLog
import push

TZ = ZoneInfo('Asia/Jerusalem')
REMINDER_DAY = 8
REMINDER_HOUR = 20
CATCH_UP_UNTIL_DAY = 10  # still send if the scheduled hour was missed
KIND = 'monthly_log'


def should_send(now):
    if now.day == REMINDER_DAY and now.hour >= REMINDER_HOUR:
        return True
    return REMINDER_DAY < now.day <= CATCH_UP_UNTIL_DAY


def run(force=False):
    now = datetime.now(TZ)
    if not force and not should_send(now):
        return 0

    period = now.strftime('%Y-%m')
    total_sent = 0

    for user in User.query.all():
        already = ReminderLog.query.filter_by(user_id=user.id, kind=KIND, period=period).first()
        if already and not force:
            continue

        sent, failed = push.send_to_user(
            user,
            title='Time to log your spending 💳',
            body='Upload this month\'s statement and review anything uncategorised.',
            url='/',
            tag='monthly-log',
        )
        total_sent += sent

        if sent and not already:
            db.session.add(ReminderLog(user_id=user.id, kind=KIND, period=period))
            db.session.commit()

    return total_sent


if __name__ == '__main__':
    force = '--force' in sys.argv
    with app.app_context():
        if not push.is_configured():
            print('VAPID keys not configured — nothing sent. Run generate_vapid_keys.py first.')
            sys.exit(0)
        count = run(force=force)
        print(f'Reminders sent: {count}')
