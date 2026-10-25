# Цепочки редиректов пачкой: canonical-шов сборки виден матрицей

> «Куда на самом деле ведёт домен» руками — `curl -IL` на каждый URL, и
> каждая пачка собирается заново. Здесь обход без `follow_redirects`, hop за
> hop, и пачка параллельно — canonical-поведение видно сводкой.

## 1. Одна цепочка

```python
from http_.http_hops import http_hops_trace

r = http_hops_trace('https://www.technica.com.ua/')
r['hops']     # [{'url': 'https://www…/', 'code': 301, 'location': 'https://…/'}]
r['final']; r['cross']   # 'to_apex'/'to_www'/'same', 'domain'/'scheme'/''
```

## 2. Матрицей

```python
from http_.http_hops import http_hops_matrix

r = http_hops_matrix(['https://a.test/', 'http://b.test/'])
r['summary']  # {'to_apex': n, 'to_www': n, 'same_form': n, 'cross_domain': n, 'dead': n}
```

```bash
PYTHONPATH=. python -m http_.http_hops https://www.technica.com.ua/ --method HEAD
```

⚠ HEAD многие отдают 405 там, где GET живой: при пустой цепочке смотри
`method='GET'`. ⚠ 200 без редиректа на http — не «работает и так», а
отсутствие HSTS; вывод о canonical делай по паре http+https.
