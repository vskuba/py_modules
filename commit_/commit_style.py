"""
Коммит против стиля истории: `<модуль>: <что и почему>`.

Коммит здесь читает тот, кто через полгода спрашивает «зачем это было». Стиль
один на все репозитории человека: префикс — модуль, который правили (такой
файл/каталог в репо есть), дальше — что и почему, а не «fix». До `git commit`
это стоит дороже, чем после: переписывать историю ради сообщения — второй
резо.

Что сверяется: формат `<module>: <что>`, существо модуля в репозитории
(`git ls-files`), содержательность subject-а (не «fix»/«wip» и не в одно слово).
Диффы инструмент не трогает — он про текст.
"""
import argparse
import re
from pathlib import Path
from subprocess import run

_SUBJECT = re.compile(r'^([a-z0-9_./+-]+):\s*(\S.*)$', re.I)
_STOP = {'fix', 'правка', 'правки', 'wip', 'update', 'обновление', 'refactor',
         'рефакторинг', 'hotfix', 'коммит'}


def commit_style_lint(n=1, ref='HEAD', root='.') -> list[dict]:
    """Последние n сообщений history по стилю; список находок {ref, subject, why}.

    Args:
        n: сколько последних сообщений смотреть.
        ref: что смотреть (`HEAD`, диапазон `main..branch`).
        root: корень репозитория (для сверки имён модулей с `git ls-files`).

    Returns:
        [{'ref', 'subject', 'why'}, ...]; пусто — стиль цел.
    """
    def git(*args):
        return run(('git', '-C', str(root), *args), capture_output=True,
                   text=True).stdout.splitlines()

    subjects = git('log', f'-{n}', '--format=%h %s', ref)
    stems = {Path(p).stem for p in git('ls-files')} | \
            {p.rsplit('/', 1)[0].rsplit('/', 1)[-1] for p in git('ls-files')
             if '/' in p}
    out = []
    for line in subjects:
        sha, _, subject = line.partition(' ')
        m = _SUBJECT.match(subject)
        if not m:
            out.append({'ref': sha, 'subject': subject[:70],
                        'why': 'нет формата `<модуль>: <что и почему>`'})
            continue
        module, body = m.group(1), m.group(2)
        if Path(module).name not in stems:
            out.append({'ref': sha, 'subject': subject[:70],
                        'why': f'модуля `{module}` нет в репозитории'})
        if body.lower() in _STOP or len(body) < 12:
            out.append({'ref': sha, 'subject': subject[:70],
                        'why': 'subject без «что и почему»'})
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Коммит-сообщения против стиля репо; диффы не трогает.')
    ap.add_argument('-n', type=int, default=1, help='сколько сообщений смотреть')
    ap.add_argument('ref', default='HEAD', help='что смотреть (HEAD, A..B)')
    ap.add_argument('--root', default='.', help='корень репозитория')
    ns = ap.parse_args()
    try:
        found = commit_style_lint(ns.n, ns.ref, ns.root)
        for item in found:
            print(f"{item['ref']}: {item['why']}: {item['subject']}")
        print(f'находок: {len(found)}')
    except (RuntimeError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')
