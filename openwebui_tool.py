"""
title: Ключи Jira и Confluence
description: Личные токены доступа. Хранятся в ваших настройках, в чат не попадают.
"""

from pydantic import BaseModel, Field


class Tools:
    # Функций нет: инструмент нужен только ради личной формы с токенами.
    # Токены отсюда забирает сервер atlassian-mcp (по коду инструмента OWUI_TOOL_ID).
    class UserValves(BaseModel):
        jira_token: str = Field(
            default="",
            description="Токен Jira: профиль → Личные токены доступа",
            json_schema_extra={"input": {"type": "password"}},
        )
        confluence_token: str = Field(
            default="",
            description="Токен Confluence: профиль → Личные токены доступа",
            json_schema_extra={"input": {"type": "password"}},
        )
