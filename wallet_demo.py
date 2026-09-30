from dataclasses import dataclass, field
from decimal import Decimal
from secrets import token_hex
from .config import CONFIG
from .security import TransactionDraft, review_transaction, require_confirmation

@dataclass
class DemoWallet:
    wallet_id: str = field(default_factory=lambda: token_hex(16))
    balances: dict = field(default_factory=lambda: {'USDT': Decimal('1000')})
    history: list = field(default_factory=list)

    @property
    def address(self):
        return 'DEMO_' + self.wallet_id.upper()

    def balance(self, asset='USDT'):
        return self.balances.get(asset.upper(), Decimal('0'))

    def prepare_send(self, recipient, amount, asset='USDT'):
        asset = asset.upper(); amount = Decimal(str(amount))
        if not amount.is_finite() or amount <= 0: raise ValueError('Amount must be a finite positive number.')
        if amount + Decimal('0.10') > self.balance(asset): raise ValueError('Insufficient demo balance.')
        return TransactionDraft('DEMO', asset, recipient, amount, Decimal('0.10'))

    def confirm_demo_send(self, draft):
        if not CONFIG.demo_mode: raise RuntimeError('Demo module is disabled.')
        review = review_transaction(draft, demo_mode=True)
        require_confirmation(review, True)
        self.balances[draft.asset] -= draft.amount + draft.fee_estimate
        record = {'type':'demo_send','network':draft.network,'asset':draft.asset,'amount':str(draft.amount),'fee':str(draft.fee_estimate),'recipient':draft.recipient,'status':'simulated'}
        self.history.append(record)
        return record
