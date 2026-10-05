"""Каталог выгрузок: где лежат файлы, какое имя допустимо, что о них известно.

Общее у бэкапов и seed'ов. Разными их делает **назначение** (снимок всей базы
против выбранных таблиц), а работа с файлами у них одна: сложить в каталог под
`data/`, перечислить свежими сверху, проверить имя из запроса, отдать размер и
время.

⚠⚠ Вынесено отдельно не ради экономии: проверка имени — **защита**, и две её
копии разойдутся в тот день, когда одну поправят. Имя приходит из запроса и
уходит в путь файловой системы; пропущенный `../` там это чтение чужого файла.
"""

import os
import re
from datetime import datetime, timezone

from config.config import config_get
from fastapi import HTTPException
from project_.project_ import project_root

# Что разрешено в имени файла. Разрешаем ровно то, что сами и порождаем: буквы,
# цифры, дефис, подчёркивание, точка — и ни одного разделителя каталогов.
PANEL_FILE_NAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')


def panel_file_dir(sub: str) -> str:
    """Каталог выгрузок внутри `data/`. Создавать не пытается.

    ⚠ Каталог общий по родителю (`data/`) потому, что он и так примонтирован в
    контейнер: класть дампы внутрь слоя образа значит терять их при первой же
    пересборке.
    """
    return os.path.join(str(project_root()), config_get('DATA_DIR', 'data'), sub)


def panel_file_path(directory: str, name: str, ext: str) -> str:
    """Путь к файлу по имени из запроса.

    Raises:
        HTTPException: 400 — имя не наше; 404 — такого файла нет.

    ⚠ Проверяются **оба** условия: и набор знаков, и расширение. Имя без проверки
    уходит в путь, и `../../etc/passwd` отдалось бы скачиванием.
    """
    if not PANEL_FILE_NAME_RE.match(str(name or '')) or not str(name).endswith(ext):
        raise HTTPException(status_code=400, detail='недопустимое имя файла')

    path = os.path.join(directory, name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail='файла нет')

    return path


def panel_file_list(directory: str, ext: str) -> list:
    """Файлы каталога, свежие сверху.

    ⚠ Порядок — по **имени** в обратную сторону, а не по времени файла: имя
    начинается с метки времени, а `mtime` сбивается копированием каталога.
    """
    if not os.path.exists(directory):
        return []

    return [panel_file_info(directory, name)
            for name in sorted(os.listdir(directory), reverse=True)
            if name.endswith(ext)]


def panel_file_info(directory: str, name: str) -> dict:
    """Что известно о файле: имя, размер, время правки в UTC."""
    stat = os.stat(os.path.join(directory, name))

    return {
        'name': name,
        'size': stat.st_size,
        'modified': datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
    }


def panel_file_stamp() -> str:
    """Метка времени для имени нового файла: `2026-10-05_12-30-00`, UTC."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')
