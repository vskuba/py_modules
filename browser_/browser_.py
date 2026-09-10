#!/usr/bin/env python3
"""Браузер руками: открыть страницу и получить с неё то, что нужно, одной командой.

Контейнер (`browser_router`) существует ради **долгой** работы: сессии живут
неделями, за страницами следят наблюдатели, вход переиспользуется. А почти всякая
отладка — это разовый вопрос: что на этой странице написано, какие у формы
селекторы, как она выглядит, работает ли сценарий. Поднимать ради такого сервис,
заводить сессию по HTTP и не забыть погасить её — дороже самого вопроса.

```bash
PYTHONPATH=. python3 -m browser_.browser_ read https://example.com
PYTHONPATH=. python3 -m browser_.browser_ snapshot https://example.com --selector form
PYTHONPATH=. python3 -m browser_.browser_ shot https://example.com page.jpg --full
PYTHONPATH=. python3 -m browser_.browser_ run scenario.json --var login=demo
```

Сессия здесь **временная и своя**: поднимается свой Chromium, гасится в `finally`.
Ничего общего с контейнерным пулом у неё нет — если контейнер запущен, эта команда
его не трогает и его сессий не видит.

⚠ **Что брать, если нужен просто кадр или просто числа со страницы:** `web_shot` и
`web_probe` (`web_shot.md`). Они дешевле — headless Chrome флагами, без Playwright, —
и умеют то, чего здесь нет: раздать локальный каталог, посеять `localStorage` до
скриптов страницы. Здесь остаётся то, что даёт именно Playwright: дерево доступности
с готовыми селекторами (`snapshot`), вырезка по селектору (`shot --selector`) и прогон
сценария тем же движком, что в сервисе (`run`).
"""

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

# Запуск по прямому пути кладёт в путь поиска каталог файла, а не корень
# репозитория — и `browser_.browser_pool` не находится. Правим первым делом,
# до собственных импортов.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from browser_.browser_capture import browser_capture_pdf, browser_capture_shot  # noqa: E402
from browser_.browser_pool import (browser_pool_session_close, browser_pool_session_get,  # noqa: E402
                                   browser_pool_session_open, browser_pool_start,
                                   browser_pool_stop)
from browser_.browser_read import browser_read_page  # noqa: E402
from browser_.browser_scenario import BrowserScenario  # noqa: E402
from browser_.browser_scenario_run import browser_scenario_run  # noqa: E402
from browser_.browser_snapshot import browser_snapshot_take  # noqa: E402

# Сколько ждём загрузки страницы. Больше, чем у контейнера: команду зовут руками,
# и «не открылось за полминуты» здесь честнее, чем отказ на медленной сети.
BROWSER_GOTO_TIMEOUT_MS = 60000


def browser_main(argv: list[str] | None = None) -> int:
    """Точка входа CLI. Возвращает код возврата процесса."""
    parser = argparse.ArgumentParser(
        prog='browser_', description='Разовые действия браузером: текст, снимок, дерево, сценарий')
    commands = parser.add_subparsers(dest='command', required=True)

    read = commands.add_parser('read', help='читаемый текст страницы')
    read.add_argument('url')
    read.add_argument('--selector', default='body', help='корень чтения')
    read.add_argument('--links', action='store_true', help='показать и ссылки')

    snapshot = commands.add_parser('snapshot', help='дерево доступности с селекторами')
    snapshot.add_argument('url')
    snapshot.add_argument('--selector', default='body')
    snapshot.add_argument('--all', action='store_true',
                          help='селекторы не только для кликабельного')

    shot = commands.add_parser('shot', help='снимок страницы в файл')
    shot.add_argument('url')
    shot.add_argument('file')
    shot.add_argument('--selector', default='', help='вырезка по элементу')
    shot.add_argument('--full', action='store_true', help='страница целиком, с прокруткой')

    pdf = commands.add_parser('pdf', help='страница как PDF в файл')
    pdf.add_argument('url')
    pdf.add_argument('file')

    run = commands.add_parser('run', help='прогнать сценарий из JSON-файла')
    run.add_argument('file')
    run.add_argument('--var', action='append', default=[], metavar='ИМЯ=ЗНАЧЕНИЕ',
                     help='значение подстановки; можно несколько раз')

    args = parser.parse_args(argv)

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


async def _run(args) -> int:
    """Поднимает браузер, выполняет команду, гасит браузер."""
    await browser_pool_start()
    try:
        if args.command == 'run':
            return await _command_run(args)

        session = await browser_pool_session_open(name=f'cli: {args.command}')
        session_id = session['session_id']
        try:
            entry = browser_pool_session_get(session_id)
            await entry['page'].goto(args.url, wait_until='domcontentloaded',
                                     timeout=BROWSER_GOTO_TIMEOUT_MS)

            if args.command == 'read':
                return await _command_read(entry, args)
            if args.command == 'snapshot':
                return await _command_snapshot(session_id, args)
            if args.command == 'shot':
                return await _command_shot(entry, args)
            if args.command == 'pdf':
                return await _command_pdf(entry, args)
        finally:
            await browser_pool_session_close(session_id)
    finally:
        await browser_pool_stop()

    return 1


async def _command_read(entry: dict, args) -> int:
    """Текст страницы в stdout — так его можно сразу отдать по конвейеру дальше."""
    result = await browser_read_page(entry['page'], selector=args.selector, links=args.links)
    if result['error']:
        print(f'не прочиталось: {result["error"]}', file=sys.stderr)
        return 1

    print(f'# {result["title"]}\n# {result["url"]}\n')
    print(result['text'])

    if args.links and result['links']:
        print('\n## ссылки\n')
        for link in result['links']:
            print(f'- {link["text"] or "(без текста)"} → {link["href"]}')

    return 0


async def _command_snapshot(session_id: str, args) -> int:
    """Дерево доступности и список элементов с селекторами — то, по чему пишут шаги."""
    result = await browser_snapshot_take(session_id=session_id, selector=args.selector,
                                         interactive_only=not args.all, keep_session=True)

    print(result['snapshot'])
    print(f'\n## элементы ({len(result["elements"])})\n')
    for item in result['elements']:
        print(f'- {item["role"]:<12} {item["name"][:40]:<42} {item["selector"]}')

    return 0


async def _command_shot(entry: dict, args) -> int:
    """Кадр в файл. `data:`-строку разбираем здесь: на диск нужны байты."""
    result = await browser_capture_shot(entry['page'], selector=args.selector,
                                        full_page=args.full)
    if result['error'] or not result['image']:
        print(f'снимок не снялся: {result["error"]}', file=sys.stderr)
        return 1

    _data_write(result['image'], args.file)
    print(f'{args.file}: {result["bytes"]} байт')

    return 0


async def _command_pdf(entry: dict, args) -> int:
    """PDF в файл."""
    result = await browser_capture_pdf(entry['page'])
    if result['error'] or not result['pdf']:
        print(f'PDF не собрался: {result["error"]}', file=sys.stderr)
        return 1

    _data_write(result['pdf'], args.file)
    print(f'{args.file}: {result["bytes"]} байт')

    return 0


async def _command_run(args) -> int:
    """
    Прогон сценария из файла: тот же движок, что в контейнере, без сессии и HTTP.

    Все значения подстановок идут как `variables`, а не `secrets`: трасса печатается
    в терминал, и прятать в нём то, что человек сам же и набрал в командной строке,
    смысла нет — а вот молча не подставить его значение было бы неожиданностью.
    """
    scenario = BrowserScenario(**json.loads(Path(args.file).read_text(encoding='utf-8')))
    variables = dict(item.split('=', 1) for item in args.var if '=' in item)

    result = await browser_scenario_run(scenario, variables=variables)

    print(f'{result["status"]}: шагов {len(result["steps"])} из {len(scenario.steps)}, '
          f'{result["duration_ms"]} мс, {result["url"]}')
    for step in result['steps']:
        mark = 'ok ' if step['ok'] else 'СБОЙ'
        print(f'  {step["index"]:>2} {mark} {step["action"]:<13} {step["target"][:50]:<52}'
              f' {step["ms"]:>6} мс  {step["error"] or step["value"][:60]}')

    if result['extract']:
        print('\n## собрано\n')
        print(json.dumps(result['extract'], ensure_ascii=False, indent=2)[:4000])

    return 0 if result['status'] == 'ok' else 1


def _data_write(data_uri: str, path: str) -> None:
    """Пишет содержимое `data:`-строки в файл."""
    payload = data_uri.split(',', 1)[1] if ',' in data_uri else ''
    Path(path).write_bytes(base64.b64decode(payload))


if __name__ == '__main__':
    sys.exit(browser_main())
