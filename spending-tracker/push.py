"""Web push delivery (VAPID) for the monthly logging reminder.

Requires HTTPS in production — iOS only allows push for a PWA installed to the
Home Screen and served over TLS.
"""

import json
import os

from models import db, PushSubscription

VAPID_SUBJECT = os.environ.get('VAPID_SUBJECT', 'mailto:omrieldor@gmail.com')


def public_key():
    return os.environ.get('VAPID_PUBLIC_KEY', '').strip()


def private_key():
    return os.environ.get('VAPID_PRIVATE_KEY', '').strip()


def is_configured():
    return bool(public_key() and private_key())


def send_to_user(user, title, body, url='/', tag='spendtrack'):
    """Push to every device this user has subscribed. Returns (sent, failed)."""
    if not is_configured():
        return 0, 0

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return 0, 0

    payload = json.dumps({'title': title, 'body': body, 'url': url, 'tag': tag})
    subs = PushSubscription.query.filter_by(user_id=user.id).all()
    sent = failed = 0

    for sub in subs:
        try:
            webpush(
                subscription_info={
                    'endpoint': sub.endpoint,
                    'keys': {'p256dh': sub.p256dh, 'auth': sub.auth},
                },
                data=payload,
                vapid_private_key=private_key(),
                vapid_claims={'sub': VAPID_SUBJECT},
                timeout=10,
            )
            sent += 1
        except WebPushException as e:
            failed += 1
            # 404/410 mean the browser dropped the subscription — stop retrying it.
            status = getattr(e.response, 'status_code', None)
            if status in (404, 410):
                db.session.delete(sub)
        except Exception:
            failed += 1

    db.session.commit()
    return sent, failed
