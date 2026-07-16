import json
import re

# Значение-перевод: JSON-словарь, где ВСЕ ключи — коды языков ('ru', 'en', 'uk'...).
# Обычные JSON-значения (фильтры {"user_id": ...}) под это условие не попадают.
_LANG_CODE = re.compile(r'^[a-z]{2}(-[a-zA-Z]{2})?$')


def i18n_text_resolve(value, language: str):
    """
    Резолвит мультиязычное значение: если value — строка с JSON-словарём переводов
    {"ru": "...", "en": "..."}, возвращает перевод для language
    (fallback: первый язык словаря). Любое другое значение возвращается как есть.
    """
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not (text.startswith('{') and text.endswith('}')):
        return value

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return value

    if not isinstance(data, dict) or not data:
        return value

    if not all(isinstance(k, str) and _LANG_CODE.match(k) for k in data.keys()):
        return value

    resolved = data.get(language)
    if resolved is None:
        resolved = next(iter(data.values()))
    return resolved


def i18n_languages_parse(languages: str) -> list[str]:
    """'ru, en' -> ['ru', 'en']; пустая строка -> ['ru']."""
    result = [x.strip().lower() for x in str(languages or '').split(',') if x.strip()]
    return result or ['ru']
