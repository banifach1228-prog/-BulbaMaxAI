import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bot
import media_service
from licenses import activate_license, get_license_status, is_admin, user_has_access

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "3000"))
MAX_BODY = 14 * 1024 * 1024
MAX_MESSAGE = 12000
MAX_FILE_BYTES = 10 * 1024 * 1024
AUTH_MAX_AGE = 24 * 60 * 60
STYLE_VALUES = {"normal", "short", "detailed"}
_REQUEST_CACHE = {}
_REQUEST_CACHE_LOCK = __import__("threading").RLock()
_REQUEST_CACHE_TTL = 90
ROOT = Path(__file__).resolve().parent
MINIAPP_FILE = ROOT / "miniapp" / "index.html"
if not MINIAPP_FILE.exists():
    MINIAPP_FILE = ROOT / "index.html"


def send_json(handler, status, data):
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store, max-age=0")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_html(handler, status, body):
    raw = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


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


def answer_text(uid, user, text, data):
    image = data.get("image")
    upload = data.get("file")
    requested = safe_model(data.get("model", user.get("model", "auto")))
    old = user.get("model", "auto")
    user["model"] = requested
    try:
        if image:
            if not isinstance(image, str) or not image.startswith("data:image/") or len(image) > MAX_FILE_BYTES * 2:
                raise ValueError("Некорректное или слишком большое изображение.")
            content = [{"type":"text","text":text or "Проанализируй изображение."},{"type":"image_url","image_url":{"url":image}}]
            messages = [{"role":"system","content":bot.style_system(user)}] + bot.build_history(user) + [{"role":"user","content":content}]
            return bot.ai_chat(user, messages, vision=True), "image"
        if upload:
            name, mime, raw = decode_upload(upload)
            file_text = bot.read_text_file(name, raw)
            prompt = f"Файл: {name}\n\nСодержимое:\n{file_text}\n\nЗадача пользователя:\n{text or 'Проанализируй файл.'}"
            messages = [{"role":"system","content":bot.style_system(user)}] + bot.build_history(user) + [{"role":"user","content":prompt}]
            return bot.ai_chat(user, messages, vision=False), "file"
        rule = bot.match_global_rule(text)
        if rule:
            return (rule, None), "rule"
        calc = bot.extract_math(text)
        if calc is not None:
            answer = f"🧮 Ответ: {calc:g}" if isinstance(calc,float) and calc.is_integer() else f"🧮 Ответ: {calc}"
            return (answer, None), "text"
        messages = [{"role":"system","content":bot.style_system(user)}] + bot.build_history(user) + [{"role":"user","content":text}]
        result = bot.ai_chat(user, messages, vision=False)
        return result, "text"
    finally:
        user["model"] = old


def handle_chat(uid, tg_user, user, data):
    request_id = str(data.get("request_id") or "").strip()[:100]
    if request_id:
        now = time.time()
        with _REQUEST_CACHE_LOCK:
            for key, item in list(_REQUEST_CACHE.items()):
                if now - item[0] > _REQUEST_CACHE_TTL:
                    _REQUEST_CACHE.pop(key, None)
            cached = _REQUEST_CACHE.get(f"{uid}:{request_id}")
            if cached and now - cached[0] <= _REQUEST_CACHE_TTL:
                return cached[1]

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

    # Media Studio is explicit. Do not accidentally turn normal chat into a media job.
    media_kind = bot.detect_media_intent(text)
    if media_kind and not data.get("image") and not data.get("file"):
        model = str(data.get("media_model") or "").strip() or None
        opts = data.get("media_opts") if isinstance(data.get("media_opts"), dict) else {}
        job_id, selected = media_service.create_job(media_kind, text, model=model, opts=opts, user_id=uid)
        media_service._set_job(job_id, prompt=text[:MAX_MESSAGE])
        return {"job_id": job_id, "kind": media_kind, "model": selected["id"], "state": state(uid,tg_user,user)}

    (answer, error), kind = answer_text(uid, user, text, data)
    if not answer:
        user["errors"] = int(user.get("errors") or 0) + 1
        bot.db["total_errors"] = int(bot.db.get("total_errors") or 0) + 1
        bot.save_db()
        raise RuntimeError(error or "Не удалось получить ответ.")

    # Files are generated only when explicitly requested and enough source data exists.
    output_file = maybe_create_file(text, answer)
    commit_chat(user, chat, text, answer, kind)
    bot.save_db()
    if not bot.consume_license_after_success(uid):
        # The answer was already produced, so never destroy the successful chat record.
        raise RuntimeError("Лицензия больше не позволяет выполнить запрос.")
    result = {"ok": True, "answer": answer, "state": state(uid,tg_user,user)}
    if output_file:
        result["file"] = output_file
    if request_id:
        with _REQUEST_CACHE_LOCK:
            _REQUEST_CACHE[f"{uid}:{request_id}"] = (time.time(), result)
    return result


def require_admin(uid):
    if not is_admin(uid):
        raise PermissionError("Нет доступа.")


def media_job_for_user(uid, job_id):
    item = media_service.job_status(job_id)
    if not item:
        raise ValueError("Медиа-задача не найдена.")
    if str(item.get("user_id")) != str(uid):
        raise PermissionError("Нет доступа к этой медиа-задаче.")
    return item


def finalize_media(uid, tg_user, user, item):
    if item.get("status") != "success":
        return item
    claimed = media_service.claim_success(item["job_id"])
    if not claimed:
        return item
    # Count a media request exactly once.
    if not bot.consume_license_after_success(uid):
        media_service._set_job(item["job_id"], license_error="Лицензионный лимит исчерпан после генерации.")
        return media_service.job_status(item["job_id"])
    cid, chat = get_chat(user, None)
    prompt = str(item.get("prompt") or "Медиа-задача")
    summary = "Медиа создано: " + ", ".join(item.get("urls") or [])[:2000]
    bot.add_history(user, "user", prompt)
    bot.add_history(user, "assistant", summary)
    chat["last_prompt"] = prompt
    chat["last_request"] = {"kind": item.get("kind", "media"), "ts": int(time.time())}
    chat["requests"] = int(chat.get("requests") or 0) + 1
    user["requests"] = int(user.get("requests") or 0) + 1
    bot.db["total_requests"] = int(bot.db.get("total_requests") or 0) + 1
    bot.save_db()
    return media_service.job_status(item["job_id"]) or item


class Handler(BaseHTTPRequestHandler):
    server_version = "BulbaMiniApp/2.0"

    def log_message(self, fmt, *args):
        print("[MINIAPP]", fmt % args)

    def error(self, status, message):
        send_json(self, status, {"ok": False, "error": str(message)})

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                send_html(self, 200, MINIAPP_FILE.read_text(encoding="utf-8"))
            except OSError:
                self.error(500, "Mini App файл не найден.")
            return
        if path == "/health":
            send_json(self, 200, {"ok": True, "service": "BulbaMaxAI", "version": bot.BOT_VERSION})
            return
        try:
            uid, tg_user, user = auth(self)
            if path == "/api/state":
                allowed, reason = access(uid)
                send_json(self, 200, {"ok": True, "state": state(uid,tg_user,user), "access": {"allowed":allowed,"reason":reason}})
                return
            if path == "/api/models":
                allowed, reason = access(uid)
                if not allowed: raise PermissionError(reason)
                send_json(self, 200, {"ok":True,"models":models_payload(),"selected":user.get("model","auto")})
                return
            if path == "/api/media/models":
                allowed, reason = access(uid)
                if not allowed: raise PermissionError(reason)
                send_json(self, 200, {"ok":True,"models":media_service.get_catalog()})
                return
            m = re.fullmatch(r"/api/media/jobs/([^/]+)", path)
            if m:
                allowed, reason = access(uid)
                if not allowed: raise PermissionError(reason)
                item = media_job_for_user(uid, urllib.parse.unquote(m.group(1)))
                item = finalize_media(uid,tg_user,user,item)
                send_json(self,200,item)
                return
            if path == "/api/admin/stats":
                require_admin(uid)
                send_json(self,200,{"ok":True,"stats":{"users":len(bot.db.get("users",{})),"requests":int(bot.db.get("total_requests") or 0),"errors":int(bot.db.get("total_errors") or 0),"rules":len(bot.get_global_rules(False))}})
                return
            if path == "/api/admin/rules":
                require_admin(uid)
                send_json(self,200,{"ok":True,"rules":bot.get_global_rules(False)})
                return
            self.error(404,"Endpoint не найден.")
        except PermissionError as exc:
            self.error(403,str(exc))
        except ValueError as exc:
            self.error(400,str(exc))
        except Exception as exc:
            print("GET error:",repr(exc))
            self.error(500,"Внутренняя серверная ошибка.")

    def do_POST(self):
        try:
            uid, tg_user, user = auth(self)
            data = read_json(self)
            path = urllib.parse.urlparse(self.path).path
            allowed, reason = access(uid)
            if path not in ("/api/activate",) and not allowed:
                raise PermissionError(reason or "Нужна активная лицензия.")

            if path == "/api/chat":
                send_json(self,200,handle_chat(uid,tg_user,user,data)); return
            if path == "/api/chat/new":
                name = bot.new_chat(user); bot.save_db(); send_json(self,200,{"ok":True,"state":state(uid,tg_user,user)}); return
            if path == "/api/chat/select":
                cid = str(data.get("chat_id") or "")
                if cid not in user.get("chats",{}): raise ValueError("Чат не найден.")
                user["active_chat"] = cid; bot.save_db(); send_json(self,200,{"ok":True,"state":state(uid,tg_user,user)}); return
            if path == "/api/chat/rename":
                cid, chat = get_chat(user,data.get("chat_id")); title=" ".join(str(data.get("title") or "").split())[:80]
                if not title: raise ValueError("Название пустое.")
                chat["title"] = title; bot.save_db(); send_json(self,200,{"ok":True,"state":state(uid,tg_user,user)}); return
            if path == "/api/chat/clear":
                cid, chat = get_chat(user,data.get("chat_id")); chat["history"]=[]; chat["last_prompt"]=None; chat["last_request"]=None; bot.save_db(); send_json(self,200,{"ok":True,"state":state(uid,tg_user,user)}); return
            if path == "/api/chat/delete":
                cid, _ = get_chat(user,data.get("chat_id"));
                if cid == "main": raise ValueError("Главный чат удалить нельзя.")
                del user["chats"][cid]; user["active_chat"]="main"; bot.save_db(); send_json(self,200,{"ok":True,"state":state(uid,tg_user,user)}); return
            if path == "/api/chat/pin":
                cid,_=get_chat(user,data.get("chat_id")); pins={str(x) for x in user.get("pinned_chats",[])}
                if cid in pins: pins.remove(cid)
                else: pins.add(cid)
                user["pinned_chats"]=list(pins); bot.save_db(); send_json(self,200,{"ok":True,"state":state(uid,tg_user,user)}); return
            if path == "/api/memory":
                action=str(data.get("action") or "").lower(); mem=user.setdefault("memory",{})
                if action=="set":
                    key=" ".join(str(data.get("key") or "").split())[:80]; value=" ".join(str(data.get("value") or "").split())[:1000]
                    if not key: raise ValueError("Название памяти пустое.")
                    if not value: raise ValueError("Значение памяти пустое.")
                    mem[key]=value
                elif action=="delete": mem.pop(str(data.get("key") or ""),None)
                elif action=="clear": mem.clear()
                else: raise ValueError("Неизвестное действие памяти.")
                bot.save_db(); send_json(self,200,{"ok":True,"state":state(uid,tg_user,user)}); return
            if path == "/api/favorite":
                answer=str(data.get("answer") or "").strip()
                if not answer: raise ValueError("Нечего сохранять.")
                fav=user.setdefault("favorites",[]); fav.insert(0,{"id":__import__('secrets').token_hex(6),"text":answer[:10000],"created":int(time.time())}); del fav[50:]
                bot.save_db(); send_json(self,200,{"ok":True,"state":state(uid,tg_user,user)}); return
            if path == "/api/favorite/delete":
                fid=str(data.get("id") or ""); user["favorites"]=[x for x in user.get("favorites",[]) if str(x.get("id"))!=fid]; bot.save_db(); send_json(self,200,{"ok":True,"state":state(uid,tg_user,user)}); return
            if path == "/api/settings":
                model=safe_model(data.get("model",user.get("model","auto"))); style=str(data.get("style",user.get("style","normal")))
                if style not in STYLE_VALUES: raise ValueError("Некорректный стиль ответа.")
                user["model"]=model; user["style"]=style; bot.save_db(); send_json(self,200,{"ok":True,"state":state(uid,tg_user,user)}); return
            if path == "/api/activate":
                code=str(data.get("code") or "").strip();
                if not code: raise ValueError("Введи код лицензии.")
                ok,msg=activate_license(uid,code); send_json(self,200,{"ok":ok,"message":msg,"state":state(uid,tg_user,user)} if ok else {"ok":False,"error":msg}); return
            if path == "/api/media/generate":
                kind=data.get("kind"); prompt=str(data.get("prompt") or "").strip(); model=str(data.get("model") or "").strip() or None; opts=data.get("opts") if isinstance(data.get("opts"),dict) else {}
                job_id,selected=media_service.create_job(kind,prompt,model=model,opts=opts,user_id=uid)
                media_service._set_job(job_id,prompt=prompt[:MAX_MESSAGE])
                send_json(self,202,{"ok":True,"job_id":job_id,"kind":media_service.normalize_kind(kind),"model":selected["id"]}); return
            if path == "/api/admin/rules":
                require_admin(uid); action=str(data.get("action") or "")
                if action=="add":
                    pattern=" ".join(str(data.get("pattern") or "").split())[:500]; response=str(data.get("response") or "")[:4000]; mode=str(data.get("mode") or "contains")
                    if not pattern or not response: raise ValueError("Фраза и ответ обязательны.")
                    if mode not in {"contains","exact"}: raise ValueError("Некорректный режим правила.")
                    bot.add_global_rule(pattern,response,mode=mode); bot.save_db()
                elif action=="delete": bot.delete_global_rule(str(data.get("id") or "")); bot.save_db()
                else: raise ValueError("Неизвестное действие правил.")
                send_json(self,200,{"ok":True,"state":state(uid,tg_user,user)}); return
            if path == "/api/admin/license":
                require_admin(uid); target=str(data.get("user_id") or "").strip(); days=int(data.get("days") or 0); requests_limit=int(data.get("requests") or 0)
                if not target: raise ValueError("USER_ID обязателен.")
                from licenses import create_license
                code=create_license(days,requests_limit); ok,msg=activate_license(target,code)
                if not ok: raise RuntimeError(msg)
                send_json(self,200,{"ok":True,"code":code,"message":msg,"state":state(uid,tg_user,user)}); return
            self.error(404,"Endpoint не найден.")
        except PermissionError as exc:
            self.error(403,str(exc))
        except ValueError as exc:
            self.error(400,str(exc))
        except Exception as exc:
            print("POST error:",repr(exc))
            try:
                user["errors"]=int(user.get("errors") or 0)+1
                bot.db["total_errors"]=int(bot.db.get("total_errors") or 0)+1
                bot.save_db()
            except Exception: pass
            self.error(500,"Внутренняя серверная ошибка.")


def run_server():
    server = ThreadingHTTPServer((HOST,PORT),Handler)
    print(f"BulbaMaxAI Mini App server listening on {HOST}:{PORT}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
