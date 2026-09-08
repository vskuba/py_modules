# Скриншот веб-страницы: headless Chrome с раздачей, бюджетом времени и состоянием

> Страница → PNG делается `web_shot_capture`: каталог раздаётся HTTP сам, кадр
> ждёт досчёта CSS-переходов (`virtual-time-budget`), а попап открывается
> `inject_js` без правки оригинала. Страницу под управлением (`FileReader`,
> `localStorage`, консоль) гоняет `web_drive_eval` по CDP, а логику её
> обработчиков — `web_unit_run` на node без браузера. Инструмент —
> `py_modules/web_` (модули `web_shot`, `web_drive`, `web_unit`), CLI:
> `python -m web_.web_shot`.

## 1. Раздача, а не `file://`

Chrome с `--screenshot file:///…` не догружает корневые ссылки (`/public/...`):
у мобильных и Cordova-страниц ассеты резолвятся от корня веб-вью, и на
`file://` страница рисуется без картинок и шрифтов. Поэтому локальный HTML
всегда раздаётся `web_shot_serve` — `http.server` в демонственной нити на
эфемерном порту, гасится при выходе из блока. Ручной `python -m http.server &`
из скрипта — та же задача с осиротевшим процессом на порту, если скрипт упал
до `kill`.

`web_shot_serve(directory, port=0)` пригоден и сам по себе: прогнать ссылку
через `curl`, открыть в браузере, скормить другому инструменту.

## 2. `virtual-time-budget`: снимок «после перехода», не в середине

`sheet`-попап с `transition: .25s` на голом снимке стоит на середине пути:
полупрозрачный слой даёт серый 233 вместо белого 255, и пороговые замеры
(`image_scan_runs` «белая полоса где») режут его на куски и сообщают неверные
границы. Лечится `--virtual-time-budget=1200`: Chrome досчитывает анимации до
отметки и стреляет затвором после. Дефолт 1200 мс встроен в
`web_shot_capture`; 0 — снять сразу (иногда середина анимации и есть предмет
съёмки).

## 3. Состояние страницы: `inject_js` на копии из симлинков

Попап/модалку из командной строки тапом не открыть. `inject_js` вставляет
`<script>` перед последним `</body>` **копии** страницы: копия лежит в
временном каталоге, куда все соседние файлы (и сам `public/`) симлинкуются —
корневые и относительные ссылки продолжают попадать в настоящие ассеты, а
оригинал на диске не тронут:

```bash
python -m web_.web_shot www/documents.html --out /tmp/menu.png --size 1080,2265 \
    --inject-js "document.getElementById('docmenu').classList.add('open')"
```

Типовой цикл проверки свёрстанного состояния: снимок → `image_scan runs` по
колонке центра → границы сравнить с телефоном (кадры сверяются с поправкой на
статус-бар, см. `image_tooling.md` / `mobile_hardware.md`).

## 4. Пиксель в пиксель

`--force-device-scale-factor=1` + `--window-size=W,H` + `--hide-scrollbars`
включены всегда: без последнего Chrome рисует панель прокрутки **поверх**
макета, ширина вьюпорта перестаёт равняться ширине снимка (проверено: сдвиг
вёрстки на 15 px при заявленных 1080). Вьюпорт высоты кадра телефона не равен:
`1080×2265` покрывает целые кадры `y 91..2356`, координаты со снимка пересчитываются
`+91` — та же арифметика, что для кроя статус-бара.

## 5. Страница под управлением: `web_drive` по CDP

Скриншот — одно действие; когда страницу надо **прогнать** (подать файл в
input, дождаться асинхронной цепочки, почитать `localStorage`, собрать
`console`), снимок с `virtual-time-budget` не годится: виртуальное время
ломает асинхронию `FileReader`/`Image` — колбэки не наступают никогда
(проверено на загрузчике QR: цепочка `readAsDataURL → Image.onload` в дампе
не выполняется). `web_drive_eval` поднимает headless Chrome с
`--remote-debugging-port=0`, подключается по CDP поверх транспортного
`adb_ws` (сырой WebSocket без зависимостей) и гоняет код внутри страницы:

```python
res = web_drive_eval('www/menu_secret.html', """
  const dt = new DataTransfer();
  dt.items.add(new File([bytes], 'qr.png', {type: 'image/png'}));
  const inp = document.getElementById('zpass_qr');
  inp.files = dt.files;                          // прямое присваивание — можно
  inp.dispatchEvent(new Event('change', {bubbles: true}));
  await new Promise(r => setTimeout(r, 400));    // дождаться FileReader+Image
  return document.getElementById('qrNote').textContent;
""", storage=('diaData',), shot='/tmp/after.png')
```

Код — тело async-функции (`return` или `done(v)`); `awaitPromise` ждёт
обещание, `returnByValue` возвращает JSON; ответ — `{value, console,
storage, shot}`; консоль собирается из `Runtime.consoleAPICalled`,
`Runtime.exceptionThrown` и `Log.entryAdded` — падение страницы видно, а не
молчит. Локальный HTML раздаётся тем же `web_shot_serve`, порт ждём через
`DevToolsActivePort` профиля.

## 6. Страница как юнит: `web_unit` без браузера

Проверять логику обработчиков (дефолты, мосты `window.X_SET`, проценты)
быстрее без Chrome: `web_unit_run` вытаскивает последний инлайн-`<script>`
страницы как есть, дописывает тесты и гонит на node с заглушками
DOM/`FileReader`/`Image` — вытаскивать скрипт в отдельный js-файл не надо,
страница тестируется в том виде, в каком лежит. Тесты — тело async-функции с
хелперами `t(имя, усл.)`, `fire(id, 'change')`, `await tick()`; заглушка
`Image` берёт размеры из хука `__image_size(url)` (`null` — «битая картинка»,
стреляет `onerror`).

## CLI

```bash
python -m web_.web_shot https://example.com --out /tmp/shot.png
python -m web_.web_shot www/page.html --out /tmp/s.png --size 390,844
python -m web_.web_shot www/page.html --out /tmp/s.png --budget 0   # снять сразу
python -m web_.web_drive www/page.html --js-file probe.js --storage diaData --shot /tmp/a.png
python -m web_.web_unit www/page.html tests.js      # код возврата 1 — есть провалы
```

## Грабли

1. **Снимок ловит середину анимации** — слои в переходе полупрозрачны,
   пороговые замеры цвета врут (233 вместо 255); нужен `--budget`.
2. **`file://` без ассетов** — корневые ссылки `/…` не резолвятся; отсюда вся
   конструкция с раздачей.
3. **Скроллбар съедает 15 px ширины вьюпорта** — вёрстка сдвигается, замеры
   «от 1080» расходятся; флаг `--hide-scrollbars` не отключать.
4. **`window-size` ≠ «страница целиком»** — снимается вьюпорт, длинная
   страница обрезана по высоте; для «всей страницы» скроллить и сшивать
   (`image_frames_stitch`).
5. **Виртуальное время ест асинхронию** — под `virtual-time-budget`
   `FileReader`/`Image` не стреляют никогда; гонять страницу надо `web_drive`
   (§ 5), а чистую логику — `web_unit` (§ 6).
6. **`web_unit` заглушки повторяют браузерные сигнатуры** — `File(parts,
   name, opts)`, колбэки через `setTimeout`, `navigator` в node ≥ 21 только
   геттер; упростить их «по-своему» значит проверить не страницу, а заглушку.
