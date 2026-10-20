"""Прогон целиком за один взгляд: трасса шагов и то, что реально уехало на сайт.

Вопрос «что случилось на прогоне» до сих пор собирался руками: SELECT трассы,
разбор JSON, потом ещё один — по колонке `extract`, потому что POST-страницы
живут там, и их `phone=%2B38%28097%29+337…` надо раскодировать, чтобы увидеть
телефон, а не процентное месиво. Два запроса и heredoc с urldecode на каждый
«а точно ли форма заполнилась».

Трасса — это то, что исполнитель пишет в журнал (`browser_scenario_run`): по
записи на шаг, с исходом, длительностью и замаскированными секретами. Здесь она
просто разворачивается в человекочитаемый вид, а собранные значения печатаются
так, чтобы POST читался глазами.

Номера прогонов растут; последний прогон сценария — `--scenario N` без номера.
"""
import argparse
import asyncio
import json
import urllib.parse

from browser_.browser_scenario_store import browser_scenario_runs


async def browser_scenario_trace(run_id: int = 0, scenario_id: int = 0) -> dict:
    """Записи прогона: трасса шагов и собранное, разобранные из колонок базы.

    Args:
        run_id: номер прогона; ноль — брать последний прогон `scenario_id`.
        scenario_id: чей последний прогон, когда своего номера нет.

    Returns:
        {'run': строка журнала, 'trace': список записей шагов, 'extract': dict
        собранного}.

    Raises:
        KeyError: прогона нет.
    """
    if run_id:
        # Строка берётся по номеру, а не перебором журнала: прогоны старых
        # карточек не должны исчезать из-за того, что не влезли в лимит.
        from mysql_.mysql_ import mysql_get_db_async
        async with mysql_get_db_async() as db:
            await db.execute(
                'SELECT r.*, s.name AS scenario_name FROM browser_scenario_run r'
                '  JOIN browser_scenario s ON s.id = r.scenario_id WHERE r.id = %s',
                (int(run_id),))
            row = await db.fetchone()
    else:
        rows = await browser_scenario_runs(scenario_id=int(scenario_id), limit=1)
        row = rows[0] if rows else None
    if not row:
        raise KeyError(f'прогона {run_id or f"у сценария {scenario_id}"} нет')
    return {
        'run': row,
        'trace': _json_load(row.get('trace'), []),
        'extract': _json_load(row.get('extract'), {}),
    }


def browser_scenario_trace_text(got: dict) -> list:
    """Разобранный прогон — строками: заголовок, шаги, собранное, отправка."""
    run = got['run']
    lines = [f'прогон {run["id"]}  «{run.get("scenario_name", "?")}»  '
             f'{run["status"]}  шаг {run["step_no"]}  {run["duration_ms"]} мс  '
             f'{run.get("created_at", "")}']
    if run.get('error'):
        lines.append(f'ОШИБКА: {run["error"]}')
    for record in got['trace']:
        mark = 'ok ' if record.get('ok') else '!! '
        tail = f' — {record["error"]}' if record.get('error') else ''
        shown = record.get('value')
        shown = f'  → {shown}' if shown not in (None, '') else ''
        lines.append(f'{mark}{record.get("index", "?"):>3}  {record.get("action", "")} '
                     f'{record.get("target", "")}{shown}  '
                     f'[{record.get("ms", 0)} мс]{tail}')
    for name, value in (got['extract'] or {}).items():
        if isinstance(value, list) and value and isinstance(value[0], dict) \
                and 'url' in value[0]:
            lines.append(f'СЕТЬ «{name}»:')
            for one in value:
                lines.append(f'  {one.get("status", "?")}  {one.get("method", "")} '
                             f'{_short(str(one.get("url", "")), 90)}')
                for key, item in _body_fields(one.get('body', '')).items():
                    lines.append(f'      {key} = {_short(item, 120)}')
        else:
            lines.append(f'СОБРАНО «{name}»: {_short(str(value), 160)}')
    return lines


def _body_fields(body):
    """Тело запроса — парами: urlencoded раскодирован, JSON разобран, прочее как есть."""
    text = str(body or '')
    if not text:
        return {}
    if text.startswith('{'):
        try:
            data = json.loads(text)
            return {key: str(value) for key, value in data.items()} \
                if isinstance(data, dict) else {'<json>': text}
        except ValueError:
            return {'<json>': text}
    pairs = urllib.parse.parse_qsl(text, keep_blank_values=True)
    return dict(pairs) if pairs else {'<body>': text}


def _short(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + '…'


def _json_load(value, default):
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Разбор прогона сценария: шаги с исходом и отправленные тела.')
    ap.add_argument('run_id', nargs='?', type=int, default=0,
                    help='номер прогона; или возьми последний у --scenario')
    ap.add_argument('--scenario', type=int, default=0, help='чей последний прогон')
    ap.add_argument('--json', action='store_true', help='сырьём, не строками')
    ns = ap.parse_args()

    async def _main():
        got = await browser_scenario_trace(ns.run_id, scenario_id=ns.scenario)
        if ns.json:
            print(json.dumps(got, ensure_ascii=False, default=str))
        else:
            for line in browser_scenario_trace_text(got):
                print(line)
        return 0

    raise SystemExit(asyncio.run(_main()))
