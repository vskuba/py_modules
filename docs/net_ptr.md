# PTR-перепись: реверс-зона квартала как карта машин хостера

> Смотреть на домен умеем (`net_who`); обратный взгляд — «кто подписан на
> этот адрес» — руками каждый раз одинаковые тридцать строк с `dig` и
> ThreadPoolExecutor. Здесь он вызовом: DoH (dns.google), без dig и сидов.

## 1. Один адрес

```python
from net_.net_ptr import net_ptr_get

net_ptr_get('157.90.95.176')      # 'h54.default-host.net' — фронт пула
net_ptr_get('203.0.113.9')         # '' — реверса нет (fresh allocation ≠ мёртв)
```

## 2. Кварталом

```python
from net_.net_ptr import net_ptr_sweep

r = net_ptr_sweep(['2a06:6440:0:2d00::/120', '185.104.45.0/24'])
r['probed']; r['named']            # сколько тыкали, сколько с реверсом
r['by_name']['business-141.default-host.net']   # имя → все его адреса
```

CIDR разворачивается адресами; `cap` (умолчание 4096) не даст пальцу в /8
разнести резолвер.

```bash
PYTHONPATH=. python -m net_.net_ptr get 157.90.95.176
PYTHONPATH=. python -m net_.net_ptr sweep 185.104.45.0/24
```

⚠ Соседство в квартале ничего не доказывает: business-N и чужие клиенты
лежат вперемешку, пул общий. Это поверхность перечисления, не вывод о
владельце.
