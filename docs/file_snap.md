# Снимок таблицы «ключ→значение» и дифф против прошлого захода

> Наблюдение живёт сравнениями: SOA-сериал вчера и сегодня, набор PTR имён,
> хеши страниц. Держать прошлое в памяти — путать; здесь память — файл: один
> вызов кладёт снимок, второй снимает и отвечает диффом.

## Как

```python
from file_.file_snap import file_snap_diff, file_snap_save, file_snap_load

file_snap_save('intel/soa.json', {'tovarioptom': '2026102201'})
d = file_snap_diff('intel/soa.json', {'tovarioptom': '2026102202'})
d['changed']   # {'tovarioptom': {'was': '2026102201', 'now': '2026102202'}}
d['added']; d['removed']; d['same']
d['first']     # True — прежнего снимка нет (первый заход, не поломка)
d['prev_saved_at']
```

Таблица плоская (`str → str`); вложенное — `setting_/setting_state.py`,
не сюда. Запись атомарна, прежняя копия — рядом `.bak`.

```bash
PYTHONPATH=. python -m file_.file_snap show  intel/soa.json
PYTHONPATH=. python -m file_.file_snap diff  intel/soa.json --json new.json
```

⚠ Пропавший ключ — `removed`, а не «изменился на пусто»: различай их, у
заметавших следы хостеров сериал не «пропал», а перестал отвечать.
