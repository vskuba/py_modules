"""
Vision: картинка -> модель -> текст, сырым HTTP мимо pydantic-ai.

Два практических урока, оба выросли на живом llama.cpp-сервере.

**JPEG без альфы — обязателен.** Инструменты снимания экрана (Android
`screencap`, буфер обмена браузера) отдают RGBA-PNG, а локальные mtmd-серверы
(llama.cpp) не читают ни его, ни WebP: отвечают
`400 "Failed to load image or audio file"`. Проверено живьём на одном снимке:
JPEG — ОК, WebP — тот самый 400. Поэтому любой вход сначала приводится к
RGB-JPEG — `ai_vision_normalize`, и только так уходит в модель.

**Модель адресуется именем, как везде в `ai/`.** Адрес и ключ берутся из
стратегии сервиса (`raw_endpoint`), зрение работает с любым провайдером
реестра, а не только с gx10. Имя модели — обычное, с префиксом сервиса:
`gx10/qwen-large`. Размышления гасятся полем стратегии (`raw_body_no_thinking`):
размышляющая модель скармливает бюджет ответа внутреннему монологу и возвращает
пустой `content`.

Запрос сырой: pydantic-ai здесь избыточен — один вопрос к одной картинке,
одно POST, одно поле ответа.
"""
import argparse
import asyncio
import base64
import io
import sys

import httpx
from PIL import Image

from config.config import config_get

# Модель по умолчанию, если её не назвали вызовом и в `.env`: размер нашей
# машины, к которому подключён проектор (mmproj). Переопределение —
# `AI_VISION_MODEL_NAME`, имя как всегда, с префиксом сервиса.
AI_VISION_DEFAULT_MODEL = 'gx10/qwen-large'

# Ограничение по длинной стороне. Локальные vision-модели берут image-токены
# площадью кадра: снимок 1080x2400 раздувает префил в тысячи токенов. 1600 px —
# компромисс: экранный текст читается, ход не ползёт.
AI_VISION_MAX_SIDE = 1600

# Просьба по умолчанию; вызывающий почти всегда хочет именно подробного чтения,
# а не односложного «что-то есть».
AI_VISION_DEFAULT_PROMPT = (
    'Опиши подробно, что изображено: все надписи транскрибируй точно, '
    'кнопки и поля — с их состояниями. Нечитаемого не додумывай.'
)


def ai_vision_normalize(image: bytes, max_side: int = AI_VISION_MAX_SIDE) -> bytes:
    """
    Привести изображение любого формата к RGB-JPEG, пригодному для vision-моделей.

    Снимает альфа-канал (RGBA-PNG снимков и WebP локальные серверы отвергают —
    см. шапку модуля) и уменьшает длинную сторону до `max_side` — площадь кадра
    у локальных моделей платная.

    Args:
        image: байты входного файла (PNG, JPEG, WebP — что угодно, что читает Pillow).
        max_side: предел длинной стороны в пикселях; 0 — не уменьшать.

    Returns:
        Байты JPEG без альфы.
    """
    img = Image.open(io.BytesIO(image))
    img.load()

    if img.mode != 'RGB':
        img = img.convert('RGB')

    if max_side and max(img.size) > max_side:
        ratio = max_side / max(img.size)
        img = img.resize((round(img.width * ratio), round(img.height * ratio)), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, 'JPEG', quality=88)
    return out.getvalue()


async def ai_vision_describe(image: bytes, prompt: str = '', model_name: str = '',
                             max_tokens: int = 2500, timeout: float = 180.0) -> str:
    """
    Спросить vision-модель, что на картинке, — одним ходом, без истории.

    Картинку нормализует к JPEG (см. шапку модуля), адрес и ключ берёт из
    стратегии сервиса по префиксу имени модели.

    Args:
        image: байты изображения (любой читаемый Pillow формат).
        prompt: вопрос о картинке; пусто — подробное описание с транскрипцией.
        model_name: модель с префиксом сервиса — `gx10/qwen-large`; пусто —
            `AI_VISION_MODEL_NAME` из окружения, затем `gx10/qwen-large`.
        max_tokens: потолок ответа. 1200 не выдержало подробного чтения целого
            экрана телефона — модель расписывает каждую папку абзацем; потолок
            щедрый, короткий ответ он не замедляет, а серверный контекст
            (gx10 — 131072) принимает с запасом.
        timeout: секунды на весь запрос; локальная модель на большой картинке
            думает медленно, щедрый таймаут по умолчанию не случайно.

    Returns:
        Текст ответа модели.

    Raises:
        ValueError: сервис в имени модели неизвестен или адрес не найден.
        RuntimeError: сервис ответил ошибкой или пустым ответом.
    """
    name = str(model_name or config_get('AI_VISION_MODEL_NAME') or AI_VISION_DEFAULT_MODEL)
    # Реестр провайдеров — тяжёлый (через них едет `pydantic_ai`), а нужен он
    # только здесь, при обращении к модели; `ai_vision_normalize` и без него
    # живёт в тощих окружениях, куда LLM-стек не ставили.
    from ai.provider.ai_provider_registry import ai_provider_registry_get

    model_clean = name.partition('/')[2]  # адрес спрашиваем у стратегии полным именем, в запрос идёт имя без префикса сервиса
    provider = ai_provider_registry_get(name)
    if not provider:
        raise ValueError(f"Vision: сервис модели '{name}' не найден в реестре провайдеров.")

    base_url, api_key = provider.raw_endpoint(model_clean)
    if not base_url:
        raise ValueError(f"Vision: для модели '{name}' нет адреса — проверь .env "
                         f"(для gx10 — GX10_<РАЗМЕР>_API_URL).")

    body = {
        'model': model_clean,
        'max_tokens': max_tokens,
        'temperature': 0.2,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': prompt or AI_VISION_DEFAULT_PROMPT},
            {'type': 'image_url', 'image_url': {
                'url': 'data:image/jpeg;base64,'
                       + base64.b64encode(ai_vision_normalize(image)).decode()}},
        ]}],
    }
    body.update(provider.raw_body_no_thinking())

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f'{base_url.rstrip("/")}/chat/completions',
                                 json=body,
                                 headers={'Authorization': f'Bearer {api_key}'})

    if resp.status_code != 200:
        raise RuntimeError(
            f"Vision: модель '{name}' ответила {resp.status_code}: {resp.text[:300]}")

    content = ((resp.json().get('choices') or [{}])[0].get('message') or {}).get('content') or ''
    if not content.strip():
        raise RuntimeError(f"Vision: модель '{name}' вернула пустой ответ "
                           f"(не съела ли размышления весь бюджет?).")
    return content


def ai_vision_describe_wait(image: bytes, prompt: str = '', model_name: str = '',
                            max_tokens: int = 2500, timeout: float = 180.0) -> str:
    """
    Синхронный вход в `ai_vision_describe` — для скриптов, CLI и тестов.

    В событийном цикле движка пользоваться нельзя: свой цикл не заводится,
    когда чужой уже крутится — там прямой `await ai_vision_describe(...)`.
    """
    return asyncio.run(ai_vision_describe(image, prompt, model_name, max_tokens, timeout))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Прочитать картинку с диска vision-моделью.',
        epilog='вопрос идёт последним, ключи — до пути: '
               'describe --tokens 400 снимок.png что на экране')
    parser.add_argument('command', choices=['describe', 'normalize'])
    parser.add_argument('path', help='файл изображения; `-` — байты со stdin')
    parser.add_argument('--out', default='', help='куда сохранить JPEG (normalize)')
    parser.add_argument('--model', default='', help='модель с префиксом сервиса, например gx10/qwen-large')
    parser.add_argument('--tokens', type=int, default=2500, help='потолок ответа')
    parser.add_argument('--timeout', type=float, default=180.0, help='секунды на весь запрос')
    parser.add_argument('prompt', nargs='*', help='вопрос о картинке (describe)')
    ns = parser.parse_args()

    raw = sys.stdin.buffer.read() if ns.path == '-' else open(ns.path, 'rb').read()

    try:
        if ns.command == 'normalize':
            jpeg = ai_vision_normalize(raw)
            if ns.out:
                open(ns.out, 'wb').write(jpeg)
                print(f'{ns.out}: {len(jpeg)} байт JPEG')
            else:
                sys.stdout.buffer.write(jpeg)
        else:
            print(ai_vision_describe_wait(raw, ' '.join(ns.prompt),
                                          model_name=ns.model,
                                          max_tokens=ns.tokens, timeout=ns.timeout))
    except (RuntimeError, ValueError, OSError) as err:
        raise SystemExit(f'ошибка: {err}')
