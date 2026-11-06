"""Real HTTP MCP + fake services: python test_chat_e2e.py. No corporate network."""

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from email.parser import BytesParser
from email.policy import default
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from test_e2e import Stub, free_port

ALICE_FILE = '11111111-1111-4111-8111-111111111111'
CAROL_FILE = '22222222-2222-4222-8222-222222222222'
DENIED_FILE = '33333333-3333-4333-8333-333333333333'
REDIRECT_FILE = '44444444-4444-4444-8444-444444444444'
HTML_FILE = '55555555-5555-4555-8555-555555555555'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=')
FILES = {ALICE_FILE: ('sess-alice', PNG), CAROL_FILE: ('sess-carol', PNG + b'other')}
SESSIONS = {
    'sess-alice': {'jira_token': 'PAT-A-J', 'confluence_token': 'PAT-A-C'},
    'sess-carol': {'jira_token': 'PAT-C-J', 'confluence_token': 'PAT-C-C'},
    'sess-empty': None,
}
traffic, uploads, downloads = [], [], []
issue_description = ''
page_body = ''


class ChatStub(Stub):
    def _handle(self):
        global issue_description, page_body
        auth = self.headers.get('Authorization', '')
        path = self.path.split('?')[0]
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if path.startswith('/api/v1/tools/'):
            session = auth.removeprefix('Bearer ')
            return self._send(200, SESSIONS[session]) if session in SESSIONS else self._send(401, {})
        traffic.append((self.command, path, auth))
        if path.startswith('/api/v1/files/'):
            downloads.append((path, auth))
            fid = path.split('/')[4]
            if fid == REDIRECT_FILE:
                self.send_response(302)
                self.send_header('Location', '/redirect-target')
                self.end_headers()
                return
            if fid == HTML_FILE:
                data = b'<html>login required</html>'
            elif fid not in FILES or auth != 'Bearer ' + FILES[fid][0]:
                return self._send(403, {'detail': 'No access'})
            else:
                data = FILES[fid][1]
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        service = 'jira' if path.startswith('/jira/') else 'confluence'
        allowed = {f'Bearer {v[service + "_token"]}' for v in SESSIONS.values() if v}
        if auth not in allowed:
            return self._send(401, {})
        if path.endswith(('/myself', '/user/current')):
            user = 'alice' if '-A-' in auth else 'carol'
            return self._send(200, {'name': user, 'key': user, 'username': user,
                                    'accountId': user, 'displayName': user})
        if path.endswith('/serverInfo'):
            return self._send(200, {'version': '11.0.0', 'deploymentType': 'Server'})
        if path.endswith('/field'):
            return self._send(200, [])
        if path.endswith('/search'):
            return self._send(200, {'issues': [], 'total': 0})
        if self.command == 'POST' and path.endswith(('/attachments', '/child/attachment')):
            message = BytesParser(policy=default).parsebytes(
                b'Content-Type: ' + self.headers['Content-Type'].encode() + b'\r\n\r\n' + body)
            part = next(p for p in message.iter_parts() if p.get_param('name', header='content-disposition') == 'file')
            name, data = part.get_filename(), part.get_payload(decode=True)
            uploads.append((path, auth, name, data, self.headers.get('X-Atlassian-Token')))
            if ('/RETRY-1/' in path or '/content/429/' in path) and len(uploads) == 1:
                self.send_response(429)
                self.send_header('Retry-After', '0')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if '/FAIL-1/' in path or '/content/403/' in path:
                return self._send(403, {'error': 'cannot attach'})
            if service == 'jira':
                return self._send(200, [{'id': '901', 'filename': name, 'size': len(data)}])
            return self._send(200, {'results': [{'id': '902', 'title': name}]})
        if path == '/jira/rest/api/2/issue/TEST-1':
            if self.command == 'PUT':
                issue_description = json.loads(body)['fields']['description']
                return self._send(204)
            return self._send(200, {'id': '100', 'key': 'TEST-1', 'fields': {
                'summary': 'Test', 'description': issue_description, 'attachment': [],
                'status': {'name': 'Open'}, 'issuetype': {'name': 'Task'}}})
        if path == '/wiki/rest/api/content/123':
            if self.command == 'PUT':
                page_body = json.loads(body)['body']['storage']['value']
            return self._send(200, {'id': '123', 'type': 'page', 'title': 'Test',
                'space': {'key': 'S', 'name': 'S'}, 'version': {'number': 2},
                'body': {'storage': {'value': page_body, 'representation': 'storage'}}})
        if path == '/wiki/rest/api/content/123/history':
            return self._send(200, {'lastUpdated': {'number': 1}})
        return self._send(404, {'error': 'stub route missing'})

    do_GET = do_POST = do_PUT = _handle


async def call(url, session, tool, args):
    async with Client(StreamableHttpTransport(url, headers={'Authorization': f'Bearer {session}'})) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
        text = ''.join(getattr(c, 'text', '') for c in result.content)
        return result.is_error, text


async def checks(url, mode):
    tools = ('jira_upload_chat_attachment', 'confluence_upload_chat_attachment')
    arguments = ({'issue_key': 'TEST-1', 'file_id': ALICE_FILE},
                 {'content_id': '123', 'file_id': ALICE_FILE})
    if mode != 'normal':
        for tool, args in zip(tools, arguments):
            traffic.clear()
            error, text = await call(url, 'sess-alice', tool, args)
            assert error and not traffic, (mode, error, text, traffic)
        return

    async with Client(StreamableHttpTransport(url, headers={'Authorization': 'Bearer sess-alice'})) as client:
        names = {t.name: t for t in await client.list_tools()}
        for tool in tools:
            props = names[tool].inputSchema['properties']
            assert props['file_id']['format'] == 'uuid', props
            assert not ({'authorization', 'token', 'file_path', 'url', 'content_base64'} & props.keys())

    for tool, args in zip(tools, arguments):
        for bad_id in ('../../secret', 'https://evil.test/image', '/mnt/uploads/image.png'):
            traffic.clear()
            error, text = await call(url, 'sess-alice', tool, {**args, 'file_id': bad_id})
            assert error and not traffic, (text, traffic)
        traffic.clear()
        error, text = await call(url, 'sess-empty', tool, args)
        assert error and not traffic, (text, traffic)
        for file_id in (CAROL_FILE, DENIED_FILE, REDIRECT_FILE, HTML_FILE):
            uploads.clear()
            error, text = await call(url, 'sess-alice', tool, {**args, 'file_id': file_id})
            assert error and not uploads, (text, uploads)
        assert not any(p == '/redirect-target' for _, p, _ in traffic)

    # Concurrent requests must use the same person's session for reading and PAT for writing.
    uploads.clear()
    downloads.clear()
    results = await asyncio.gather(*(
        call(url, session, tool, {**args, 'file_id': fid})
        for session, fid in (('sess-alice', ALICE_FILE), ('sess-carol', CAROL_FILE))
        for tool, args in zip(tools, arguments)
    ))
    assert all(not error for error, text in results), results
    assert len(uploads) == len(downloads) == 4, (uploads, downloads)
    for path, auth, name, data, xsrf in uploads:
        fid = ALICE_FILE if '-A-' in auth else CAROL_FILE
        assert name == f'chat-{fid}.png' and data == FILES[fid][1], (auth, name, data)
        assert xsrf == 'no-check', xsrf
        assert auth.endswith('-J') == path.startswith('/jira/'), (path, auth)
    assert all(auth == 'Bearer ' + FILES[path.split('/')[4]][0] for path, auth in downloads)
    for error, text in results:
        result = json.loads(text)
        assert result['sha256'] == hashlib.sha256(FILES[result['file_id']][1]).hexdigest()
        assert 'PAT-' not in text and 'sess-' not in text

    # Reuse one MCP connection while identity changes; spoofed service headers are discarded.
    current = {'session': 'sess-alice'}

    async def authorize(request):
        request.headers['Authorization'] = 'Bearer ' + current['session']
        request.headers['X-Atlassian-Jira-Personal-Token'] = 'SPOOF'
        request.headers['X-Atlassian-Jira-Url'] = 'https://evil.test'

    transport = StreamableHttpTransport(url, httpx_client_factory=lambda **kw: httpx.AsyncClient(
        **kw, event_hooks={'request': [authorize]}))
    async with Client(transport) as client:
        session_id = transport.get_session_id()
        for user, fid in (('sess-alice', ALICE_FILE), ('sess-carol', CAROL_FILE), ('sess-alice', ALICE_FILE)):
            current['session'] = user
            for tool, args in zip(tools, arguments):
                uploads.clear()
                result = await client.call_tool(tool, {**args, 'file_id': fid}, raise_on_error=False)
                assert not result.is_error and len(uploads) == 1, result
                service = 'jira' if tool.startswith('jira') else 'confluence'
                assert uploads[0][1] == 'Bearer ' + SESSIONS[user][service + '_token'], uploads
                assert uploads[0][3] == FILES[fid][1], uploads
                assert transport.get_session_id() == session_id

    # Use existing update tools; verify conversion leaves embedding markup intact.
    jira_result, confluence_result = (json.loads(results[i][1]) for i in (0, 1))
    error, text = await call(url, 'sess-alice', 'jira_update_issue', {
        'issue_key': 'TEST-1', 'fields': json.dumps({'description': 'Keep this text\n\n' + jira_result['jira_wiki']})})
    assert not error and jira_result['jira_wiki'] in issue_description and 'Keep this text' in issue_description, text
    storage = '<p>Keep this text</p>' + confluence_result['confluence_storage']
    error, text = await call(url, 'sess-alice', 'confluence_update_page', {
        'page_id': '123', 'title': 'Test', 'content': storage, 'content_format': 'storage'})
    assert not error and page_body == storage, (text, page_body)

    for tool, args in zip(tools, ({'issue_key': 'FAIL-1'}, {'content_id': '403'})):
        uploads.clear()
        error, text = await call(url, 'sess-alice', tool, {**args, 'file_id': ALICE_FILE})
        assert error and len(uploads) == 1 and 'PAT-' not in text, (text, uploads)

    for tool, args in zip(tools, ({'issue_key': 'RETRY-1'}, {'content_id': '429'})):
        uploads.clear()
        error, text = await call(url, 'sess-alice', tool, {**args, 'file_id': ALICE_FILE})
        assert error and len(uploads) == 1, ('Upload retried after 429', text, len(uploads))


def main():
    port = free_port()
    stub = ThreadingHTTPServer(('127.0.0.1', port), ChatStub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    host = os.getenv('STUB_HOST', 'localhost.')
    try:
        for mode in ('normal', 'read-only', 'excluded'):
            mcp_port = free_port()
            url = f'http://127.0.0.1:{mcp_port}/mcp'
            env = {**os.environ, 'OWUI_URL': f'http://127.0.0.1:{port}',
                   'JIRA_URL': f'http://{host}:{port}/jira',
                   'CONFLUENCE_URL': f'http://{host}:{port}/wiki',
                   'READ_ONLY_MODE': str(mode == 'read-only').lower(),
                   'EXCLUDED_TOOLS': 'jira_upload_chat_attachment,confluence_upload_chat_attachment' if mode == 'excluded' else '',
                   'TOOLSETS': 'default,jira_attachments,confluence_attachments',
                   'ATLASSIAN_RETRY_TOTAL': '1', 'ATLASSIAN_RETRY_INCLUDE_WRITES': 'true',
                   'HOST': '127.0.0.1', 'PORT': str(mcp_port), 'PYTHONIOENCODING': 'utf-8'}
            with tempfile.TemporaryFile() as log:
                process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('server.py'))],
                                           env=env, stdout=log, stderr=log)
                try:
                    for _ in range(100):
                        if process.poll() is not None:
                            raise RuntimeError('MCP exited during startup')
                        try:
                            httpx.get(url, timeout=1)
                            break
                        except httpx.HTTPError:
                            time.sleep(0.1)
                    asyncio.run(checks(url, mode))
                    print(f'OK: chat attachments {mode}')
                except Exception:
                    log.seek(0)
                    print(log.read().decode('utf-8', errors='replace')[-6000:])
                    raise
                finally:
                    process.terminate()
                    process.wait(timeout=10)
    finally:
        stub.shutdown()
        stub.server_close()


if __name__ == '__main__':
    main()
