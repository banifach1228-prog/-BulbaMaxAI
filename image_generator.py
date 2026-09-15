import os
import time
import threading
import requests


PLUSVIBE_URL = "https://plusvibeapi.ru"
IMAGE_MODEL = "gpt-image-1-5"

POLL_INTERVAL = 3
POLL_TIMEOUT = 90

# Telegram sendPhoto: 10 MB
PHOTO_MAX_BYTES = 10 * 1024 * 1024

# Telegram sendDocument: 50 MB
DOCUMENT_MAX_BYTES = 50 * 1024 * 1024

_PENDING = set()
_PENDING_LOCK = threading.Lock()


def install(bot):
    """Подключает генерацию изображений только к Telegram-боту."""

    original_keyboard = bot.main_keyboard
    original_process_message = bot.process_message

    def image_keyboard():
        keyboard = original_keyboard()
        rows = list(
            keyboard.get("keyboard", [])
        )

        exists = any(
            any(
                button.get("text")
                == "🖼️ Изображение"
                for button in row
            )
            for row in rows
        )

        if not exists:
            rows.append(
                [
                    {
                        "text": "🖼️ Изображение"
                    }
                ]
            )

        keyboard["keyboard"] = rows

        return keyboard

    bot.main_keyboard = image_keyboard

    def wrapped_process_message(msg):

        if "chat" not in msg:
            return original_process_message(msg)

        chat_id = msg["chat"]["id"]

        user_id = (
            msg.get("from", {})
            .get(
                "id",
                chat_id,
            )
        )

        text = (
            msg.get("text") or ""
        ).strip()

        with bot.get_user_lock(user_id):

            # Кнопка генерации
            if text == "🖼️ Изображение":

                with _PENDING_LOCK:
                    _PENDING.add(
                        str(user_id)
                    )

                bot.send_message(
                    chat_id,
                    "🖼️ Генерация изображения\n\n"
                    "Отправь описание картинки одним сообщением.\n\n"
                    "Например:\n"
                    "Космический город на Марсе ночью, "
                    "кинематографичный свет, высокая детализация\n\n"
                    "Или сразу используй:\n"
                    "/image описание",
                    image_keyboard(),
                )

                return

            # /image текст
            if text.startswith("/image"):

                prompt = (
                    text[
                        len("/image"):
                    ]
                    .strip()
                )

                if not prompt:

                    with _PENDING_LOCK:
                        _PENDING.add(
                            str(user_id)
                        )

                    bot.send_message(
                        chat_id,
                        "🖼️ Напиши описание после команды /image.",
                        image_keyboard(),
                    )

                    return

                with _PENDING_LOCK:
                    _PENDING.discard(
                        str(user_id)
                    )

                generate_image(
                    bot,
                    chat_id,
                    user_id,
                    prompt,
                )

                return

            # Следующее сообщение после кнопки
            with _PENDING_LOCK:
                pending = (
                    str(user_id)
                    in _PENDING
                )

            if (
                pending
                and text
                and not text.startswith("/")
            ):

                with _PENDING_LOCK:
                    _PENDING.discard(
                        str(user_id)
                    )

                generate_image(
                    bot,
                    chat_id,
                    user_id,
                    text,
                )

                return

        return original_process_message(msg)

    bot.process_message = wrapped_process_message

    print(
        "🖼️ Image Generator: Telegram test mode installed"
    )


def generate_image(
    bot,
    chat_id,
    user_id,
    prompt,
):
    api_key = os.getenv(
        "PLUSVIBE_API_KEY",
        "",
    ).strip()

    if not api_key:

        bot.send_message(
            chat_id,
            "❌ PLUSVIBE_API_KEY не настроен.",
            bot.main_keyboard(),
        )

        return

    u = bot.get_user(
        user_id
    )

    # Защита от слишком частых запросов
    if not bot.allowed_request(u):

        bot.send_message(
            chat_id,
            "⏳ Слишком много запросов. "
            "Подожди несколько секунд.",
            bot.main_keyboard(),
        )

        return

    # Проверка лицензии
    if (
        os.getenv(
            "LICENSE_REQUIRED",
            "0",
        ).strip() == "1"
        and not bot.is_admin(user_id)
    ):

        allowed, reason = (
            bot.user_has_access(
                user_id
            )
        )

        if not allowed:

            bot.send_message(
                chat_id,
                reason
                + "\n\n"
                "🔑 Активируй доступ командой "
                "/activate КОД.",
                bot.main_keyboard(),
            )

            return

    prompt = prompt.strip()[:8000]

    if not prompt:

        bot.send_message(
            chat_id,
            "❌ Описание изображения пустое.",
            bot.main_keyboard(),
        )

        return

    status = bot.send_message(
        chat_id,
        "🖼️ Создаю изображение…\n\n"
        "Модель: GPT Image 1.5\n"
        "Качество: Medium\n"
        "Формат: 1:1\n\n"
        "⏳ Обычно это занимает некоторое время.",
    )

    try:

        # -------------------------------------------------
        # 1. Создаём задачу PlusVibe
        # -------------------------------------------------

        response = requests.post(
            f"{PLUSVIBE_URL}/api/media/generate",
            headers={
                "Authorization":
                    f"Bearer {api_key}",
                "Content-Type":
                    "application/json",
            },
            json={
                "model":
                    IMAGE_MODEL,

                "prompt":
                    prompt,

                "opts": {
                    "mode":
                        "text-to-image",

                    "aspect_ratio":
                        "1:1",

                    "quality":
                        "medium",
                },
            },
            timeout=30,
        )

        if not response.ok:
            raise RuntimeError(
                _api_error(response)
            )

        data = response.json()

        job_id = data.get(
            "jobId"
        )

        if not job_id:
            raise RuntimeError(
                "PlusVibe не вернул jobId."
            )

        print(
            f"Image job created: {job_id}"
        )

        # -------------------------------------------------
        # 2. Ждём результат
        # -------------------------------------------------

        result = poll_job(
            api_key,
            job_id,
        )

        print(
            "Image job result:",
            {
                "status":
                    result.get("status"),
                "urls":
                    len(
                        result.get(
                            "resultUrls",
                            [],
                        )
                    ),
            },
        )

        urls = (
            result.get(
                "resultUrls"
            )
            or []
        )

        if not urls:
            raise RuntimeError(
                "PlusVibe завершил генерацию, "
                "но не вернул resultUrls."
            )

        image_url = urls[0]

        # -------------------------------------------------
        # 3. Скачиваем изображение
        # -------------------------------------------------

        image_response = requests.get(
            image_url,
            timeout=60,
        )

        if not image_response.ok:
            raise RuntimeError(
                "Не удалось скачать изображение "
                f"из PlusVibe: HTTP "
                f"{image_response.status_code}"
            )

        image_bytes = (
            image_response.content
        )

        size = len(
            image_bytes
        )

        print(
            f"Image downloaded: "
            f"{size / 1024 / 1024:.2f} MB"
        )

        if size > DOCUMENT_MAX_BYTES:
            raise RuntimeError(
                "PlusVibe вернул файл больше "
                "50 MB — Telegram не сможет его принять."
            )

        if size == 0:
            raise RuntimeError(
                "PlusVibe вернул пустой файл."
            )

        # -------------------------------------------------
        # 4. Отправляем в Telegram
        # -------------------------------------------------

        price = result.get(
            "priceRub"
        )

        caption = (
            "🖼️ Готово — GPT Image 1.5"
        )

        if isinstance(
            price,
            (int, float),
        ) and price > 0:

            caption += (
                f"\nСтоимость: "
                f"{price:.2f} ₽"
            )

        sent = False

        # Сначала обычная картинка
        if size <= PHOTO_MAX_BYTES:

            photo_result = bot.tg(
                "sendPhoto",
                {
                    "chat_id":
                        chat_id,

                    "caption":
                        caption,
                },
                files={
                    "photo": (
                        "bulba_image.png",
                        image_bytes,
                        "image/png",
                    )
                },
                timeout=60,
            )

            if photo_result:
                sent = True

        # Если sendPhoto не прошёл —
        # отправляем как документ
        if not sent:

            document_result = bot.tg(
                "sendDocument",
                {
                    "chat_id":
                        chat_id,

                    "caption":
                        caption,
                },
                files={
                    "document": (
                        "bulba_image.png",
                        image_bytes,
                        "image/png",
                    )
                },
                timeout=60,
            )

            if document_result:
                sent = True

        if not sent:
            raise RuntimeError(
                "Telegram не смог принять "
                "сгенерированное изображение. "
                "Проверь логи Bothost."
            )

        # -------------------------------------------------
        # 5. Сохраняем статистику
        # -------------------------------------------------

        u["requests"] += 1

        bot.db[
            "total_requests"
        ] += 1

        chat = bot.get_chat(
            u
        )

        chat["requests"] += 1

        chat["last_prompt"] = (
            prompt
        )

        chat["last_request"] = {
            "kind":
                "image_generation",

            "text":
                prompt,
        }

        bot.add_history(
            u,
            "user",
            f"🖼️ {prompt}",
        )

        bot.add_history(
            u,
            "assistant",
            "Изображение сгенерировано.",
        )

        bot.save_db()

        bot.consume_license_after_success(
            user_id
        )

        # -------------------------------------------------
        # 6. Меняем сообщение статуса
        # -------------------------------------------------

        if status:

            bot.edit_message(
                chat_id,
                status["message_id"],
                "✅ Изображение готово "
                "и отправлено выше.",
                bot.main_keyboard(),
            )

    except Exception as e:

        print(
            "Image generation error:",
            repr(e),
        )

        error_text = (
            "❌ Ошибка генерации изображения.\n\n"
            f"{e}"
        )

        if status:

            bot.edit_message(
                chat_id,
                status["message_id"],
                error_text,
                bot.main_keyboard(),
            )

        else:

            bot.send_message(
                chat_id,
                error_text,
                bot.main_keyboard(),
            )


def poll_job(
    api_key,
    job_id,
):
    deadline = (
        time.monotonic()
        + POLL_TIMEOUT
    )

    attempt = 0

    while (
        time.monotonic()
        < deadline
    ):

        attempt += 1

        response = requests.get(
            f"{PLUSVIBE_URL}/api/media/jobs/{job_id}",
            headers={
                "Authorization":
                    f"Bearer {api_key}",
            },
            timeout=20,
        )

        if not response.ok:
            raise RuntimeError(
                _api_error(response)
            )

        data = response.json()

        status = data.get(
            "status"
        )

        print(
            f"Image job {job_id}: "
            f"{status} "
            f"(poll #{attempt})"
        )

        if status == "success":
            return data

        if status == "fail":

            raise RuntimeError(
                data.get(
                    "failMsg"
                )
                or
                data.get(
                    "message"
                )
                or
                "Генерация завершилась ошибкой."
            )

        time.sleep(
            POLL_INTERVAL
        )

    raise RuntimeError(
        "Генерация заняла больше "
        "90 секунд. Попробуй ещё раз."
    )


def _api_error(response):

    try:

        data = response.json()

        message = (
            data.get("message")
            or data.get("error")
            or data.get("failMsg")
        )

        code = data.get(
            "errorCode"
        )

        if code and message:
            return (
                f"{code}: {message}"
            )

        if message:
            return str(
                message
            )

    except Exception:
        pass

    text = (
        response.text or ""
    ).strip()

    return (
        f"PlusVibe HTTP "
        f"{response.status_code}"
        + (
            f": {text[:500]}"
            if text
            else ""
        )
    )