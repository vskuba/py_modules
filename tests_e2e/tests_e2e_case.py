"""Случай E2E: чтение YAML и его разбор в то, что умеет прогон.

Случай описывает **одну страницу**: как её зовут человеку, что она стережёт, на
каких ширинах её смотреть, какие области мерить и что проверить в содержимом.
Файлы лежат в проекте (`tests_e2e/*.yaml`), а не в слое: селектор живёт в одном
коммите с шаблоном, который стережёт, — переименовал класс, поправил рядом.

```yaml
name: Трассы — нет ошибок за сегодня
description: Вкладка «Error». Стережёт, что за текущую дату нет ни одной записи.
page: /admin/workflow_trace
widths: [1440]
steps:
  - click: '.status-tab[data-status="error"]'
  - wait: 2000
areas:
  - name: Лента запусков
    selector: '#runsList'
    rules: [visible, inside]
checks:
  - name: ошибок за сегодня
    expect: 0
    js: |
      const today = window.fmtTz(new Date().toISOString()).slice(0, 10);
      return [...document.querySelectorAll('.run-card .run-meta')]
        .filter(m => m.textContent.trim().startsWith(today)).length;
```

⚠ `areas` и `checks` отвечают на разные вопросы и оба нужны. Область — про
**вёрстку**: где элемент стоит и цел ли он; проверка — про **содержимое**: что
страница показывает. Зелёная геометрия у страницы, показывающей вчерашние данные,
— это зелёный тест на сломанной странице.

⚠⚠ `js` — тело функции, возвращающее ЧИСЛО, и это ограничение намеренное.
Вердикт обязан читаться как «ожидали 0, получили 3», а не как «истина/ложь»:
по булеву ответу нельзя сказать, насколько промах, и отчёт теряет смысл.
"""

import os

import yaml

# Ширины по умолчанию, если случай молчит: узкая мобильная, планшет, широкая.
# Те же три, что в `frontend_check_rules.md`, § 7 — расходиться им незачем.
TESTS_E2E_CASE_WIDTHS = (360, 768, 1440)

# Высота вьюпорта под ширину. Кадр снимается вьюпортом, а не всей страницей:
# ниже сгиба страница отвечает прокруткой, и мерить там нечего.
TESTS_E2E_CASE_HEIGHT = {360: 800, 768: 1024, 1440: 900}
TESTS_E2E_CASE_HEIGHT_DEFAULT = 900

# Сколько ждать шага `wait` по умолчанию и сколько — всего на зонд.
TESTS_E2E_CASE_WAIT = 1500


def tests_e2e_case_load(path: str) -> dict:
    """Прочитать один случай из YAML-файла.

    Args:
        path: путь к `.yaml`.

    Returns:
        dict: случай с проставленными умолчаниями — `name`, `description`,
        `page`, `widths`, `steps`, `areas`, `checks`, `file`.

    Raises:
        ValueError: в случае нет имени или страницы — без них нечего ни
            запускать, ни показывать на странице результатов.
    """
    with open(path, encoding='utf-8') as fh:
        raw = yaml.safe_load(fh) or {}

    return tests_e2e_case_parse(raw, path)


def tests_e2e_case_list(folder: str) -> list:
    """Все случаи каталога, по имени файла.

    Args:
        folder: каталог с `*.yaml`.

    Returns:
        list: случаи в порядке имён файлов; каталога нет — пустой список.

    ⚠ Порядок по имени, а не по времени правки: прогон должен давать один и тот
    же порядок строк, иначе страница результатов «дрожит» между прогонами.
    """
    if not os.path.isdir(folder):
        return []

    out = []
    for name in sorted(os.listdir(folder)):
        if name.endswith(('.yaml', '.yml')) and not name.startswith('_'):
            out.append(tests_e2e_case_load(os.path.join(folder, name)))
    return out


def tests_e2e_case_parse(raw: dict, path: str = '') -> dict:
    """Сырой YAML → случай с умолчаниями; см. `tests_e2e_case_load`."""
    name = str(raw.get('name') or '').strip()
    page = str(raw.get('page') or '').strip()
    if not name or not page:
        raise ValueError(f'случай без имени или страницы: {path or raw}')

    widths = [int(one) for one in (raw.get('widths') or TESTS_E2E_CASE_WIDTHS)]

    areas = []
    for area in (raw.get('areas') or []):
        areas.append({
            'name': str(area.get('name') or area.get('selector') or '—'),
            'selector': str(area.get('selector') or ''),
            'rules': list(area.get('rules') or []),
        })

    checks = []
    for check in (raw.get('checks') or []):
        checks.append({
            'name': str(check.get('name') or '—'),
            'expect': int(check.get('expect') or 0),
            'js': str(check.get('js') or ''),
        })

    return {
        'name': name,
        'description': str(raw.get('description') or '').strip(),
        'page': page,
        'widths': widths,
        'steps': list(raw.get('steps') or []),
        'areas': areas,
        'checks': checks,
        'file': path,
    }


def tests_e2e_case_size(width: int) -> tuple:
    """Вьюпорт под ширину: (ширина, высота)."""
    return int(width), TESTS_E2E_CASE_HEIGHT.get(int(width),
                                                 TESTS_E2E_CASE_HEIGHT_DEFAULT)
