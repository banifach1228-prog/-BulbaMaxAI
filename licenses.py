import json
import os
import secrets
import time
from pathlib import Path

LICENSE_FILE = "licenses.json"
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}


def _load():
    try:
        data = json.loads(Path(LICENSE_FILE).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("codes", {})
            data.setdefault("users", {})
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"codes": {}, "users": {}}


def _save(db):
    tmp = Path(LICENSE_FILE + ".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, LICENSE_FILE)


def admin_ids():
    return ADMIN_IDS


def is_admin(user_id):
    return str(user_id) in ADMIN_IDS


def _now():
    return int(time.time())


def create_license(days, requests_limit=0):
    days = int(days)
    requests_limit = int(requests_limit)
    if days <= 0 or days > 3650:
        raise ValueError("Некорректный срок")
    if requests_limit < 0:
        raise ValueError("Некорректный лимит")
    db = _load()
    while True:
        code = "BULBA-" + secrets.token_hex(4).upper() + "-" + secrets.token_hex(3).upper()
        if code not in db["codes"]:
            break
    db["codes"][code] = {
        "days": days,
        "requests_limit": requests_limit,
        "used": False,
        "created_at": _now(),
    }
    _save(db)
    return code


def activate_license(user_id, code):
    uid = str(user_id)
    code = str(code).strip().upper()
    db = _load()
    item = db["codes"].get(code)
    if not item:
        return False, "❌ Код не найден."
    if item.get("used"):
        return False, "❌ Этот код уже использован."
    if int(item.get("days", 0)) <= 0:
        return False, "❌ Код недействителен."
    start = _now()
    existing = db["users"].get(uid, {})
    # New activation starts now; an already-active subscription is extended.
    old_until = int(existing.get("expires_at", 0))
    start = max(start, old_until) if old_until > start else start
    expires = start + int(item["days"]) * 86400
    db["users"][uid] = {
        "expires_at": expires,
        "requests_limit": int(item.get("requests_limit", 0)),
        "requests_used": 0,
        "blocked": False,
    }
    item["used"] = True
    item["used_by"] = uid
    item["used_at"] = _now()
    _save(db)
    return True, f"✅ Доступ активирован до {time.strftime('%d.%m.%Y %H:%M', time.localtime(expires))}."


def user_has_access(user_id):
    uid = str(user_id)
    db = _load()
    item = db["users"].get(uid)
    if not item:
        return False, "⛔ Активной лицензии нет."
    if item.get("blocked"):
        return False, "⛔ Доступ заблокирован."
    expires = int(item.get("expires_at", 0))
    if expires <= _now():
        return False, "⏰ Срок доступа закончился."
    limit = int(item.get("requests_limit", 0))
    used = int(item.get("requests_used", 0))
    if limit > 0 and used >= limit:
        return False, "📊 Лимит запросов по тарифу исчерпан."
    return True, ""


def consume_request(user_id):
    uid = str(user_id)
    db = _load()
    item = db["users"].get(uid)
    if not item or item.get("blocked") or int(item.get("expires_at", 0)) <= _now():
        return False, "⛔ Доступ недоступен."
    limit = int(item.get("requests_limit", 0))
    used = int(item.get("requests_used", 0))
    if limit > 0 and used >= limit:
        return False, "📊 Лимит запросов исчерпан."
    item["requests_used"] = used + 1
    _save(db)
    return True, ""


def get_license_status(user_id):
    uid = str(user_id)
    db = _load()
    item = db["users"].get(uid)
    if not item:
        return "👤 Лицензия не найдена."
    expires = int(item.get("expires_at", 0))
    if expires <= _now():
        state = "⏰ Истёк"
    elif item.get("blocked"):
        state = "🚫 Заблокирован"
    else:
        state = "✅ Активна"
    limit = int(item.get("requests_limit", 0))
    used = int(item.get("requests_used", 0))
    left = "∞" if limit <= 0 else str(max(0, limit - used))
    return (
        f"👤 Профиль\n\n"
        f"Статус: {state}\n"
        f"До: {time.strftime('%d.%m.%Y %H:%M', time.localtime(expires))}\n"
        f"Запросов осталось: {left}"
    )


def revoke_user(user_id):
    db = _load()
    uid = str(user_id)
    if uid not in db["users"]:
        return "❌ Пользователь не найден."
    db["users"][uid]["expires_at"] = 0
    _save(db)
    return "✅ Доступ отозван."


def block_user(user_id):
    db = _load()
    uid = str(user_id)
    db["users"].setdefault(uid, {"expires_at": 0, "requests_limit": 0, "requests_used": 0})["blocked"] = True
    _save(db)
    return "🚫 Пользователь заблокирован."


def unblock_user(user_id):
    db = _load()
    uid = str(user_id)
    if uid not in db["users"]:
        return "❌ Пользователь не найден."
    db["users"][uid]["blocked"] = False
    _save(db)
    return "✅ Пользователь разблокирован."
