"""
Видно ли с прода, что правила новые: приметы правки в живом круге.

`deploy_wait` отвечает на «доехал ли код». Это следующий вопрос — «работает ли
поведение»: коммит на проде ещё не значит, что круг зовёт новый код (фича за
выключателем, круг спит, контейнер перезапустился дополовиной). Проверять это
полагается по **признакам самого круга**: новый запрос бегает — в журнале воркера
видна его сигнатура; темп ответа свой — в тех же строках видно, чем круг
отбирает пул.

Что инструмент делает руками оператора шестью заходами по ssh, то делает разом:

    1. приметы берёт из диффа — строковые литералы уезжающих строк (SQL нового
       запроса, имена новых символов) — руками их никто не вычитывает;
    2. журнал прода спрашивает одной командой (какой — дело стенда: `ssh …
       docker logs …` оператору виднее);
    3. отвечает по каждой примете: бегает ×N (первая строка) или **не видно
       вовсе** — значит код ещё старый или занятие молчит.

⚠ Отличие от `deploy_wait` то же, что у «включено» от «работает»: тот — факт
выкладки, этот — дело после неё.
"""
import argparse
import subprocess
import sys
from pathlib import Path

# Как часто примета слишком коротка, чтобы быть признаком: «SELECT» короток,
# а весь фрагмент нового запроса — нет.
DEPLOY_PROOF_MIN = 12


def deploy_proof_markers(root: str = '.', base: str = 'origin/master') -> list[str]:
    """Приметы правки: строковые литералы уезжающих строк кода.

    Берётся из **добавленных** строк: литералы длиннее `DEPLOY_PROOF_MIN` —
    сигнатура нового запроса, имя нового символа — тем и проверяется, что
    круг заговорил новым голосом.

    ⚠ Не доверять списку вслепую: правка трогает и литералы не про круг.
    Список — кандидаты, hands сокращают (`--marker`), если примет много.
    """
    root = str(Path(root).resolve())
    out, seen = [], set()

    for args in (['diff', '-U0', f'{base}...HEAD'], ['diff', '-U0', 'HEAD'],
                 ['diff', '-U0', '--cached']):
        got = subprocess.run(['git', '-C', root, *args], capture_output=True,
                             text=True, check=False)
        for line in got.stdout.split('\n'):
            if not line.startswith('+') or line.startswith('+++'):
                continue
            for lit in _literals(line[1:]):
                if lit not in seen and len(lit) >= DEPLOY_PROOF_MIN:
                    seen.add(lit)
                    out.append(lit)
    return out


def deploy_proof_seen(marker: str, logs: str) -> dict:
    """Бегает ли примета в журнале: сколько раз и первой строкой."""
    hits = [one.strip() for one in logs.splitlines() if marker in one]
    return {'marker': marker, 'hits': len(hits), 'sample': hits[0][:160] if hits else ''}


def deploy_proof_verify(markers, logs_command: str) -> list[dict]:
    """По каждой примете — видно ли её в выводе команды.

    Args:
        markers: приметы (`deploy_proof_markers` или свои `--marker`).
        logs_command: команда, печатающая журнал/вывод прода. Какой стенд,
            тем и команда — инструмент не знает ни ssh, ни имён контейнеров:
            `ssh хост 'docker logs котейнер --since 30m 2>&1'` целиком,
            как её написал бы человек.

    ⚠ `shell=True` намеренно (тот же договор, что у `deploy_wait._shell`):
    сюда приходит команда оператора с кавычками в три слоя.
    """
    got = subprocess.run(logs_command, shell=True, capture_output=True,
                         text=True, timeout=300)
    logs = got.stdout + got.stderr
    return [deploy_proof_seen(one, logs) for one in markers]


def deploy_proof_format(report: list[dict]) -> str:
    """Отчёт словами: примета бегает или её не видно."""
    if not report:
        return 'примет нет — правка не тронула литералов? сверьтесь с диффом'

    lines = []
    for one in report:
        mark = '✓' if one['hits'] else '✗'
        state = f'бегает ×{one["hits"]}' if one['hits'] else 'НЕ ВИДНО — круги ещё по-старому'
        lines.append(f"{mark} {one['marker'][:70]} — {state}")
        if one['hits']:
            lines.append(f"    {one['sample'][:140]}")

    missed = sum(1 for one in report if not one['hits'])
    lines.append(f"\nпримет: {len(report)}, не видно: {missed}"
                 + ('' if missed else ' — новые правила ходят живьём'))
    return '\n'.join(lines)


def _literals(line: str) -> list[str]:
    """Строковые литералы строки — наивным разбором, без ast: строка правки
    не обязана быть валидным python (половина выражения)."""
    out, rest = [], line
    while True:
        q = min((i for i in (rest.find("'"), rest.find('"')) if i >= 0),
                default=-1)
        if q < 0:
            return out
        quote, rest2 = rest[q], rest[q + 1:]
        end = rest2.find(quote)
        if end < 0:
            return out
        out.append(rest2[:end])
        rest = rest2[end + 1:]


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1])) \
        if __package__ in (None, '') else None

    ap = argparse.ArgumentParser(
        description='Видно ли с прода, что правила новые: приметы правки в журнале.')
    ap.add_argument('--logs', required=True,
                    help='команда, печатающая журнал прода (ssh … docker logs …)')
    ap.add_argument('--marker', action='append', default=[],
                    help='примета; можно несколько. Пусто — из уезжающего диффа')
    ap.add_argument('--root', default='.', help='каталог репозитория')
    ap.add_argument('--base', default='origin/master', help='база диффа')
    args = ap.parse_args()

    try:
        markers = args.marker or deploy_proof_markers(args.root, args.base)
        if not markers:
            raise SystemExit('примет нет: ни --marker, ни литералов в диффе')
        report = deploy_proof_verify(markers, args.logs)
        print(deploy_proof_format(report))
        raise SystemExit(0 if all(one['hits'] for one in report) else 1)
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')
