import os
import json
import time
import base64
import ast
import operator as op
import io
import csv
import re
import threading
import secrets
from difflib import SequenceMatcher
from pathlib import Path

from licenses import (
    activate_license,
    admin_ids,
    block_user,
    create_license,
    get_license_status,
    is_admin,
    release_request,
    reserve_request,
    revoke_user,
    unblock_user,
    user_has_access,
)

import requests

import media_service

BOT_VERSION = "V18.5.1"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()

BASE_URL = "https://api.baza-ai.org/v1"
TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DATA_FILE = "v17_memory.json"
LEGACY_DATA_FILES = ("v16_memory.json", "v15_memory.json", "v14_memory.json")

MAX_HISTORY = 24
MAX_CHATS = 30
MAX_REPLY = 3900
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_TEXT = 40000
POLL_TIMEOUT = 25
API_TIMEOUT = 90
RATE_LIMIT_COUNT = 6
RATE_LIMIT_WINDOW = 10

STYLE_PROMPTS = {
    "normal": "Отвечай понятно, точно и по делу. Если есть неопределённость, прямо обозначай её.",
    "short": "Отвечай кратко и по существу. Не добавляй лишнюю воду.",
    "detailed": "Отвечай подробно и структурированно. Объясняй важные шаги и причины, но не растягивай ответ без необходимости.",
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not API_KEY:
    raise RuntimeError("API_KEY is not set")

tg_session = requests.Session()
api_session = requests.Session()
api_session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
})

db = {"users": {}, "total_requests": 0, "total_errors": 0, "rules": [], "version": BOT_VERSION}
DB_LOCK = threading.RLock()
AI_INFLIGHT_LOCK = threading.RLock()
AI_INFLIGHT_USERS = set()


def load_db():
    global db
    with DB_LOCK:
        source = DATA_FILE
        if not Path(source).exists():
            for legacy in LEGACY_DATA_FILES:
                if Path(legacy).exists():
                    source = legacy
                    break
        try:
            with open(source, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                db.update(data)
        except (OSError, json.JSONDecodeError):
            pass
        db.setdefault("users", {})
        db.setdefault("total_requests", 0)
        db.setdefault("total_errors", 0)
        db.setdefault("rules", [])
        db.setdefault("version", BOT_VERSION)
        db["version"] = BOT_VERSION
        for u in db["users"].values():
            if isinstance(u, dict):
                u.pop("_user_id", None)
                u.pop("_telegram_user", None)


def save_db():
    with DB_LOCK:
        tmp = DATA_FILE + ".tmp"
        try:
            clean = json.loads(json.dumps(db, ensure_ascii=False))
            for u in clean.get("users", {}).values():
                if isinstance(u, dict):
                    u.pop("_user_id", None)
                    u.pop("_telegram_user", None)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DATA_FILE)
        except OSError as e:
            print("DB save error:", repr(e))


def get_rule_response(text):
    """Backward-compatible alias for the single global-rule engine."""
    return match_global_rule(text)


def add_rule(kind, pattern, response, priority=0, threshold=0.86):
    return add_global_rule(pattern, response, mode=kind, priority=priority, threshold=threshold)


def remove_rule(rule_id):
    return delete_global_rule(rule_id)


def set_rule_enabled(rule_id, enabled):
    try:
        update_global_rule(rule_id, enabled=enabled)
        return True
    except KeyError:
        return False

def default_chat():
    return {
        "history": [],
        "requests": 0,
        "created": int(time.time()),
        "last_prompt": None,
        "last_request": None,
    }


def default_user():
    return {
        "model": "auto",
        "style": "normal",
        "memory": {},
        "chats": {"main": default_chat()},
        "active_chat": "main",
        "requests": 0,
        "errors": 0,
        "rate": [],
        "pinned_chats": [],
        "favorites": [],
        "pending_action": None,
        "notifications": True,
    }


def get_user(uid):
    uid = str(uid)
    if uid not in db["users"]:
        db["users"][uid] = default_user()
    u = db["users"][uid]
    # Runtime identity is never persisted inside the user record.
    u.pop("_user_id", None)
    u.pop("_telegram_user", None)
    u.setdefault("model", "auto")
    u.setdefault("style", "normal")
    u.setdefault("memory", {})
    u.setdefault("chats", {"main": default_chat()})
    u.setdefault("active_chat", "main")
    u.setdefault("requests", 0)
    u.setdefault("errors", 0)
    u.setdefault("rate", [])
    u.setdefault("pinned_chats", [])
    u.setdefault("favorites", [])
    u.setdefault("pending_action", None)
    u.setdefault("notifications", True)
    if not u["chats"]:
        u["chats"]["main"] = default_chat()
        u["active_chat"] = "main"
    for c in u["chats"].values():
        c.setdefault("history", [])
        c.setdefault("requests", 0)
        c.setdefault("created", int(time.time()))
        c.setdefault("last_prompt", None)
        c.setdefault("last_request", None)
    return u


def get_chat(u):
    name = u.get("active_chat", "main")
    if name not in u["chats"]:
        u["chats"][name] = default_chat()
    return u["chats"][name]


def allowed_request(u):
    now = time.time()
    u["rate"] = [x for x in u.get("rate", []) if now - x < RATE_LIMIT_WINDOW]
    if len(u["rate"]) >= RATE_LIMIT_COUNT:
        return False
    u["rate"].append(now)
    return True


def tg(method, data=None, timeout=40, files=None):
    try:
        r = tg_session.post(
            f"{TG_URL}/{method}",
            data=data or {},
            files=files,
            timeout=timeout,
        )
        if not r.ok:
            print("Telegram HTTP:", r.status_code, r.text[:500])
            return None
        p = r.json()
        if not p.get("ok"):
            print("Telegram API:", p)
            return None
        return p.get("result")
    except Exception as e:
        print("Telegram error:", repr(e))
        return None


def send_message(chat_id, text, keyboard=None):
    text = str(text or "...")
    chunks = [text[i:i + MAX_REPLY] for i in range(0, len(text), MAX_REPLY)] or ["..."]
    first = None
    for i, chunk in enumerate(chunks):
        data = {"chat_id": chat_id, "text": chunk}
        if keyboard and i == len(chunks) - 1:
            data["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
        result = tg("sendMessage", data)
        if first is None:
            first = result
    return first


def edit_message(chat_id, message_id, text, keyboard=None):
    if len(text) <= MAX_REPLY:
        data = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if keyboard:
            data["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
        return tg("editMessageText", data)
    first = text[:MAX_REPLY]
    rest = text[MAX_REPLY:]
    tg("editMessageText", {
        "chat_id": chat_id, "message_id": message_id, "text": first
    })
    return send_message(chat_id, rest, keyboard)


def answer_callback(callback_id, text=None):
    data = {"callback_query_id": callback_id}
    if text:
        data["text"] = text
    tg("answerCallbackQuery", data)


def main_keyboard(user_id=None):
    rows = [
        [{"text": "🤖 Чат"}, {"text": "🧠 Модели"}],
        [{"text": "💬 Чаты"}, {"text": "🎨 Генерация"}],
        [{"text": "💾 Память"}, {"text": "⭐ Избранное"}],
        [{"text": "📊 Статистика"}, {"text": "⚙️ Настройки"}],
    ]
    if user_id is not None and is_admin(user_id):
        rows.append([{"text": "👑 Админ"}, {"text": "🔑 Лицензия"}])
    else:
        rows.append([{"text": "🔑 Лицензия"}])
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}



def answer_keyboard():
    return {"inline_keyboard": [[{"text": "⭐ Сохранить", "callback_data": "favorite:last"}], [{"text": "🔄 Повторить", "callback_data": "retry"}]]}

def retry_keyboard():
    return {"inline_keyboard": [[{"text": "🔄 Повторить", "callback_data": "retry"}]]}


def settings_keyboard(style):
    def mark(v, label):
        return ("✅ " if style == v else "") + label
    return {"inline_keyboard": [
        [{"text": mark("normal", "🧠 Обычно"), "callback_data": "style:normal"}],
        [{"text": mark("short", "⚡ Кратко"), "callback_data": "style:short"}],
        [{"text": mark("detailed", "📚 Подробно"), "callback_data": "style:detailed"}],
        [{"text": "⬅️ Назад", "callback_data": "back"}],
    ]}


def memory_keyboard():
    return {"inline_keyboard": [
        [{"text": "🗑 Очистить память", "callback_data": "memory_clear"}],
        [{"text": "⬅️ Назад", "callback_data": "back"}],
    ]}


def get_models(force=False):
    cache = get_models.cache
    now = time.time()
    if not force and cache["models"] and now - cache["time"] < 60:
        return cache["models"]
    try:
        r = api_session.get(f"{BASE_URL}/models", timeout=20)
        if not r.ok:
            return cache["models"]
        data = r.json()
        models = [x for x in data.get("data", []) if isinstance(x, dict) and x.get("id")]
        models.sort(key=lambda x: x["id"].lower())
        cache["models"], cache["time"] = models, now
        return models
    except Exception as e:
        print("Models error:", repr(e))
        return cache["models"]


get_models.cache = {"models": [], "time": 0}


def is_bad_model(mid):
    s = mid.lower()
    return any(x in s for x in (
        "embedding", "moderation", "tts", "transcrib", "realtime",
        "image-generation", "rerank"
    ))


def rank_models(models, vision=False):
    def score(item):
        s = item["id"].lower()
        if is_bad_model(s):
            return -10000
        n = 0
        if vision and any(x in s for x in ("vision", "vl", "omni", "4o", "multimodal", "gemini", "mistral")):
            n += 100
        if any(x in s for x in ("gpt", "claude", "gemini", "mistral", "deepseek", "qwen", "kimi", "minimax", "glm", "grok")):
            n += 20
        return n
    return sorted(models, key=score, reverse=True)


def model_keyboard(u):
    rows = [[{"text": "🤖 Авто", "callback_data": "model:auto"}]]
    visible_models = [m for m in get_models() if not is_bad_model(m["id"])]
    for i, m in enumerate(visible_models):
        mid = m["id"]
        title = ("✅ " if u["model"] == mid else "") + mid
        rows.append([{"text": title[:55], "callback_data": f"model:{i}"}])
    rows.append([{"text": "⬅️ Назад", "callback_data": "back"}])
    return {"inline_keyboard": rows}


def extract_error(r):
    try:
        d = r.json()
        e = d.get("error", d)
        return str(e.get("message", e) if isinstance(e, dict) else e)
    except Exception:
        return r.text[:800]


def call_ai(messages, model):
    """Exactly one provider call. Model fallback is handled by ai_chat, not here."""
    payload = {"model": model, "messages": messages}
    try:
        r = api_session.post(f"{BASE_URL}/chat/completions", json=payload, timeout=API_TIMEOUT)
        if not r.ok:
            return None, extract_error(r), r.status_code
        d = r.json()
        choices = d.get("choices") or []
        if not choices:
            return None, "API не вернуло choices.", r.status_code
        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        return str(content or "").strip(), None, r.status_code
    except requests.RequestException as e:
        return None, str(e), 0
    except Exception as e:
        return None, repr(e), 0


def choose_model(u, vision=False, preferred=None):
    models = [m for m in get_models() if not is_bad_model(m["id"])]
    available = {m["id"] for m in models}
    if preferred and preferred in available:
        primary = preferred
    else:
        ranked = rank_models(models, vision=vision)
        primary = ranked[0]["id"] if ranked else None
    if not primary:
        return []
    # Exactly one primary model; one fallback is allowed only after a real API failure.
    ranked = [m["id"] for m in rank_models(models, vision=vision) if m["id"] != primary]
    return [primary] + ranked[:1]


# ---------------- SAFE CALCULATOR ----------------

_BIN = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv, ast.Mod: op.mod, ast.Pow: op.pow,
}
_UN = {ast.UAdd: op.pos, ast.USub: op.neg}


def _normalize_rule_text(value):
    return " ".join(str(value or "").casefold().split())[:500]

def get_global_rules(enabled_only=True):
    rules = db.get("rules", [])
    return [r for r in rules if isinstance(r, dict) and (not enabled_only or r.get("enabled", True))]

def match_global_rule(text):
    normalized = _normalize_rule_text(text)
    for rule in sorted(get_global_rules(), key=lambda r: int(r.get("priority", 0)), reverse=True):
        pattern = _normalize_rule_text(rule.get("pattern"))
        if not pattern:
            continue
        mode = rule.get("mode", "contains")
        if mode == "exact":
            matched = normalized == pattern
        elif mode == "similar":
            matched = SequenceMatcher(None, normalized, pattern).ratio() >= float(rule.get("threshold", 0.86))
        else:
            matched = pattern in normalized
        if matched:
            return str(rule.get("response") or "")[:4000]
    return None

def add_global_rule(pattern, response, mode="contains", priority=0, threshold=0.86):
    if mode not in ("exact", "contains", "similar"):
        raise ValueError("mode must be exact, contains or similar")
    rule = {"id": int(time.time() * 1000), "pattern": str(pattern).strip()[:500], "response": str(response).strip()[:4000], "mode": mode, "priority": int(priority), "threshold": max(0.5, min(1.0, float(threshold))), "enabled": True, "created": int(time.time())}
    if not rule["pattern"] or not rule["response"]:
        raise ValueError("pattern and response are required")
    db.setdefault("rules", []).append(rule)
    save_db()
    return rule

def update_global_rule(rule_id, **changes):
    for rule in db.get("rules", []):
        if str(rule.get("id")) == str(rule_id):
            for k in ("pattern", "response", "mode", "priority", "enabled", "threshold"):
                if k in changes and changes[k] is not None:
                    rule[k] = changes[k]
            if rule.get("mode") not in ("exact", "contains", "similar"):
                raise ValueError("Некорректный режим правила.")
            rule["pattern"] = str(rule.get("pattern") or "").strip()[:500]
            rule["response"] = str(rule.get("response") or "").strip()[:4000]
            rule["threshold"] = max(0.5, min(1.0, float(rule.get("threshold", 0.86))))
            save_db()
            return rule
    raise KeyError("Rule not found")

def delete_global_rule(rule_id):
    before = len(db.get("rules", []))
    db["rules"] = [r for r in db.get("rules", []) if str(r.get("id")) != str(rule_id)]
    if len(db["rules"]) == before:
        return False
    save_db()
    return True


def safe_eval(expr):
    expr = expr.replace(",", ".").replace("×", "*").replace("÷", "/")
    tree = ast.parse(expr, mode="eval")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            if abs(float(node.value)) > 1e100:
                raise ValueError("Слишком большое число.")
            return node.value
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UN:
            return _UN[type(node.op)](ev(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(float(b)) > 100:
                raise ValueError("Слишком большая степень.")
            return _BIN[type(node.op)](a, b)
        raise ValueError("Недопустимое выражение.")
    return ev(tree)


def extract_math(text):
    s = text.strip()
    if len(s) > 150 or not re.search(r"\d", s):
        return None
    candidate = re.sub(r"(?i)^(посчитай|вычисли|сколько будет|calculate)\s*", "", s).strip()
    if not re.fullmatch(r"[0-9\s\+\-\*\/\(\)\.,%×÷]+", candidate):
        return None
    try:
        return safe_eval(candidate)
    except Exception:
        return None


# ---------------- FILE TOOLS ----------------

def decode_bytes(data):
    for enc in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def tg_file(file_id):
    info = tg("getFile", {"file_id": file_id})
    if not info or not info.get("file_path"):
        raise RuntimeError("Не удалось получить файл из Telegram.")
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['file_path']}"
    r = tg_session.get(url, timeout=60)
    r.raise_for_status()
    if len(r.content) > MAX_FILE_BYTES:
        raise RuntimeError("Файл слишком большой.")
    return r.content


def read_text_file(name, data):
    suffix = Path(name).suffix.lower()

    if suffix in (".txt", ".md", ".json", ".csv", ".log", ".py", ".js", ".html", ".css"):
        text = decode_bytes(data)
        if suffix == ".json":
            try:
                obj = json.loads(text)
                text = json.dumps(obj, ensure_ascii=False, indent=2)
            except Exception:
                pass
        elif suffix == ".csv":
            try:
                rows = list(csv.reader(io.StringIO(text)))
                text = "\n".join(" | ".join(row) for row in rows)
            except Exception:
                pass
        return text[:MAX_FILE_TEXT]

    if suffix == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise RuntimeError("Для XLSX нужен пакет openpyxl.")
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parts = []
        try:
            for ws in wb.worksheets:
                parts.append(f"=== Лист: {ws.title} ===")
                for row in ws.iter_rows(values_only=True):
                    vals = ["" if v is None else str(v) for v in row]
                    if any(vals):
                        parts.append(" | ".join(vals))
                        if sum(len(x) for x in parts) > MAX_FILE_TEXT:
                            break
                if sum(len(x) for x in parts) > MAX_FILE_TEXT:
                    break
        finally:
            wb.close()
        return "\n".join(parts)[:MAX_FILE_TEXT]

    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("Для PDF нужен пакет pypdf.")
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                parts.append(f"=== Страница {i + 1} ===\n{text}")
            if sum(len(x) for x in parts) > MAX_FILE_TEXT:
                break
        return "\n\n".join(parts)[:MAX_FILE_TEXT] or "В PDF не удалось извлечь текст."

    if suffix == ".docx":
        try:
            from docx import Document
        except ImportError:
            raise RuntimeError("Для DOCX нужен пакет python-docx.")
        doc = Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            parts.append("=== Таблица ===")
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(parts)[:MAX_FILE_TEXT] or "В DOCX нет читаемого текста."

    raise RuntimeError(
        "Формат пока не поддерживается. Поддерживаются TXT, MD, JSON, CSV, LOG, "
        "PY, JS, HTML, CSS, XLSX, PDF и DOCX."
    )


def image_content(data, mime, question):
    b64 = base64.b64encode(data).decode("ascii")
    return [
        {"type": "text", "text": question},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]


# ---------------- REAL FILE CREATION ----------------

def make_xlsx(rows, filename):
    try:
        from openpyxl import Workbook
    except ImportError:
        raise RuntimeError("Не установлен openpyxl.")
    wb = Workbook()
    ws = wb.active
    ws.title = "BulbaMaxAI"
    for r in rows:
        ws.append([str(x) if x is not None else "" for x in r])
    for col in ws.columns:
        width = min(max(len(str(cell.value or "")) for cell in col) + 2, 50)
        ws.column_dimensions[col[0].column_letter].width = width
    path = Path(filename)
    wb.save(path)
    return str(path)


def make_csv(rows, filename):
    path = Path(filename)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)
    return str(path)


def make_chart(rows, filename):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        raise RuntimeError("Не установлен matplotlib.")
    if len(rows) < 2:
        raise RuntimeError("Для графика нужно минимум 2 строки данных.")
    labels, values = [], []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        try:
            labels.append(str(row[0]))
            values.append(float(str(row[1]).replace(",", ".")))
        except ValueError:
            continue
    if not values:
        raise RuntimeError("Не удалось найти числовые данные для графика.")
    plt.figure(figsize=(9, 5))
    plt.bar(labels, values)
    plt.xticks(rotation=35, ha="right")
    plt.tight_layout()
    path = Path(filename)
    plt.savefig(path, dpi=160)
    plt.close()
    return str(path)


def make_pdf(text, filename, title="BulbaMaxAI"):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from xml.sax.saxutils import escape

    doc = SimpleDocTemplate(
        str(filename),
        pagesize=A4,
        rightMargin=40,
        leftMargin=40,
        topMargin=40,
        bottomMargin=40,
    )
    # Register a Unicode font when available so Cyrillic text does not turn into empty boxes.
    font_name = "Helvetica"
    for font_path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ):
        if Path(font_path).exists():
            try:
                pdfmetrics.registerFont(TTFont("BulbaUnicode", font_path))
                font_name = "BulbaUnicode"
                break
            except Exception:
                pass

    styles = getSampleStyleSheet()
    styles["Title"].fontName = font_name
    body = ParagraphStyle(
        "BulbaBody",
        parent=styles["BodyText"],
        alignment=TA_LEFT,
        leading=15,
        spaceAfter=8,
        fontName=font_name,
    )
    story = [Paragraph(escape(str(title)), styles["Title"]), Spacer(1, 10)]
    for block in str(text or "").split("\n"):
        block = block.strip()
        if block:
            story.append(Paragraph(escape(block), body))
        else:
            story.append(Spacer(1, 6))
    doc.build(story)
    return str(filename)


def make_docx(text, filename, title="BulbaMaxAI"):
    from docx import Document
    doc = Document()
    doc.add_heading(str(title), level=1)
    for block in str(text or "").split("\n"):
        doc.add_paragraph(block)
    doc.save(str(filename))
    return str(filename)


def send_document(chat_id, path, caption=""):
    with open(path, "rb") as f:
        return tg(
            "sendDocument",
            {"chat_id": chat_id, "caption": caption[:1000]},
            files={"document": (Path(path).name, f)},
            timeout=60,
        )


def send_photo(chat_id, path, caption=""):
    with open(path, "rb") as f:
        return tg(
            "sendPhoto",
            {"chat_id": chat_id, "caption": caption[:1000]},
            files={"photo": (Path(path).name, f)},
            timeout=60,
        )


def parse_rows_from_text(text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    rows = []
    for line in lines:
        if "|" in line:
            cells = [x.strip() for x in line.strip("|").split("|")]
        elif ";" in line:
            cells = [x.strip() for x in line.split(";")]
        elif "," in line:
            cells = [x.strip() for x in line.split(",")]
        else:
            cells = re.split(r"\s{2,}", line)
        if cells:
            rows.append(cells)
    return rows


def local_action_plan(text, has_image=False, has_file=False):
    """Deterministic zero-extra-token router. It never calls an AI model."""
    t = str(text or "").strip().lower()
    if not has_image and not has_file and extract_math(text) is not None:
        return {"action": "calculator"}
    if has_image:
        return {"action": "vision"}
    if has_file:
        return {"action": "analyze_file"}
    if any(x in t for x in ("excel", "xlsx", "таблиц", "таблицу", "таблица для скач", "сделай таблицу")):
        return {"action": "create_xlsx"}
    if "csv" in t:
        return {"action": "create_csv"}
    if any(x in t for x in ("график", "диаграмм", "визуализац")):
        return {"action": "create_chart"}
    if "pdf" in t:
        return {"action": "create_pdf"}
    if any(x in t for x in ("docx", "word-документ", "word документ")):
        return {"action": "create_docx"}
    return {"action": "chat"}


def style_system(u):
    memory = u.get("memory", {})
    memory_text = json.dumps(memory, ensure_ascii=False)[:5000]
    return (
        "Ты BulbaMaxAI. " + STYLE_PROMPTS.get(u.get("style", "normal"), STYLE_PROMPTS["normal"]) +
        "\nИспользуй сохранённую память пользователя только когда она относится к запросу.\n"
        f"Память: {memory_text}"
    )


def ai_chat(u, messages, vision=False):
    preferred = None if u.get("model", "auto") == "auto" else u.get("model")
    candidates = choose_model(u, vision=vision, preferred=preferred)
    if not candidates:
        return None, "Нет доступных моделей."
    last_error = "Не удалось получить ответ."
    for index, model in enumerate(candidates):
        content, err, status = call_ai(messages, model)
        if content:
            return content, None
        last_error = err or last_error
        # Fallback only after an actual failure; never probe multiple models on success.
        if index == 0 and len(candidates) > 1 and status in (0, 400, 404, 408, 409, 429, 500, 502, 503, 504):
            continue
        break
    return None, last_error


def build_history(u):
    h = get_chat(u)["history"]
    return h[-MAX_HISTORY:]


def add_history(u, role, content):
    chat = get_chat(u)
    chat["history"].append({"role": role, "content": content})
    chat["history"] = chat["history"][-MAX_HISTORY:]


def detect_media_intent(text):
    """Legacy no-op kept for compatibility with older integrations."""
    return None


def reserve_license_request(user_id):
    """Reserve one licensed request before expensive work starts."""
    if os.getenv("LICENSE_REQUIRED", "0").strip() != "1" or is_admin(user_id):
        return True
    ok, _ = reserve_request(str(user_id))
    return bool(ok)


def release_license_request(user_id):
    """Return a reserved request when processing fails before completion."""
    if os.getenv("LICENSE_REQUIRED", "0").strip() != "1" or is_admin(user_id):
        return True
    ok, _ = release_request(str(user_id))
    return bool(ok)


def consume_license_after_success(user_id):
    """Backward-compatible alias for older integrations."""
    return reserve_license_request(user_id)


def record_success(u, chat):
    u["requests"] = int(u.get("requests") or 0) + 1
    chat["requests"] = int(chat.get("requests") or 0) + 1
    db["total_requests"] = int(db.get("total_requests") or 0) + 1


def start_ai_request(chat_id, user_id, u, text, image=None, file_text=None, file_name=None, media_ref=None):
    """Run a potentially long AI/file task outside the Telegram polling loop."""
    uid = str(user_id)
    with AI_INFLIGHT_LOCK:
        if uid in AI_INFLIGHT_USERS:
            send_message(chat_id, "⏳ Предыдущий запрос ещё выполняется. Дождись его завершения.", main_keyboard(user_id=user_id))
            return False
        AI_INFLIGHT_USERS.add(uid)

    def worker():
        try:
            handle_ai_request(
                chat_id, u, text, image=image, file_text=file_text,
                file_name=file_name, media_ref=media_ref, user_id=user_id
            )
        finally:
            with AI_INFLIGHT_LOCK:
                AI_INFLIGHT_USERS.discard(uid)

    threading.Thread(target=worker, name=f"bulba-ai-{uid}", daemon=True).start()
    return True


def handle_ai_request(chat_id, u, text, image=None, file_text=None, file_name=None, media_ref=None, user_id=None):
    if not allowed_request(u):
        send_message(chat_id, "⏳ Слишком много запросов. Подожди несколько секунд.", main_keyboard())
        return

    license_user_id = str(user_id if user_id is not None else chat_id)
    reserved = reserve_license_request(license_user_id)
    if not reserved:
        send_message(chat_id, "⛔ Лимит лицензии исчерпан или доступ недоступен.", main_keyboard())
        return
    completed = False

    status = f"🧠 {BOT_VERSION}: анализирую задачу…"
    if image:
        status = "📸 Анализирую изображение…"
    elif file_text:
        status = "📎 Читаю и анализирую файл…"
    status_msg = send_message(chat_id, status)

    chat = get_chat(u)

    try:
        # Fast deterministic calculator path.
        calc = extract_math(text)
        if calc is not None and not image and not file_text:
            if isinstance(calc, float) and (calc != calc or abs(calc) == float("inf")):
                raise ValueError("Некорректный числовой результат.")
            answer = f"🧮 Ответ: {calc:g}" if isinstance(calc, float) and calc.is_integer() else f"🧮 Ответ: {calc}"
            add_history(u, "user", text)
            add_history(u, "assistant", answer)
            chat["last_prompt"] = text
            chat["last_request"] = {"kind": "text", "text": text}
            record_success(u, chat)
            save_db()
            completed = True
            if status_msg:
                edit_message(chat_id, status_msg["message_id"], answer, answer_keyboard())
            else:
                send_message(chat_id, answer, answer_keyboard())
            return

        plan = local_action_plan(text, has_image=bool(image), has_file=bool(file_text))
        action = plan.get("action", "chat")

        if action in ("create_xlsx", "create_csv", "create_chart", "create_pdf", "create_docx"):
            stem = re.sub(r"[^A-Za-zА-Яа-я0-9_-]+", "_", text[:35]).strip("_") or "bulbamaxai"
            title = "BulbaMaxAI"

            if action in ("create_xlsx", "create_csv", "create_chart"):
                rows = parse_rows_from_text(text)
                if not isinstance(rows, list) or len(rows) < 2:
                    rows = parse_rows_from_text(text)

                # Normalize rows so mixed numeric/string values never break file creation.
                clean_rows = []
                for row in rows if isinstance(rows, list) else []:
                    if isinstance(row, (list, tuple)) and row:
                        clean_rows.append([
                            (x if isinstance(x, (int, float)) else str(x))
                            for x in row
                        ])
                rows = clean_rows

                if len(rows) < 2:
                    action = "chat"
                else:
                    ext = {"create_xlsx": ".xlsx", "create_csv": ".csv", "create_chart": ".png"}[action]
                    path = Path(f"{stem}{ext}")

                    if action == "create_xlsx":
                        make_xlsx(rows, path)
                        sent = send_document(chat_id, path, "📊 Готовая таблица Excel")
                        if not sent:
                            raise RuntimeError("Telegram не принял Excel-файл.")
                    elif action == "create_csv":
                        make_csv(rows, path)
                        sent = send_document(chat_id, path, "📄 Готовый CSV-файл")
                        if not sent:
                            raise RuntimeError("Telegram не принял CSV-файл.")
                    else:
                        make_chart(rows, path)
                        sent = send_photo(chat_id, path, "📈 Готовый график")
                        if not sent:
                            raise RuntimeError("Telegram не принял график.")

                    try:
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    add_history(u, "user", text)
                    add_history(u, "assistant", f"Создан файл: {path.name}")
                    chat["last_prompt"] = text
                    chat["last_request"] = {"kind": "text", "text": text}
                    record_success(u, chat)
                    save_db()
                    completed = True
                    return

            if action in ("create_pdf", "create_docx"):
                # Generate the document body with the normal single-model AI path,
                # then render that answer into the requested file format.
                messages = [{"role": "system", "content": style_system(u)}]
                messages += build_history(u)
                messages.append({"role": "user", "content": text})
                answer, error = ai_chat(u, messages, vision=False)
                if not answer:
                    raise RuntimeError(error or "Не удалось подготовить содержимое документа.")
                ext = ".pdf" if action == "create_pdf" else ".docx"
                path = Path(f"{stem}{ext}")
                if action == "create_pdf":
                    make_pdf(answer, path, title)
                    caption = "📕 Готовый PDF"
                else:
                    make_docx(answer, path, title)
                    caption = "📝 Готовый Word-документ"
                sent = send_document(chat_id, path, caption)
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
                if not sent:
                    raise RuntimeError("Telegram не принял документ.")
                add_history(u, "user", text)
                add_history(u, "assistant", answer)
                chat["last_prompt"] = text
                chat["last_request"] = {"kind": "text", "text": text}
                record_success(u, chat)
                save_db()
                completed = True
                return

        if image:
            mime = image.get("mime", "image/jpeg")
            content = image_content(image["data"], mime, text)
            messages = [{"role": "system", "content": style_system(u)}]
            messages += build_history(u)
            messages.append({"role": "user", "content": content})
            answer, error = ai_chat(u, messages, vision=True)
        else:
            prompt = text
            if file_text:
                prompt = f"Файл: {file_name}\n\nСодержимое:\n{file_text}\n\nЗадача пользователя:\n{text}"
            messages = [{"role": "system", "content": style_system(u)}]
            messages += build_history(u)
            messages.append({"role": "user", "content": prompt})
            answer, error = ai_chat(u, messages, vision=False)

        if not answer:
            u["errors"] += 1
            db["total_errors"] += 1
            save_db()
            msg = f"❌ Не удалось получить ответ.\n\n{error or 'Неизвестная ошибка'}"
            if status_msg:
                edit_message(chat_id, status_msg["message_id"], msg, main_keyboard())
            else:
                send_message(chat_id, msg, main_keyboard())
            return

        add_history(u, "user", text)
        add_history(u, "assistant", answer)
        chat["last_prompt"] = text
        last_kind = "image" if image else ("file" if file_text else "text")
        chat["last_request"] = {
            "kind": last_kind,
            "text": text,
            "file_id": (media_ref or {}).get("file_id") if media_ref else None,
            "file_name": file_name,
        }
        record_success(u, chat)
        save_db()
        completed = True

        if status_msg:
            edit_message(chat_id, status_msg["message_id"], answer, answer_keyboard())
        else:
            send_message(chat_id, answer, answer_keyboard())

    except Exception as e:
        print("Request error:", repr(e))
        u["errors"] += 1
        db["total_errors"] += 1
        save_db()
        msg = "❌ Ошибка при обработке запроса. Попробуй ещё раз."
        if status_msg:
            edit_message(chat_id, status_msg["message_id"], msg, main_keyboard())
        else:
            send_message(chat_id, msg, main_keyboard())
    finally:
        if reserved and not completed:
            release_license_request(license_user_id)


def show_stats(chat_id, u):
    send_message(chat_id,
        f"📊 Статистика\n\n"
        f"Запросов: {u['requests']}\n"
        f"Ошибок: {u['errors']}\n"
        f"Чатов: {len(u['chats'])}\n"
        f"Текущий чат: {u['active_chat']}\n"
        f"Всего запросов бота: {db['total_requests']}",
        main_keyboard())


def show_memory(chat_id, u):
    mem = u.get("memory", {})
    if not mem:
        text = "💾 Память пуста."
    else:
        text = "💾 Память:\n\n" + "\n".join(f"• {k}: {v}" for k, v in mem.items())
    send_message(chat_id, text, memory_keyboard())


def show_chats(chat_id, u):
    rows=[]
    for name in u["chats"]:
        mark="📌 " if name in u.get("pinned_chats",[]) else ""
        active="✅ " if name==u["active_chat"] else ""
        rows.append([{"text":active+mark+name[:40],"callback_data":"chat:open:"+name[:45]}])
        rows.append([{"text":"✏️","callback_data":"chat:rename:"+name[:40]},{"text":"📌","callback_data":"chat:pin:"+name[:40]},{"text":"🗑","callback_data":"chat:delete:"+name[:40]}])
    rows.append([{"text":"➕ Новый чат","callback_data":"new_chat"}])
    rows.append([{"text":"⬅️ Назад","callback_data":"back"}])
    send_message(chat_id,"💬 Мои чаты:", {"inline_keyboard":rows})


def new_chat(u):
    if len(u["chats"]) >= MAX_CHATS:
        # Remove the oldest inactive chat.
        candidates = [(c.get("created", 0), n) for n, c in u["chats"].items() if n != u["active_chat"]]
        if candidates:
            _, old = sorted(candidates)[0]
            del u["chats"][old]
        else:
            return u["active_chat"]
    base = "Чат"
    i = 1
    while f"{base} {i}" in u["chats"]:
        i += 1
    name = f"{base} {i}"
    u["chats"][name] = default_chat()
    u["active_chat"] = name
    return name



def media_keyboard():
    return {"inline_keyboard": [
        [{"text": "🖼 Изображение", "callback_data": "media:image"}],
        [{"text": "🎬 Видео", "callback_data": "media:video"}],
        [{"text": "🔊 Голос", "callback_data": "media:tts"}],
        [{"text": "🎵 Музыка", "callback_data": "media:music"}],
        [{"text": "🧊 3D", "callback_data": "media:3d"}],
        [{"text": "⬅️ Назад", "callback_data": "back"}],
    ]}


def admin_keyboard():
    return {"inline_keyboard": [
        [{"text": "👥 Пользователи", "callback_data": "admin:users"}],
        [{"text": "🔑 Лицензии", "callback_data": "admin:licenses"}],
        [{"text": "📜 Правила", "callback_data": "admin:rules"}],
        [{"text": "📊 Статистика", "callback_data": "admin:stats"}],
        [{"text": "🚫 Блокировки", "callback_data": "admin:blocks"}],
        [{"text": "⚙️ Настройки", "callback_data": "admin:settings"}],
        [{"text": "⬅️ Назад", "callback_data": "back"}],
    ]}


def favorite_keyboard(fid):
    return {"inline_keyboard": [[{"text": "🗑 Удалить", "callback_data": f"favdel:{fid}"}]]}


def show_license(chat_id, user_id):
    send_message(chat_id, "🔑 Лицензия\n\n" + get_license_status(user_id), {
        "inline_keyboard": [
            [{"text": "🔑 Активировать код", "callback_data": "license:activate"}],
            [{"text": "🔄 Обновить", "callback_data": "license:status"}],
        ]
    })


def show_settings(chat_id, u):
    kb = {"inline_keyboard": [
        [{"text": ("✅ " if u["style"] == "normal" else "") + "🧠 Обычно", "callback_data": "style:normal"}],
        [{"text": ("✅ " if u["style"] == "short" else "") + "⚡ Кратко", "callback_data": "style:short"}],
        [{"text": ("✅ " if u["style"] == "detailed" else "") + "📚 Подробно", "callback_data": "style:detailed"}],
        [{"text": ("🔔 Уведомления: ВКЛ" if u.get("notifications", True) else "🔕 Уведомления: ВЫКЛ"), "callback_data": "settings:notifications"}],
        [{"text": "🧹 Очистить текущий чат", "callback_data": "settings:clear"}],
        [{"text": "⬅️ Назад", "callback_data": "back"}],
    ]}
    send_message(chat_id, "⚙️ Настройки\n\nВыбери нужный параметр:", kb)


def show_favorites(chat_id, u):
    favs = u.get("favorites", [])
    if not favs:
        send_message(chat_id, "⭐ Избранное\n\nПока ничего не сохранено.", main_keyboard(user_id=user_id))
        return
    for i, item in enumerate(favs[-20:], 1):
        fid = str(item.get("id", i))
        send_message(chat_id, f"⭐ Ответ #{i}\n\n{item.get('text','')}", favorite_keyboard(fid))


def save_favorite(u, text):
    favs = u.setdefault("favorites", [])
    fid = secrets.token_hex(4)
    favs.append({"id": fid, "text": str(text)[:12000], "created": int(time.time())})
    u["favorites"] = favs[-50:]
    save_db()
    return fid


def start_media_prompt(chat_id, u, kind):
    user_id = str(u.get("_user_id") or "")
    if not user_id:
        # Runtime identity is normally supplied by process_message; keep this helper safe.
        user_id = str(next((uid for uid, item in db.get("users", {}).items() if item is u), ""))
    if not media_service.configured():
        send_message(chat_id, "⚠️ Media Studio сейчас недоступна: PLUSVIBE_API_KEY не настроен.", main_keyboard(user_id=user_id))
        return
    models = media_service.models_for_kind(kind)
    if not models:
        send_message(chat_id, "⚠️ Для этого типа генерации сейчас нет доступных моделей.", main_keyboard(user_id=user_id))
        return
    u["pending_action"] = {"type": "media_prompt", "kind": kind}
    save_db()
    labels = {"image":"🖼 Изображение", "video":"🎬 Видео", "tts":"🔊 Голос", "music":"🎵 Музыка", "3d":"🧊 3D"}
    send_message(chat_id, f"{labels.get(kind, '🎨 Генерация')}\n\nНапиши описание того, что нужно создать.", main_keyboard(user_id=user_id))


def _download_media_result(url, timeout=180):
    """Download the provider result ourselves so Telegram does not have to fetch a signed URL."""
    r = requests.get(str(url), timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    data = r.content
    if not data:
        raise RuntimeError("Провайдер вернул пустой файл.")
    content_type = (r.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0].lower()
    return data, content_type


def _media_filename(kind, content_type):
    ext = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "video/mp4": ".mp4", "video/webm": ".webm",
        "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/wav": ".wav",
    }.get(content_type)
    if not ext:
        ext = {"image": ".jpg", "video": ".mp4", "tts": ".mp3", "music": ".mp3", "3d": ".glb"}.get(kind, ".bin")
    return f"bulbamaxai_{kind}{ext}"


def run_media_job(chat_id, user_id, job_id, kind):
    user_id = str(user_id)
    u = get_user(user_id)
    deadline = time.time() + {"image":180,"video":720,"tts":240,"music":360,"3d":420}.get(kind,360)
    while time.time() < deadline:
        job = media_service.job_status(job_id)
        if not job:
            send_message(chat_id, "❌ Медиа-задача не найдена.", main_keyboard(user_id=user_id)); return
        if job.get("status") == "success":
            claimed = media_service.claim_success(job_id)
            if not claimed:
                return
            urls = claimed.get("urls") or []
            if not urls:
                media_service._set_job(job_id, finalized=False)
                send_message(chat_id, "❌ Провайдер завершил генерацию, но не вернул файл.", main_keyboard(user_id=user_id)); return
            url = urls[0]
            try:
                raw, content_type = _download_media_result(url, timeout=180 if kind != "video" else 300)
                filename = _media_filename(kind, content_type)
                if kind == "image":
                    sent = tg("sendPhoto", {"chat_id": chat_id, "caption": "🖼 Готово"}, files={"photo": (filename, raw, content_type)}, timeout=120)
                    if not sent:
                        sent = tg("sendDocument", {"chat_id": chat_id, "caption": "🖼 Готово"}, files={"document": (filename, raw, content_type)}, timeout=120)
                elif kind == "video":
                    sent = tg("sendVideo", {"chat_id": chat_id, "caption": "🎬 Готово"}, files={"video": (filename, raw, content_type)}, timeout=300)
                    if not sent:
                        sent = tg("sendDocument", {"chat_id": chat_id, "caption": "🎬 Готово"}, files={"document": (filename, raw, content_type)}, timeout=300)
                elif kind in ("tts", "music"):
                    sent = tg("sendAudio", {"chat_id": chat_id, "caption": "🔊 Готово"}, files={"audio": (filename, raw, content_type)}, timeout=180)
                    if not sent:
                        sent = tg("sendDocument", {"chat_id": chat_id, "caption": "🎵 Готово"}, files={"document": (filename, raw, content_type)}, timeout=180)
                else:
                    sent = tg("sendDocument", {"chat_id": chat_id, "caption": "🧊 3D результат"}, files={"document": (filename, raw, content_type)}, timeout=180)
            except Exception as exc:
                print("Media result download/send error:", repr(exc))
                sent = None
            if not sent:
                # Allow a later status check/retry to finalize the result again.
                media_service._set_job(job_id, finalized=False)
                send_message(chat_id, "❌ Генерация готова, но файл не удалось отправить в Telegram. Попробуй ещё раз.", main_keyboard(user_id=user_id))
                return
            prompt = str(claimed.get("prompt") or "Медиа-задача")
            add_history(u, "user", prompt)
            add_history(u, "assistant", f"Создано медиа: {url}")
            chat = get_chat(u)
            chat["last_prompt"] = prompt
            chat["last_request"] = {"kind": kind, "text": prompt}
            record_success(u, chat)
            save_db()
            send_message(chat_id, "Готово.\n\nМожно создать ещё один результат.", main_keyboard(user_id=user_id))
            return
        if job.get("status") == "failed":
            send_message(chat_id, f"❌ Генерация не удалась.\n\n{job.get('error','Неизвестная ошибка')}", main_keyboard(user_id=user_id)); return
        time.sleep(2.0)
    send_message(chat_id, "⏳ Генерация ещё выполняется. Проверь результат позже или создай новый запрос.", main_keyboard(user_id=user_id))


def start_media_job(chat_id, user_id, u, kind, prompt):
    try:
        job_id, selected = media_service.create_job(kind, prompt, user_id=user_id)
        u["pending_action"] = None
        save_db()
        send_message(chat_id, f"🎨 Генерация запущена.\n\nМодель: {selected['id']}\n⏳ Статус: выполняется…")
        threading.Thread(target=run_media_job, args=(chat_id, user_id, job_id, kind), daemon=True).start()
    except PermissionError as e:
        u["pending_action"] = None; save_db(); send_message(chat_id, f"⛔ {e}", main_keyboard(user_id=user_id))
    except Exception as e:
        u["pending_action"] = None; save_db(); print("Media start error:", repr(e)); send_message(chat_id, "❌ Не удалось запустить генерацию. Попробуй ещё раз.", main_keyboard(user_id=user_id))


def process_message(msg):
    if "chat" not in msg:
        return
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id", chat_id)
    u = get_user(user_id)

    text = (msg.get("text") or "").strip()

    pending = u.get("pending_action")
    if text and pending and not text.startswith("/"):
        if pending.get("type") == "media_prompt":
            start_media_job(chat_id, user_id, u, pending.get("kind"), text)
            return
        if pending.get("type") == "remember":
            u["memory"][str(int(time.time()))] = text[:1000]
            u["pending_action"] = None
            save_db()
            send_message(chat_id, "💾 Запомнил.", main_keyboard(user_id=user_id))
            return
        if pending.get("type") == "activate":
            ok, message = activate_license(user_id, text)
            u["pending_action"] = None
            save_db()
            send_message(chat_id, message, main_keyboard(user_id=user_id))
            return
        if pending.get("type") == "chat_name":
            name = re.sub(r"[\n\r]", " ", text).strip()[:40] or "Чат"
            if name in u["chats"]: name += " 2"
            u["chats"][name] = default_chat(); u["active_chat"] = name; u["pending_action"] = None; save_db()
            send_message(chat_id, f"✅ Создан чат «{name}».\nТеперь ты находишься в нём.", main_keyboard(user_id=user_id))
            return
        if pending.get("type") == "rename_chat":
            old=pending.get("old")
            name=re.sub(r"[\n\r]", " ", text).strip()[:40] or old
            if old in u["chats"] and name not in u["chats"]:
                u["chats"][name]=u["chats"].pop(old)
                if u["active_chat"]==old: u["active_chat"]=name
                if old in u.get("pinned_chats",[]): u["pinned_chats"]=[name if x==old else x for x in u["pinned_chats"]]
                u["pending_action"]=None; save_db(); send_message(chat_id,f"✏️ Чат переименован в «{name}».",main_keyboard(user_id=user_id))
            else:
                u["pending_action"]=None; save_db(); send_message(chat_id,"❌ Такое название уже занято.",main_keyboard(user_id=user_id))
            return
    caption = (msg.get("caption") or "").strip()

    # Global admin rules are enforced server-side and apply to every user.
    if text and not text.startswith("/"):
        rule_answer = get_rule_response(text)
        if rule_answer:
            send_message(chat_id, rule_answer, main_keyboard())
            return

    # Public access/account commands. AI features are checked below.
    if text.startswith("/myid"):
        send_message(chat_id, f"🆔 Твой Telegram ID: {user_id}", main_keyboard())
        return
    if text.startswith("/profile"):
        status = get_license_status(user_id)
        send_message(chat_id, status, main_keyboard())
        return
    if text.startswith("/activate"):
        code = text[len("/activate"):].strip()
        if not code:
            send_message(chat_id, "🔑 Используй: /activate КОД", main_keyboard())
            return
        ok, message = activate_license(user_id, code)
        send_message(chat_id, message, main_keyboard())
        return
    if text.startswith("/admin"):
        if not is_admin(user_id):
            send_message(chat_id, "⛔ Нет доступа.", main_keyboard())
            return
        send_message(chat_id,
            "👑 Админ-команды:\n\n"
            "/give USER_ID DAYS [REQUESTS] — выдать лицензию\n"
            "/newcode DAYS [REQUESTS] — создать код\n"
            "/license USER_ID — статус\n"
            "/revoke USER_ID — отозвать доступ\n"
            "/block USER_ID — заблокировать\n"
            "/unblock USER_ID — разблокировать\n/rules — глобальные правила\n/rule_add TYPE TEXT RESPONSE — добавить правило\n/rule_del ID — удалить правило",
            main_keyboard())
        return
    if text.startswith("/rules"):
        if not is_admin(user_id):
            send_message(chat_id, "⛔ Нет доступа.", main_keyboard())
            return
        rules = db.get("rules", [])
        if not rules:
            send_message(chat_id, "📜 Глобальных правил пока нет.", main_keyboard())
        else:
            lines = ["📜 Глобальные правила:"]
            for r in rules:
                lines.append(f"\n• {r.get('id')} | {r.get('kind')} | {'ON' if r.get('enabled', True) else 'OFF'}\n  {r.get('pattern')} → {r.get('response')}")
            send_message(chat_id, "\n".join(lines)[:MAX_REPLY], main_keyboard())
        return
    if text.startswith("/rule_add"):
        if not is_admin(user_id):
            send_message(chat_id, "⛔ Нет доступа.", main_keyboard())
            return
        parts = text.split(" ", 3)
        if len(parts) < 4 or parts[1] not in {"exact", "contains", "similar"}:
            send_message(chat_id, "Используй: /rule_add exact|contains|similar ТЕКСТ ОТВЕТ", main_keyboard())
            return
        try:
            r = add_rule(parts[1], parts[2], parts[3])
            send_message(chat_id, f"✅ Правило создано: {r['id']}", main_keyboard())
        except Exception as e:
            send_message(chat_id, f"❌ {e}", main_keyboard())
        return
    if text.startswith("/rule_del"):
        if not is_admin(user_id):
            send_message(chat_id, "⛔ Нет доступа.", main_keyboard())
            return
        parts = text.split()
        if len(parts) != 2 or not remove_rule(parts[1]):
            send_message(chat_id, "❌ Правило не найдено.", main_keyboard())
        else:
            send_message(chat_id, "✅ Правило удалено.", main_keyboard())
        return
    if text.startswith("/newcode"):
        if not is_admin(user_id):
            send_message(chat_id, "⛔ Нет доступа.", main_keyboard())
            return
        parts = text.split()
        if len(parts) < 2:
            send_message(chat_id, "Используй: /newcode DAYS [REQUESTS]", main_keyboard())
            return
        try:
            days = int(parts[1])
            requests_limit = int(parts[2]) if len(parts) > 2 else 0
            code = create_license(days, requests_limit)
            send_message(chat_id, f"🔑 Новый код:\n\n{code}\n\nСрок: {days} дн.\nЛимит запросов: {'без лимита' if requests_limit <= 0 else requests_limit}", main_keyboard())
        except Exception:
            send_message(chat_id, "❌ Формат: /newcode DAYS [REQUESTS]", main_keyboard())
        return
    if text.startswith("/give"):
        if not is_admin(user_id):
            send_message(chat_id, "⛔ Нет доступа.", main_keyboard())
            return
        parts = text.split()
        if len(parts) < 3:
            send_message(chat_id, "Используй: /give USER_ID DAYS [REQUESTS]", main_keyboard())
            return
        try:
            target = parts[1]
            days = int(parts[2])
            requests_limit = int(parts[3]) if len(parts) > 3 else 0
            code = create_license(days, requests_limit)
            ok, message = activate_license(target, code)
            send_message(chat_id, f"{message}\n\nКод: {code}", main_keyboard())
        except Exception:
            send_message(chat_id, "❌ Формат: /give USER_ID DAYS [REQUESTS]", main_keyboard())
        return
    if text.startswith("/license"):
        if not is_admin(user_id):
            send_message(chat_id, "⛔ Нет доступа.", main_keyboard())
            return
        parts = text.split()
        if len(parts) != 2:
            send_message(chat_id, "Используй: /license USER_ID", main_keyboard())
            return
        send_message(chat_id, get_license_status(parts[1]), main_keyboard())
        return
    if text.startswith("/revoke") or text.startswith("/block") or text.startswith("/unblock"):
        if not is_admin(user_id):
            send_message(chat_id, "⛔ Нет доступа.", main_keyboard())
            return
        parts = text.split()
        if len(parts) != 2:
            send_message(chat_id, "Укажи USER_ID.", main_keyboard())
            return
        target = parts[1]
        if text.startswith("/revoke"):
            msg_text = revoke_user(target)
        elif text.startswith("/block"):
            msg_text = block_user(target)
        else:
            msg_text = unblock_user(target)
        send_message(chat_id, msg_text, main_keyboard())
        return

    if text.startswith("/rule"):
        if not is_admin(user_id):
            send_message(chat_id, "⛔ Нет доступа.", main_keyboard()); return
        parts = text.split(" ", 3)
        if len(parts) < 4:
            send_message(chat_id, "Используй: /rule contains|exact ТЕКСТ ОТВЕТ", main_keyboard()); return
        mode = parts[1].strip().lower()
        try:
            add_global_rule(parts[2], parts[3], mode=mode)
            send_message(chat_id, "✅ Глобальное правило добавлено.", main_keyboard())
        except Exception as e:
            send_message(chat_id, f"❌ {e}", main_keyboard())
        return
    if text == "/rules":
        if not is_admin(user_id):
            send_message(chat_id, "⛔ Нет доступа.", main_keyboard()); return
        rules = get_global_rules(enabled_only=False)
        if not rules:
            send_message(chat_id, "📜 Глобальных правил нет.", main_keyboard())
        else:
            send_message(chat_id, "📜 Правила:\n\n" + "\n".join([f"• {r.get('id')}: {r.get('pattern')} → {r.get('response')[:120]}" for r in rules]), main_keyboard())
        return

    if text and not msg.get("photo") and not msg.get("document"):
        rule_response = match_global_rule(text)
        if rule_response:
            send_message(chat_id, rule_response, main_keyboard())
            return

    if os.getenv("LICENSE_REQUIRED", "0").strip() == "1" and not is_admin(user_id):
        allowed, reason = user_has_access(user_id)
        if not allowed:
            send_message(chat_id, reason + "\n\n🔑 Активируй доступ командой /activate КОД.", main_keyboard())
            return

    if text.startswith("/start"):
        send_message(chat_id, f"🤖 BulbaMaxAI {BOT_VERSION} готов.\nПросто отправь задачу — я сам выберу способ выполнения.", main_keyboard())
        return
    if text.startswith("/help"):
        send_message(chat_id, "Просто отправляй текст, фото или поддерживаемый файл. Агент сам определит задачу.\n\nКоманды: /new /clear /stats /memory /remember /forget /models", main_keyboard())
        return
    if text.startswith("/new"):
        name = new_chat(u)
        save_db()
        send_message(chat_id, f"🆕 Создан чат «{name}».", main_keyboard())
        return
    if text.startswith("/clear"):
        get_chat(u)["history"] = []
        get_chat(u)["last_prompt"] = None
        get_chat(u)["last_request"] = None
        save_db()
        send_message(chat_id, "🧹 Текущий чат очищен.", main_keyboard())
        return
    if text.startswith("/stats"):
        show_stats(chat_id, u)
        return
    if text.startswith("/memory"):
        show_memory(chat_id, u)
        return
    if text.startswith("/remember"):
        arg = text[len("/remember"):].strip()
        if "=" in arg:
            k, v = [x.strip() for x in arg.split("=", 1)]
            if k:
                u["memory"][k] = v[:1000]
                save_db()
                send_message(chat_id, f"💾 Сохранил: {k}", main_keyboard())
            return
        send_message(chat_id, "Используй: /remember ключ=значение", main_keyboard())
        return
    if text.startswith("/forget"):
        k = text[len("/forget"):].strip()
        if k:
            u["memory"].pop(k, None)
            save_db()
        send_message(chat_id, "🗑 Готово.", main_keyboard())
        return
    if text.startswith("/models"):
        send_message(chat_id, "🧠 Доступные модели:", model_keyboard(u))
        return
    if text in ("🤖 Чат", "🤖 Авто"):
        u["model"] = "auto" if text == "🤖 Авто" else u.get("model", "auto")
        u["pending_action"] = None
        save_db()
        send_message(chat_id, "🤖 Режим чата включён.\nОтправь сообщение.", main_keyboard(user_id=user_id))
        return
    if text in ("🧠 Модели", "🧠 Модель"):
        send_message(chat_id, "🧠 Выбор модели:", model_keyboard(u))
        return
    if text == "📊 Статистика":
        show_stats(chat_id, u)
        return
    if text == "💾 Память":
        show_memory(chat_id, u)
        return
    if text == "💬 Чаты":
        show_chats(chat_id, u)
        return
    if text == "🆕 Новый чат":
        u["pending_action"] = {"type": "chat_name"}; save_db()
        send_message(chat_id, "💬 Как назвать новый чат?", main_keyboard(user_id=user_id))
        return
    if text == "⭐ Избранное":
        show_favorites(chat_id, u)
        return
    if text == "🎨 Генерация":
        send_message(chat_id, "🎨 Media Studio\n\nЧто создать?", media_keyboard())
        return
    if text == "🔑 Лицензия":
        show_license(chat_id, user_id)
        return
    if text == "👑 Админ":
        if is_admin(user_id): send_message(chat_id, "👑 Панель администратора", admin_keyboard())
        else: send_message(chat_id, "⛔ Нет доступа.", main_keyboard(user_id=user_id))
        return
    if text == "🧹 Очистить":
        get_chat(u)["history"] = []
        get_chat(u)["last_prompt"] = None
        get_chat(u)["last_request"] = None
        save_db()
        send_message(chat_id, "🧹 Текущий чат очищен.", main_keyboard(user_id=user_id))
        return
    if text == "⚙️ Настройки":
        show_settings(chat_id, u)
        return

    if "photo" in msg:
        photo = msg["photo"][-1]
        try:
            data = tg_file(photo["file_id"])
            if len(data) > MAX_IMAGE_BYTES:
                raise RuntimeError("Изображение слишком большое.")
            start_ai_request(
                chat_id, user_id, u,
                caption or "Что изображено на этом фото? Проанализируй изображение.",
                image={"data": data, "mime": "image/jpeg"},
                media_ref={"file_id": photo["file_id"]},
            )
        except Exception as e:
            send_message(chat_id, f"❌ Не удалось обработать изображение: {e}", main_keyboard())
        return

    if "document" in msg:
        doc = msg["document"]
        try:
            data = tg_file(doc["file_id"])
            file_text = read_text_file(doc.get("file_name", "file.txt"), data)
            start_ai_request(
                chat_id, user_id, u,
                caption or "Проанализируй этот файл и объясни его содержимое.",
                file_text=file_text,
                file_name=doc.get("file_name", "file.txt"),
                media_ref={"file_id": doc["file_id"]},
            )
        except Exception as e:
            send_message(chat_id, f"❌ Не удалось обработать файл: {e}", main_keyboard())
        return

    if text:
        start_ai_request(chat_id, user_id, u, text)


def process_callback(q):
    data = q.get("data", "")
    msg = q.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    user_id = q.get("from", {}).get("id", chat_id)
    if chat_id is None:
        return
    u = get_user(user_id)

    if data == "back":
        answer_callback(q["id"])
        tg("editMessageText", {"chat_id": chat_id, "message_id": msg.get("message_id"), "text": "🤖 Главное меню"})
        send_message(chat_id, "Готов.", main_keyboard(user_id=user_id))
        return

    if data == "memory_clear":
        u["memory"] = {}
        save_db()
        answer_callback(q["id"], "Память очищена")
        tg("editMessageText", {"chat_id": chat_id, "message_id": msg.get("message_id"), "text": "💾 Память очищена."})
        return

    if data.startswith("style:"):
        value = data.split(":", 1)[1]
        if value not in STYLE_PROMPTS:
            answer_callback(q["id"], "Неизвестный стиль")
            return
        u["style"] = value
        save_db()
        answer_callback(q["id"], "Стиль изменён")
        tg("editMessageText", {"chat_id": chat_id, "message_id": msg.get("message_id"), "text": "⚙️ Стиль ответа изменён."})
        return

    if data.startswith("model:"):
        value = data.split(":", 1)[1]
        if value == "auto":
            u["model"] = "auto"
        else:
            try:
                idx = int(value)
                models = [m for m in get_models() if not is_bad_model(m["id"])]
                if idx < 0 or idx >= len(models):
                    raise ValueError
                u["model"] = models[idx]["id"]
            except Exception:
                answer_callback(q["id"], "Модель уже недоступна")
                return
        save_db()
        answer_callback(q["id"], "Модель выбрана")
        tg("editMessageText", {"chat_id": chat_id, "message_id": msg.get("message_id"), "text": f"🧠 Выбрано: {u['model']}"})
        return

    if data == "new_chat":
        u["pending_action"]={"type":"chat_name"}; save_db(); answer_callback(q["id"], "Введите название"); tg("editMessageText", {"chat_id": chat_id, "message_id": msg.get("message_id"), "text": "💬 Как назвать новый чат?"}); return

    if data.startswith("chat:open:"):
        name = data.split(":", 2)[2]
        if name in u["chats"]:
            u["active_chat"] = name
            save_db()
            answer_callback(q["id"], "Чат выбран")
            edit_message(chat_id, msg.get("message_id"), f"💬 Активен чат «{name}».")
        else:
            answer_callback(q["id"], "Чат не найден")
        return

    if data.startswith("chat:rename:"):
        name = data.split(":", 2)[2]
        if name in u["chats"]:
            u["pending_action"] = {"type": "rename_chat", "old": name}
            save_db()
            answer_callback(q["id"], "Введите новое название")
            send_message(chat_id, "✏️ Отправь новое название чата.", main_keyboard(user_id=user_id))
        else:
            answer_callback(q["id"], "Чат не найден")
        return

    if data.startswith("chat:pin:"):
        name = data.split(":", 2)[2]
        if name not in u["chats"]:
            answer_callback(q["id"], "Чат не найден")
            return
        pins = u.setdefault("pinned_chats", [])
        if name in pins:
            pins.remove(name)
            answer_callback(q["id"], "Откреплён")
        else:
            pins.append(name)
            answer_callback(q["id"], "Закреплён")
        save_db()
        show_chats(chat_id, u)
        return

    if data.startswith("chat:delete:"):
        name = data.split(":", 2)[2]
        if name == "main":
            answer_callback(q["id"], "Главный чат нельзя удалить")
            return
        if name in u["chats"]:
            del u["chats"][name]
            u["pinned_chats"] = [x for x in u.get("pinned_chats", []) if x != name]
            if u.get("active_chat") == name:
                u["active_chat"] = "main"
            save_db()
            answer_callback(q["id"], "Удалён")
            show_chats(chat_id, u)
        else:
            answer_callback(q["id"], "Чат не найден")
        return

    if data.startswith("media:"):
        kind = data.split(":", 1)[1]
        answer_callback(q["id"], "Выбрано")
        start_media_prompt(chat_id, u, kind)
        return

    if data == "favorite:last":
        last=get_chat(u).get("history",[])
        answer=next((x.get("content") for x in reversed(last) if x.get("role")=="assistant"),None)
        if answer: save_favorite(u,answer); answer_callback(q["id"],"Сохранено ⭐")
        else: answer_callback(q["id"],"Нечего сохранять")
        return

    if data.startswith("favdel:"):
        fid = data.split(":", 1)[1]
        u["favorites"] = [x for x in u.get("favorites", []) if str(x.get("id")) != fid]
        save_db(); answer_callback(q["id"], "Удалено")
        tg("editMessageText", {"chat_id": chat_id, "message_id": msg.get("message_id"), "text": "🗑 Ответ удалён из избранного."})
        return

    if data == "license:activate":
        u["pending_action"] = {"type": "activate"}; save_db(); answer_callback(q["id"])
        tg("editMessageText", {"chat_id": chat_id, "message_id": msg.get("message_id"), "text": "🔑 Отправь код лицензии следующим сообщением."})
        return
    if data == "license:status":
        answer_callback(q["id"]); edit_message(chat_id, msg.get("message_id"), "🔑 Лицензия\n\n" + get_license_status(user_id), {"inline_keyboard":[[{"text":"🔑 Активировать код","callback_data":"license:activate"}]]})
        return
    if data.startswith("settings:"):
        action = data.split(":",1)[1]
        if action == "notifications":
            u["notifications"] = not u.get("notifications", True); save_db(); answer_callback(q["id"], "Настройка изменена"); show_settings(chat_id,u); return
        if action == "clear":
            get_chat(u)["history"]=[]; get_chat(u)["last_prompt"]=None; get_chat(u)["last_request"]=None; save_db(); answer_callback(q["id"], "Чат очищен"); show_settings(chat_id,u); return

    if data.startswith("chat:delete:"):
        name=data.split(":",2)[2]
        if name != "main" and name in u["chats"]:
            del u["chats"][name]
            if u["active_chat"]==name: u["active_chat"]="main"
            save_db(); answer_callback(q["id"], "Удалён"); show_chats(chat_id,u)
        return

    if data.startswith("admin:"):
        if not is_admin(user_id): answer_callback(q["id"],"Нет доступа"); return
        action=data.split(":",1)[1]; answer_callback(q["id"])
        if action=="stats":
            send_message(chat_id,f"📊 Админ-статистика\n\n👥 Пользователей: {len(db.get('users',{}))}\n💬 Запросов: {db.get('total_requests',0)}\n❌ Ошибок: {db.get('total_errors',0)}\n📜 Правил: {len(db.get('rules',[]))}",admin_keyboard()); return
        if action=="users":
            send_message(chat_id,f"👥 Пользователей: {len(db.get('users',{}))}\n\nДля точечной проверки используй /license USER_ID.",admin_keyboard()); return
        if action=="rules":
            send_message(chat_id,"📜 Управление правилами\n\n/rules\n/rule_add exact|contains|similar ТЕКСТ ОТВЕТ\n/rule_del ID",admin_keyboard()); return
        if action=="licenses":
            send_message(chat_id,"🔑 Лицензии\n\n/newcode DAYS [REQUESTS]\n/give USER_ID DAYS [REQUESTS]\n/license USER_ID\n/revoke USER_ID",admin_keyboard()); return
        if action=="blocks":
            send_message(chat_id,"🚫 Блокировки\n\n/block USER_ID\n/unblock USER_ID",admin_keyboard()); return
        send_message(chat_id,"⚙️ Системные настройки управляются через переменные окружения и конфигурацию проекта.",admin_keyboard()); return

    if data == "retry":
        last = get_chat(u).get("last_request")
        answer_callback(q["id"], "Повторяю")
        if not last:
            send_message(chat_id, "Нет запроса для повтора.", main_keyboard())
            return

        try:
            kind = last.get("kind")
            if kind == "image" and last.get("file_id"):
                data_bytes = tg_file(last["file_id"])
                start_ai_request(
                    chat_id, user_id, u, last.get("text", ""),
                    image={"data": data_bytes, "mime": "image/jpeg"},
                    media_ref={"file_id": last["file_id"]},
                )
            elif kind == "file" and last.get("file_id"):
                data_bytes = tg_file(last["file_id"])
                name = last.get("file_name") or "file.txt"
                file_text = read_text_file(name, data_bytes)
                start_ai_request(
                    chat_id, user_id, u, last.get("text", ""),
                    file_text=file_text,
                    file_name=name,
                    media_ref={"file_id": last["file_id"]},
                )
            else:
                start_ai_request(chat_id, user_id, u, last.get("text", ""))
        except Exception as e:
            send_message(chat_id, f"❌ Не удалось повторить запрос: {e}", main_keyboard())
        return


def configure_telegram_menu():
    # Reset the Telegram menu button to the standard commands button.
    result=tg("setChatMenuButton", {"menu_button": json.dumps({"type":"commands"})})
    if result is None:
        print("Telegram menu button configuration skipped/failed")


def main():
    load_db()
    configure_telegram_menu()
    print(f"BulbaMaxAI {BOT_VERSION} started")
    offset = None
    while True:
        try:
            params = {"timeout": POLL_TIMEOUT}
            if offset is not None:
                params["offset"] = offset
            updates = tg("getUpdates", params, timeout=POLL_TIMEOUT + 10)
            if not updates:
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    if "callback_query" in update:
                        process_callback(update["callback_query"])
                    elif "message" in update:
                        process_message(update["message"])
                except Exception as e:
                    print("Update error:", repr(e))
        except Exception as e:
            print("Main loop error:", repr(e))
            time.sleep(3)


if __name__ == "__main__":
    main()
