from dataclasses import dataclass
from decimal import Decimal
from threading import RLock

@dataclass
class Order:
    order_id: int
    side: str
    order_type: str
    pair: str
    quantity: Decimal
    price: Decimal | None
    status: str = 'open'


class DemoExchange:
    """In-memory exchange simulator. No real market/network access."""

    SUPPORTED_PAIRS = {'BTC/USDT'}

    def __init__(self, starting_usdt='10000', starting_btc='0'):
        self.balances = {'USDT': Decimal(str(starting_usdt)), 'BTC': Decimal(str(starting_btc))}
        self.orders = []
        self.trades = []
        self._next_id = 1
        self._lock = RLock()

    def _validate(self, side, pair, quantity, price):
        try:
            q, p = Decimal(str(quantity)), Decimal(str(price))
        except Exception as e:
            raise ValueError('Некорректное числовое значение.') from e
        pair = str(pair or '').upper().strip()
        if not q.is_finite() or not p.is_finite() or side not in ('buy', 'sell') or q <= 0 or p <= 0:
            raise ValueError('Некорректная заявка.')
        if pair not in self.SUPPORTED_PAIRS:
            raise ValueError(f'Пара {pair or "?"} пока недоступна в демо.')
        return q, p, pair

    def place_market(self, side, pair, quantity, market_price):
        with self._lock:
            q, p, pair = self._validate(side, pair, quantity, market_price)
            o = Order(self._next_id, side, 'market', pair, q, p, 'filled')
            self._next_id += 1
            self._execute(o, p)
            return o

    def place_limit(self, side, pair, quantity, price):
        with self._lock:
            q, p, pair = self._validate(side, pair, quantity, price)
            o = Order(self._next_id, side, 'limit', pair, q, p)
            self._next_id += 1
            self.orders.append(o)
            return o

    def _execute(self, o, p):
        base, quote = o.pair.split('/', 1)
        value = o.quantity * p
        fee = value * Decimal('0.001')
        if o.side == 'buy':
            if self.balances.get(quote, Decimal('0')) < value + fee:
                raise ValueError('Недостаточно средств в котируемой валюте.')
            self.balances[quote] -= value + fee
            self.balances[base] = self.balances.get(base, Decimal('0')) + o.quantity
        else:
            if self.balances.get(base, Decimal('0')) < o.quantity:
                raise ValueError('Недостаточно базового актива.')
            self.balances[base] -= o.quantity
            self.balances[quote] = self.balances.get(quote, Decimal('0')) + value - fee
        self.trades.append({
            'order_id': o.order_id, 'pair': o.pair, 'side': o.side,
            'quantity': str(o.quantity), 'price': str(p), 'fee': str(fee),
        })
