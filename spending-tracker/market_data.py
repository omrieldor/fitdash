"""Market data lookups (live quotes, historical closes, FX) via Twelve Data.

Everything here degrades gracefully: if no API key is configured, or the
provider is unreachable, functions return None and callers fall back to the
manually-entered values already stored on each holding.
"""

import os
from datetime import date, datetime, timedelta

import requests

from models import db, PriceCache

API_BASE = 'https://api.twelvedata.com'
BENCHMARK_SYMBOL = 'SPY'          # S&P 500 tracker, used for the "vs market" chart
BENCHMARK_LABEL = 'S&P 500'
BASE_CURRENCY = 'ILS'
QUOTE_TTL_SECONDS = 3600          # re-use a same-day quote for an hour; a portfolio
                                  # logged monthly doesn't need finer resolution, and
                                  # this keeps call volume well inside the free tier
REQUEST_TIMEOUT = 10


def api_key():
    return os.environ.get('MARKET_DATA_API_KEY', '').strip()


def is_configured():
    return bool(api_key())


def _get(endpoint, params):
    if not is_configured():
        return None
    params = dict(params)
    params['apikey'] = api_key()
    try:
        resp = requests.get(f'{API_BASE}/{endpoint}', params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    # Twelve Data signals problems with {"status": "error", "message": ...}
    if isinstance(data, dict) and data.get('status') == 'error':
        return None
    return data


def _cache_get(symbol, as_of, max_age_seconds=None):
    row = PriceCache.query.filter_by(symbol=symbol, as_of_date=as_of).first()
    if not row:
        return None
    if max_age_seconds is not None:
        age = (datetime.utcnow() - (row.fetched_at or datetime.utcnow())).total_seconds()
        if age > max_age_seconds:
            return None
    return row


def _cache_put(symbol, as_of, close, currency):
    row = PriceCache.query.filter_by(symbol=symbol, as_of_date=as_of).first()
    if row:
        row.close = close
        row.currency = currency
        row.fetched_at = datetime.utcnow()
    else:
        db.session.add(PriceCache(symbol=symbol, as_of_date=as_of, close=close,
                                  currency=currency, fetched_at=datetime.utcnow()))
    db.session.commit()


def get_quote(symbol):
    """Latest price for a symbol. Returns (price, currency) or None."""
    symbol = (symbol or '').strip().upper()
    if not symbol:
        return None

    today = date.today()
    cached = _cache_get(symbol, today, max_age_seconds=QUOTE_TTL_SECONDS)
    if cached:
        return cached.close, cached.currency

    data = _get('quote', {'symbol': symbol})
    if not data:
        # fall back to the most recent cached close we have, however old
        stale = (PriceCache.query.filter_by(symbol=symbol)
                 .order_by(PriceCache.as_of_date.desc()).first())
        return (stale.close, stale.currency) if stale else None

    try:
        price = float(data.get('close') or data.get('price'))
    except (TypeError, ValueError):
        return None
    currency = data.get('currency') or 'USD'
    _cache_put(symbol, today, price, currency)
    return price, currency


def get_closes(symbol, start, end):
    """Daily closes for a symbol between two dates. Returns {date: close}."""
    symbol = (symbol or '').strip().upper()
    if not symbol or start > end:
        return {}

    cached_rows = (PriceCache.query
                   .filter(PriceCache.symbol == symbol,
                           PriceCache.as_of_date >= start,
                           PriceCache.as_of_date <= end)
                   .all())
    closes = {r.as_of_date: r.close for r in cached_rows}

    # Only hit the API when the cache clearly doesn't span the window. Markets are
    # shut on weekends/holidays so we never expect a close for every calendar day.
    expected_min = max(1, int((end - start).days * 0.5))
    if len(closes) >= expected_min:
        return closes

    data = _get('time_series', {
        'symbol': symbol,
        'interval': '1day',
        'start_date': start.isoformat(),
        'end_date': end.isoformat(),
        'outputsize': 5000,
    })
    if not data or 'values' not in data:
        return closes

    currency = (data.get('meta') or {}).get('currency', 'USD')
    for row in data['values']:
        try:
            d = datetime.strptime(row['datetime'][:10], '%Y-%m-%d').date()
            close = float(row['close'])
        except (KeyError, TypeError, ValueError):
            continue
        closes[d] = close
        existing = PriceCache.query.filter_by(symbol=symbol, as_of_date=d).first()
        if existing:
            existing.close = close
        else:
            db.session.add(PriceCache(symbol=symbol, as_of_date=d, close=close, currency=currency))
    db.session.commit()
    return closes


def close_on_or_before(closes, target):
    """Pick the close for `target`, walking back over weekends/holidays."""
    if not closes:
        return None
    candidates = [d for d in closes if d <= target]
    if not candidates:
        return None
    return closes[max(candidates)]


def get_fx_rate(from_currency, to_currency=BASE_CURRENCY):
    """Conversion rate, e.g. USD -> ILS. Returns a float or None."""
    from_currency = (from_currency or '').upper()
    to_currency = (to_currency or '').upper()
    if not from_currency or from_currency == to_currency:
        return 1.0

    pair = f'{from_currency}/{to_currency}'
    today = date.today()
    cached = _cache_get(pair, today, max_age_seconds=QUOTE_TTL_SECONDS)
    if cached:
        return cached.close

    data = _get('price', {'symbol': pair})
    rate = None
    if data:
        try:
            rate = float(data['price'])
        except (KeyError, TypeError, ValueError):
            rate = None

    if rate is None:
        stale = PriceCache.query.filter_by(symbol=pair).order_by(PriceCache.as_of_date.desc()).first()
        return stale.close if stale else None

    _cache_put(pair, today, rate, to_currency)
    return rate


def value_holdings(holdings):
    """Price a list of Holding rows, converting everything into ILS.

    Returns (rows, total_ils, live_count) where each row describes one holding
    and notes whether it was priced live or fell back to its manual value.
    """
    rows = []
    total = 0.0
    live_count = 0
    fx_cache = {}

    for h in holdings:
        price = None
        currency = h.currency or 'USD'
        quote = get_quote(h.ticker) if h.ticker else None
        if quote:
            price, currency = quote

        if price is not None:
            native_value = price * (h.quantity or 0)
            is_live = True
        else:
            native_value = h.manual_value if h.manual_value is not None else 0.0
            is_live = False

        if currency not in fx_cache:
            fx_cache[currency] = get_fx_rate(currency) or (1.0 if currency == BASE_CURRENCY else None)
        rate = fx_cache[currency]
        value_ils = native_value * rate if rate else native_value

        if is_live:
            live_count += 1
        total += value_ils

        rows.append({
            'id': h.id,
            'ticker': h.ticker,
            'label': h.label,
            'quantity': h.quantity,
            'price': round(price, 4) if price is not None else None,
            'currency': currency,
            'value_ils': round(value_ils, 2),
            'live': is_live,
        })

    return rows, round(total, 2), live_count
