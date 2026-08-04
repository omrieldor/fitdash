"""Dev-only launcher that serves fake market data.

Lets the portfolio / benchmark charts be exercised locally without a real
MARKET_DATA_API_KEY. Never used in production — deploy runs gunicorn on app:app.
"""

import os
from datetime import date, timedelta

os.environ.setdefault('MARKET_DATA_API_KEY', 'dev-stub')

import market_data
from app import app

_PRICES = {'VOO': 520.0, 'AAPL': 232.5, 'SPY': 566.0, 'TEVA.TA': 41.2}
_DRIFT = {'VOO': 1.0016, 'AAPL': 1.0021, 'SPY': 1.0011, 'TEVA.TA': 1.0009}


def _fake_get(endpoint, params):
    symbol = params.get('symbol', '')
    if endpoint == 'quote':
        if symbol in _PRICES:
            return {'close': str(_PRICES[symbol]), 'currency': 'USD'}
        return None
    if endpoint == 'price':
        return {'price': '3.70'} if symbol == 'USD/ILS' else None
    if endpoint == 'time_series':
        if symbol not in _PRICES:
            return None
        start = date.fromisoformat(params['start_date'])
        end = date.fromisoformat(params['end_date'])
        drift = _DRIFT[symbol]
        # Walk backwards from today's price so the series ends at the live quote.
        days = [d for d in (start + timedelta(n) for n in range((end - start).days + 1))
                if d.weekday() < 5]
        values, price = [], _PRICES[symbol]
        for d in reversed(days):
            values.append({'datetime': d.isoformat(), 'close': f'{price:.2f}'})
            price /= drift
        return {'meta': {'currency': 'USD'}, 'values': values}
    return None


market_data._get = _fake_get

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5001)
