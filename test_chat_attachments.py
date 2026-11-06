"""Offline boundary checks: python -m unittest test_chat_attachments -v."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import httpx
from fastmcp.exceptions import ToolError

import chat_attachments as chat

FILE_ID = UUID('11111111-1111-4111-8111-111111111111')
PNG = b'\x89PNG\r\n\x1a\n' + b'image bytes'


class ChunkStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield PNG
        yield b'x' * 20


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def download(self, response, auth='Bearer session-a'):
        calls = []

        def respond(request):
            calls.append(request)
            return response

        client_class = httpx.AsyncClient
        with patch.object(chat, 'OWUI_URL', 'http://webui.test'), \
             patch.object(chat, 'get_http_request', return_value=SimpleNamespace(
                 state=SimpleNamespace(owui_authorization=auth))), \
             patch.object(chat.httpx, 'AsyncClient', side_effect=lambda **kw: client_class(
                 transport=httpx.MockTransport(respond), **kw)):
            result = await chat.read_openwebui_image(FILE_ID)
        return result, calls

    async def test_download_uses_session_and_fixed_url(self):
        (content, extension), calls = await self.download(httpx.Response(200, content=PNG))
        self.assertEqual((content, extension), (PNG, 'png'))
        self.assertEqual(str(calls[0].url), f'http://webui.test/api/v1/files/{FILE_ID}/content')
        self.assertEqual(calls[0].headers['Authorization'], 'Bearer session-a')

    async def test_authorization_required(self):
        with self.assertRaises(ToolError):
            await self.download(httpx.Response(200, content=PNG), auth='')

    async def test_http_errors_and_redirect_are_rejected(self):
        for status in (401, 403, 404, 500, 302):
            with self.subTest(status=status), self.assertRaises(ToolError):
                await self.download(httpx.Response(status, headers={'location': 'http://evil.test'}))

    async def test_empty_or_nonimage_rejected(self):
        for body in (b'', b'<html>login</html>', b'%PDF-1.4'):
            with self.subTest(body=body), self.assertRaises(ToolError):
                await self.download(httpx.Response(200, content=body))

    async def test_limit_includes_stream_without_content_length(self):
        with patch.object(chat, 'MAX_IMAGE_BYTES', 25), self.assertRaises(ToolError):
            await self.download(httpx.Response(200, stream=ChunkStream()))

    async def test_supported_signatures_and_exact_limit(self):
        for body, extension in ((PNG, 'png'), (b'\xff\xd8\xffjpeg', 'jpg'),
                                (b'GIF89aimage', 'gif'), (b'RIFF1234WEBPimage', 'webp')):
            with self.subTest(extension=extension), patch.object(chat, 'MAX_IMAGE_BYTES', len(body)):
                result, _ = await self.download(httpx.Response(200, content=body))
                self.assertEqual(result, (body, extension))

    async def test_declared_size_limit(self):
        with patch.object(chat, 'MAX_IMAGE_BYTES', 25), self.assertRaises(ToolError):
            await self.download(httpx.Response(200, content=PNG, headers={'content-length': '26'}))

    async def test_download_timeout_has_no_secrets(self):
        def fail(request):
            raise httpx.ReadTimeout('Bearer SECRET', request=request)

        client_class = httpx.AsyncClient
        with patch.object(chat, 'OWUI_URL', 'http://webui.test'), \
             patch.object(chat, 'get_http_request', return_value=SimpleNamespace(
                 state=SimpleNamespace(owui_authorization='Bearer SECRET'))), \
             patch.object(chat.httpx, 'AsyncClient', side_effect=lambda **kw: client_class(
                 transport=httpx.MockTransport(fail), **kw)), \
             self.assertRaises(ToolError) as caught:
            await chat.read_openwebui_image(FILE_ID)
        self.assertNotIn('SECRET', str(caught.exception))


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_exceptions_are_not_retried_or_exposed(self):
        for function, service, method in (
            (chat.upload_jira_chat_attachment, 'jira', 'add_attachment_object'),
            (chat.upload_confluence_chat_attachment, 'confluence', 'post'),
        ):
            upload = Mock(side_effect=TimeoutError('SECRET'))
            client = SimpleNamespace(**{method: upload}, _session=SimpleNamespace(adapters={}))
            fetcher = SimpleNamespace(**{service: client})
            with self.subTest(service=service), \
                 patch.object(chat, 'read_openwebui_image', new=AsyncMock(return_value=(PNG, 'png'))), \
                 patch.object(chat, f'get_{service}_fetcher', new=AsyncMock(return_value=fetcher)), \
                 self.assertRaises(ToolError) as caught:
                await function(None, '123', FILE_ID)
            self.assertEqual(upload.call_count, 1)
            self.assertNotIn('SECRET', str(caught.exception))

    async def test_jira_bytes_and_confirmed_name(self):
        uploads = []

        def upload(key, file):
            uploads.append((key, file.name, file.read()))
            return [{'id': '10', 'filename': file.name, 'size': len(PNG)}]

        fetcher = SimpleNamespace(jira=SimpleNamespace(add_attachment_object=upload,
                                  _session=SimpleNamespace(adapters={})))
        with patch.object(chat, 'read_openwebui_image', new=AsyncMock(return_value=(PNG, 'png'))), \
             patch.object(chat, 'get_jira_fetcher', new=AsyncMock(return_value=fetcher)):
            result = await chat.upload_jira_chat_attachment(None, 'TEST-1', FILE_ID)
        self.assertEqual(uploads, [('TEST-1', f'chat-{FILE_ID}.png', PNG)])
        self.assertEqual(result['attachment_id'], '10')
        self.assertEqual(result['jira_wiki'], f'!chat-{FILE_ID}.png|thumbnail!')
        self.assertFalse(result['description_updated'])

    async def test_confluence_bytes_and_markup(self):
        uploads = []

        def upload(path, *, headers, files, data):
            filename, content = files['file']
            uploads.append((path, filename, content))
            self.assertEqual(headers['X-Atlassian-Token'], 'no-check')
            return {'results': [{'id': '20', 'title': filename}]}

        fetcher = SimpleNamespace(confluence=SimpleNamespace(post=upload,
                                       _session=SimpleNamespace(adapters={})))
        with patch.object(chat, 'read_openwebui_image', new=AsyncMock(return_value=(PNG, 'png'))), \
             patch.object(chat, 'get_confluence_fetcher', new=AsyncMock(return_value=fetcher)):
            result = await chat.upload_confluence_chat_attachment(None, '123', FILE_ID)
        self.assertEqual(uploads, [('rest/api/content/123/child/attachment', f'chat-{FILE_ID}.png', PNG)])
        self.assertIn(f'ri:filename="chat-{FILE_ID}.png"', result['confluence_storage'])
        self.assertFalse(result['page_updated'])

    async def test_confluence_failed_upload_does_not_claim_success(self):
        fetcher = SimpleNamespace(confluence=SimpleNamespace(post=lambda *args, **kw: {
            'success': False, 'error': 'SECRET'}, _session=SimpleNamespace(adapters={})))
        with patch.object(chat, 'read_openwebui_image', new=AsyncMock(return_value=(PNG, 'png'))), \
             patch.object(chat, 'get_confluence_fetcher', new=AsyncMock(return_value=fetcher)), \
             self.assertRaises(ToolError) as caught:
            await chat.upload_confluence_chat_attachment(None, '123', FILE_ID)
        self.assertNotIn('SECRET', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
