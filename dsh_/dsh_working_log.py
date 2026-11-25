"""Живой журнал работающих субагентов — файлом, и ссылка на него из статусной строки.

Веб хоста DeepSeek Harness на сессию сослаться не даёт: выбранная карточка живёт в
памяти страницы, а не в адресе («no anchor deep links» в его же документации), и вход
туда — по одноразовому токену `dsh web`. Поэтому ссылка ведёт на файл: журнал карточки
(промпты и ответы, как `dsh-board history`) пишется в `<каталог>/<сессия>.md`, а в
строку уходит гиперссылка OSC 8 на него — терминал открывает её по Ctrl+клику.

```bash
dsh-board working --line --links ~/.cache/dsh-session   # строка со ссылками
```

⚠ Файл переписывается не чаще `DSH_WORKING_LOG_REFRESH` секунд: строку рисуют раз в
секунду из каждого окна, а журнал — запрос к хосту на карточку.
⚠ Ссылку обрезать нельзя: обрезанная последовательность делает ссылкой весь хвост
строки. Сокращают видимый текст — до обёртки.
"""
import os
import time

from dsh_.dsh_api import DSH_API_ADDR_DEFAULT
from dsh_.dsh_session import dsh_session_dialog

# Каталог журналов по умолчанию.
DSH_WORKING_LOG_DIR = '~/.cache/dsh-session'

# Не чаще раза в столько секунд на карточку.
DSH_WORKING_LOG_REFRESH = 5

# Сколько последних реплик держать в файле.
DSH_WORKING_LOG_COUNT = 60


def dsh_working_log_write(rows: list, directory: str = DSH_WORKING_LOG_DIR, *,
                          base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Освежить файлы журналов работающих карточек.

    Args:
        rows: работающие карточки — `working` из `dsh_board_motors_busy` (нужен `session`).
        directory: куда писать; создаётся сам.

    Returns:
        `{сессия: абсолютный путь файла}` — и для освежённых, и для ещё свежих.
        Сбой хоста на одной карточке не роняет остальные: её файл остаётся прежним.
    """
    directory = os.path.abspath(os.path.expanduser(directory))
    os.makedirs(directory, exist_ok=True)
    paths = {}
    for row in rows:
        session = str(row.get('session') or '')
        if not session or '/' in session:
            continue
        path = os.path.join(directory, f'{session}.md')
        paths[session] = path
        if _dsh_working_log_fresh(path):
            continue
        try:
            dialog = dsh_session_dialog(session, DSH_WORKING_LOG_COUNT, base=base)
        except Exception:
            continue
        _dsh_working_log_save(path, row, dialog)
    return paths


def dsh_working_log_link(path: str, text: str) -> str:
    """Текст гиперссылкой OSC 8 на локальный файл."""
    return f'\x1b]8;;file://{path}\x1b\\{text}\x1b]8;;\x1b\\'


def _dsh_working_log_fresh(path: str) -> bool:
    try:
        return time.time() - os.path.getmtime(path) < DSH_WORKING_LOG_REFRESH
    except OSError:
        return False


def _dsh_working_log_save(path: str, row: dict, dialog: list) -> None:
    title = row.get('title') or row.get('session')
    model = row.get('model') or {}
    head = [f"# {title}", '',
            f"- сессия: `{row.get('session')}`",
            f"- мотор: {row.get('motor', '—')} · модель: {model.get('provider', '?')}/{model.get('model', '?')}",
            f"- обновлено: {time.strftime('%d.%m %H:%M:%S')}", '']
    body = [f"## {'▶ промпт' if item['role'] == 'user' else '◀ ответ'} · "
            f"{_dsh_working_log_clock(item.get('time'))}\n\n{item['text']}\n" for item in dialog]
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(head + (body or ['сообщений пока нет'])) + '\n')
    os.replace(tmp, path)


def _dsh_working_log_clock(ms) -> str:
    return time.strftime('%d.%m %H:%M', time.localtime(ms / 1000)) if ms else '—'
