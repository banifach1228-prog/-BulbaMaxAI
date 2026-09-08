import os
import json
import time
import base64
import ast
import operator as op
import io
import csv
import re
from pathlib import Path

import requests

BOT_VERSION = "V15"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()

BASE_URL = "https://api.baza-ai.org/v1"
TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DATA_FILE = "v15_memory.json"
LEGACY_DATA_FILE = "v14_memory.json"

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

db = {"users": {}, "total_requests": 0, "total_errors": 0}


def load_db():
    global db
    source = DATA_FILE if Path(DATA_FILE).exists() else LEGACY_DATA_FILE
    try:
        with open(source, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            db.update(data)
        db.setdefault("users", {})
        db.setdefault("total_requests", 0)
        db.setdefault("total_errors", 0)
    except (OSError, json.JSONDecodeError):
        pass


def save_db():
    tmp = DATA_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)
    except OSError as e:
        print("DB save error:", repr(e))


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
    }


def get_user(uid):
    uid = str(uid)
    if uid not in db["users"]:
        db["users"][uid] = default_user()
    u = db["users"][uid]
    u.setdefault("model", "auto")
    u.setdefault("style", "normal")
    u.setdefault("memory", {})
    u.setdefault("chats", {"main": default_chat()})
    u.setdefault("active_chat", "main")
    u.setdefault("requests", 0)
    u.setdefault("errors", 0)
    u.setdefault("rate", [])
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


def main_keyboard():
    return {
        "keyboard": [
            [{"text": "🤖 Авто"}, {"text": "🧠 Модель"}],
            [{"text": "📊 Статистика"}, {"text": "💾 Память"}],
            [{"text": "💬 Чаты"}, {"text": "🆕 Новый чат"}],
            [{"text": "🧹 Очистить"}, {"text": "⚙️ Настройки"}],
        ],
        "resize_keyboard": True,
    }


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
    payload = {"model": model, "messages": messages}
    for attempt in range(3):
        try:
            r = api_session.post(f"{BASE_URL}/chat/completions", json=payload, timeout=API_TIMEOUT)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 + attempt)
                continue
            if not r.ok:
                return None, extract_error(r), r.status_code
            d = r.json()
            choices = d.get("choices") or []
            if not choices:
                return None, "API не вернуло choices.", r.status_code
            content = choices[0].get("message", {}).get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            return str(content or "").strip(), None, r.status_code
        except requests.RequestException as e:
            if attempt < 2:
                time.sleep(2 + attempt)
            else:
                return None, str(e), 0
        except Exception as e:
            return None, repr(e), 0
    return None, "Временная ошибка API.", 0


def choose_model(u, vision=False, preferred=None):
    models = [m for m in get_models() if not is_bad_model(m["id"])]
    available = {m["id"] for m in models}

    if preferred and preferred in available:
        return [preferred]

    ranked = rank_models(models, vision=vision)
    return [m["id"] for m in ranked]


# ---------------- SAFE CALCULATOR ----------------

_BIN = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv, ast.Mod: op.mod, ast.Pow: op.pow,
}
_UN = {ast.UAdd: op.pos, ast.USub: op.neg}


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


# ---------------- AGENT ----------------

AGENT_SYSTEM = """Ты — BulbaMaxAI V15, автоматический агент.
Главное правило: НЕ заставляй пользователя выбирать режим или инструмент.
Сам реши, нужен ли обычный ответ, расчёт или настоящий файл.

Реальные actions:
chat — обычный ответ.
calculator — точный расчёт.
create_xlsx — настоящий Excel-файл.
create_csv — настоящий CSV-файл.
create_chart — настоящий PNG-график.
create_pdf — настоящий PDF-документ.
create_docx — настоящий DOCX-документ.
analyze_file — анализ предоставленного файла.
vision — анализ изображения.

Верни ТОЛЬКО один JSON-объект:
{"action":"...", "reason":"кратко", "expression":"...", "rows":[["Колонка 1","Колонка 2"],["значение",123]], "title":"...", "content":"...", "answer_hint":"..."}

Правила выбора:
1. Если пользователь даёт структурированные данные и просит "удобно", "оформи", "сведи", "сделай список/таблицу" — предпочитай create_xlsx.
2. Если пользователь просит Excel/xlsx/таблицу для скачивания — create_xlsx.
3. Если просит CSV — create_csv.
4. Если просит график, диаграмму, визуализацию — create_chart.
5. Если просит PDF — create_pdf.
6. Если просит Word/DOCX — create_docx.
7. Если это простое математическое выражение — calculator.
8. Если приложен файл — analyze_file, если задача относится к содержимому файла.
9. Если приложено изображение и вопрос относится к нему — vision.
10. Если инструмент не нужен — chat.
11. Не говори, что файл создан, если action не создаёт файл.
12. Не придумывай отсутствующие факты. Но если пользователь явно просит придумать тестовые/примерные данные, их можно сгенерировать.
13. Для create_xlsx/create_csv/create_chart обязательно постарайся вернуть rows с заголовком и данными.
"""

STYLE_PROMPTS = {
    "normal": "Отвечай естественно и понятно.",
    "short": "Отвечай кратко, без лишней воды.",
    "detailed": "Отвечай подробно, структурированно и с объяснениями.",
}


def extract_json_object(text):
    text = str(text or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    decoder = json.JSONDecoder()
    for pos, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[pos:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def agent_plan(user_text, has_image=False, has_file=False):
    hints = []
    if has_image:
        hints.append("Пользователь приложил изображение: при вопросе о нём используй vision.")
    if has_file:
        hints.append("Пользователь приложил файл: анализируй его содержимое.")
    prompt = user_text + ("\n\n" + "\n".join(hints) if hints else "")
    candidates = choose_candidates_for_agent()
    for model in candidates[:5]:
        out, err, _ = call_ai([
            {"role": "system", "content": AGENT_SYSTEM},
            {"role": "user", "content": prompt},
        ], model)
        if out:
            plan = extract_json_object(out)
            if plan and plan.get("action"):
                return plan
    return {"action": "chat", "reason": "fallback"}


def choose_candidates_for_agent():
    return [m["id"] for m in rank_models(get_models(), False) if not is_bad_model(m["id"])]


def style_system(u):
    memory = u.get("memory", {})
    memory_text = json.dumps(memory, ensure_ascii=False)[:5000]
    return (
        "Ты BulbaMaxAI. " + STYLE_PROMPTS.get(u.get("style", "normal"), STYLE_PROMPTS["normal"]) +
        "\nИспользуй сохранённую память пользователя только когда она относится к запросу.\n"
        f"Память: {memory_text}"
    )


def ai_chat(u, messages, vision=False):
    preferred = None if u["model"] == "auto" else u["model"]
    candidates = choose_model(u, vision=vision, preferred=preferred)
    if not candidates:
        return None, "Нет доступных моделей."
    last_error = "Не удалось получить ответ."
    for model in candidates[:8]:
        content, err, status = call_ai(messages, model)
        if content:
            return content, None
        last_error = err or last_error
        if preferred:
            break
        if status not in (400, 404, 429, 500, 502, 503, 504, 0):
            break
    return None, last_error


def build_history(u):
    h = get_chat(u)["history"]
    return h[-MAX_HISTORY:]


def add_history(u, role, content):
    chat = get_chat(u)
    chat["history"].append({"role": role, "content": content})
    chat["history"] = chat["history"][-MAX_HISTORY:]


def handle_ai_request(chat_id, u, text, image=None, file_text=None, file_name=None, media_ref=None):
    if not allowed_request(u):
        send_message(chat_id, "⏳ Слишком много запросов. Подожди несколько секунд.", main_keyboard())
        return

    status = f"🧠 {BOT_VERSION}: анализирую задачу…"
    if image:
        status = "📸 Анализирую изображение…"
    elif file_text:
        status = "📎 Читаю и анализирую файл…"
    status_msg = send_message(chat_id, status)

    u["requests"] += 1
    db["total_requests"] += 1
    chat = get_chat(u)
    chat["requests"] += 1

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
            save_db()
            if status_msg:
                edit_message(chat_id, status_msg["message_id"], answer, retry_keyboard())
            else:
                send_message(chat_id, answer, retry_keyboard())
            return

        plan = {"action": "chat"}
        if not image and not file_text:
            plan = agent_plan(text)

        action = plan.get("action", "chat")

        if action in ("create_xlsx", "create_csv", "create_chart", "create_pdf", "create_docx"):
            stem = re.sub(r"[^A-Za-zА-Яа-я0-9_-]+", "_", text[:35]).strip("_") or "bulbamaxai"
            title = str(plan.get("title") or "BulbaMaxAI").strip()[:120]

            if action in ("create_xlsx", "create_csv", "create_chart"):
                rows = plan.get("rows")
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
                    save_db()
                    return

            if action in ("create_pdf", "create_docx"):
                content = str(plan.get("content") or "").strip()
                if not content:
                    # Ask the main model to produce the document body rather than creating an empty file.
                    content = str(plan.get("answer_hint") or "").strip()

                if not content:
                    action = "chat"
                else:
                    ext = ".pdf" if action == "create_pdf" else ".docx"
                    path = Path(f"{stem}{ext}")
                    if action == "create_pdf":
                        make_pdf(content, path, title)
                        caption = "📕 Готовый PDF"
                    else:
                        make_docx(content, path, title)
                        caption = "📝 Готовый Word-документ"

                    sent = send_document(chat_id, path, caption)
                    if not sent:
                        raise RuntimeError("Telegram не принял документ.")

                    try:
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    add_history(u, "user", text)
                    add_history(u, "assistant", f"Создан файл: {path.name}")
                    chat["last_prompt"] = text
                    chat["last_request"] = {"kind": "text", "text": text}
                    save_db()
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
        save_db()

        if status_msg:
            edit_message(chat_id, status_msg["message_id"], answer, retry_keyboard())
        else:
            send_message(chat_id, answer, retry_keyboard())

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
    rows = []
    for name in u["chats"]:
        mark = "✅ " if name == u["active_chat"] else ""
        rows.append([{"text": mark + name[:45], "callback_data": "chat:" + name[:50]}])
    rows.append([{"text": "➕ Новый чат", "callback_data": "new_chat"}])
    rows.append([{"text": "⬅️ Назад", "callback_data": "back"}])
    send_message(chat_id, "💬 Выбери чат:", {"inline_keyboard": rows})


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


def process_message(msg):
    if "chat" not in msg:
        return
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id", chat_id)
    u = get_user(user_id)

    text = (msg.get("text") or "").strip()
    caption = (msg.get("caption") or "").strip()

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
    if text == "🤖 Авто":
        u["model"] = "auto"
        save_db()
        send_message(chat_id, "🤖 Auto включён. Модель и способ обработки выбираются автоматически.", main_keyboard())
        return
    if text == "🧠 Модель":
        send_message(chat_id, "🧠 Выбери модель:", model_keyboard(u))
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
        name = new_chat(u)
        save_db()
        send_message(chat_id, f"🆕 Создан чат «{name}».", main_keyboard())
        return
    if text == "🧹 Очистить":
        get_chat(u)["history"] = []
        get_chat(u)["last_prompt"] = None
        get_chat(u)["last_request"] = None
        save_db()
        send_message(chat_id, "🧹 Текущий чат очищен.", main_keyboard())
        return
    if text == "⚙️ Настройки":
        send_message(chat_id, "⚙️ Стиль ответа:", settings_keyboard(u["style"]))
        return

    if "photo" in msg:
        photo = msg["photo"][-1]
        try:
            data = tg_file(photo["file_id"])
            if len(data) > MAX_IMAGE_BYTES:
                raise RuntimeError("Изображение слишком большое.")
            handle_ai_request(
                chat_id,
                u,
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
            handle_ai_request(
                chat_id,
                u,
                caption or "Проанализируй этот файл и объясни его содержимое.",
                file_text=file_text,
                file_name=doc.get("file_name", "file.txt"),
                media_ref={"file_id": doc["file_id"]},
            )
        except Exception as e:
            send_message(chat_id, f"❌ Не удалось обработать файл: {e}", main_keyboard())
        return

    if text:
        handle_ai_request(chat_id, u, text)


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
        send_message(chat_id, "Готов.", main_keyboard())
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
        name = new_chat(u)
        save_db()
        answer_callback(q["id"], "Новый чат")
        tg("editMessageText", {"chat_id": chat_id, "message_id": msg.get("message_id"), "text": f"🆕 Создан чат «{name}»."})
        return

    if data.startswith("chat:"):
        name = data.split(":", 1)[1]
        if name in u["chats"]:
            u["active_chat"] = name
            save_db()
            answer_callback(q["id"], "Чат выбран")
            tg("editMessageText", {"chat_id": chat_id, "message_id": msg.get("message_id"), "text": f"💬 Активен чат «{name}»."})
        return

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
                handle_ai_request(
                    chat_id,
                    u,
                    last.get("text", ""),
                    image={"data": data_bytes, "mime": "image/jpeg"},
                    media_ref={"file_id": last["file_id"]},
                )
            elif kind == "file" and last.get("file_id"):
                data_bytes = tg_file(last["file_id"])
                name = last.get("file_name") or "file.txt"
                file_text = read_text_file(name, data_bytes)
                handle_ai_request(
                    chat_id,
                    u,
                    last.get("text", ""),
                    file_text=file_text,
                    file_name=name,
                    media_ref={"file_id": last["file_id"]},
                )
            else:
                handle_ai_request(chat_id, u, last.get("text", ""))
        except Exception as e:
            send_message(chat_id, f"❌ Не удалось повторить запрос: {e}", main_keyboard())
        return


def main():
    load_db()
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
