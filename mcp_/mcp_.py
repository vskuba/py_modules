import json
import os
import traceback
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional, Any
from pydantic import BaseModel, Field

from config.config import config_get
from logging_.logging_ import logger_info


@dataclass
class McpToolInputSchemaProperty:
    type: str = 'string'
    description: str = 'description'


@dataclass
class McpToolInputSchema:
    type: str = 'object'
    properties: Optional[dict[str, McpToolInputSchemaProperty]] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    additionalProperties: bool = False


@dataclass
class McpTool:
    name: str
    description: str
    inputSchema: McpToolInputSchema = field(default_factory=McpToolInputSchema)


class McpConfig(BaseModel):
    title: str
    cmd: str
    args: list[str]
    status: str
    env: list[str]
    tools: list[McpTool] = Field(default_factory=list)


def mcp_config_list_get() -> list[str]:
    """Имена сохранённых конфигураций MCP-серверов — по файлам каталога настроек.

    Returns:
        list[str]: имена без расширения `.json`; каталога нет — пустой список.
    """
    if not os.path.exists(_mcp_config_dir()):
        return []

    return [os.path.splitext(f)[0] for f in os.listdir(_mcp_config_dir()) if f.endswith('.json')]


def mcp_config_get(name: str) -> McpConfig | None:
    """Прочитать конфигурацию MCP-сервера: команда запуска, окружение, инструменты.

    Args:
        name: имя конфигурации, как в `mcp_config_list_get`.

    Returns:
        McpConfig | None: `None` и когда файла нет, и когда он не читается.

    ⚠ Ошибка чтения наружу не поднимается — она уходит в журнал, а возвращается
    `None`: «нет конфигурации» и «конфигурация сломана» отсюда неразличимы.
    """
    filename = _mcp_config_filename(name)
    if not os.path.exists(filename):
        return None

    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return McpConfig.model_validate(json.load(f))
    except Exception as e:
        backtrace = traceback.format_exc()
        logger_info(
            f"❌ Ошибка при чтении MCP конфига: {e}."
            f"Полный стек вызовов:\n{backtrace}"
        )
        return None


def mcp_config_tools_json_get(name: str) -> list[dict[str, Any]]:
    """Инструменты MCP-сервера сырым JSON — без разбора в `McpConfig`.

    Нужно там, где схему инструментов отдают модели как есть: разбор в датакласс и
    обратная сборка потеряли бы поля, которых нет в `McpTool`.

    Args:
        name: имя конфигурации.

    Returns:
        list[dict]: инструменты как записаны; нет файла, нет ключа или файл битый —
        пустой список.
    """
    filename = _mcp_config_filename(name)
    if not os.path.exists(filename):
        return []

    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('tools', [])
    except Exception as e:
        backtrace = traceback.format_exc()
        logger_info(
            f"❌ Ошибка при чтении MCP конфига: {e}."
            f"Полный стек вызовов:\n{backtrace}"
        )
        return []


def mcp_config_save(name: str, config: McpConfig):
    """
    Сохраняет список конфигураций MCP серверов в JSON файл.
    Принимает список словарей.
    """
    filename = _mcp_config_filename(name)

    file_path = Path(filename)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(filename, 'w', encoding='utf-8') as f:
            dict_data = asdict(config)
            json_string = json.dumps(dict_data, indent=4, ensure_ascii=False)
            logger_info(f"💾 Сохраняем MCP конфиг: {json_string}")
            json.dump(dict_data, f, ensure_ascii=False, indent=2)
        logger_info(f"Конфигурация MCP успешно сохранена в {filename}")
    except Exception as e:
        backtrace = traceback.format_exc()
        logger_info(
            f"❌ Ошибка при сохранении MCP конфига: {e}."
            f"Полный стек вызовов:\n{backtrace}"
        )


def mcp_config_delete(name: str):
    """Удалить файл конфигурации MCP-сервера.

    Args:
        name: имя конфигурации.

    Returns:
        Всегда `None` — и когда файл удалён, и когда его не было.
    """
    filename = _mcp_config_filename(name)
    file_path = Path(filename)
    if file_path.exists():
        return file_path.unlink()

    return None


def _mcp_config_dir() -> str:
    return '/'.join([
        config_get('data_dir'),
        config_get('mcp_dir')
    ])


def _mcp_config_filename(name: str) -> str:
    return '/'.join([
        _mcp_config_dir(),
        name + '.json'
    ])
