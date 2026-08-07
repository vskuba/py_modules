import json
import traceback

from logging_.logging_ import logger_info


def json_from_string(string: str, raise_on_error: bool = False) -> dict:
    # 1. Если уже пришел словарь, сразу возвращаем его
    if isinstance(string, dict):
        return string

    # 2. Если пришла не строка (например, None, int, list)
    if not isinstance(string, str):
        if raise_on_error:
            raise ValueError(f"Ожидалась строка или словарь, получен тип: {type(string).__name__}")
        return {}

    try:
        # Убираем лишние пробелы по краям перед парсингом
        string_clean = string.strip()
        if not string_clean:
            return {}

        parsed = json.loads(string_clean, strict=False)

        # Гарантируем, что результат парсинга — это именно словарь (dict)
        if isinstance(parsed, dict):
            return parsed

        # Если распарсился массив [ ... ] или примитив
        error_msg = f"Ожидался JSON-объект (dict), но получен {type(parsed).__name__}."
        if raise_on_error:
            raise TypeError(error_msg)

        logger_info(f"⚠️ {error_msg} Текст: {string}")
        return {}

    except json.JSONDecodeError as e:
        # LLM часто добавляет текст после JSON — ищем matching закрывающую '}'
        # для первого '{'. Это покрывает: `{"plan": {...}}\n\nПроверь: ...`
        if 'Extra data' in str(e):
            depth = 0
            match_end = -1
            in_string = False
            escape_next = False
            for i, ch in enumerate(string_clean):
                if escape_next:
                    escape_next = False
                    continue
                if ch == '\\':
                    escape_next = True
                    continue
                if ch == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        match_end = i
                        break
            if match_end > 0:
                try:
                    parsed = json.loads(string_clean[:match_end+1], strict=False)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass

        backtrace = traceback.format_exc()
        logger_info(
            f"❌ Не удалось распарсить json: {e}, string: {string}\n"
            f"Полный стек вызовов:\n{backtrace}"
        )
        backtrace = traceback.format_exc()
        logger_info(
            f"❌ Не удалось распарсить json: {e}, string: {string}\n"
            f"Полный стек вызовов:\n{backtrace}"
        )

        # Если включен флаг, пробрасываем ошибку дальше
        if raise_on_error:
            raise

        return {}