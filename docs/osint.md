# OSINT-переписи: CT-лог, вкус ответа, архив

> Разведка чужой инфраструктуры без ключей: какие имена когда-либо светились
> на корни (CT), кто из них живой и по какому ремеслу (вкус ответа), что
> показывал мёртвый ныне сайт (Wayback). Всё пачкой, с паузами: бесплатные
> безлимитные API банят молча.

## Как

```python
from osint_.osint_ct import osint_ct_names
from osint_.osint_flavor import osint_flavor_sweep
from osint_.osint_arch import osint_arch_snapshots, osint_arch_fetch

names = osint_ct_names(['auebox.shop', 'trend-house.shop'])  # {'names': […]}
rows = osint_flavor_sweep(names['names'])      # класс + куки каждого хоста
arch = osint_arch_snapshots('web-devils.com')  # {'first','last','rows'}
body = osint_arch_fetch('web-devils.com', arch['first']['ts'])  # сырая копия
```

Классы `osint_flavor`: `nx` (нет DNS) / `dns` / `down` / `ghost` (заглушка
хостера или пустой 404) / `congrat` (брошенная поздравилка конвейера) /
`land` (лендинг с формой) / `shop` (магазин: корзина, yml, движок) / `page`.
Маркеры опознания — константы `GHOST/CONGRAT/SHOP/LAND` в шапке модуля, их
правят под новую сеть. utm-зонд (`?utm_source=probe`) выдаёт печать
самописного кода: хост,
ставящий `utm_*` куками, — один программист с тем, что уже найден.

```bash
PYTHONPATH=. python -m osint_.osint_ct roots.txt          # TSV root→имя
PYTHONPATH=. python -m osint_.osint_flavor names.txt      # TSV name→класс
PYTHONPATH=. python -m osint_.osint_arch site.com --all   # снимки; --get TS --out f
```

## Грабли

1. **crt.sh банит молча** — ответ HTML-ошибкой или пустым JSON после N
   запросов; модуль жмёт exponential backoff, но корни всё равно стоит
   гонять пачками до ~60 за сутки.
2. **Имя в CT ≠ живой сайт**: большинство — пустые слоты генераторов
   сертификатов; за живостью — шаг `osint_flavor`, не шаг CT.
3. **CDX лежит «Temporarily Offline» сутками** — пустой ответ здесь не «нет
   снимков», а «потом»; retry внутри, но вывод «0 снимков» доверяй только
   после полного числа попыток (`error` пустой).
4. **Снимок — потолок возраста, не дата запуска**: между появлением сайта и
   первым визитом краулера проходят недели.
