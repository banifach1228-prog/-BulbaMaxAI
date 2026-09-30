from dataclasses import dataclass, field
from decimal import Decimal
from secrets import token_urlsafe
from threading import RLock
import time

from .wallet_demo import DemoWallet
from .exchange_demo import DemoExchange
from .intent import Intent
from .security import review_transaction


@dataclass
class BulbaXService:
    wallet: DemoWallet = field(default_factory=DemoWallet)
    exchange: DemoExchange = field(default_factory=DemoExchange)
    pending: dict = field(default_factory=dict)
    pending_ttl: int = 300
    _lock: RLock = field(default_factory=RLock, repr=False)

    def _cleanup_pending(self):
        now = time.time()
        expired = [k for k, item in self.pending.items() if now - item['created'] > self.pending_ttl]
        for key in expired:
            self.pending.pop(key, None)

    def handle(self, intent: Intent, user_id: str) -> str | None:
        with self._lock:
            self._cleanup_pending()
            if intent.action == 'wallet':
                return (f'💰 BulbaX Demo Wallet\n\nАдрес: `{self.wallet.address}`\n'
                        f'USDT: {self.wallet.balance():f}\n\n⚠️ Это DEMO-кошелёк. Реальных средств здесь нет.')
            if intent.action == 'balance':
                return (f'💵 Баланс BulbaX\n\nUSDT: {self.wallet.balance():f}\n'
                        f'BTC (биржа): {self.exchange.balances.get("BTC", Decimal("0")):f}')
            if intent.action == 'exchange':
                return '📈 BulbaX Demo Exchange\n\n/buy BTC/USDT 0.01\n\nТорговля использует виртуальные средства.'
            if intent.action == 'invalid':
                return '❌ Формат команды не распознан.\nПример: /send DEMO_ADDRESS 25'
            if intent.action == 'prepare_send':
                if intent.amount is None or intent.recipient is None:
                    return '❌ Недостаточно данных.'
                try:
                    draft = self.wallet.prepare_send(intent.recipient, intent.amount)
                except Exception as e:
                    return f'❌ {e}'
                review = review_transaction(draft, demo_mode=True)
                token = f'send:{user_id}:{token_urlsafe(9)}'
                self.pending[token] = {'draft': draft, 'created': time.time()}
                return (f'🛡️ BulbaX Shield\n\nОтправка: {draft.amount} {draft.asset}\n'
                        f'Получатель: {draft.recipient}\nКомиссия: {draft.fee_estimate} {draft.asset}\n'
                        f'Риск: {review.risk}\n\nПодтверди только отдельной командой:\n/confirm {token}')
            if intent.action == 'demo_buy':
                if intent.amount is None or intent.pair is None:
                    return '❌ Недостаточно данных.'
                try:
                    order = self.exchange.place_market('buy', intent.pair, intent.amount, '50000')
                    return (f'📈 Demo-сделка выполнена\n\n{order.quantity} {order.pair}\n'
                            f'Цена: {order.price}\nСтатус: {order.status}')
                except Exception as e:
                    return f'❌ {e}'
            return None

    def confirm(self, token: str, user_id: str) -> str:
        with self._lock:
            self._cleanup_pending()
            token = str(token or '').strip()
            expected = f'send:{user_id}:'
            if not token.startswith(expected):
                return '❌ Эта операция не принадлежит текущему пользователю.'
            item = self.pending.pop(token, None)
            if item is None:
                return '❌ Операция не найдена, истекла или уже подтверждена.'
            try:
                record = self.wallet.confirm_demo_send(item['draft'])
            except Exception:
                # Never lose a still-valid pending operation because of a transient failure.
                self.pending[token] = item
                raise
            return (f'✅ Demo-транзакция выполнена\n\n{record["amount"]} {record["asset"]} → '
                    f'{record["recipient"]}\nКомиссия: {record["fee"]}')
