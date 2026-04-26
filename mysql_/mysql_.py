import pymysql
import pymysql.cursors


def mysql_get_url() -> str:
    return 'mysql://developer:password@mysql/project'


def mysql_conn_get() -> pymysql.Connection:
    return pymysql.connect(
        host='mysql',
        user='developer',
        password='password',
        database='project',
        cursorclass=pymysql.cursors.DictCursor,
        charset='utf8mb4'  # Рекомендуется для корректной работы с текстом/эмодзи
    )


def mysql_get_db():
    connection = mysql_conn_get()
    try:
        yield connection
    finally:
        connection.close()
