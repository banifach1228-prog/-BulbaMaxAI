from dataclasses import dataclass
from decimal import Decimal

@dataclass(frozen=True)
class TransactionDraft:
    network: str
    asset: str
    recipient: str
    amount: Decimal
    fee_estimate: Decimal

@dataclass(frozen=True)
class SecurityReview:
    allowed: bool
    risk: str
    reasons: tuple[str, ...]

def review_transaction(draft: TransactionDraft, demo_mode=True):
    if not draft.amount.is_finite() or not draft.fee_estimate.is_finite():
        return SecurityReview(False, 'blocked', ('Amount and fee must be finite numbers.',))
    if draft.amount <= 0:
        return SecurityReview(False, 'blocked', ('Amount must be greater than zero.',))
    if not draft.recipient.strip():
        return SecurityReview(False, 'blocked', ('Recipient is empty.',))
    if draft.fee_estimate < 0:
        return SecurityReview(False, 'blocked', ('Fee cannot be negative.',))
    reasons = ('Demo mode: real blockchain signing is disabled.',) if demo_mode else ()
    return SecurityReview(True, 'review', reasons)

def require_confirmation(review, confirmed):
    if not review.allowed:
        raise PermissionError('Transaction blocked by security policy.')
    if not confirmed:
        raise PermissionError('Explicit user confirmation is required.')
