"""Бэкап базы: полный дамп, скачивание и восстановление.

⚠⚠ Отличие от seed (`panel_seed_api.py`) не в размере, а в назначении. Seed
переносит между установками то, что завели руками, и ложится **поверх** живой
базы. Бэкап — снимок **всей** базы на случай аварии, и восстановление его
**перезаписывает** всё, что есть сейчас. Поэтому и разные страницы: перепутать
их — потерять день работы.

Снимает и заливает дамп `mysql_/backup_engine.py`: `mysqldump` из образа, а при
его отсутствии — запасной путь на самом pymysql, без триггеров и процедур. Знать
про запасной путь надо до аварии, а не после, поэтому он назван здесь, хотя
живёт не здесь.

⚠ Файлы лежат в `data/backups` и в репозиторий не попадают: в дампе вся
переписка и пароли учёток.
"""

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from logging_.logging_ import logger_info
from mysql_.backup_engine import backup_engine_dump, backup_engine_restore
from mysql_.mysql_ import db, host, password, port, user

from panel_.panel_auth import panel_auth_admin_required, panel_auth_need
from panel_.panel_file import (panel_file_dir, panel_file_info, panel_file_list,
                               panel_file_path, panel_file_stamp)

# Под каким путём живут точки.
PANEL_BACKUP_API_PREFIX = '/api/backup'

# Подкаталог в `data/` и расширение файла.
PANEL_BACKUP_SUB = 'backups'
PANEL_BACKUP_EXT = '.sql.gz'


def panel_backup_api_router(view=None, manage=None,
                            prefix: str = PANEL_BACKUP_API_PREFIX) -> APIRouter:
    """Роутер бэкапов: `app.include_router(panel_backup_api_router())`.

    Args:
        view: право на список и скачивание. `None` — полноправному.
        manage: право снимать, восстанавливать и удалять. `None` — то же.
        prefix: под каким путём жить.

    ⚠ Скачивание отнесено к **чтению**, а не к управлению, и это осознанно: кто
    видит список дампов, тот всё равно знает, что в базе. Прятать от него файл,
    разрешив видеть имя, — защита, которой нет.
    """
    guard_view = panel_auth_need(view) if view else panel_auth_admin_required
    guard_manage = panel_auth_need(manage) if manage else panel_auth_admin_required

    router = APIRouter(prefix=prefix, tags=['backup'])

    @router.get('/file')
    async def backup_file_list(user_=Depends(guard_view)):
        """Что уже снято. Свежие сверху — по имени, в нём дата."""
        return {'result': panel_file_list(_dir(), PANEL_BACKUP_EXT)}

    @router.post('/create')
    async def backup_create(user_=Depends(guard_manage)):
        """Снимает дамп всей базы и кладёт его сжатым в `data/backups`."""
        directory = _dir()
        os.makedirs(directory, exist_ok=True)

        name = f'backup_{panel_file_stamp()}{PANEL_BACKUP_EXT}'
        path = os.path.join(directory, name)

        try:
            await asyncio.to_thread(backup_engine_dump, path, host, port, user,
                                    password, db)
        except Exception as e:
            # ⚠ Недописанный файл убираем: в списке он неотличим от целого, а
            # восстановиться из него нельзя — выяснится это в худший момент.
            if os.path.exists(path):
                os.remove(path)
            logger_info(f'[backup] дамп не снялся: {type(e).__name__}: {e}')
            raise HTTPException(status_code=500, detail=f'дамп не снялся: {e}')

        logger_info(f'[backup] {user_}: снят {name}')

        return {'result': panel_file_info(directory, name)}

    @router.post('/restore/{name}')
    async def backup_restore(name: str, user_=Depends(guard_manage)):
        """Заливает дамп обратно. **Перезаписывает базу целиком.**"""
        path = panel_file_path(_dir(), name, PANEL_BACKUP_EXT)

        try:
            await asyncio.to_thread(backup_engine_restore, path, host, port, user,
                                    password, db)
        except Exception as e:
            logger_info(f'[backup] {name} не восстановился: {type(e).__name__}: {e}')
            raise HTTPException(status_code=500, detail=f'не восстановился: {e}')

        logger_info(f'[backup] {user_}: восстановлен {name}')

        return {'result': f'Бэкап {name} восстановлен'}

    @router.get('/download/{name}')
    async def backup_download(name: str, user_=Depends(guard_view)):
        """Отдаёт файл целиком. Скачанный дамп — та же база: не в общие каталоги."""
        return FileResponse(panel_file_path(_dir(), name, PANEL_BACKUP_EXT),
                            filename=name, media_type='application/gzip')

    @router.delete('/{name}')
    async def backup_delete(name: str, user_=Depends(guard_manage)):
        """Удаляет дамп. ⚠⚠ Последний оставшийся удалить нельзя.

        Это не забота о порядке в списке: «почистил старьё» и «остался без
        единой копии» выглядят одинаково ровно до того дня, когда копия
        понадобится.
        """
        directory = _dir()
        path = panel_file_path(directory, name, PANEL_BACKUP_EXT)

        if len(panel_file_list(directory, PANEL_BACKUP_EXT)) < 2:
            raise HTTPException(
                status_code=409,
                detail='это единственный бэкап — снимите новый до удаления')

        os.remove(path)
        logger_info(f'[backup] {user_}: удалён {name}')

        return {'result': 'удалено'}

    return router


# ── Приватное ────────────────────────────────────────────────────────────────

def _dir() -> str:
    """Каталог дампов. ⚠ Считается на вызове, а не на импорте: корень проекта
    при импорте модуля слоя ещё может быть не тем, что в работе."""
    return panel_file_dir(PANEL_BACKUP_SUB)
