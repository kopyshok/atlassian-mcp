"""Сквозная проверка: Open WebUI, Jira и Confluence заменены заглушкой.

Запуск: python test_e2e.py  (нужен установленный mcp-atlassian из Dockerfile)
Проверяет, что вызовы идут строго с личным токеном пользователя, а без токена
или без сессии ничего не выполняется.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

SESSIONS = {  # сессия Open WebUI -> личные настройки формы
    "sess-alice": {"jira_token": "PAT-ALICE-J", "confluence_token": "PAT-ALICE-C"},
    "sess-bob": None,  # пользователь ещё не заполнял форму
}
TOKENS = {"/jira/": "Bearer PAT-ALICE-J", "/wiki/": "Bearer PAT-ALICE-C"}
seen = []  # (метод, путь, Authorization, тело)

ISSUE = {"id": "10001", "key": "TEST-1", "fields": {
    "summary": "Привет", "status": {"name": "Open"}, "issuetype": {"name": "Task"},
    "customfield_10010": "значение доп. поля"}}
PAGE = {"id": "123", "type": "page", "title": "Страница", "space": {"key": "S", "name": "S"},
        "version": {"number": 1}, "body": {"storage": {"value": "<p>текст</p>", "representation": "storage"}}}


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body=None):
        data = b"" if code == 204 else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        auth = self.headers.get("Authorization", "")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        path = self.path.split("?")[0]
        if path.startswith("/api/v1/tools/id/atlassian_tokens/valves/user"):
            session = auth.removeprefix("Bearer ")
            return self._send(200, SESSIONS[session]) if session in SESSIONS else self._send(401, {})
        seen.append((self.command, path, auth, body))
        prefix = next((p for p in TOKENS if path.startswith(p)), None)
        if prefix is None or auth != TOKENS[prefix]:
            return self._send(401, {"message": "bad token"})
        rest = path[len(prefix) - 1:]
        if rest.endswith("/myself") or rest.endswith("/user/current"):
            return self._send(200, {"name": "alice", "key": "alice", "displayName": "Alice",
                                    "username": "alice", "accountId": "alice"})
        if rest == "/rest/api/2/serverInfo":
            return self._send(200, {"version": "11.0.0", "deploymentType": "Server"})
        if rest == "/rest/api/2/field":
            return self._send(200, [{"id": "summary", "name": "Summary", "schema": {"type": "string"}}])
        if rest == "/rest/api/2/issue/TEST-1":
            return self._send(204) if self.command == "PUT" else self._send(200, ISSUE)
        if rest == "/rest/api/content/123":
            return self._send(200, PAGE)
        return self._send(404, {"message": f"stub: {self.command} {rest}"})

    do_GET = do_POST = do_PUT = _handle


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def call(session, tool, args):
    transport = StreamableHttpTransport(URL, headers={"Authorization": f"Bearer {session}"})
    async with Client(transport) as client:
        if tool is None:
            return [t.name for t in await client.list_tools()]
        result = await client.call_tool(tool, args, raise_on_error=False)
        return result.is_error, "".join(getattr(c, "text", "") for c in result.content)


async def checks():
    # 1. Инструменты Jira и Confluence видны пользователю.
    names = await call("sess-alice", None, None)
    for name in ("jira_get_issue", "jira_update_issue", "confluence_get_page"):
        assert name in names, f"нет инструмента {name}"
    assert "jira_delete_issue" not in names

    # Скрытый инструмент нельзя вызвать и по имени.
    seen.clear()
    is_error, text = await call("sess-alice", "jira_delete_issue", {"issue_key": "TEST-1"})
    assert is_error and not seen, (text, seen)

    # 2. Чтение задачи со всеми полями — личным токеном Jira.
    seen.clear()
    is_error, text = await call("sess-alice", "jira_get_issue", {"issue_key": "TEST-1", "fields": "*all"})
    assert not is_error and "TEST-1" in text, text
    assert seen and all(a == "Bearer PAT-ALICE-J" for _, _, a, _ in seen), seen

    # 3. Запись поля — тоже под пользователем.
    seen.clear()
    is_error, text = await call("sess-alice", "jira_update_issue",
                                {"issue_key": "TEST-1",
                                 "fields": json.dumps({"summary": "Новое название"}, ensure_ascii=False)})
    puts = [b for m, p, a, b in seen if m == "PUT" and a == "Bearer PAT-ALICE-J"]
    assert puts and json.loads(puts[0])["fields"]["summary"] == "Новое название", (is_error, text, seen)

    # 4. Confluence — отдельным токеном Confluence.
    seen.clear()
    is_error, text = await call("sess-alice", "confluence_get_page", {"page_id": "123"})
    assert not is_error and "Страница" in text, text
    assert seen and all(a == "Bearer PAT-ALICE-C" for _, _, a, _ in seen), seen

    # 5. Без токена — подсказка, в Jira ничего не уходит.
    seen.clear()
    is_error, text = await call("sess-bob", "jira_get_issue", {"issue_key": "TEST-1"})
    assert is_error and "токен Jira не задан" in text, text
    assert not seen, seen

    # 6. Без сессии и с чужой сессией — отказ.
    async with httpx.AsyncClient() as http:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        assert (await http.post(URL, json=init)).status_code == 401
        r = await http.post(URL, json=init, headers={"Authorization": "Bearer sess-mallory"})
        assert r.status_code == 401


if __name__ == "__main__":
    stub_port, mcp_port = free_port(), free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", stub_port), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    # mcp-atlassian не ходит на «localhost» и IP-адреса; «localhost.» с точкой — то же
    # самое имя, которое его защита не узнаёт (Windows). В Linux такое имя не
    # распознаётся: там STUB_HOST задаётся через --add-host. Сеть наружу не открывается.
    host = os.getenv("STUB_HOST", "localhost.")
    URL = f"http://127.0.0.1:{mcp_port}/mcp"
    env = {**os.environ, "OWUI_URL": f"http://127.0.0.1:{stub_port}",
           "JIRA_URL": f"http://{host}:{stub_port}/jira",
           "CONFLUENCE_URL": f"http://{host}:{stub_port}/wiki",
           "EXCLUDED_TOOLS": "jira_delete_issue",
           "HOST": "127.0.0.1", "PORT": str(mcp_port)}
    server = subprocess.Popen([sys.executable, str(Path(__file__).with_name("server.py"))], env=env)
    try:
        for _ in range(100):
            try:
                httpx.get(URL, timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.3)
        asyncio.run(checks())
        print("OK: все проверки прошли")
    finally:
        server.terminate()
        stub.shutdown()
