from threading import RLock
from .intent import parse_intent
from .service import BulbaXService

_SERVICES = {}
_SERVICES_LOCK = RLock()


def service_for(user_id):
    key = str(user_id)
    with _SERVICES_LOCK:
        return _SERVICES.setdefault(key, BulbaXService())


def handle_text(user_id, text):
    """Return a BulbaX response or None so the existing AI bot can handle normal chat."""
    s = str(text or '').strip()
    if s.casefold().startswith('/confirm '):
        return service_for(user_id).confirm(s.split(None, 1)[1], str(user_id))
    intent = parse_intent(s)
    if intent is None:
        return None
    try:
        return service_for(user_id).handle(intent, str(user_id))
    except Exception as e:
        return f'❌ BulbaX: {e}'
