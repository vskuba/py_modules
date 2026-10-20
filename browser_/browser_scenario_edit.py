"""Правка сценария из shell: точечная операция вместо перезаписи всего списка.

Каждая правка шагов руками — это `docker compose exec … python - <<'PY'`, в
котором список dict'ов читают, шьют в него шаг позиционной вставкой и пишут
обратно через `browser_scenario_save` со всеми полями. Обвязка длиннее смысла,
а опечатка в номере шага перезаписывает не тот: сценарий живёт списком, и
«поменять третий selector» наружу никак не выражено.

Здесь три операции над списком шагов — `set` (править поля N-го), `insert`
(вставить перед N-м), `rm` (убрать N-й) и `move` — и всё это с сохранением через
`browser_scenario_save`, который проверяет шаги схемой контейнера. Номера шагов
— с единицы, как их показывает плёнка и панель.

Само чтение и запись — `browser_scenario_get`/`browser_scenario_save`; этот
модуль не хранилище, а правщик: он знает только форму шага
(`BrowserScenarioStep`).
"""
import argparse
import asyncio
import json
import sys

from browser_.browser_scenario import BROWSER_SCENARIO_ACTIONS
from browser_.browser_scenario_store import browser_scenario_get, browser_scenario_save

# Поля, которые вообще бывают у шага (`BrowserScenarioStep`). Обязательность
# по конкретному действию проверяет схема при сохранении, а не здесь: тот же
# `click` с `value` «x,y» вне реестровых полей действия, но законен — реестр
# описывает обязательное, а не единственно возможное.
_STEP_FIELDS = {'action', 'selector', 'url', 'value', 'key', 'name', 'fields',
                'timeout_ms', 'optional', 'submit', 'shot', 'shot_selector'}


async def browser_scenario_edit(scenario_id: int, ops: list) -> dict:
    """Применить операции к шагам сценария и сохранить его.

    Args:
        scenario_id: какой сценарий.
        ops: операции, каждая — dict:
            {'op': 'set',    'no': 3, 'step': {'selector': '…'}} — править поля;
            {'op': 'insert', 'no': 4, 'step': {'action': 'hover', …}} — вставить
                перед шагом 4 (no = шагов+1 — в конец);
            {'op': 'rm',     'no': 7} — убрать шаг;
            {'op': 'move',   'no': 5, 'to': 2} — переставить.

    Returns:
        {'steps': обновлённый список — им же, не перечитывая, видно, что вышло}.

    Raises:
        KeyError: сценария нет; ValueError: операция вне списка, действие не из
            реестра, `set` притянул чужое поле — всё это до записи, список в
            базе остаётся нетронутым.

    ⚠ Исполняющий сервис кэширует прочитанные сценарии (в CyberAi — 600 с,
    `cyber_worker`). Правка уедет в базу сразу, но ближайший прогон может идти
    старым списком: после правки рестартуй исполнитель (`docker_compose_ops`)
    или жди истечения кэша.
    """
    scenario = await browser_scenario_get(int(scenario_id))
    steps = [dict(step) for step in scenario['steps']]
    for one in ops or []:
        steps = _step_op(steps, one)
    await browser_scenario_save(
        scenario['name'], steps, site_id=scenario.get('site_id', 0),
        goal=scenario.get('goal', ''), start_url=scenario.get('start_url', ''),
        variables=scenario.get('variables') or {},
        status=scenario.get('status', 'active'), scenario_id=int(scenario_id),
        device=scenario.get('device', 'any'))
    return {'steps': steps}


def browser_scenario_steps_table(steps: list) -> list:
    """Шаги в человекочитаемые строки: №, действие, цель, значение, флаги.

    Тем же порядком их покажет и панель, и плёнка прогона; строки — это то, что
    вставляют в `--show`, не пересобирая dict'ы руками.
    """
    rows = []
    for no, step in enumerate(steps, start=1):
        fields = [str(step.get('action', ''))]
        target = step.get('selector') or step.get('url') or step.get('key') or ''
        if target:
            fields.append(str(target))
        if step.get('value'):
            fields.append(f'= {step["value"]}')
        flags = [word for word in ('submit', 'optional') if step.get(word)]
        if step.get('shot'):
            flags.append(f'shot={step["shot"]}')
        if flags:
            fields.append('+' + ','.join(flags))
        rows.append(f'{no:>3}  ' + '  '.join(fields))
    return rows


def _step_op(steps: list, one: dict) -> list:
    """Одна операция над списком шагов; ошибка — ValueError, список не тронут."""
    op, no = one.get('op', ''), int(one.get('no', 0))
    if op not in ('set', 'insert', 'rm', 'move'):
        raise ValueError(f'не знаю операцию {op!r}: set, insert, rm, move')
    if not 1 <= no <= len(steps) + (1 if op == 'insert' else 0):
        raise ValueError(f'шаг {no} вне списка (шагов {len(steps)})')
    step = dict(one.get('step') or {})
    if op in ('set', 'insert'):
        if op == 'insert':
            if step.get('action') not in BROWSER_SCENARIO_ACTIONS:
                raise ValueError(f'действие {step.get("action")!r} не из реестра: '
                                 f'{", ".join(sorted(BROWSER_SCENARIO_ACTIONS))}')
            if not set(step) <= _STEP_FIELDS:
                raise ValueError(f'у шага чужие поля: '
                                 f'{", ".join(sorted(set(step) - _STEP_FIELDS))}')
        else:
            if not set(step) <= _STEP_FIELDS:
                raise ValueError(f'в шаг правят чужие поля: '
                                 f'{", ".join(sorted(set(step) - _STEP_FIELDS))}')
            steps[no - 1] = {**steps[no - 1], **step}
            return steps
        steps.insert(no - 1, step)
        return steps
    if op == 'rm':
        return steps[:no - 1] + steps[no:]
    to = int(one.get('to', 0))
    if not 1 <= to <= len(steps):
        raise ValueError(f'куда ({to}) вне списка')
    moved = steps[no - 1]
    rest = steps[:no - 1] + steps[no:]
    rest.insert(to - 1, moved)
    return rest


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Точечная правка шагов сценария: set, insert, rm, move.')
    ap.add_argument('scenario_id', type=int)
    ap.add_argument('--show', action='store_true',
                    help='только показать шаги таблицей')
    ap.add_argument('--set', nargs=2, metavar=('N', 'JSON'), action='append',
                    help='править шаг N: JSON полей, напр. \'{"selector":"a.b"}\'')
    ap.add_argument('--insert', nargs=2, metavar=('N', 'JSON'), action='append',
                    help='вставить шаг перед N-м: JSON шага целиком')
    ap.add_argument('--rm', type=int, action='append', metavar='N')
    ap.add_argument('--move', nargs=2, type=int, metavar=('N', 'TO'),
                    action='append')
    ns = ap.parse_args()

    async def _main():
        # CLI может идти с хоста: имя контейнера из `.env` отсюда не видно, а пул
        # заводится один раз на процесс — заводим его рабочим адресом до первого
        # запроса, дальше общий слой возьмёт именно его.
        from mysql_.mysql_ import mysql_pool_get
        from mysql_.mysql_host import mysql_host_address
        await mysql_pool_get(*mysql_host_address())


        if ns.show:
            scenario = await browser_scenario_get(ns.scenario_id)
            print(f'«{scenario["name"]}» ({scenario.get("status", "?")})')
            for line in browser_scenario_steps_table(scenario['steps']):
                print(line)
            return 0
        ops = ([{'op': 'set', 'no': int(n), 'step': json.loads(body)}
                for n, body in ns.set or []]
               + [{'op': 'insert', 'no': int(n), 'step': json.loads(body)}
                  for n, body in ns.insert or []]
               + [{'op': 'rm', 'no': n} for n in ns.rm or []]
               + [{'op': 'move', 'no': n, 'to': to} for n, to in ns.move or []])
        if not ops:
            print('ни одной операции; --show покажет текущие шаги', file=sys.stderr)
            return 2
        got = await browser_scenario_edit(ns.scenario_id, ops)
        for line in browser_scenario_steps_table(got['steps']):
            print(line)
        return 0

    async def _cli():
        try:
            return await _main()
        finally:
            # пул живёт дольше этого запуска: не закрыть — соединения доедут до
            # сборщика мусора после конца цикла и заорут «Event loop is closed»
            from mysql_.mysql_ import mysql_pool_close
            await mysql_pool_close()


    raise SystemExit(asyncio.run(_cli()))
