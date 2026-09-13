import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bot
from licenses import activate_license, get_license_status, is_admin, user_has_access

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "8080"))
MAX_BODY = 14 * 1024 * 1024
MAX_MESSAGE = 12000
MAX_FILE_BYTES = 10 * 1024 * 1024
AUTH_MAX_AGE = 24 * 60 * 60
STYLE_VALUES = {"normal", "short", "detailed"}
ROOT = Path(__file__).resolve().parent
MINIAPP_FILE = ROOT / "miniapp" / "index.html"
if not MINIAPP_FILE.exists():
    MINIAPP_FILE = ROOT / "index.html"


def send_json(handler, status, data):
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise ValueError("Некорректный Content-Length.") from exc
    if length < 0 or length > MAX_BODY:
        raise ValueError("Запрос слишком большой.")
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Некорректный JSON.") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON должен быть объектом.")
    return data


def validate_init_data(init_data):
    if not init_data:
        raise ValueError("Открой Bulba AI через Telegram.")
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не установлен.")
    parsed = urllib.parse.parse_qs(init_data, keep_blank_values=True)
    received_hash = parsed.get("hash", [None])[0]
    auth_date_raw = parsed.get("auth_date", [None])[0]
    if not received_hash or not auth_date_raw:
        raise ValueError("Некорректные Telegram initData.")
    try:
        auth_date = int(auth_date_raw)
    except ValueError as exc:
        raise ValueError("Некорректный auth_date.") from exc
    if abs(int(time.time()) - auth_date) > AUTH_MAX_AGE:
        raise ValueError("Сессия Telegram устарела. Перезапусти Mini App.")
    pairs = [f"{k}={parsed[k][0]}" for k in sorted(parsed) if k != "hash"]
    data_check_string = "\n".join(pairs)
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise ValueError("Неверная подпись Telegram.")
    user_raw = parsed.get("user", [None])[0]
    try:
        tg_user = json.loads(user_raw or "{}")
    except Exception as exc:
        raise ValueError("Некорректный Telegram user.") from exc
    if not tg_user.get("id"):
        raise ValueError("Telegram user ID отсутствует.")
    return tg_user


def auth(handler):
    tg_user = validate_init_data(handler.headers.get("X-Telegram-Init-Data", "").strip())
    uid = str(tg_user["id"])
    return uid, tg_user, bot.get_user(uid)


def access(uid):
    if os.getenv("LICENSE_REQUIRED", "0").strip() != "1" or is_admin(uid):
        return True, ""
    return user_has_access(uid)


def safe_model(model):
    model = "auto" if model is None else str(model).strip()
    if model == "auto":
        return model
    available = {str(x.get("id")) for x in bot.get_models() if isinstance(x, dict) and x.get("id") and not bot.is_bad_model(str(x["id"]))}
    if model not in available:
        raise ValueError("Выбранная модель недоступна.")
    return model


def models_payload():
    out, seen = [], set()
    for item in bot.get_models():
        mid = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
        if not mid or mid in seen or bot.is_bad_model(mid):
            continue
        seen.add(mid)
        out.append({"id": mid, "name": mid})
    return out


def chat_title(cid, chat):
    title = str(chat.get("title") or "").strip()
    if title:
        return title
    prompt = " ".join(str(chat.get("last_prompt") or "").split())
    if prompt:
        return prompt[:42] + ("…" if len(prompt) > 42 else "")
    return "Главный чат" if cid == "main" else "Новый чат"


def state(uid, tg_user, user):
    chats = []
    pinned = {str(x) for x in user.get("pinned_chats", [])}
    for cid, c in user.get("chats", {}).items():
        if not isinstance(c, dict):
            continue
        history = []
        for m in c.get("history", []):
            if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
                history.append({"role": m["role"], "content": str(m.get("content") or "")})
        last = c.get("last_request")
        ts = int(last.get("ts", 0)) if isinstance(last, dict) else int(last or 0) if isinstance(last, (int, float)) else 0
        chats.append({
            "id": str(cid), "title": chat_title(str(cid), c), "messages": history,
            "requests": int(c.get("requests") or 0), "created": int(c.get("created") or 0),
            "last_request": ts, "pinned": str(cid) in pinned,
        })
    chats.sort(key=lambda x: (not x["pinned"], -x["last_request"], -x["created"]))
    allowed, reason = access(uid)
    return {
        "version": bot.BOT_VERSION, "model": user.get("model", "auto"), "style": user.get("style", "normal"),
        "active_chat": str(user.get("active_chat", "main")), "chats": chats,
        "memory": user.get("memory", {}), "favorites": user.get("favorites", []),
        "requests": int(user.get("requests") or 0), "errors": int(user.get("errors") or 0),
        "telegram_user": {k: tg_user.get(k, "") for k in ("id", "first_name", "last_name", "username", "language_code", "photo_url")},
        "license": {"allowed": bool(allowed), "active": bool(allowed), "reason": reason, "required": os.getenv("LICENSE_REQUIRED", "0") == "1", "status": get_license_status(uid)},
        "is_admin": bool(is_admin(uid)), "admin": bool(is_admin(uid)), "rules_count": len(bot.get_global_rules(False)) if is_admin(uid) else 0,
        "stats": {"total_requests": int(bot.db.get("total_requests") or 0), "total_errors": int(bot.db.get("total_errors") or 0)},
    }


def get_chat(user, cid=None):
    cid = str(cid or user.get("active_chat", "main"))
    if cid not in user.get("chats", {}):
        raise ValueError("Чат не найден.")
    return cid, user["chats"][cid]


def commit_chat(user, chat, text, answer, kind="text"):
    bot.add_history(user, "user", text or ("[Изображение]" if kind == "image" else "[Файл]"))
    bot.add_history(user, "assistant", answer)
    chat["last_prompt"] = text or "[Изображение]"
    chat["last_request"] = {"kind": kind, "ts": int(time.time())}
    chat["requests"] = int(chat.get("requests") or 0) + 1
    user["requests"] = int(user.get("requests") or 0) + 1
    bot.db["total_requests"] = int(bot.db.get("total_requests") or 0) + 1
    if not str(chat.get("title") or "").strip() and text:
        clean = " ".join(text.split())
        chat["title"] = clean[:42] + ("…" if len(clean) > 42 else "")


def decode_upload(item):
    if not isinstance(item, dict):
        return None, None, None
    name = str(item.get("name") or "file.txt")[:180]
    mime = str(item.get("mime") or "application/octet-stream")[:100]
    raw = str(item.get("data") or "")
    if not raw.startswith("data:") or "," not in raw:
        raise ValueError("Некорректный файл.")
    payload = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise ValueError("Не удалось прочитать файл.") from exc
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("Файл слишком большой. Максимум 10 МБ.")
    return name, mime, data



def maybe_create_file(text, answer=None):
    plan = bot.local_action_plan(text)
    action = plan.get("action")
    if action not in {"create_xlsx", "create_csv", "create_chart", "create_pdf", "create_docx"}:
        return None
    if action in {"create_xlsx", "create_csv", "create_chart"}:
        rows = bot.parse_rows_from_text(text)
        if len(rows) < 2:
            return None
    if action in {"create_pdf", "create_docx"} and not answer:
        return None
    stem = re.sub(r"[^A-Za-zА-Яа-я0-9_-]+", "_", text[:35]).strip("_") or "bulbamaxai"
    ext = {"create_xlsx":".xlsx","create_csv":".csv","create_chart":".png","create_pdf":".pdf","create_docx":".docx"}[action]
    path = ROOT / f"{stem}_{int(time.time()*1000)}{ext}"
    try:
        if action == "create_xlsx": bot.make_xlsx(rows, path)
        elif action == "create_csv": bot.make_csv(rows, path)
        elif action == "create_chart": bot.make_chart(rows, path)
        elif action == "create_pdf": bot.make_pdf(answer, path, "BulbaMaxAI")
        else: bot.make_docx(answer, path, "BulbaMaxAI")
        raw = path.read_bytes()
        mime = {".xlsx":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",".csv":"text/csv",".png":"image/png",".pdf":"application/pdf",".docx":"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}[ext]
        return {"name": path.name, "mime": mime, "data": base64.b64encode(raw).decode("ascii")}
    finally:
        try: path.unlink(missing_ok=True)
        except OSError: pass

def handle_chat(uid, user, data):
    allowed, reason = access(uid)
    if not allowed:
        raise PermissionError(reason or "Нужна активная лицензия.")
    text = str(data.get("message") or "").strip()
    if len(text) > MAX_MESSAGE:
        raise ValueError("Сообщение слишком длинное.")
    cid, chat = get_chat(user, data.get("chat_id"))
    user["active_chat"] = cid
    if not text and not data.get("image") and not data.get("file"):
        raise ValueError("Сообщение пустое.")
    if not bot.allowed_request(user):
        raise ValueError("Слишком много запросов. Подожди немного.")

    requested = safe_model(data.get("model", user.get("model", "auto")))
    old = user.get("model", "auto")
    user["model"] = requested
    try:
        image = data.get("image")
        upload = data.get("file")
        kind = "text"
        if image:
            if not isinstance(image, str) or not image.startswith("data:image/") or len(image) > 10 * 1024 * 1024:
                raise ValueError("Некорректное или слишком большое изображение.")
            content = [{"type": "text", "text": text or "Проанализируй изображение."}, {"type": "image_url", "image_url": {"url": image}}]
            messages = [{"role": "system", "content": bot.style_system(user)}] + bot.build_history(user) + [{"role": "user", "content": content}]
            answer, error = bot.ai_chat(user, messages, vision=True)
            kind = "image"
        elif upload:
            name, mime, raw = decode_upload(upload)
            file_text = bot.read_text_file(name, raw)
            prompt = f"Файл: {name}\n\nСодержимое:\n{file_text}\n\nЗадача пользователя:\n{text or 'Проанализируй файл.'}"
            messages = [{"role": "system", "content": bot.style_system(user)}] + bot.build_history(user) + [{"role": "user", "content": prompt}]
            answer, error = bot.ai_chat(user, messages, vision=False)
            kind = "file"
        else:
            rule = bot.match_global_rule(text)
            if rule:
                answer, error = rule, None
                kind = "rule"
            else:
                calc = bot.extract_math(text)
                if calc is not None:
                    answer = f"🧮 Ответ: {calc:g}" if isinstance(calc, float) and calc.is_integer() else f"🧮 Ответ: {calc}"
                    error = None
                else:
                    messages = [{"role": "system", "content": bot.style_system(user)}] + bot.build_history(user) + [{"role": "user", "content": text}]
                    answer, error = bot.ai_chat(user, messages, vision=False)
    finally:
        user["model"] = old

    if not answer:
        user["errors"] = int(user.get("errors") or 0) + 1
        bot.db["total_errors"] = int(bot.db.get("total_errors") or 0) + 1
        bot.save_db()
        raise RuntimeError(error or "Не удалось получить ответ.")

    commit_chat(user, chat, text, answer, kind)
    bot.save_db()
    if not bot.consume_license_after_success(uid):
        raise RuntimeError("Лицензия больше не позволяет выполнить запрос.")
    downloadable = maybe_create_file(text, answer=answer) if kind == "text" and text else None
    return {"answer": answer, "model": requested, "file": downloadable, "state": None}


class MiniAppHandler(BaseHTTPRequestHandler):
    server_version = "BulbaMiniApp/16"

    def log_message(self, fmt, *args):
        print("MiniApp:", fmt % args)

    def error(self, status, message):
        send_json(self, status, {"ok": False, "error": str(message)})

    def _do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/miniapp", "/miniapp/"):
                body = MINIAPP_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self' https://telegram.org; script-src 'self' https://telegram.org 'unsafe-inline'; img-src 'self' data: blob: https:; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            if path == "/health":
                send_json(self, 200, {"ok": True, "service": "BulbaMaxAI", "version": bot.BOT_VERSION}); return
            uid, tg_user, user = auth(self)
            if path == "/api/state":
                send_json(self, 200, {"ok": True, "state": state(uid, tg_user, user)}); return
            if path == "/api/models":
                allowed, reason = access(uid)
                if not allowed: raise PermissionError(reason)
                send_json(self, 200, {"ok": True, "models": models_payload(), "selected": safe_model(user.get("model"))}); return
            if path == "/api/stats":
                send_json(self, 200, {"ok": True, "stats": {"requests": int(user.get("requests") or 0), "errors": int(user.get("errors") or 0), "chats": len(user.get("chats", {})), "total_requests": int(bot.db.get("total_requests") or 0), "total_errors": int(bot.db.get("total_errors") or 0), "version": bot.BOT_VERSION}}); return
            if path == "/api/admin/stats":
                if not is_admin(uid): raise PermissionError("Нет доступа.")
                send_json(self, 200, {"ok": True, "stats": {"users": len(bot.db.get("users", {})), "requests": int(bot.db.get("total_requests") or 0), "errors": int(bot.db.get("total_errors") or 0), "rules": len(bot.db.get("rules", [])), "version": bot.BOT_VERSION}}); return
            if path == "/api/admin/rules":
                if not is_admin(uid): raise PermissionError("Нет доступа.")
                send_json(self, 200, {"ok": True, "rules": bot.get_global_rules(False)}); return
            if path == "/api/admin/users":
                if not is_admin(uid): raise PermissionError("Нет доступа.")
                users = []
                for user_id, item in bot.db.get("users", {}).items():
                    users.append({"id": str(user_id), "requests": int(item.get("requests") or 0), "errors": int(item.get("errors") or 0), "chats": len(item.get("chats", {}))})
                users.sort(key=lambda x: x["requests"], reverse=True)
                send_json(self, 200, {"ok": True, "users": users[:200]}); return
            self.error(404, "Страница или API-метод не найден.")
        except PermissionError as exc: self.error(403, exc)
        except (ValueError, RuntimeError) as exc: self.error(400, exc)
        except Exception as exc:
            print("GET error:", repr(exc)); self.error(500, "Внутренняя ошибка сервера.")

    def do_GET(self):
        try:
            uid, _, _ = auth(self)
            with bot.get_user_lock(uid):
                return self._do_GET()
        except PermissionError as exc:
            self.error(403, exc)
        except (ValueError, RuntimeError) as exc:
            self.error(400, exc)
        except Exception as exc:
            print("GET lock/auth error:", repr(exc))
            self.error(500, "Внутренняя ошибка сервера.")

    def _do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            uid, tg_user, user = auth(self)
            data = read_json(self)
            if path == "/api/chat":
                result = handle_chat(uid, user, data)
                result["state"] = state(uid, tg_user, user)
                send_json(self, 200, {"ok": True, **result}); return
            if path == "/api/chat/new":
                if not access(uid)[0]: raise PermissionError(access(uid)[1])
                cid = bot.new_chat(user); bot.save_db(); send_json(self, 200, {"ok": True, "state": state(uid, tg_user, user)}); return
            if path == "/api/chat/select":
                cid, _ = get_chat(user, data.get("chat_id")); user["active_chat"] = cid; bot.save_db(); send_json(self, 200, {"ok": True, "state": state(uid, tg_user, user)}); return
            if path == "/api/chat/rename":
                cid, chat = get_chat(user, data.get("chat_id")); title = " ".join(str(data.get("title") or "").split()).strip()
                if not title: raise ValueError("Название не может быть пустым.")
                chat["title"] = title[:60]; bot.save_db(); send_json(self, 200, {"ok": True, "state": state(uid, tg_user, user)}); return
            if path == "/api/chat/clear":
                cid, chat = get_chat(user, data.get("chat_id")); chat["history"] = []; chat["last_prompt"] = None; chat["last_request"] = None; chat.pop("title", None); bot.save_db(); send_json(self, 200, {"ok": True, "state": state(uid, tg_user, user)}); return
            if path == "/api/chat/delete":
                cid, _ = get_chat(user, data.get("chat_id"))
                if len(user["chats"]) <= 1: raise ValueError("Нельзя удалить последний чат.")
                del user["chats"][cid]; user["active_chat"] = next(iter(user["chats"])); user["pinned_chats"] = [x for x in user.get("pinned_chats", []) if str(x) != cid]; bot.save_db(); send_json(self, 200, {"ok": True, "state": state(uid, tg_user, user)}); return
            if path == "/api/chat/pin":
                cid, _ = get_chat(user, data.get("chat_id")); pins = {str(x) for x in user.get("pinned_chats", [])};
                if bool(data.get("pinned", True)): pins.add(cid)
                else: pins.discard(cid)
                user["pinned_chats"] = list(pins)[:20]; bot.save_db(); send_json(self, 200, {"ok": True, "state": state(uid, tg_user, user)}); return
            if path == "/api/settings":
                if "model" in data: user["model"] = safe_model(data["model"])
                if "style" in data:
                    style = str(data["style"]); 
                    if style not in STYLE_VALUES: raise ValueError("Неизвестный стиль.")
                    user["style"] = style
                bot.save_db(); send_json(self, 200, {"ok": True, "state": state(uid, tg_user, user)}); return
            if path == "/api/memory":
                action = str(data.get("action") or "").lower()
                if action == "clear": user["memory"] = {}
                elif action == "set":
                    key = " ".join(str(data.get("key") or "").split()).strip(); value = " ".join(str(data.get("value") or "").split()).strip()
                    if not key or not value: raise ValueError("Укажи ключ и значение.")
                    user.setdefault("memory", {})[key[:80]] = value[:1000]
                elif action == "delete": user.setdefault("memory", {}).pop(str(data.get("key") or ""), None)
                else: raise ValueError("Неизвестное действие памяти.")
                bot.save_db(); send_json(self, 200, {"ok": True, "state": state(uid, tg_user, user)}); return
            if path == "/api/favorite":
                answer = str(data.get("answer") or "").strip()
                if not answer: raise ValueError("Пустой ответ.")
                fav = user.setdefault("favorites", [])
                item = {"id": int(time.time() * 1000), "text": answer[:12000], "created": int(time.time())}
                fav.append(item); user["favorites"] = fav[-50:]; bot.save_db(); send_json(self, 200, {"ok": True, "state": state(uid, tg_user, user)}); return
            if path == "/api/favorite/delete":
                fid = str(data.get("id")); user["favorites"] = [x for x in user.get("favorites", []) if str(x.get("id")) != fid]; bot.save_db(); send_json(self, 200, {"ok": True, "state": state(uid, tg_user, user)}); return
            if path in ("/api/license/activate", "/api/activate"):
                code = str(data.get("code") or "").strip();
                if not code: raise ValueError("Введи код.")
                ok, message = activate_license(uid, code)
                if not ok: raise ValueError(message)
                send_json(self, 200, {"ok": True, "message": message, "state": state(uid, tg_user, user)}); return
            if path.startswith("/api/admin/"):
                if not is_admin(uid): raise PermissionError("Нет доступа.")
                if path in ("/api/admin/rule", "/api/admin/rules"):
                    action = str(data.get("action") or "create")
                    if action in ("create", "add"):
                        rule = bot.add_global_rule(data.get("pattern"), data.get("response"), data.get("mode", "contains"), data.get("priority", 0))
                    elif action in ("delete", "remove"):
                        if not bot.delete_global_rule(data.get("id")): raise ValueError("Правило не найдено.")
                        rule = None
                    elif action == "toggle":
                        rule = bot.update_global_rule(data.get("id"), enabled=bool(data.get("enabled")))
                    elif action == "update":
                        rule = bot.update_global_rule(data.get("id"), pattern=data.get("pattern"), response=data.get("response"), mode=data.get("mode"), priority=data.get("priority"), enabled=data.get("enabled"))
                    else: raise ValueError("Неизвестное действие правила.")
                    send_json(self, 200, {"ok": True, "rule": rule, "rules": bot.get_global_rules(False)}); return
                if path == "/api/admin/license":
                    target = str(data.get("user_id") or "").strip(); days = int(data.get("days") or 0); limit = int(data.get("requests") or 0)
                    if not target or days <= 0: raise ValueError("Укажи user_id и days.")
                    from licenses import create_license
                    code = create_license(days, limit)
                    ok, message = activate_license(target, code)
                    send_json(self, 200, {"ok": ok, "code": code, "message": message}); return
            self.error(404, "API-метод не найден.")
        except PermissionError as exc: self.error(403, exc)
        except (ValueError, RuntimeError) as exc: self.error(400, exc)
        except Exception as exc:
            print("POST error:", repr(exc)); self.error(500, "Внутренняя ошибка сервера.")


    def do_POST(self):
        try:
            uid, _, _ = auth(self)
            with bot.get_user_lock(uid):
                return self._do_POST()
        except PermissionError as exc:
            self.error(403, exc)
        except (ValueError, RuntimeError) as exc:
            self.error(400, exc)
        except Exception as exc:
            print("POST lock/auth error:", repr(exc))
            self.error(500, "Внутренняя ошибка сервера.")


def run_server():
    server = ThreadingHTTPServer((HOST, PORT), MiniAppHandler)
    print(f"Mini App server: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
