"""Все роутеры контейнера-браузера одним включением.

Точке входа проекта незачем знать, из каких файлов собран контейнер: она поднимает
Chromium (`browser_pool_start`) и включает вот этот роутер. Новое умение,
добавленное здесь, появляется во всех проектах сразу — без правки их
`main_browser.py`, о которой пришлось бы помнить и которую забыли бы в первом же.

Порядок включения значения не имеет: пути не пересекаются. Собран список по
ответственностям — сессии, страница, окно, сценарий, снимок, запись, сокет.
"""

from fastapi import APIRouter

from browser_.browser_api import router as browser_api_router
from browser_.browser_api_page import router as browser_api_page_router
from browser_.browser_record import router as browser_record_router
from browser_.browser_scenario_run import router as browser_scenario_run_router
from browser_.browser_snapshot import router as browser_snapshot_router
from browser_.browser_view import router as browser_view_router
from browser_.browser_ws import router as browser_ws_router

router = APIRouter()

router.include_router(browser_api_router)
router.include_router(browser_api_page_router)
router.include_router(browser_view_router)
router.include_router(browser_ws_router)
router.include_router(browser_scenario_run_router)
router.include_router(browser_snapshot_router)
router.include_router(browser_record_router)
