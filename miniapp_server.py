import os
import json
import time
import hmac
import hashlib
import urllib.parse
import base64
import threading

from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bot
import licenses


HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "8080"))

MAX_BODY = 12 * 1024 * 1024
MAX_MESSAGE = 12000
MAX_IMAGE_BASE64 = 10 * 1024 * 1024
AUTH_MAX_AGE = 24 * 60 * 60

STYLE_VALUES = {
    "normal",
    "short",
    "detailed",
}

ROOT = Path(__file__).resolve().parent
MINIAPP_FILE = ROOT / "miniapp" / "index.html"


# ============================================================
# USER LOCKS
# ============================================================

_user_locks = {}
_user_locks_lock = threading.RLock()


def get_user_lock(user_id):
    uid = str(user_id)

    with _user_locks_lock:
        lock = _user_locks.get(uid)

        if lock is None:
            lock = threading.RLock()
            _user_locks[uid] = lock

        return lock


# ============================================================
# HTTP
# ============================================================

def send_json(handler, status, data):
    body = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    handler.send_response(status)

    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8",
    )

    handler.send_header(
        "Content-Length",
        str(len(body)),
    )

    handler.send_header(
        "Cache-Control",
        "no-store",
    )

    handler.send_header(
        "X-Content-Type-Options",
        "nosniff",
    )

    handler.send_header(
        "Referrer-Policy",
        "no-referrer",
    )

    handler.end_headers()

    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def send_html(handler, html):
    body = html.encode("utf-8")

    handler.send_response(200)

    handler.send_header(
        "Content-Type",
        "text/html; charset=utf-8",
    )

    handler.send_header(
        "Content-Length",
        str(len(body)),
    )

    handler.send_header(
        "Cache-Control",
        "no-store",
    )

    handler.send_header(
        "X-Content-Type-Options",
        "nosniff",
    )

    handler.end_headers()

    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def read_json(handler):
    try:
        content_length = int(
            handler.headers.get(
                "Content-Length",
                "0",
            )
        )
    except (TypeError, ValueError):
        raise ValueError(
            "Некорректный Content-Length."
        )

    if content_length <= 0:
        raise ValueError(
            "Пустой запрос."
        )

    if content_length > MAX_BODY:
        raise ValueError(
            "Запрос слишком большой."
        )

    raw = handler.rfile.read(
        content_length
    )

    try:
        data = json.loads(
            raw.decode("utf-8")
        )
    except Exception:
        raise ValueError(
            "Некорректный JSON."
        )

    if not isinstance(data, dict):
        raise ValueError(
            "JSON должен быть объектом."
        )

    return data


# ============================================================
# TELEGRAM WEB APP AUTH
# ============================================================

def validate_init_data(init_data):
    if not init_data:
        return None

    try:
        parsed = urllib.parse.parse_qs(
            init_data,
            keep_blank_values=True,
        )

        received_hash = parsed.get(
            "hash",
            [""],
        )[0]

        if not received_hash:
            return None

        auth_date_raw = parsed.get(
            "auth_date",
            [""],
        )[0]

        if not auth_date_raw:
            return None

        auth_date = int(auth_date_raw)

        if (
            int(time.time()) - auth_date
            > AUTH_MAX_AGE
        ):
            return None

        if (
            auth_date - int(time.time())
            > 60
        ):
            return None

        data_pairs = []

        for key, values in parsed.items():
            if key == "hash":
                continue

            value = (
                values[0]
                if values
                else ""
            )

            data_pairs.append(
                f"{key}={value}"
            )

        data_pairs.sort()

        data_check_string = "\n".join(
            data_pairs
        )

        secret_key = hmac.new(
            b"WebAppData",
            bot.BOT_TOKEN.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash,
        ):
            return None

        user_raw = parsed.get(
            "user",
            [""],
        )[0]

        if not user_raw:
            return None

        user_data = json.loads(
            user_raw
        )

        if not isinstance(
            user_data,
            dict,
        ):
            return None

        if not user_data.get("id"):
            return None

        return user_data

    except Exception as exc:
        print(
            "Mini App auth error:",
            repr(exc),
        )
        return None


def get_user_from_request(handler):
    init_data = handler.headers.get(
        "X-Telegram-Init-Data",
        "",
    ).strip()

    if not init_data:
        init_data = handler.headers.get(
            "X-Telegram-Init-Data-Raw",
            "",
        ).strip()

    return validate_init_data(
        init_data
    )


# ============================================================
# ACCESS
# ============================================================

def require_access(user_id):
    if os.getenv(
        "LICENSE_REQUIRED",
        "0",
    ).strip() != "1":
        return True, ""

    if licenses.is_admin(user_id):
        return True, ""

    return licenses.user_has_access(
        user_id
    )


# ============================================================
# MODELS
# ============================================================

def model_list():
    result = []

    try:
        models = bot.get_models()
    except Exception as exc:
        print(
            "Models error:",
            repr(exc),
        )
        models = []

    for item in models:
        if not isinstance(
            item,
            dict,
        ):
            continue

        model_id = item.get("id")

        if not model_id:
            continue

        try:
            if bot.is_bad_model(
                model_id
            ):
                continue
        except Exception:
            pass

        result.append({
            "id": str(model_id),
            "name": str(
                item.get(
                    "name",
                    model_id,
                )
            ),
        })

    return result


def validate_model(model):
    if not model:
        return True

    if model == "auto":
        return True

    return any(
        item["id"] == model
        for item in model_list()
    )


# ============================================================
# STATE
# ============================================================

def chat_title(name):
    name = str(
        name or ""
    ).strip()

    if not name:
        return "Новый чат"

    return name[:80]


def history_for_frontend(history):
    result = []

    for item in history:
        if not isinstance(
            item,
            dict,
        ):
            continue

        role = item.get("role")

        if role not in (
            "user",
            "assistant",
        ):
            continue

        content = item.get(
            "content",
            "",
        )

        if isinstance(
            content,
            list,
        ):
            text_parts = []

            for part in content:
                if not isinstance(
                    part,
                    dict,
                ):
                    continue

                if part.get("type") == "text":
                    text_parts.append(
                        str(
                            part.get(
                                "text",
                                "",
                            )
                        )
                    )

            content = "\n".join(
                text_parts
            )

        result.append({
            "role": role,
            "content": str(
                content or ""
            ),
        })

    return result


def frontend_state(user_id, user):
    chats = []

    for chat_id, chat in user.get(
        "chats",
        {},
    ).items():

        if not isinstance(
            chat,
            dict,
        ):
            continue

        chats.append({
            "id": str(chat_id),
            "name": chat_title(
                chat_id
            ),
            "active": (
                str(chat_id)
                == str(
                    user.get(
                        "active_chat",
                        "main",
                    )
                )
            ),
            "messages": history_for_frontend(
                chat.get(
                    "history",
                    [],
                )
            ),
            "requests": int(
                chat.get(
                    "requests",
                    0,
                )
            ),
            "created": int(
                chat.get(
                    "created",
                    0,
                )
            ),
        })

    memory = user.get(
        "memory",
        {},
    )

    if not isinstance(
        memory,
        dict,
    ):
        memory = {}

    return {
        "user": {
            "id": str(user_id),
        },

        "model": user.get(
            "model",
            "auto",
        ),

        "style": user.get(
            "style",
            "normal",
        ),

        "active_chat": user.get(
            "active_chat",
            "main",
        ),

        "chats": chats,

        "memory": memory,

        "requests": int(
            user.get(
                "requests",
                0,
            )
        ),

        "errors": int(
            user.get(
                "errors",
                0,
            )
        ),

        "version": bot.BOT_VERSION,
    }


# ============================================================
# CHAT
# ============================================================

def get_chat(user, chat_id=None):
    if chat_id is None:
        chat_id = user.get(
            "active_chat",
            "main",
        )

    chats = user.get(
        "chats",
        {},
    )

    chat = chats.get(
        chat_id
    )

    return chat


def build_messages(
    user,
    chat,
    message,
):
    messages = [
        {
            "role": "system",
            "content": bot.style_system(
                user
            ),
        }
    ]

    history = chat.get(
        "history",
        [],
    )

    for item in history[
        -bot.MAX_HISTORY:
    ]:
        if not isinstance(
            item,
            dict,
        ):
            continue

        role = item.get("role")

        if role not in (
            "user",
            "assistant",
        ):
            continue

        content = item.get(
            "content",
            "",
        )

        if not content:
            continue

        messages.append({
            "role": role,
            "content": content,
        })

    messages.append({
        "role": "user",
        "content": message,
    })

    return messages


# ============================================================
# IMAGE
# ============================================================

def make_image_content(
    data_url,
    question,
):
    if not isinstance(
        data_url,
        str,
    ):
        raise ValueError(
            "Некорректное изображение."
        )

    if len(data_url) > MAX_IMAGE_BASE64:
        raise ValueError(
            "Изображение слишком большое."
        )

    if not data_url.startswith(
        "data:image/"
    ):
        raise ValueError(
            "Разрешены только изображения."
        )

    try:
        header, encoded = data_url.split(
            ",",
            1,
        )
    except ValueError:
        raise ValueError(
            "Некорректный формат изображения."
        )

    if ";base64" not in header:
        raise ValueError(
            "Изображение должно быть base64."
        )

    mime = (
        header[5:]
        .split(";", 1)[0]
        .strip()
        .lower()
    )

    allowed = {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
    }

    if mime not in allowed:
        raise ValueError(
            "Формат изображения не поддерживается."
        )

    try:
        base64.b64decode(
            encoded,
            validate=True,
        )
    except Exception:
        raise ValueError(
            "Повреждённое изображение."
        )

    return [
        {
            "type": "text",
            "text": question,
        },
        {
            "type": "image_url",
            "image_url": {
                "url": data_url,
            },
        },
    ]


# ============================================================
# CHAT MANAGEMENT
# ============================================================

def create_chat(
    user,
    requested_name=None,
):
    name = str(
        requested_name or ""
    ).strip()

    if not name:
        name = "Чат 1"

    name = name[:60]

    if name in user["chats"]:
        base = name
        index = 2

        while (
            f"{base} {index}"
            in user["chats"]
        ):
            index += 1

        name = f"{base} {index}"

    if len(
        user["chats"]
    ) >= bot.MAX_CHATS:

        candidates = []

        for old_name, old_chat in user[
            "chats"
        ].items():

            if old_name == user.get(
                "active_chat"
            ):
                continue

            candidates.append(
                (
                    int(
                        old_chat.get(
                            "created",
                            0,
                        )
                    ),
                    old_name,
                )
            )

        if candidates:
            _, oldest = min(
                candidates
            )

            del user[
                "chats"
            ][oldest]

    user["chats"][name] = (
        bot.default_chat()
    )

    user["active_chat"] = name

    return name


def rename_chat(
    user,
    old_name,
    new_name,
):
    if old_name not in user[
        "chats"
    ]:
        raise ValueError(
            "Чат не найден."
        )

    new_name = str(
        new_name or ""
    ).strip()[:60]

    if not new_name:
        raise ValueError(
            "Название не может быть пустым."
        )

    if (
        new_name != old_name
        and new_name in user["chats"]
    ):
        raise ValueError(
            "Чат с таким названием уже существует."
        )

    if new_name == old_name:
        return new_name

    chat = user[
        "chats"
    ].pop(old_name)

    user[
        "chats"
    ][new_name] = chat

    if user.get(
        "active_chat"
    ) == old_name:
        user[
            "active_chat"
        ] = new_name

    return new_name


def delete_chat(
    user,
    chat_id,
):
    if chat_id not in user[
        "chats"
    ]:
        raise ValueError(
            "Чат не найден."
        )

    if len(
        user["chats"]
    ) <= 1:
        raise ValueError(
            "Нельзя удалить последний чат."
        )

    del user[
        "chats"
    ][chat_id]

    if user.get(
        "active_chat"
    ) == chat_id:

        user[
            "active_chat"
        ] = next(
            iter(
                user["chats"]
            )
        )


def clear_chat(
    user,
    chat_id,
):
    chat = get_chat(
        user,
        chat_id,
    )

    if chat is None:
        raise ValueError(
            "Чат не найден."
        )

    chat["history"] = []

    chat["last_prompt"] = None

    chat["last_request"] = None


# ============================================================
# AI
# ============================================================

def call_ai(
    user,
    messages,
    vision=False,
    requested_model=None,
):
    """
    Совместимость с текущим bot.py.

    Если в будущем ai_chat получит preferred_model,
    используем его.

    Если его пока нет — временно используем выбранную
    модель и сразу возвращаем значение обратно.
    """

    if requested_model in (
        None,
        "",
        "auto",
    ):
        return bot.ai_chat(
            user,
            messages,
            vision=vision,
        )

    ai_chat_code = bot.ai_chat

    try:
        return ai_chat_code(
            user,
            messages,
            vision=vision,
            preferred_model=requested_model,
        )

    except TypeError as exc:
        if (
            "preferred_model"
            not in str(exc)
        ):
            raise

    old_model = user.get(
        "model",
        "auto",
    )

    user["model"] = requested_model

    try:
        return bot.ai_chat(
            user,
            messages,
            vision=vision,
        )
    finally:
        user["model"] = old_model


def handle_chat(
    user_id,
    user,
    payload,
):
    message = str(
        payload.get(
            "message",
            "",
        )
    ).strip()

    image = payload.get(
        "image"
    )

    if not message and not image:
        raise ValueError(
            "Введите сообщение."
        )

    if not message:
        message = (
            "Проанализируй это изображение."
        )

    if len(message) > MAX_MESSAGE:
        raise ValueError(
            f"Сообщение слишком длинное. Максимум {MAX_MESSAGE} символов."
        )

    chat_id = payload.get(
        "chat_id"
    )

    if chat_id:
        chat_id = str(chat_id)

        if chat_id not in user[
            "chats"
        ]:
            raise ValueError(
                "Чат не найден."
            )

        user[
            "active_chat"
        ] = chat_id

    else:
        chat_id = user.get(
            "active_chat",
            "main",
        )

    chat = get_chat(
        user,
        chat_id,
    )

    if chat is None:
        raise ValueError(
            "Чат не найден."
        )

    requested_model = str(
        payload.get(
            "model",
            "",
        )
    ).strip()

    if requested_model and not validate_model(
        requested_model
    ):
        raise ValueError(
            "Выбранная модель недоступна."
        )

    style = payload.get(
        "style"
    )

    if style in STYLE_VALUES:
        user["style"] = style

    messages = build_messages(
        user,
        chat,
        message,
    )

    vision = False

    if image:
        image_content = make_image_content(
            image,
            message,
        )

        messages[-1] = {
            "role": "user",
            "content": image_content,
        }

        vision = True

    answer, error = call_ai(
        user,
        messages,
        vision=vision,
        requested_model=(
            requested_model
            or None
        ),
    )

    if not answer:
        user["errors"] = int(
            user.get(
                "errors",
                0,
            )
        ) + 1

        bot.db["total_errors"] = int(
            bot.db.get(
                "total_errors",
                0,
            )
        ) + 1

        bot.save_db()

        raise RuntimeError(
            error
            or "Не удалось получить ответ."
        )

    # Добавляем историю только после успешного ответа.

    chat["history"].append({
        "role": "user",
        "content": message,
    })

    chat["history"].append({
        "role": "assistant",
        "content": answer,
    })

    chat["history"] = chat[
        "history"
    ][
        -bot.MAX_HISTORY:
    ]

    chat["last_prompt"] = message

    chat["last_request"] = {
        "kind": (
            "image"
            if image
            else "text"
        ),
        "text": message,
    }

    chat["requests"] = int(
        chat.get(
            "requests",
            0,
        )
    ) + 1

    user["requests"] = int(
        user.get(
            "requests",
            0,
        )
    ) + 1

    bot.db["total_requests"] = int(
        bot.db.get(
            "total_requests",
            0,
        )
    ) + 1

    # Сохраняем через существующую систему bot.py.
    bot.save_db()

    return {
        "answer": answer,
        "state": frontend_state(
            user_id,
            user,
        ),
    }


# ============================================================
# REQUEST HANDLER
# ============================================================

class MiniAppHandler(
    BaseHTTPRequestHandler
):

    server_version = (
        "BulbaMaxAI-MiniApp/16"
    )

    def log_message(
        self,
        format,
        *args,
    ):
        print(
            "MiniApp:",
            format % args,
        )

    # --------------------------------------------------------
    # GET
    # --------------------------------------------------------

    def do_GET(self):
        try:
            path = urllib.parse.urlparse(
                self.path
            ).path

            if path in (
                "/",
                "/miniapp",
                "/miniapp/",
            ):
                if not MINIAPP_FILE.exists():
                    send_json(
                        self,
                        404,
                        {
                            "ok": False,
                            "error": (
                                "miniapp/index.html "
                                "не найден."
                            ),
                        },
                    )
                    return

                html = MINIAPP_FILE.read_text(
                    encoding="utf-8"
                )

                send_html(
                    self,
                    html,
                )
                return

            if path == "/health":
                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "service": "BulbaMaxAI",
                        "version": bot.BOT_VERSION,
                    },
                )
                return

            user_data = (
                get_user_from_request(
                    self
                )
            )

            if not user_data:
                send_json(
                    self,
                    401,
                    {
                        "ok": False,
                        "error": (
                            "Telegram авторизация "
                            "недействительна."
                        ),
                    },
                )
                return

            user_id = str(
                user_data["id"]
            )

            allowed, reason = (
                require_access(
                    user_id
                )
            )

            if not allowed:
                send_json(
                    self,
                    403,
                    {
                        "ok": False,
                        "error": reason,
                    },
                )
                return

            user = bot.get_user(
                user_id
            )

            if path == "/api/state":
                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "state": frontend_state(
                            user_id,
                            user,
                        ),
                    },
                )
                return

            if path == "/api/models":
                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "models": model_list(),
                    },
                )
                return

            send_json(
                self,
                404,
                {
                    "ok": False,
                    "error": "Маршрут не найден.",
                },
            )

        except Exception as exc:
            print(
                "Mini App GET error:",
                repr(exc),
            )

            send_json(
                self,
                500,
                {
                    "ok": False,
                    "error": (
                        "Внутренняя ошибка сервера."
                    ),
                },
            )

    # --------------------------------------------------------
    # POST
    # --------------------------------------------------------

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(
                self.path
            ).path

            user_data = (
                get_user_from_request(
                    self
                )
            )

            if not user_data:
                send_json(
                    self,
                    401,
                    {
                        "ok": False,
                        "error": (
                            "Telegram авторизация "
                            "недействительна."
                        ),
                    },
                )
                return

            user_id = str(
                user_data["id"]
            )

            allowed, reason = (
                require_access(
                    user_id
                )
            )

            if not allowed:
                send_json(
                    self,
                    403,
                    {
                        "ok": False,
                        "error": reason,
                    },
                )
                return

            payload = read_json(
                self
            )

            user = bot.get_user(
                user_id
            )

            # Один пользователь — один одновременный
            # изменяющий запрос.
            with get_user_lock(
                user_id
            ):

                # ============================================
                # CHAT
                # ============================================

                if path == "/api/chat":
                    result = handle_chat(
                        user_id,
                        user,
                        payload,
                    )

                    send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            **result,
                        },
                    )
                    return

                # ============================================
                # NEW CHAT
                # ============================================

                if path == "/api/chat/new":
                    name = create_chat(
                        user,
                        payload.get(
                            "name"
                        ),
                    )

                    bot.save_db()

                    send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "chat_id": name,
                            "state": frontend_state(
                                user_id,
                                user,
                            ),
                        },
                    )
                    return

                # ============================================
                # SELECT CHAT
                # ============================================

                if path == "/api/chat/select":
                    chat_id = str(
                        payload.get(
                            "chat_id",
                            "",
                        )
                    )

                    if chat_id not in user[
                        "chats"
                    ]:
                        send_json(
                            self,
                            404,
                            {
                                "ok": False,
                                "error": (
                                    "Чат не найден."
                                ),
                            },
                        )
                        return

                    user[
                        "active_chat"
                    ] = chat_id

                    bot.save_db()

                    send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "state": frontend_state(
                                user_id,
                                user,
                            ),
                        },
                    )
                    return

                # ============================================
                # RENAME CHAT
                # ============================================

                if path == "/api/chat/rename":
                    chat_id = str(
                        payload.get(
                            "chat_id",
                            "",
                        )
                    )

                    new_name = str(
                        payload.get(
                            "name",
                            "",
                        )
                    )

                    rename_chat(
                        user,
                        chat_id,
                        new_name,
                    )

                    bot.save_db()

                    send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "state": frontend_state(
                                user_id,
                                user,
                            ),
                        },
                    )
                    return

                # ============================================
                # DELETE CHAT
                # ============================================

                if path == "/api/chat/delete":
                    chat_id = str(
                        payload.get(
                            "chat_id",
                            "",
                        )
                    )

                    delete_chat(
                        user,
                        chat_id,
                    )

                    bot.save_db()

                    send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "state": frontend_state(
                                user_id,
                                user,
                            ),
                        },
                    )
                    return

                # ============================================
                # CLEAR CHAT
                # ============================================

                if path == "/api/chat/clear":
                    chat_id = str(
                        payload.get(
                            "chat_id",
                            user.get(
                                "active_chat",
                                "main",
                            ),
                        )
                    )

                    clear_chat(
                        user,
                        chat_id,
                    )

                    bot.save_db()

                    send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "state": frontend_state(
                                user_id,
                                user,
                            ),
                        },
                    )
                    return

                # ============================================
                # SETTINGS
                # ============================================

                if path == "/api/settings":
                    if "style" in payload:
                        style = payload.get(
                            "style"
                        )

                        if style not in STYLE_VALUES:
                            raise ValueError(
                                "Неизвестный стиль."
                            )

                        user[
                            "style"
                        ] = style

                    if "model" in payload:
                        model = str(
                            payload.get(
                                "model",
                                "auto",
                            )
                        ).strip()

                        if not model:
                            model = "auto"

                        if not validate_model(
                            model
                        ):
                            raise ValueError(
                                "Модель недоступна."
                            )

                        user[
                            "model"
                        ] = model

                    bot.save_db()

                    send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "state": frontend_state(
                                user_id,
                                user,
                            ),
                        },
                    )
                    return

                # ============================================
                # MEMORY
                # ============================================

                if path == "/api/memory":
                    action = str(
                        payload.get(
                            "action",
                            "",
                        )
                    ).strip()

                    memory = user.get(
                        "memory"
                    )

                    if not isinstance(
                        memory,
                        dict,
                    ):
                        memory = {}
                        user[
                            "memory"
                        ] = memory

                    if action == "clear":
                        user[
                            "memory"
                        ] = {}

                    elif action == "set":
                        key = str(
                            payload.get(
                                "key",
                                "",
                            )
                        ).strip()

                        value = str(
                            payload.get(
                                "value",
                                "",
                            )
                        ).strip()

                        if not key:
                            raise ValueError(
                                "Ключ памяти пуст."
                            )

                        user[
                            "memory"
                        ][
                            key[:100]
                        ] = value[:2000]

                    elif action == "delete":
                        key = str(
                            payload.get(
                                "key",
                                "",
                            )
                        ).strip()

                        user[
                            "memory"
                        ].pop(
                            key,
                            None,
                        )

                    elif isinstance(
                        payload.get(
                            "memory"
                        ),
                        dict,
                    ):
                        user[
                            "memory"
                        ] = payload[
                            "memory"
                        ]

                    bot.save_db()

                    send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "state": frontend_state(
                                user_id,
                                user,
                            ),
                        },
                    )
                    return

                # ============================================
                # STATS
                # ============================================

                if path == "/api/stats":
                    send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "stats": {
                                "requests": int(
                                    user.get(
                                        "requests",
                                        0,
                                    )
                                ),
                                "errors": int(
                                    user.get(
                                        "errors",
                                        0,
                                    )
                                ),
                                "chats": len(
                                    user.get(
                                        "chats",
                                        {},
                                    )
                                ),
                                "total_requests": int(
                                    bot.db.get(
                                        "total_requests",
                                        0,
                                    )
                                ),
                                "total_errors": int(
                                    bot.db.get(
                                        "total_errors",
                                        0,
                                    )
                                ),
                            },
                        },
                    )
                    return

                send_json(
                    self,
                    404,
                    {
                        "ok": False,
                        "error": "Маршрут не найден.",
                    },
                )

        except ValueError as exc:
            send_json(
                self,
                400,
                {
                    "ok": False,
                    "error": str(exc),
                },
            )

        except RuntimeError as exc:
            send_json(
                self,
                500,
                {
                    "ok": False,
                    "error": str(exc),
                },
            )

        except Exception as exc:
            print(
                "Mini App POST error:",
                repr(exc),
            )

            send_json(
                self,
                500,
                {
                    "ok": False,
                    "error": (
                        "Внутренняя ошибка сервера."
                    ),
                },
            )


# ============================================================
# SERVER
# ============================================================

def run_server():
    # ВАЖНО:
    # bot.load_db() здесь НЕ вызываем.
    # База уже загружается launcher.py перед запуском
    # Telegram и Mini App.

    server = ThreadingHTTPServer(
        (HOST, PORT),
        MiniAppHandler,
    )

    print(
        f"BulbaMaxAI Mini App server started on "
        f"{HOST}:{PORT}"
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()