"""mcp-atlassian под личными токенами пользователей Open WebUI.

Open WebUI подключается к серверу в режиме «Session» и присылает токен сессии
пользователя. По нему из Open WebUI берутся личные настройки инструмента-формы
(токены Jira и Confluence) и подставляются в запрос как per-request PAT
mcp-atlassian. Общей учётки нет: без токена пользователя Jira/Confluence
недоступны.
"""

import asyncio
import json
import os
import sys
from urllib.parse import urlparse

import httpx

OWUI_URL = os.getenv("OWUI_URL", "").rstrip("/")
OWUI_TOOL_ID = os.getenv("OWUI_TOOL_ID", "atlassian_tokens")
# сервис -> (адрес, поле в форме Open WebUI)
SERVICES = {
    name: (os.getenv(f"{name.upper()}_URL", "").rstrip("/"), f"{name}_token")
    for name in ("jira", "confluence")
}
SERVICES = {name: s for name, s in SERVICES.items() if s[0]}

MISSING = "missing"  # заглушка вместо токена: Jira её не примет, вызов перехватывается ниже
HINT = (
    "Личный токен {title} не задан. Откройте в чате «Интеграции → Инструменты → ⚙ "
    "у «Ключи Jira и Confluence»», вставьте токен из профиля {title} "
    "(«Личные токены доступа») и повторите запрос."
)

# Любой из этих параметров включает общую учётку или отключает личные токены.
FORBIDDEN_ENV = [
    "JIRA_USERNAME", "JIRA_API_TOKEN", "JIRA_PERSONAL_TOKEN",
    "JIRA_CLIENT_CERT", "JIRA_OAUTH_CLIENT_ID",
    "CONFLUENCE_USERNAME", "CONFLUENCE_API_TOKEN", "CONFLUENCE_PERSONAL_TOKEN",
    "CONFLUENCE_CLIENT_CERT", "CONFLUENCE_OAUTH_CLIENT_ID",
    "ATLASSIAN_OAUTH_CLIENT_ID", "ATLASSIAN_OAUTH_ACCESS_TOKEN",
    "ATLASSIAN_OAUTH_ENABLE", "ATLASSIAN_EXTERNAL_AUTH_ENABLE",
    "ALLOW_GLOBAL_CRED_FALLBACK", "IGNORE_HEADER_AUTH",
]

# ponytail: один запрос в Open WebUI на каждый запрос MCP; кэш на минуту, если нагрузка вырастет.
_client = httpx.AsyncClient(timeout=10)


async def _reply(send, status: int, body: dict) -> None:
    data = json.dumps(body, ensure_ascii=False).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(data)).encode())],
    })
    await send({"type": "http.response.body", "body": data})


async def _read_body(receive) -> tuple[bytes, list]:
    messages, chunks = [], []
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    return b"".join(chunks), messages


class OpenWebUITokens:
    """ASGI-обёртка перед mcp-atlassian: меняет сессию Open WebUI на личные PAT."""

    def __init__(self, app):
        self.app = app

    def __getattr__(self, name):  # FastMCP читает app.state у возвращённого приложения
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        auth = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        if not auth.startswith("Bearer ") or not auth[7:].strip():
            return await _reply(send, 401, {"error": "Нужна сессия Open WebUI (режим Session)"})

        try:
            r = await _client.get(
                f"{OWUI_URL}/api/v1/tools/id/{OWUI_TOOL_ID}/valves/user",
                headers={"Authorization": auth},
            )
        except httpx.HTTPError:
            return await _reply(send, 502, {"error": "Open WebUI недоступен"})
        if r.status_code in (401, 403):
            return await _reply(send, 401, {"error": "Сессия Open WebUI недействительна"})
        if r.status_code != 200:
            return await _reply(send, 502, {"error": f"Open WebUI ответил {r.status_code}"})
        valves = r.json() or {}

        # Входящие учётные заголовки не пропускаем: подставляем только свои.
        headers = [(k, v) for k, v in scope["headers"]
                   if k != b"authorization" and not k.startswith(b"x-atlassian-")]
        missing = []
        for name, (url, field) in SERVICES.items():
            token = str(valves.get(field) or "").strip()
            if not token:
                missing.append(name)
            headers += [
                (f"x-atlassian-{name}-url".encode(), url.encode()),
                (f"x-atlassian-{name}-personal-token".encode(), (token or MISSING).encode("latin-1")),
            ]
        scope = {**scope, "headers": headers}

        if missing and scope["method"] == "POST":
            body, messages = await _read_body(receive)
            try:
                rpc = json.loads(body)
            except ValueError:
                rpc = None
            tool = ""
            if isinstance(rpc, dict) and rpc.get("method") == "tools/call":
                tool = str(rpc.get("params", {}).get("name", ""))
            for name in missing:
                if tool.startswith(f"{name}_"):
                    return await _reply(send, 200, {
                        "jsonrpc": "2.0",
                        "id": rpc.get("id"),
                        "result": {
                            "content": [{"type": "text", "text": HINT.format(title=name.capitalize())}],
                            "isError": True,
                        },
                    })

            async def replay():
                return messages.pop(0) if messages else await receive()

            return await self.app(scope, replay, send)

        return await self.app(scope, receive, send)


def _check_config() -> None:
    errors = [f"задан {name}: общая учётка запрещена" for name in FORBIDDEN_ENV if os.getenv(name)]
    if not OWUI_URL:
        errors.append("не задан OWUI_URL")
    if not SERVICES:
        errors.append("не задан ни JIRA_URL, ни CONFLUENCE_URL")
    hosts = []
    for name, (url, _) in SERVICES.items():
        host = urlparse(url).hostname or ""
        if not host or host.replace(".", "").isdigit() or ":" in host:
            errors.append(f"{name.upper()}_URL: нужно имя сервера, а не IP-адрес (IP отсекает защита mcp-atlassian)")
        hosts.append(host)
    if errors:
        sys.exit("Ошибка настройки: " + "; ".join(errors))
    # Внутренние адреса mcp-atlassian пропускает только по явному списку.
    os.environ.setdefault("MCP_ALLOWED_URL_DOMAINS", ",".join(hosts))
    os.environ.setdefault("TRANSPORT", "streamable-http")


def _exclude_tools() -> None:
    """EXCLUDED_TOOLS -> ENABLED_TOOLS: в mcp-atlassian есть только список разрешённых."""
    excluded = {n.strip() for n in os.getenv("EXCLUDED_TOOLS", "").split(",") if n.strip()}
    if not excluded:
        return
    from mcp_atlassian.servers import main_mcp

    names = {tool.name for tool in asyncio.run(main_mcp.list_tools())}
    if excluded - names:
        sys.exit(f"Ошибка настройки: в EXCLUDED_TOOLS неизвестные инструменты: {', '.join(sorted(excluded - names))}")
    os.environ["ENABLED_TOOLS"] = ",".join(sorted(names - excluded))


def main() -> None:
    _check_config()
    _exclude_tools()
    from mcp_atlassian import main as mcp_main
    from mcp_atlassian.servers.main import AtlassianMCP

    original = AtlassianMCP.http_app

    def http_app(self, *args, **kwargs):
        return OpenWebUITokens(original(self, *args, **kwargs))

    AtlassianMCP.http_app = http_app  # версия mcp-atlassian закреплена в Dockerfile
    mcp_main()


if __name__ == "__main__":
    main()
