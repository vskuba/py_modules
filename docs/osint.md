# OSINT-переписи: CT-лог, вкус ответа, архив, ключи, витрины, Википедия

> Разведка чужой инфраструктуры без ключей: какие имена когда-либо светились
> на корни (CT), кто из них живой и по какому ремеслу (вкус ответа), что
> показывал мёртвый ныне сайт (Wayback), чем сайт пользуется из чужих
> сервисов (ключи), что у него внутри (sitemap витрины) и кто за именем
> стоит (Википедия). Всё пачкой, с паузами: бесплатные безлимитные API
> банят молча.

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
PYTHONPATH=. python -m osint_.osint_keys https://site.ua/ # ключи со страницы; --deep
PYTHONPATH=. python -m osint_.osint_shop https://site.ua/ # платформа, sitemap, модели
PYTHONPATH=. python -m osint_.osint_wiki it "Ariete_(azienda)"   # вики-выжимка; --full
```

## Ключи и счётчики со страницы (`osint_keys`)

Витрина почти никогда не самостойна: в ней ключ карты, пиксель аналитики,
контейнер тегов, мерчант платёжки. Это реестр чужих идентификаторов, по ним
ферма светится у соседей (тот же GTM на двух сайтах — та же рука).

```python
from osint_.osint_keys import osint_keys_harvest, osint_keys_sweep

k = osint_keys_harvest('https://ariete.ua/')          # {'ok', 'found': {тип: [знач.]}}
k = osint_keys_harvest('https://ariete.ua/', deep=True)  # залезть и в <script src>
```

Реестр типов (`OSINT_KEYS`) — от recaptcha и карт до GTM/GA, пикселей fb/tiktok/
hotjar/clarity, метрики, sentry и платёжных шлюзов (pay.pay.ua, fondy, wayforpay
…); мерчант и ключ карт — подпись того, кто принимает деньги.

⚠⚠ Находка — не публикация: ключ, GTM, телефон и пиксель в отпечатке
публикуют только по решению Хозяина; инструмент их находит и складывает в
`keys`, человек решает, что светить наружу.

## Карта витрины без ключей (`osint_shop`)

`/products.json` у Shopify закрыт, а sitemap — открыт: оттуда счётчик товаров,
названия и коллекция. Платформа — по маркерам в разметке (`OSINT_SHOP_PLATFORM`:
shopify/opencart/woocommerce/prestashop/okaycms/tilda).

```python
from osint_.osint_shop import osint_shop_map, osint_shop_models

m = osint_shop_map('https://ariete.shop')   # {'platform','products','titles','collections'}
osint_shop_models(m['titles'])              # частотные артикулы: 14960-…, 8K…
```

Гомоним-угадка: «Ariete» у фермы — очки (goggles), а не кофеварки De'Longhi;
карта витрины это различает по названиям, а не по названию домена.

## Википедия как справка об имени (`osint_wiki`)

«Что за бренд этот Ariete?» — голый `fetch` вики даёт 403, REST/action API с
контактным UA — 200.

```python
from osint_.osint_wiki import osint_wiki_summary, osint_wiki_text

osint_wiki_summary('it', 'Ariete_(azienda)')   # краткая выжимка + URL
osint_wiki_text('ru', 'Ариете', intro=False)   # полное intro (или вся статья)
```

⚠ Разрешает редиректы (`redirects=1`): статья про бренд-омоним может оказаться
статьёй про материнскую фирму — читай `title` в ответе, а не тот, что просил.

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
