"""Vision со структурным ответом: картинка -> модель -> разобранный объект.

Отдельно от `ai_vision.py`, потому что отвечает на другой вопрос. Та даёт
прозу — человеку в журнал, в отчёт, на глаз. Здесь ответ кладут в колонки и
поля: описание и теги снимка, замеры, перечень найденного.

Превращение прозы в данные всегда одинаковое, и собирать его заново в каждом
проекте незачем: попросить JSON, снять обрамление ```json и болтовню вокруг,
разобрать, а на невнятный ответ переспросить ровно один раз. Разбор берёт
`json_from_string` — он изначально писан под ответы LLM и на их чистоту не
рассчитывает.

⚠ **Поля называют в просьбе.** Модель не угадывает схему: не назовёшь поля —
придумает свои, и каждый раз другие. Просьба «опиши фото» даст то `caption`,
то `text`, то `summary`, и колонку заполнять будет нечем.

⚠ **Пустой словарь отсюда не возвращается никогда.** «Модель не смогла» и «на
снимке ничего нет» — разные исходы, и первый обязан быть отказом: тихий `{}`
ложится в базу как достоверное «ничего» и больше не пересматривается.

Тема целиком — `docs/vision_llm.md`.
"""

import asyncio

from ai.vision.ai_vision import ai_vision_describe

# Требование формата дописывается к просьбе вызывающего. Отдельной константой,
# потому что это часть контракта, а не часть вопроса: вопрос меняет вызывающий,
# формат — нет.
AI_VISION_JSON_DEMAND = (
    'Ответь только объектом JSON, без пояснений до и после и без обрамления '
    'markdown. Строки — в двойных кавычках.'
)


async def ai_vision_json(image: bytes, prompt: str, model_name: str = '',
                         max_tokens: int = 2500, timeout: float = 180.0,
                         out: dict | None = None) -> dict:
    """Описание, теги и прочие поля по картинке — готовым JSON, а не прозой.

    Для случаев, когда ответ модели кладут в колонки базы: что изображено,
    какие теги, что найдено. Проза `ai_vision_describe` для этого непригодна —
    её пришлось бы разбирать на стороне каждого вызывающего.

    Args:
        image: байты картинки любого формата — нормализуются внутри
            (`ai_vision_normalize`, см. его про RGBA-PNG).
        prompt: о чём спрашиваем. **Поля назовите прямо здесь** — см. шапку.
        model_name: модель с префиксом сервиса; пусто — как у `ai_vision_describe`.
        max_tokens: потолок ответа.
        timeout: секунды на **один** запрос; переспрос стоит ещё столько же.
        out: `url` и `status` последнего запроса, как у `ai_vision_describe`.

    Returns:
        Разобранный объект ответа модели.

    Raises:
        RuntimeError: модель и со второй попытки ответила не объектом; сервис
            отказал или вернул пустоту (из `ai_vision_describe`).
        ValueError: сервис в имени модели неизвестен.

    ⚠ **Годится только объект верхнего уровня** (ограничение `json_from_string`).
    Нужен список — просите его **полем**: `{"tags": [...]}`, а не голым массивом.

    ⚠ **Переспрос стоит второго вызова модели.** На локальной машине это
    секунды и не страшно; на платном сервисе — двойная цена у каждого
    невнятного ответа, и если такие пошли чередой, дело в просьбе, а не в
    модели: назовите поля точнее.
    """
    from json_.json_ import json_from_string

    ask = f'{prompt}\n\n{AI_VISION_JSON_DEMAND}'
    answer = await ai_vision_describe(image, ask, model_name, max_tokens, timeout, out)
    parsed = json_from_string(answer)

    if not parsed:
        # Переспрашиваем, назвав беду: «верни только JSON» в пустоту работает
        # хуже, чем та же просьба вместе с указанием, что предыдущий ответ не
        # разобрался.
        again = (f'{ask}\n\nПредыдущий ответ не разобрался как JSON. '
                 f'Верни только объект JSON, без пояснений до и после.')
        answer = await ai_vision_describe(image, again, model_name, max_tokens, timeout, out)
        parsed = json_from_string(answer)

    if not parsed:
        raise RuntimeError(f'Vision: модель дважды ответила не объектом JSON: {answer[:200]}')
    return parsed


def ai_vision_json_wait(image: bytes, prompt: str, model_name: str = '',
                        max_tokens: int = 2500, timeout: float = 180.0) -> dict:
    """Синхронный вход в `ai_vision_json` — для скриптов, CLI и тестов.

    В событийном цикле движка пользоваться нельзя: свой цикл не заводится,
    когда чужой уже крутится — там прямой `await ai_vision_json(...)`.
    """
    return asyncio.run(ai_vision_json(image, prompt, model_name, max_tokens, timeout))


if __name__ == '__main__':
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description='Спросить vision-модель о картинке и получить JSON.',
        epilog="пример: ai.vision.ai_vision_json снимок.png 'поля: description, tags'")
    parser.add_argument('path', help='файл изображения; `-` — байты со stdin')
    parser.add_argument('prompt', nargs='+', help='просьба; назовите поля в ней')
    parser.add_argument('--model', default='', help='модель с префиксом сервиса')
    parser.add_argument('--tokens', type=int, default=2500, help='потолок ответа')
    parser.add_argument('--timeout', type=float, default=180.0, help='секунды на запрос')
    ns = parser.parse_args()

    raw = sys.stdin.buffer.read() if ns.path == '-' else open(ns.path, 'rb').read()
    try:
        got = ai_vision_json_wait(raw, ' '.join(ns.prompt), model_name=ns.model,
                                  max_tokens=ns.tokens, timeout=ns.timeout)
        print(json.dumps(got, ensure_ascii=False, indent=2))
    except (RuntimeError, ValueError, OSError) as err:
        raise SystemExit(f'ошибка: {err}')
