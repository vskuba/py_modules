"""Шаблоны панели: свой каталог проекта первым, общий (`dashboard`) следом.

У статики такой поиск уже есть — `panel_static_files`. У шаблонов его не было, и
поэтому каталог `dashboard/template/` стоял пустым: страницу туда положить можно,
а найти её оттуда нечем. Этот модуль закрывает вторую половину.

```python
app.state.jinja2_template = panel_template_env(own_template_dir,
                                               shared='dashboard/template')
```

## ⚠⚠ Порядок каталогов — тот же договор, что у статики

Свой каталог ищется **первым**. Это единственный способ отойти от общего вида, не
ломая остальных: положил у себя `admin/e2e.html` — отдаётся твой. Переставь
порядок, и такая правка молча перестала бы действовать.

Отсюда же следствие, ради которого всё и затевалось: общая страница может
`{% extends "base.html" %}`, и возьмётся **base проекта** — у каждого своё меню,
свои разделы и свой заголовок. Общей странице не нужно знать, во что её вставят.

## ⚠ Общего каталога может не быть

Проект выкачали без submodule — Jinja получит один свой каталог и будет работать.
Панель без общей страницы лучше, чем панель, которая не поднялась.
"""

import os

from starlette.templating import Jinja2Templates


def panel_template_dirs(directory, shared: str = '') -> list:
    """Каталоги шаблонов в порядке поиска: свой, затем общий.

    Args:
        directory: каталог шаблонов проекта — ищется первым.
        shared: каталог общих шаблонов (`dashboard/template`). Пусто или нет на
            диске — в списке его не будет.

    Returns:
        list: пути, существующие на диске; первый — свой.
    """
    dirs = [str(directory)]
    if shared and os.path.isdir(shared):
        dirs.append(str(shared))
    return dirs


def panel_template_env(directory, shared: str = '', **kw) -> Jinja2Templates:
    """`Jinja2Templates` с поиском по двум каталогам.

    Args:
        directory: каталог шаблонов проекта — ищется первым.
        shared: каталог общих шаблонов (submodule `dashboard`).
        kw: прочее для `Jinja2Templates`.

    Returns:
        Jinja2Templates: кладётся в `app.state`, как и раньше.

    ⚠ Отдаёт объект, а не кладёт его в приложение: что и под каким именем
    держать в `app.state` — дело точки входа проекта.
    """
    return Jinja2Templates(directory=panel_template_dirs(directory, shared), **kw)
