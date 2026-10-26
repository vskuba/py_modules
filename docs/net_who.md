# net_who — кто хостит: DNS-записи, авторитетный ответ, владелец IP и даты

> Третий вопрос после «что за движок» — «кто крутит»: NS-серверы и AS выдают
> хостинг, даты реестра — возраст имени, авторитетный ответ — куда зона
> указывает без посредников. Всё без ключей: DoH, rdap.org, whois-43, dig.

## 1. Записи и владелец

```python
from net_.net_who import net_who_dns, net_who_ip, net_who_reg, net_who_aa, net_who_sweep

await net_who_dns('ariete.ua')          # A/AAAA/NS/MX/TXT через DoH
await net_who_ip('185.104.45.93')       # {'asn': '200000', 'org': '…', 'country': 'UA'}
await net_who_reg('ariete.ua')          # created/expiry/регретратор (RDAP, запасной whois-43)
```

## 2. Авторитетный ответ (`net_who_aa`) — куда зона указывает без посредников

Рекурсивный резолвер отвечает то, что видят все; NS зоны — то, что лежит в
зоне. Расхождение ловит переезды, устаревшие NS и зоны под бренд-защитой
(CSC): `aa: true` у чужого хостера на украденном имени — делегирование под
подписью владельца бренда, не стелс-сквоттинг.

```python
r = await net_who_aa('ariete.ua')
r['servers']['udns1.cscdns.net.']['A']      # ['185.104.45.93'] — сам NS, авторитетно
r['servers']['udns1.cscdns.net.']['soa']    # серийник зоны (замирание — по нему видно)
r['diff']                                   # разошёлся ли A с рекурсивным
```

## 3. Пачкой

```bash
PYTHONPATH=. python -m net_.net_who dns ariete.ua
PYTHONPATH=. python -m net_.net_who aa ariete.ua
PYTHONPATH=. python -m net_.net_who sweep ariete.ua ariete.shop ariete.it   # таблица слоёв
```

⚠ DoH-ответ разбирается как есть — сырые `name/value` по типу, разбор у
вызывающего.
⚠ `diff` — поверхность: NS вправе отвечать по-разному (split-horizon,
геораздача); польза — сам факт, куда указывает зона без посредников.
⚠ `aa` зовёт системный `dig`; без него в PATH — `error` словами, не падение.
