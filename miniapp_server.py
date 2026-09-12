import os
import json
import time
import hmac
import hashlib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bot


HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "8080"))

MAX_BODY = 12 * 1024 * 1024
MAX_MESSAGE = 12000
MAX_IMAGE_BASE64 = 10 * 1024 * 1024
AUTH_MAX_AGE = 24 * 60 * 60

STYLE_VALUES = {"normal", "short", "detailed"}

ROOT = Path(__file__).resolve().parent
MINIAPP_FILE = ROOT / "miniapp" / "index.html"


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
        "Cache-Control",
        "no-store",
    )
    handler.send_header(
        "Content-Length",
        str(len(body)),
    )
    handler.end_headers()

    handler.wfile.write(body)


def read_json(handler):
    try:
        length = int(
            handler.headers.get(
                "Content-Length",
                "0",
            )
        )
    except ValueError as exc:
        raise ValueError(
            "Некорректный Content-Length."
        ) from exc

    if length < 0 or length > MAX_BODY:
        raise ValueError(
            "Запрос слишком большой."
        )

    if length == 0:
        return {}

    try:
        raw = handler.rfile.read(length)
        data = json.loads(
            raw.decode("utf-8")
        )
    except Exception as exc:
        raise ValueError(
            "Некорректный JSON."
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            "JSON должен быть объектом."
        )

    return data


def validate_init_data(init_data):
    if not init_data:
        raise ValueError(
            "Открой Bulba AI через Telegram."
        )

    token = os.getenv(
        "BOT_TOKEN",
        "",
    ).strip()

    if not token:
        raise RuntimeError(
            "BOT_TOKEN не установлен."
        )

    parsed = urllib.parse.parse_qs(
        init_data,
        keep_blank_values=True,
    )

    received_hash = parsed.get(
        "hash",
        [None],
    )[0]

    auth_date_raw = parsed.get(
        "auth_date",
        [None],
    )[0]

    if not received_hash:
        raise ValueError(
            "В Telegram initData отсутствует hash."
        )

    if not auth_date_raw:
        raise ValueError(
            "В Telegram initData отсутствует auth_date."
        )

    try:
        auth_date = int(auth_date_raw)
    except ValueError as exc:
        raise ValueError(
            "Некорректный auth_date."
        ) from exc

    if abs(
        int(time.time()) - auth_date
    ) > AUTH_MAX_AGE:
        raise ValueError(
            "Сессия Telegram устарела. "
            "Перезапусти Mini App."
        )

    pairs = []

    for key in sorted(parsed):
        if key == "hash":
            continue

        pairs.append(
            f"{key}={parsed[key][0]}"
        )

    data_check_string = "\n".join(
        pairs
    )

    secret_key = hmac.new(
        b"WebAppData",
        token.encode("utf-8"),
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
        raise ValueError(
            "Неверная подпись Telegram."
        )

    user_raw = parsed.get(
        "user",
        [None],
    )[0]

    if not user_raw:
        raise ValueError(
            "Telegram user отсутствует."
        )

    try:
        tg_user = json.loads(
            user_raw
        )
    except Exception as exc:
        raise ValueError(
            "Некорректные данные Telegram user."
        ) from exc

    if not tg_user.get("id"):
        raise ValueError(
            "Telegram user ID отсутствует."
        )

    return tg_user


def get_user_from_request(handler):
    tg_user = validate_init_data(
        handler.headers.get(
            "X-Telegram-Init-Data",
            "",
        ).strip()
    )

    uid = str(
        tg_user["id"]
    )

    user = bot.get_user(uid)

    user["_user_id"] = uid

    user["_telegram_user"] = {
        "id": tg_user.get("id"),
        "first_name": tg_user.get(
            "first_name",
            "",
        ),
        "last_name": tg_user.get(
            "last_name",
            "",
        ),
        "username": tg_user.get(
            "username",
            "",
        ),
        "language_code": tg_user.get(
            "language_code",
            "",
        ),
        "photo_url": tg_user.get(
            "photo_url",
            "",
        ),
    }

    return user


def require_access(user):
    if (
        os.getenv(
            "LICENSE_REQUIRED",
            "0",
        ).strip()
        != "1"
    ):
        return True

    uid = user.get(
        "_user_id"
    )

    if not uid:
        return False

    try:
        if bot.is_admin(uid):
            return True
    except Exception:
        pass

    try:
        return bool(
            bot.user_has_access(uid)
        )
    except Exception:
        return False


def model_list():
    result = []
    seen = set()

    for item in bot.get_models():
        if not isinstance(
            item,
            dict,
        ):
            continue

        mid = str(
            item.get("id") or ""
        ).strip()

        if (
            not mid
            or mid in seen
            or bot.is_bad_model(mid)
        ):
            continue

        seen.add(mid)

        result.append(
            {
                "id": mid,
                "name": mid,
            }
        )

    return result


def validate_model(model):
    model = (
        "auto"
        if model is None
        else str(model).strip()
    )

    if model == "auto":
        return "auto"

    available = {
        item["id"]
        for item in model_list()
    }

    if model not in available:
        raise ValueError(
            "Выбранная модель недоступна."
        )

    return model


def chat_title(chat_id, chat):
    explicit = str(
        chat.get("title") or ""
    ).strip()

    if explicit:
        return explicit

    prompt = str(
        chat.get(
            "last_prompt"
        ) or ""
    ).strip()

    if prompt:
        clean = " ".join(
            prompt.split()
        )

        return (
            clean[:42]
            + (
                "…"
                if len(clean) > 42
                else ""
            )
        )

    if str(chat_id) == "main":
        return "Главный чат"

    return "Новый чат"


def frontend_state(user):
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

        messages = []

        for item in chat.get(
            "history",
            [],
        ):

            if not isinstance(
                item,
                dict,
            ):
                continue

            role = item.get(
                "role"
            )

            if role not in (
                "user",
                "assistant",
            ):
                continue

            messages.append(
                {
                    "role": role,
                    "content": str(
                        item.get(
                            "content",
                            "",
                        )
                        or ""
                    ),
                }
            )

        last_request = chat.get(
            "last_request"
        )

        if isinstance(
            last_request,
            dict,
        ):
            last_request_value = int(
                last_request.get(
                    "ts",
                    0,
                )
                or 0
            )
        elif isinstance(
            last_request,
            (int, float),
        ):
            last_request_value = int(
                last_request
                or 0
            )
        else:
            last_request_value = 0

        chats.append(
            {
                "id": str(chat_id),
                "title": chat_title(
                    chat_id,
                    chat,
                ),
                "messages": messages,
                "requests": int(
                    chat.get(
                        "requests",
                        0,
                    )
                    or 0
                ),
                "created": int(
                    chat.get(
                        "created",
                        0,
                    )
                    or 0
                ),
                "last_request":
                    last_request_value,
            }
        )

    chats.sort(
        key=lambda x: (
            x["last_request"],
            x["created"],
        ),
        reverse=True,
    )

    return {
        "model": user.get(
            "model",
            "auto",
        ),
        "style": user.get(
            "style",
            "normal",
        ),
        "active_chat": str(
            user.get(
                "active_chat",
                "main",
            )
        ),
        "chats": chats,
        "memory": user.get(
            "memory",
            {},
        ),
        "requests": int(
            user.get(
                "requests",
                0,
            )
            or 0
        ),
        "errors": int(
            user.get(
                "errors",
                0,
            )
            or 0
        ),
        "telegram_user": user.get(
            "_telegram_user",
            {},
        ),
        "version": getattr(
            bot,
            "BOT_VERSION",
            "V16",
        ),
    }


def get_chat(
    user,
    chat_id=None,
):
    chat_id = str(
        chat_id
        or user.get(
            "active_chat",
            "main",
        )
    )

    if chat_id not in user["chats"]:
        raise ValueError(
            "Чат не найден."
        )

    return (
        chat_id,
        user["chats"][chat_id],
    )


def build_messages(
    user,
    chat,
    current_content,
):
    messages = [
        {
            "role": "system",
            "content": bot.style_system(
                user
            ),
        }
    ]

    for item in chat.get(
        "history",
        [],
    )[-bot.MAX_HISTORY:]:

        if not isinstance(
            item,
            dict,
        ):
            continue

        role = item.get(
            "role"
        )

        content = item.get(
            "content"
        )

        if (
            role in (
                "user",
                "assistant",
            )
            and content
        ):
            messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

    messages.append(
        {
            "role": "user",
            "content": current_content,
        }
    )

    return messages


def make_image_content(
    text,
    image,
):
    if (
        not isinstance(
            image,
            str,
        )
        or not image.startswith(
            "data:image/"
        )
    ):
        raise ValueError(
            "Поддерживаются только изображения."
        )

    if len(image) > MAX_IMAGE_BASE64:
        raise ValueError(
            "Изображение слишком большое."
        )

    return [
        {
            "type": "text",
            "text": (
                text
                or "Проанализируй изображение."
            ),
        },
        {
            "type": "image_url",
            "image_url": {
                "url": image
            },
        },
    ]


def create_chat(user):
    chat_id = str(
        bot.new_chat(user)
    )

    bot.save_db()

    return chat_id


def rename_chat(
    user,
    chat_id,
    title,
):
    _, chat = get_chat(
        user,
        chat_id,
    )

    title = " ".join(
        str(title or "").split()
    ).strip()

    if not title:
        raise ValueError(
            "Название не может быть пустым."
        )

    chat["title"] = (
        title[:60].rstrip()
    )

    bot.save_db()


def delete_chat(
    user,
    chat_id,
):
    chat_id = str(chat_id)

    get_chat(
        user,
        chat_id,
    )

    if len(
        user["chats"]
    ) <= 1:

        user["chats"] = {
            "main": bot.default_chat()
        }

        user["active_chat"] = "main"

    else:
        del user["chats"][chat_id]

        if str(
            user.get(
                "active_chat"
            )
        ) == chat_id:

            user["active_chat"] = next(
                iter(
                    user["chats"]
                )
            )

    bot.save_db()


def clear_chat(
    user,
    chat_id,
):
    _, chat = get_chat(
        user,
        chat_id,
    )

    chat["history"] = []
    chat["last_prompt"] = None
    chat["last_request"] = None

    if str(chat_id) == "main":
        chat.pop(
            "title",
            None,
        )

    bot.save_db()


def update_chat_title_after_success(
    chat,
    text,
):
    if not str(
        chat.get("title") or ""
    ).strip():

        clean = " ".join(
            str(text or "").split()
        )

        if clean:
            chat["title"] = (
                clean[:42].rstrip()
                + (
                    "…"
                    if len(clean) > 42
                    else ""
                )
            )


def handle_chat(
    user,
    data,
):
    text = str(
        data.get(
            "message",
            "",
        )
        or ""
    ).strip()

    image = data.get(
        "image"
    )

    if not text and not image:
        raise ValueError(
            "Сообщение пустое."
        )

    if len(text) > MAX_MESSAGE:
        raise ValueError(
            "Сообщение слишком длинное."
        )

    requested_model = validate_model(
        data.get(
            "model",
            user.get(
                "model",
                "auto",
            ),
        )
    )

    chat_id, chat = get_chat(
        user,
        data.get(
            "chat_id"
        ),
    )

    user["active_chat"] = chat_id

    if not require_access(
        user
    ):
        raise PermissionError(
            "Для использования Bulba AI "
            "нужна активная лицензия."
        )

    if not bot.allowed_request(
        user
    ):
        raise ValueError(
            "Слишком много запросов. "
            "Подожди немного."
        )

    old_model = user.get(
        "model",
        "auto",
    )

    user["model"] = (
        requested_model
    )

    try:
        if image:
            current_content = (
                make_image_content(
                    text,
                    image,
                )
            )

            messages = build_messages(
                user,
                chat,
                current_content,
            )

            answer, error = bot.ai_chat(
                user,
                messages,
                vision=True,
            )

        else:
            messages = build_messages(
                user,
                chat,
                text,
            )

            answer, error = bot.ai_chat(
                user,
                messages,
                vision=False,
            )

    finally:
        user["model"] = old_model

    if not answer:
        user["errors"] = int(
            user.get(
                "errors",
                0,
            )
            or 0
        ) + 1

        bot.db["total_errors"] = int(
            bot.db.get(
                "total_errors",
                0,
            )
            or 0
        ) + 1

        bot.save_db()

        raise RuntimeError(
            error
            or "Не удалось получить ответ."
        )

    # ВАЖНО:
    # текущий запрос НЕ находится в history
    # до успешного ответа API.
    #
    # Поэтому в контекст он попадает ровно один раз:
    # через build_messages().
    #
    # В history он добавляется только после успеха.

    bot.add_history(
        user,
        "user",
        text or "[Изображение]",
    )

    bot.add_history(
        user,
        "assistant",
        answer,
    )

    chat["last_prompt"] = (
        text
        or "[Изображение]"
    )

    chat["last_request"] = {
        "kind": (
            "image"
            if image
            else "text"
        ),
        "ts": int(
            time.time()
        ),
    }

    chat["requests"] = int(
        chat.get(
            "requests",
            0,
        )
        or 0
    ) + 1

    user["requests"] = int(
        user.get(
            "requests",
            0,
        )
        or 0
    ) + 1

    bot.db["total_requests"] = int(
        bot.db.get(
            "total_requests",
            0,
        )
        or 0
    ) + 1

    update_chat_title_after_success(
        chat,
        text,
    )

    bot.save_db()

    try:
        bot.consume_license_after_success(
            user
        )
    except Exception:
        pass

    return {
        "answer": answer,
        "model": requested_model,
        "state": frontend_state(
            user
        ),
    }


class MiniAppHandler(
    BaseHTTPRequestHandler
):

    server_version = (
        "BulbaMiniApp/2.0"
    )

    def log_message(
        self,
        fmt,
        *args,
    ):
        print(
            "MiniApp:",
            fmt % args,
        )

    def _error(
        self,
        status,
        message,
    ):
        send_json(
            self,
            status,
            {
                "ok": False,
                "error": str(message),
            },
        )

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header(
            "Cache-Control",
            "no-store",
        )
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(
            self.path
        ).path

        try:

            if path in (
                "/",
                "/miniapp",
                "/miniapp/",
            ):

                if not MINIAPP_FILE.exists():
                    raise FileNotFoundError(
                        "miniapp/index.html не найден."
                    )

                body = (
                    MINIAPP_FILE.read_bytes()
                )

                self.send_response(
                    200
                )

                self.send_header(
                    "Content-Type",
                    "text/html; charset=utf-8",
                )

                self.send_header(
                    "Cache-Control",
                    "no-store",
                )

                self.send_header(
                    "Content-Length",
                    str(len(body)),
                )

                self.end_headers()

                self.wfile.write(
                    body
                )

                return

            if path == "/health":
                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "service": "BulbaMaxAI",
                        "version": getattr(
                            bot,
                            "BOT_VERSION",
                            "V16",
                        ),
                    },
                )
                return

            if path == "/api/state":

                user = get_user_from_request(
                    self
                )

                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "state":
                            frontend_state(
                                user
                            ),
                    },
                )

                return

            if path == "/api/models":

                user = get_user_from_request(
                    self
                )

                if not require_access(
                    user
                ):
                    raise PermissionError(
                        "Для использования Bulba AI "
                        "нужна активная лицензия."
                    )

                models = model_list()

                selected = validate_model(
                    user.get(
                        "model",
                        "auto",
                    )
                )

                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "models": models,
                        "selected": selected,
                    },
                )

                return

            self._error(
                404,
                "Страница не найдена.",
            )

        except PermissionError as exc:
            self._error(
                403,
                exc,
            )

        except (
            ValueError,
            RuntimeError,
        ) as exc:
            self._error(
                400,
                exc,
            )

        except Exception as exc:
            print(
                "GET error:",
                repr(exc),
            )

            self._error(
                500,
                "Внутренняя ошибка сервера.",
            )

    def do_POST(self):
        path = urllib.parse.urlparse(
            self.path
        ).path

        try:

            user = get_user_from_request(
                self
            )

            data = read_json(
                self
            )

            if path == "/api/chat":

                result = handle_chat(
                    user,
                    data,
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

            if path == "/api/chat/new":

                if not require_access(
                    user
                ):
                    raise PermissionError(
                        "Для использования Bulba AI "
                        "нужна активная лицензия."
                    )

                chat_id = create_chat(
                    user
                )

                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "chat_id": chat_id,
                        "state":
                            frontend_state(
                                user
                            ),
                    },
                )

                return

            if path == "/api/chat/select":

                chat_id, _ = get_chat(
                    user,
                    data.get(
                        "chat_id"
                    ),
                )

                user["active_chat"] = (
                    chat_id
                )

                bot.save_db()

                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "state":
                            frontend_state(
                                user
                            ),
                    },
                )

                return

            if path == "/api/chat/rename":

                rename_chat(
                    user,
                    data.get(
                        "chat_id"
                    ),
                    data.get(
                        "title"
                    ),
                )

                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "state":
                            frontend_state(
                                user
                            ),
                    },
                )

                return

            if path == "/api/chat/delete":

                delete_chat(
                    user,
                    data.get(
                        "chat_id"
                    ),
                )

                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "state":
                            frontend_state(
                                user
                            ),
                    },
                )

                return

            if path == "/api/chat/clear":

                clear_chat(
                    user,
                    data.get(
                        "chat_id"
                    ),
                )

                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "state":
                            frontend_state(
                                user
                            ),
                    },
                )

                return

            if path == "/api/settings":

                if "model" in data:
                    user["model"] = (
                        validate_model(
                            data.get(
                                "model"
                            )
                        )
                    )

                if "style" in data:

                    style = str(
                        data.get(
                            "style"
                        )
                        or "normal"
                    ).strip()

                    if style not in STYLE_VALUES:
                        raise ValueError(
                            "Неизвестный стиль ответа."
                        )

                    user["style"] = style

                bot.save_db()

                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "state":
                            frontend_state(
                                user
                            ),
                    },
                )

                return

            if path == "/api/memory":

                action = str(
                    data.get(
                        "action"
                    )
                    or ""
                ).strip().lower()

                if action == "clear":
                    user["memory"] = {}

                elif action == "set":

                    key = " ".join(
                        str(
                            data.get(
                                "key"
                            )
                            or ""
                        ).split()
                    ).strip()

                    value = " ".join(
                        str(
                            data.get(
                                "value"
                            )
                            or ""
                        ).split()
                    ).strip()

                    if not key or not value:
                        raise ValueError(
                            "Укажи название "
                            "и значение памяти."
                        )

                    user.setdefault(
                        "memory",
                        {},
                    )[key[:80]] = value[:500]

                else:
                    raise ValueError(
                        "Неизвестное действие памяти."
                    )

                bot.save_db()

                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "state":
                            frontend_state(
                                user
                            ),
                    },
                )

                return

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
                                or 0
                            ),
                            "errors": int(
                                user.get(
                                    "errors",
                                    0,
                                )
                                or 0
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
                                or 0
                            ),
                            "version": getattr(
                                bot,
                                "BOT_VERSION",
                                "V16",
                            ),
                        },
                    },
                )

                return

            self._error(
                404,
                "API-метод не найден.",
            )

        except PermissionError as exc:
            self._error(
                403,
                exc,
            )

        except (
            ValueError,
            RuntimeError,
        ) as exc:
            self._error(
                400,
                exc,
            )

        except Exception as exc:
            print(
                "POST error:",
                repr(exc),
            )

            self._error(
                500,
                "Внутренняя ошибка сервера.",
            )


def run_server():
    server = ThreadingHTTPServer(
        (
            HOST,
            PORT,
        ),
        MiniAppHandler,
    )

    print(
        f"Mini App server: "
        f"http://{HOST}:{PORT}"
    )

    try:
        server.serve_forever()
    finally:
        server.server_close()