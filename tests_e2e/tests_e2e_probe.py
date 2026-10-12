"""Один зонд на страницу: шаги, rect'ы областей и проверки содержимого разом.

Зонд идёт **одним** вызовом `web_drive_eval`, а не вызовом на область. Причина
не в скорости: каждый вызов поднимает свой Chrome и открывает страницу заново,
то есть мерил бы ДРУГОЕ её состояние. Две области, замеренные порознь, могут не
сойтись между собой — и расследовать потом будут вёрстку, а не замер.

Порядок внутри зонда: сперва шаги (нажать вкладку, дождаться), потом замеры.
Шаг — это приведение страницы в то состояние, про которое случай; без него
проверялась бы та вкладка, на которой страница открылась.

Шагов четыре: `click`, `set`, `wait_for`, `wait`.

⚠⚠ `set` — про **формы**, и без него половина страниц панели недостижима:
отбор там живёт в `<select>`, а щелчок по списку значения не меняет. Поле
получает `input` и `change` пузырями, потому что страницы слушают разное:
одна вешает обработчик на `change`, соседняя на `input`, и шаг, шлющий одно
событие, работал бы через раз — через страницу.

⚠ У `set` три вида, и выбор между ними — про то, что в случае можно написать
честно:

| Вид | Когда | Пример |
|-----|-------|--------|
| `value` | значение устойчиво и само себя объясняет | `value: mail` |
| `label` | значение техническое, а подпись человеческая | `label: Ladadate` |
| `index` | важен порядок, а не конкретный вариант | `index: 0` |

`label` ищет **вхождение**, а не точное совпадение: подписи носят счётчики и
значки («Ladadate · 42»), и точное сравнение ломалось бы от каждой цифры.
`index` нужен спискам с сервера — разговор, анкета, узел: там в `value` лежат
номера строк, и в случае их знать неоткуда; `index: 0` читается как «первый из
того, что пришло».

⚠⚠ Шагов ожидания два, и это **не** одно и то же. `wait_for` опрашивает условие
до его наступления — им и надо пользоваться; `wait` просто спит. Ожидание
временем даёт плавающий тест: на занятой машине лента не успевает прийти, и
«записей нет» читается как «ошибок нет» — зелёный тест на непроверенной
странице. А плавающий тест хуже отсутствующего: он приучает не смотреть на
красное. `wait` оставлен только для пауз, у которых нет наблюдаемого признака
(анимация), и в случаях его быть почти не должно.

⚠ Зонд ловит свои падения сам и возвращает их полем `error` области или
проверки. Исключение изнутри `Runtime.evaluate` обнулило бы весь ответ, и
единственный мёртвый селектор стирал бы замеры всех остальных областей.
"""

import json

from web_.web_drive import web_drive_eval

# Запас бюджета сверх того, что случай просит своими шагами: на замеры, кадр и
# на то, что страница панели добирает данные запросами.
#
# ⚠⚠ Это ЗАПАС, а не потолок. Потолок считает `tests_e2e_probe_budget` —
# сумма `timeout` всех шагов плюс этот запас. Пока он был фиксированным, случай
# с ожиданием в 30 с обрывался на 25-й секунде: зонд не успевал ответить, и
# строка получала `error` вместо вердикта. Снаружи это выглядело как сбой
# браузера, а не как «случай просит больше, чем ему дали».
TESTS_E2E_PROBE_WAIT = 20000


def tests_e2e_probe(url: str, case: dict, size: tuple, shot: str = '',
                    chrome: str = '') -> dict:
    """Снять со страницы всё, что нужно случаю: кадр, области и проверки.

    Args:
        url: адрес живой страницы (копия панели — `uvicorn_mirror`).
        case: случай (`tests_e2e_case_load`).
        size: (ширина, высота) вьюпорта.
        shot: куда положить кадр; пусто — кадр не снимается.
        chrome: путь к бинарю браузера; пусто — поиск `web_shot`.

    Returns:
        dict: `viewport`, `scroll`, `areas` (по селектору), `checks` (по имени),
        `steps` (что из шагов не вышло), `console` — строки консоли страницы.

    Raises:
        RuntimeError: страница не открылась или Chrome не ответил — это
            `error` прогона, а не провал теста.

    ⚠⚠ Кадр снимается ЗДЕСЬ, тем же прогоном, а не отдельным вызовом `web_shot`.
    Отдельный вызов — это второй запуск Chrome и вторая загрузка страницы, то
    есть кадр **другого её состояния**: случай нажимает вкладку «Error», а на
    снимке остаётся та, с которой страница открылась. Поймано глазами на первом
    же настоящем случае — числа при этом были верные, и беда видна была только
    в кадре.
    """
    answer = web_drive_eval(url, tests_e2e_probe_js(case), size=size,
                            wait_ms=tests_e2e_probe_budget(case), shot=shot,
                            chrome=chrome)
    seen = answer.get('value') or {}
    seen['console'] = [str(line) for line in (answer.get('console') or [])]
    return seen


def tests_e2e_probe_budget(case: dict) -> int:
    """Сколько времени дать зонду: сумма ожиданий случая плюс запас.

    Args:
        case: случай (или его сторона) — читаются `timeout` шагов.

    Returns:
        int: потолок в миллисекундах.

    ⚠⚠ Считается по случаю, а не берётся постоянной. Шаг вправе ждать сколько
    написано — это его договор со страницей; зонд, оборвавший такое ожидание
    раньше срока, пишет в строку `error`, и снаружи это читается как сбой
    браузера, а не как «дали меньше, чем попросили». Поймано на проде: шаг с
    `timeout: 30000` обрывался на 25-й секунде постоянного бюджета.

    ⚠ `wait` (который просто спит) тоже считается: он так же съедает время.
    """
    asked = 0
    for step in (case.get('steps') or []):
        if not isinstance(step, dict):
            continue
        if step.get('wait_for'):
            asked += int(step.get('timeout') or 15000)
        if step.get('wait'):
            asked += int(step.get('wait') or 0)

    return int(asked + TESTS_E2E_PROBE_WAIT)


def tests_e2e_probe_js(case: dict) -> str:
    """Тело зонда под случай: шаги, замер областей, проверки содержимого."""
    selectors = [area['selector'] for area in case.get('areas') or []]
    checks = [{'name': one['name'], 'js': one['js']}
              for one in (case.get('checks') or []) if one.get('js')]

    return _TESTS_E2E_PROBE_BODY % {
        'steps': json.dumps(case.get('steps') or [], ensure_ascii=False),
        'selectors': json.dumps(selectors, ensure_ascii=False),
        'checks': json.dumps(checks, ensure_ascii=False),
    }


# ⚠ Проверка исполняется через `new Function`, а не `eval`: тело приходит из
# YAML проекта как **тело функции с return**, и `eval` такой текст не примет.
# Это не дыра: случай лежит в репозитории рядом с кодом и правится так же, как
# код, — чужого текста сюда не приходит.
_TESTS_E2E_PROBE_BODY = """
const steps = %(steps)s;
const selectors = %(selectors)s;
const checks = %(checks)s;
const out = {viewport: 0, scroll: 0, areas: {}, checks: {}, steps: []};

const nap = ms => new Promise(res => setTimeout(res, ms));

// Ждать ИЗМЕНЕНИЕ, а не время: условие опрашивается, пока не станет истинным.
const until = async (body, ms) => {
  const fn = new Function('return (async () => {' + body + '})()');
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    try { if (await fn()) return true; } catch (err) { /* страница ещё рисуется */ }
    await nap(150);
  }
  return false;
};

for (const step of steps) {
  try {
    if (step.click) {
      const node = document.querySelector(step.click);
      if (!node) { out.steps.push({step: 'click ' + step.click, error: 'узла нет'}); continue; }
      node.click();
      await nap(300);
    }
    if (step.set) {
      const node = document.querySelector(step.set);
      if (!node) { out.steps.push({step: 'set ' + step.set, error: 'узла нет'}); continue; }

      if (step.label !== undefined && step.label !== null) {
        // Вариант по ПОДПИСИ: в `value` у таких списков технический номер
        // (id сайта, строки, узла), один на установку, а подпись человеческая
        // и в случае читается. Совпадение по вхождению, а не точное: подпись
        // носит счётчики и значки («Ladadate · 42»).
        const want = String(step.label);
        const options = [...(node.options || [])];
        const found = options.findIndex(o => (o.textContent || '').includes(want));
        if (found < 0) {
          out.steps.push({step: 'set ' + step.set,
                          error: 'варианта с подписью «' + want + '» в списке нет'});
          continue;
        }
        node.selectedIndex = found;
      } else if (step.index !== undefined && step.index !== null) {
        // Вариант по счёту — для списков, пришедших с сервера: их значения
        // номера строк, и в случае их знать неоткуда.
        const options = node.options || [];
        if (!options.length) {
          out.steps.push({step: 'set ' + step.set, error: 'список пуст: выбирать не из чего'});
          continue;
        }
        const at = Number(step.index) || 0;
        if (at >= options.length) {
          out.steps.push({step: 'set ' + step.set,
                          error: 'вариантов ' + options.length + ', а просят ' + (at + 1) + '-й'});
          continue;
        }
        node.selectedIndex = at;
      } else {
        node.value = String(step.value === undefined ? '' : step.value);
        // Поставили значение, которого в списке нет — браузер молча оставляет
        // пустое, и дальше случай мерил бы не тот отбор. Говорим прямо.
        if (node.options && node.selectedIndex < 0) {
          out.steps.push({step: 'set ' + step.set,
                          error: 'варианта «' + step.value + '» в списке нет'});
          continue;
        }
      }

      // Оба события и оба пузырём: страницы слушают разное — см. ⚠⚠ в шапке.
      node.dispatchEvent(new Event('input', {bubbles: true}));
      node.dispatchEvent(new Event('change', {bubbles: true}));
      await nap(300);
    }
    if (step.wait_for) {
      const ok = await until(step.wait_for, Number(step.timeout) || 15000);
      if (!ok) out.steps.push({step: 'wait_for', error: 'условие не наступило за отведённое время'});
    }
    if (step.wait) await nap(Number(step.wait) || 0);
  } catch (err) {
    out.steps.push({step: JSON.stringify(step), error: String(err)});
  }
}

out.viewport = innerWidth;
out.scroll = document.documentElement.scrollWidth;

selectors.forEach(function (sel) {
  try {
    const e = document.querySelector(sel);
    if (!e) { out.areas[sel] = null; return; }
    const r = e.getBoundingClientRect();
    out.areas[sel] = {
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height),
      clipped: e.scrollWidth > e.clientWidth + 1,
      over: e.scrollWidth - e.clientWidth,
      text: (e.textContent || '').trim().length
    };
  } catch (err) {
    out.areas[sel] = null;
  }
});

for (const check of checks) {
  try {
    const value = await (new Function('return (async () => {' + check.js + '})()'))();
    out.checks[check.name] = {value: Number(value)};
  } catch (err) {
    out.checks[check.name] = {error: String(err)};
  }
}

return out;
"""
