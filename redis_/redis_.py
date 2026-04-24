import os

from redis import Redis

_conn = None


def redis_conn_get():
    global _conn
    if _conn is None:
        redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
        _conn = Redis.from_url(redis_url)
    return _conn
