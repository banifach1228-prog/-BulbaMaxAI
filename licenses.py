import json
import os
import secrets
import time
from pathlib import Path
from threading import RLock

LICENSE_FILE = "licenses.json"
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
LICENSE_LOCK = RLock()


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


def _save(data):
    tmp = Path(LICENSE_FILE + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, LICENSE_FILE)


def _now():
    return int(time.time())


def admin_ids():
    return ADMIN_IDS


def is_admin(user_id):
    return str(user_id) in ADMIN_IDS


def create_license(days, requests_limit=0):
    days = int(days); requests_limit = int(requests_limit)
    if days <= 0 or days > 3650: raise ValueError("Некорректный срок")
    if requests_limit < 0: raise ValueError("Некорректный лимит")
    with LICENSE_LOCK:
        data = _load()
        while True:
            code = "BULBA-" + secrets.token_hex(4).upper() + "-" + secrets.token_hex(3).upper()
            if code not in data["codes"]: break
        data["codes"][code] = {"days": days, "requests_limit": requests_limit, "used": False, "created_at": _now()}
        _save(data)
        return code


def activate_license(user_id, code):
    uid = str(user_id); code = str(code).strip().upper()
    with LICENSE_LOCK:
        data = _load(); item = data["codes"].get(code)
        if not item: return False, "❌ Код не найден."
        if item.get("used"): return False, "❌ Этот код уже использован."
        days = int(item.get("days", 0))
        if days <= 0: return False, "❌ Код недействителен."
        now = _now(); existing = data["users"].get(uid, {})
        old_until = int(existing.get("expires_at", 0)); start = max(now, old_until)
        expires = start + days * 86400
        data["users"][uid] = {"expires_at": expires, "requests_limit": int(item.get("requests_limit", 0)), "requests_used": 0, "blocked": False}
        item.update({"used": True, "used_by": uid, "used_at": now})
        _save(data)
        return True, f"✅ Доступ активирован до {time.strftime('%d.%m.%Y %H:%M', time.localtime(expires))}."


def user_has_access(user_id):
    uid = str(user_id)
    with LICENSE_LOCK:
        item = _load()["users"].get(uid)
        if not item: return False, "⛔ Активной лицензии нет."
        if item.get("blocked"): return False, "⛔ Доступ заблокирован."
        if int(item.get("expires_at", 0)) <= _now(): return False, "⏰ Срок доступа закончился."
        limit = int(item.get("requests_limit", 0)); used = int(item.get("requests_used", 0))
        if limit > 0 and used >= limit: return False, "📊 Лимит запросов по тарифу исчерпан."
        return True, ""


def consume_request(user_id):
    uid = str(user_id)
    with LICENSE_LOCK:
        data = _load(); item = data["users"].get(uid)
        if not item or item.get("blocked") or int(item.get("expires_at", 0)) <= _now(): return False, "⛔ Доступ недоступен."
        limit = int(item.get("requests_limit", 0)); used = int(item.get("requests_used", 0))
        if limit > 0 and used >= limit: return False, "📊 Лимит запросов исчерпан."
        item["requests_used"] = used + 1
        _save(data)
        return True, ""


def get_license_status(user_id):
    uid = str(user_id)
    with LICENSE_LOCK:
        item = _load()["users"].get(uid)
        if not item: return "👤 Лицензия не найдена."
        expires = int(item.get("expires_at", 0)); now = _now()
        if expires <= now: state = "⏰ Истёк"
        elif item.get("blocked"): state = "🚫 Заблокирован"
        else: state = "✅ Активна"
        limit = int(item.get("requests_limit", 0)); used = int(item.get("requests_used", 0))
        left = "∞" if limit <= 0 else str(max(0, limit - used))
        until = time.strftime('%d.%m.%Y %H:%M', time.localtime(expires)) if expires else "—"
        return f"👤 Профиль\n\nСтатус: {state}\nДо: {until}\nЗапросов осталось: {left}"


def revoke_user(user_id):
    with LICENSE_LOCK:
        data = _load(); uid = str(user_id)
        if uid not in data["users"]: return "❌ Пользователь не найден."
        data["users"][uid]["expires_at"] = 0; _save(data)
        return "✅ Доступ отозван."


def block_user(user_id):
    with LICENSE_LOCK:
        data = _load(); uid = str(user_id)
        data["users"].setdefault(uid, {"expires_at": 0, "requests_limit": 0, "requests_used": 0})["blocked"] = True
        _save(data); return "🚫 Пользователь заблокирован."


def unblock_user(user_id):
    with LICENSE_LOCK:
        data = _load(); uid = str(user_id)
        if uid not in data["users"]: return "❌ Пользователь не найден."
        data["users"][uid]["blocked"] = False; _save(data); return "✅ Пользователь разблокирован."
