"""Обход дерева Google Диска: папка по имени и всё, что лежит под ней.

Отдельно от двери (`google_drive_.py`), потому что отвечает на другой вопрос.
Дверь знает «спросить Диск», обход — «пройти вглубь и собрать листья с путями».

**Зачем вообще обход, а не список папки.** Папки на Диске раскладывают люди, и
раскладывают не плоско: за папкой, которую назвали «папкой с фото», обычно
стоит ещё уровень-другой (`photo/`, а под ним `public/` и `intim/`). Код,
спрашивающий только прямых детей, увидит там ноль картинок и честно скажет «фото
нет» — отказ правдивый по форме и бесполезный по сути. Поэтому обход идёт
вглубь, а имена промежуточных уровней не зашиваются: их называет тот, кто
раскладывал, и завтра он заведёт третий.

⚠ **Путь листа — это и есть группировка.** Показывающему обычно нужно не
«сорок файлов», а «сорок файлов, разложенных так, как их разложил человек».
Поэтому каждый лист несёт `path` от точки входа — по нему группируют, не
выдумывая своих категорий.

Тема целиком — `docs/google_drive.md`.
"""

import asyncio

from google_drive_.google_drive_ import (GOOGLE_DRIVE_FOLDER_MIME, google_drive_list)

# Потолок глубины. Не ожидаемая раскладка, а предохранитель: ярлык папки,
# указывающий на предка, превращает обход в бесконечный — а такое на общих
# дисках встречается.
GOOGLE_DRIVE_WALK_DEPTH = 6


async def google_drive_find(name: str, parent: str = '', key_path: str = '',
                            folders_only: bool = False) -> list[dict]:
    """Найти на Диске запись по точному имени — например общую папку проекта.

    Args:
        name: имя как оно выглядит у человека в Диске.
        parent: искать только внутри этой папки (её `id`); пусто — везде, где
            аккаунту видно.
        key_path: путь к ключу; пусто — из настроек.
        folders_only: только папки. Обычно да: ищут точку входа, а не файл.

    Returns:
        Список совпадений, старейшее первым. Их **может быть несколько**: Диск
        разрешает тёзок в разных местах, и решать, какая нужна, — вызывающему.
        Молчаливый выбор первой попавшейся здесь был бы хуже: человек увидел бы
        чужие файлы и не понял, почему.

    ⚠ **Имя ищется точным, с кавычками внутри — не ищется.** Апостроф в имени
    (`Anna's`) ломает условие Диска; такие имена находят обходом родителя и
    сравнением в Python. Случай редкий, но отказ у него невнятный.
    """
    if "'" in name:
        raise RuntimeError(f'в имени есть апостроф, точный поиск невозможен: {name!r}')

    where = [f"name = '{name}'", 'trashed = false']
    if folders_only:
        where.append(f"mimeType = '{GOOGLE_DRIVE_FOLDER_MIME}'")
    if parent:
        where.append(f"'{parent}' in parents")
    return await google_drive_list(' and '.join(where), key_path=key_path,
                                   order_by='createdTime')


async def google_drive_walk(folder_id: str, key_path: str = '',
                            depth_max: int = GOOGLE_DRIVE_WALK_DEPTH) -> list[dict]:
    """Все файлы под папкой, на любой глубине, каждый со своим путём.

    Args:
        folder_id: `id` папки, с которой начинаем.
        key_path: путь к ключу; пусто — из настроек.
        depth_max: предохранитель от зацикленных ярлыков.

    Returns:
        Список записей Диска, у каждой добавлено поле `path` — путь от точки
        входа без её собственного имени (`photo/public/diana.png`). Папки в
        список не попадают: они не файлы, а дорога к ним.

    ⚠ **Пустая папка и папка без доступа выглядят одинаково** — пустым списком.
    Отличать их здесь нечем, и это не недосмотр: Диск отвечает так же. Кто
    показывает результат человеку, спрашивает дверь (см. шапку
    `google_drive_.py`).
    """
    out: list[dict] = []
    await _walk_into(folder_id, '', out, key_path, max(depth_max, 1))
    return out


async def google_drive_walk_groups(folder_id: str, key_path: str = '',
                                   depth_max: int = GOOGLE_DRIVE_WALK_DEPTH) -> dict[str, list]:
    """То же дерево, но сразу разложенное по папкам — ключ словаря это путь.

    Нужно там, где показывают группами: человек разложил файлы по папкам,
    и порядок показа обязан повторять его раскладку, а не плоский список.

    Returns:
        `{'photo/public': [запись, …], 'photo/intim': [...]}`. Файлы, лежащие
        прямо в точке входа, идут под пустым ключом `''`.
    """
    groups: dict[str, list] = {}
    for item in await google_drive_walk(folder_id, key_path, depth_max):
        groups.setdefault(item['path'].rpartition('/')[0], []).append(item)
    return groups


def google_drive_walk_wait(folder_id: str, key_path: str = '') -> list[dict]:
    """Синхронный вход в `google_drive_walk` — для скриптов, CLI и тестов."""
    return asyncio.run(google_drive_walk(folder_id, key_path))


async def _walk_into(folder_id: str, prefix: str, out: list,
                     key_path: str, depth_left: int) -> None:
    """Один уровень обхода: файлы в список, папки — рекурсией, пока есть глубина."""
    children = await google_drive_list(f"'{folder_id}' in parents and trashed = false",
                                       key_path=key_path)
    for child in children:
        path = f'{prefix}/{child["name"]}' if prefix else child['name']
        if child['mimeType'] != GOOGLE_DRIVE_FOLDER_MIME:
            out.append({**child, 'path': path})
        elif depth_left > 1:
            await _walk_into(child['id'], path, out, key_path, depth_left - 1)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Обход дерева Google Диска.')
    parser.add_argument('target', help='имя общей папки либо её id')
    parser.add_argument('--key', default='', help='путь к JSON-ключу; пусто — GOOGLE_DRIVE_KEY')
    parser.add_argument('--groups', action='store_true', help='разложить по папкам')
    args = parser.parse_args()

    async def _main() -> None:
        found = await google_drive_find(args.target, key_path=args.key, folders_only=True)
        folder_id = found[0]['id'] if found else args.target
        if found:
            print(f'папка «{args.target}» → {folder_id}')
        if len(found) > 1:
            print(f'⚠ тёзок найдено {len(found)}, взята старейшая')

        if args.groups:
            for where, items in (await google_drive_walk_groups(folder_id, args.key)).items():
                print(f'\n📁 {where or "."}  ({len(items)})')
                for item in items:
                    print(f'   {item["name"]}  {item["mimeType"]}  {item.get("size", "?")} б')
            return

        rows = await google_drive_walk(folder_id, args.key)
        if not rows:
            print('файлов не найдено (пустая папка либо доступа нет)')
        for row in rows:
            print(f'{row["path"]}  {row["mimeType"]}  {row.get("size", "?")} б  '
                  f'md5 {row.get("md5Checksum", "—")}')

    try:
        asyncio.run(_main())
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')
