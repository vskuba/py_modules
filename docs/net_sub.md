# Проба поддоменами: живой хост против фантома wildcard-DNS

> Перебор `dev/shop/stage…` рапортует «стагинг!» на восемь имён из восьми,
> если зона с wildcard: запись есть на каждое придуманное имя. Живой хост
> познаётся не по факту записи, а по адресу — он смотрит **не** на IP домена.

## Как

```python
from net_.net_sub import net_sub_probe

r = net_sub_probe(['moltani.com.ua', 'vsedodomu.com.ua'])
for row in r['rows']:
    row['wildcard']    # True: пробы осыпаются адресом apex (дикая зона)
    row['phantom']     # имена, совпадающие с apex — фантомы
    row['live']        # [{'sub': 'shop.vsedodomu…', 'a': ['161.97…']}]
```

Слова-пробы — дефолтная карта (`dev,test,stage,shop,m,cdn,old,admin,api,
new,demo,beta,www`), передаётся свой `words`. Wildcard — большинство проб
совпало с apex; две-три совпадения (www+cdn на тот же фронт) зоной не
считаются.

```bash
PYTHONPATH=. python -m net_.net_sub moltani.com.ua --words shop,test,demo
```

⚠ Резолвящееся имя — не хост: суди по IP, не по записи. ⚠ В дикой зоне
живой поддомен возможен поверх (shop на чужой машине) — сравнение с apex
это ловит.
