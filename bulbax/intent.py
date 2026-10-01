from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

@dataclass(frozen=True)
class Intent:
    action: str
    amount: Decimal | None = None
    asset: str = "USDT"
    recipient: str | None = None
    pair: str | None = None
    side: str | None = None


def parse_intent(text: str) -> Intent | None:
    s = " ".join(str(text or "").strip().split())
    low = s.casefold()
    if low in {"/wallet", "кошелек", "кошелёк", "💰 кошелек"}:
        return Intent("wallet")
    if low in {"/balance", "баланс", "💰 баланс", "💵 баланс"}:
        return Intent("balance")
    if low.startswith("/send "):
        p=s.split()
        if len(p) != 3 or len(p[1]) > 128: return Intent("invalid")
        try: amount=Decimal(p[2])
        except InvalidOperation: return Intent("invalid")
        return Intent("prepare_send", amount=amount, recipient=p[1])
    if low.startswith("/buy "):
        p=s.split()
        if len(p) != 3 or "/" not in p[1] or len(p[1]) > 32: return Intent("invalid")
        try: amount=Decimal(p[2])
        except InvalidOperation: return Intent("invalid")
        return Intent("demo_buy", amount=amount, pair=p[1].upper(), side="buy")
    if low in {"/exchange", "биржа", "📈 биржа"}:
        return Intent("exchange")
    return None
