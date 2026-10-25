# Выкладка статики на surge и сверка по хешу

> Статический отчёт (разведочная карта) не ждёт `deploy_wait`: вопрос
> короче — **доехал ли файл байт в байт**. Цепочка `npx surge` + curl +
> sha256 руками на семь строк; промах «выложил, а отдаётся старое по кэшу»
> ловится только сверкой хешей.

## Как

```python
from deploy_.deploy_sha import deploy_sha_push

r = deploy_sha_push('.tmp/wt24/site', 'wt24-intel.surge.sh')
r['ok']        # выложен и все хеши совпали
r['files']     # [{'file','local','served','match','status'}] по каталогу
r['pushed']    # хвост вывода surge
```

Сверка — по **всем** файлам каталога, не только по index; `npx surge`
под `npm_config_cache` рядом с каталогом, fetch с `Accept-Encoding`.

```bash
PYTHONPATH=. python -m deploy_.deploy_sha .tmp/wt24/site wt24-intel.surge.sh
```

⚠ Surge отдаёт gzip'ом — сравнивается декодированное; голое сравнение
сырых байтов враньё на тексте. ⚠ Чужой домен во власти чужой песочницы:
не совпал один файл — смотри, не тронул ли хостинг (favicon, 404-страница).
