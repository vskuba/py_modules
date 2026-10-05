"""Прогон модуля страницы на node так, как его видит браузер.

Страницы подключают общие модули абсолютным путём — `/static/js/notification.js`.
Браузеру этого достаточно: раздача панели ищет файл в двух каталогах (своём и
общем submodule), и путь разрешается в URL.

А node ходит по **файловой системе** и про `/static/` не знает ничего: он берёт
это за абсолютный путь от корня диска и отвечает `ERR_MODULE_NOT_FOUND`. Тест,
проверяющий модуль страницы, падает — хотя в браузере модуль работает.

## ⚠⚠ Почему именно так, а не симлинками и не относительными путями

Три способа, и два плохи:

* **симлинки** общих файлов в каталог проекта — лишняя сущность в git, и на
  Windows их нет;
* **относительные пути** (`'../notification.js'`) node понимает, но тогда файл
  обязан лежать на диске проекта — то есть общего submodule не бывает вовсе.

Остаётся научить node той же карте, что у раздачи: `/static/<путь>` ищется в
перечисленных каталогах, в том же порядке — свой, потом общий.

## ⚠ Хук ставится через `register`, а не `--import`

`--import` исполняет файл в том же потоке, и перехват резолва из него **не
действует** (проверено: `ERR_MODULE_NOT_FOUND` как без хука). Загрузчик обязан
регистрироваться через `node:module`.`register` — он уходит в отдельный поток, и
только тогда его `resolve` вызывается.
"""

import json
import os
import shutil
import subprocess
import tempfile

# Под каким путём страницы подключают статику. ⚠ То же значение, что у раздачи
# (`panel_/panel_static.py`): разойдись они — тест искал бы не там, где сервер.
JS_STATIC_PREFIX = '/static/'

# Сколько ждём node. Прогон модуля — доли секунды; минута это запас на холодный
# старт и импорт цепочки.
JS_STATIC_TIMEOUT = 60.0

# Тело загрузчика. ⚠ Живёт строкой, а не файлом в репозитории: файл пришлось бы
# искать относительно вызывающего, а он бывает где угодно.
_LOADER = '''
import { pathToFileURL } from 'node:url';
import { existsSync } from 'node:fs';
import path from 'node:path';

const ROOTS = JSON.parse(process.env.JS_STATIC_ROOTS || '[]');
const PREFIX = process.env.JS_STATIC_PREFIX || '/static/';

export function resolve(spec, ctx, next) {
  if (spec.startsWith(PREFIX)) {
    const tail = spec.slice(PREFIX.length);
    for (const root of ROOTS) {
      const full = path.join(root, tail);
      if (existsSync(full)) {
        return {url: pathToFileURL(full).href, shortCircuit: true};
      }
    }
  }
  return next(spec, ctx);
}
'''


def js_static_run(script: str, roots, prefix: str = JS_STATIC_PREFIX,
                  timeout: float = JS_STATIC_TIMEOUT) -> subprocess.CompletedProcess:
    """Выполнить ESM-скрипт на node, разрешая `/static/…` по каталогам раздачи.

    Args:
        script: тело модуля — то же, что ушло бы в `node --input-type=module -e`.
        roots: каталоги статики **в порядке поиска**: свой проекта, затем общий.
        prefix: под каким путём подключается статика.
        timeout: сколько ждём node.

    Returns:
        `CompletedProcess` как есть — с `returncode`, `stdout` и `stderr`.
        ⚠ Отказ **не бросается**: тест сам решает, что значит ненулевой код, и
        показывает `stderr` человеку — в нём и лежит причина.

    Raises:
        FileNotFoundError: нет самого `node`. ⚠ Отдельная ошибка, а не тихий
            пропуск: «тест не нашёл node» и «модуль сломан» — разные новости.

    ```python
    done = js_static_run("const m = await import('/static/js/x.js');"
                         "console.log(JSON.stringify(Object.keys(m)));",
                         roots=[own_public, 'dashboard'])
    assert done.returncode == 0, done.stderr
    ```
    """
    if not shutil.which('node'):
        raise FileNotFoundError('node не найден: прогнать модуль страницы нечем')

    wanted = [str(one) for one in roots if one]

    with tempfile.TemporaryDirectory() as tmp:
        loader = os.path.join(tmp, 'loader.mjs')
        register = os.path.join(tmp, 'register.mjs')

        with open(loader, 'w', encoding='utf-8') as fh:
            fh.write(_LOADER)

        # ⚠ Отдельный файл-регистратор: хук обязан ставиться через `register`,
        # а не исполняться сам — см. ⚠ в заголовке модуля.
        with open(register, 'w', encoding='utf-8') as fh:
            fh.write("import { register } from 'node:module';\n"
                     "import { pathToFileURL } from 'node:url';\n"
                     f'register({json.dumps(loader)}, pathToFileURL("./"));\n')

        env = {**os.environ,
               'JS_STATIC_ROOTS': json.dumps(wanted),
               'JS_STATIC_PREFIX': prefix}

        return subprocess.run(
            ['node', '--input-type=module', f'--import={register}', '-e', script],
            capture_output=True, text=True, timeout=timeout, env=env)
