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
    if not os.path.exists(_mcp_config_dir()):
        return []

    return [os.path.splitext(f)[0] for f in os.listdir(_mcp_config_dir()) if f.endswith('.json')]


def mcp_config_get(name: str) -> McpConfig | None:
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
