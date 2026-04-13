import contextlib
import inspect
import os
import traceback
from typing import Callable, Dict, Any

from mcp import ClientSession, StdioServerParameters, stdio_client
from ai.framework.ai_framework import AiFrameworkModel
from logging_.logging_ import logger_info
from mcp_.mcp_ import mcp_config_tools_json_get, mcp_config_get


class AiMcpManager:
    def __init__(self):
        self.exit_stack = contextlib.AsyncExitStack()  # Для авто-закрытия всех серверов
        self.sessions: dict[str, ClientSession] = {}

    async def mcp_server_add_tools(self, engine, framework_model: AiFrameworkModel):
        try:
            mcp_sessions = await self._mcp_server_session(framework_model.mcp_servers)
            mcp_tools_added = []
            for mcp_server_name, session in mcp_sessions.items():
                mcp_config_tools_json = mcp_config_tools_json_get(mcp_server_name)
                for tool_json in mcp_config_tools_json:

                    if not tool_json.get('name', None) in framework_model.tools:
                        continue

                    mcp_tool_func = self._mcp_tool_func_from_json(mcp_server_name, tool_json, session)

                    mcp_tools_added.append(
                        f"🔹 {mcp_tool_func.__name__}{inspect.signature(mcp_tool_func)}"
                        f"- {tool_json['description']}"
                    )

                    engine.tool_plain(mcp_tool_func)

            logger_info('🛠️ MCP инструменты добавлены: \n' + ',\n'.join(mcp_tools_added))
        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Ошибка при добавлении инструментов для mcp серверов: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )
            raise

    async def mcp_server_close_all(self):
        await self.exit_stack.aclose()
        self.sessions.clear()

    def mcp_tool_func_from_json(self, mcp_tool_json: dict[str, Any]) -> Callable:
        func = lambda **kwargs: f"Вызван инструмент с аргументами: {kwargs}"
        return self._mcp_tool_decorator_from_json_input_schema(func, mcp_tool_json)

    def _mcp_tool_func_from_json(self, mcp_server_name: str, tool_json: dict[str, Any], session) -> Callable:
        async def mcp_tool_decorator(**kwargs) -> str:
            logger_info(f"🛠️ Инструмент '{tool_json['name']}' вызов → ({kwargs})")
            result = await session.call_tool(tool_json['name'], kwargs)
            logger_info(f"🛠️ Инструмент '{tool_json['name']}' результат  → {repr(result)}")

            return result

        return self._mcp_tool_decorator_from_json_input_schema(mcp_tool_decorator, tool_json)

    async def _mcp_server_session(self, mcp_servers: list[str]):
        logger_info(f"📡 Создание сессий для mcp серверов")

        try:
            for mcp_server_name in mcp_servers:
                mcp_server_name = mcp_server_name.lower()
                mcp_config = mcp_config_get(mcp_server_name)
                if not mcp_config:
                    raise ValueError(f'MCP конфиг для "{mcp_server_name}" не найден')

                logger_info(f"📡 Создаем сессию для mcp сервера {mcp_server_name}")

                envs: dict[str, str] = {}
                for i in mcp_config.env:
                    value = os.getenv(i.upper())
                    if not value:
                        raise ValueError(f"MCP сервер '{mcp_config.title}' не может найти env: {i.upper()}")
                    envs[i] = value

                server_params = StdioServerParameters(
                    command=mcp_config.cmd,
                    args=mcp_config.args,
                    env=envs if envs else None
                )
                # Открываем stdio и сессию, добавляем в стек для очистки в конце
                read, write = await self.exit_stack.enter_async_context(stdio_client(server_params))
                session = await self.exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()

                logger_info(f"📡 Cессия для mcp сервера '{mcp_config.title}' создана и инициализирована")
                self.sessions[mcp_server_name] = session
            return self.sessions
        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Ошибка при создании сессий для mcp серверов: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )
            raise

    def _mcp_tool_decorator_from_json_input_schema(
            self,
            mcp_tool_decorator: Callable,
            json_input_schema: Dict[str, Any]
    ) -> Callable:

        tool_name = json_input_schema['name']
        tool_desc = json_input_schema.get('description', f"MCP Tool: {tool_name}")
        schema = json_input_schema.get('inputSchema', json_input_schema.get('input_schema', {}))
        properties = schema.get('properties', {})
        required = schema.get('required', [])

        # 1. Создаем "чистую" обертку через замыкание (никакого exec!)
        # Это гарантирует, что mcp_tool_decorator (наш _callback) всегда будет доступен
        async def final_tool(*args, **kwargs):
            # Сопоставляем аргументы с сигнатурой
            sig = inspect.signature(final_tool)
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()

            # Удаляем None значения (как ты хотел)
            payload = {k: v for k, v in bound.arguments.items() if v is not None}

            # Вызываем оригинальный декоратор (он доступен тут благодаря замыканию)
            return await mcp_tool_decorator(**payload)

        # 2. Формируем список параметров для подмены сигнатуры
        params = []

        # Сортируем: сначала обязательные
        all_names = list(properties.keys())
        sorted_names = [n for n in all_names if n in required] + [n for n in all_names if n not in required]

        for p_name in sorted_names:
            p_info = properties[p_name]
            is_req = p_name in required

            # Определяем тип для аннотаций
            type_map = {"string": str, "number": float, "integer": int, "boolean": bool, "array": list, "object": dict}
            py_type = type_map.get(p_info.get("type"), Any)

            params.append(
                inspect.Parameter(
                    name=p_name,
                    kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=inspect.Parameter.empty if is_req else p_info.get("default"),
                    annotation=py_type
                )
            )

        # 3. Подменяем метаданные у функции final_tool
        final_tool.__signature__ = inspect.Signature(params)
        final_tool.__name__ = tool_name
        final_tool.__doc__ = tool_desc
        final_tool.__annotations__ = {p.name: p.annotation for p in params}
        final_tool.__annotations__['return'] = Any

        return final_tool
