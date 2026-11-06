"""Transfer chat images without exposing Open WebUI credentials to tools or models."""

import asyncio
import hashlib
import io
import os
from typing import Annotated
from uuid import UUID

import httpx
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from pydantic import Field
from urllib3.util.retry import Retry

from mcp_atlassian.servers.confluence import confluence_mcp
from mcp_atlassian.servers.dependencies import get_confluence_fetcher, get_jira_fetcher
from mcp_atlassian.servers.jira import ISSUE_KEY_PATTERN, jira_mcp
from mcp_atlassian.utils.decorators import check_write_access

OWUI_URL = os.getenv('OWUI_URL', '').rstrip('/')
MAX_IMAGE_BYTES = 10 * 1024 * 1024
FileId = Annotated[UUID, Field(description=(
    'Full Open WebUI file UUID from the user attachment metadata (id or url). '
    'Not a sandbox path or a UUID guessed from pasted_<prefix>.png. '
    'Use for images uploaded/pasted in the chat; sandbox-only generated files must '
    'first be uploaded to Open WebUI.'
))]
IssueKey = Annotated[str, Field(pattern=ISSUE_KEY_PATTERN, description='Jira issue key')]
ContentId = Annotated[str, Field(pattern=r'^\d+$', description='Confluence page ID')]


async def read_openwebui_image(file_id: UUID) -> tuple[bytes, str]:
    """Read only from the configured origin, with the current HTTP request's identity."""
    auth = getattr(get_http_request().state, 'owui_authorization', '')
    if not auth:
        raise ToolError('Нет сессии Open WebUI для получения файла.')
    url = f'{OWUI_URL}/api/v1/files/{file_id}/content'
    try:
        async with asyncio.timeout(30), httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            async with client.stream('GET', url, headers={'Authorization': auth}) as response:
                if response.status_code != 200:
                    raise ToolError(f'Open WebUI не выдал файл (HTTP {response.status_code}). '
                                    'Проверьте доступ к вложению и его идентификатор.')
                length = response.headers.get('content-length', '')
                if length.isdigit() and int(length) > MAX_IMAGE_BYTES:
                    raise ToolError('Картинка превышает лимит 10 МиБ.')
                content = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    if len(content) + len(chunk) > MAX_IMAGE_BYTES:
                        raise ToolError('Картинка превышает лимит 10 МиБ.')
                    content.extend(chunk)
    except (httpx.HTTPError, TimeoutError):
        raise ToolError('Не удалось получить файл из Open WebUI: ошибка сети или тайм-аут.') from None

    data = bytes(content)
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        extension = 'png'
    elif data.startswith(b'\xff\xd8\xff'):
        extension = 'jpg'
    elif data.startswith((b'GIF87a', b'GIF89a')):
        extension = 'gif'
    elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        extension = 'webp'
    else:
        raise ToolError('Файл не распознан как PNG, JPEG, GIF или WebP; загрузка отменена.')
    return data, extension


def _result(file_id: UUID, data: bytes, filename: str, attachment_id: str) -> dict:
    return {'success': True, 'file_id': str(file_id), 'filename': filename,
            'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'attachment_id': attachment_id}


def _upload_error(service: str) -> ToolError:
    return ToolError(f'Загрузка в {service} не подтверждена. Текст не изменён. '
                     'Перед повтором проверьте вложения: файл мог сохраниться. '
                     'Проверьте также права на добавление вложений и лимит размера.')


def _disable_upload_retries(client) -> None:
    # Clients belong to this HTTP request; preserve the SSRF-protecting adapters.
    client.retry_with_header = False
    client.backoff_and_retry = False
    for adapter in client._session.adapters.values():
        adapter.max_retries = Retry(total=0)


async def upload_jira_chat_attachment(ctx: Context, issue_key: IssueKey, file_id: FileId) -> dict:
    """Attach a chat image to Jira using its Open WebUI file ID, without local paths/Base64.

    Returns jira_wiki only after confirmed upload. Then use jira_update_issue to
    insert it into the description while preserving existing text. This tool does
    not edit the description. Do not retry uncertain uploads blindly: Jira may
    create duplicate attachments. Images only, at most 10 MiB.
    """
    data, extension = await read_openwebui_image(file_id)
    filename = f'chat-{file_id}.{extension}'
    jira = await get_jira_fetcher(ctx)
    _disable_upload_retries(jira.jira)
    with io.BytesIO(data) as file:
        file.name = filename
        try:
            attachments = await asyncio.to_thread(jira.jira.add_attachment_object, issue_key, file)
        except Exception:
            raise _upload_error('Jira') from None
    if (not isinstance(attachments, list) or len(attachments) != 1
            or not isinstance(attachments[0], dict)
            or attachments[0].get('filename') != filename or not attachments[0].get('id')):
        raise _upload_error('Jira')
    result = _result(file_id, data, filename, str(attachments[0]['id']))
    return {**result, 'issue_key': issue_key, 'jira_wiki': f'!{filename}|thumbnail!',
            'description_updated': False}


async def upload_confluence_chat_attachment(ctx: Context, content_id: ContentId, file_id: FileId) -> dict:
    """Attach a chat image to a Confluence page by Open WebUI file ID, without paths/Base64.

    Returns confluence_storage markup after confirmed upload. To display the image,
    use confluence_update_page with content_format="storage" and preserve the page's existing
    storage body. This tool does not edit the page. Inspect attachments before retrying
    an uncertain upload. Images only, at most 10 MiB.
    """
    data, extension = await read_openwebui_image(file_id)
    filename = f'chat-{file_id}.{extension}'
    confluence = await get_confluence_fetcher(ctx)
    _disable_upload_retries(confluence.confluence)
    try:
        # The public client supplies its configured timeout; the bytes helper does not.
        response = await asyncio.to_thread(
            confluence.confluence.post, f'rest/api/content/{content_id}/child/attachment',
            headers={'X-Atlassian-Token': 'no-check'},
            files={'file': (filename, data)}, data={'minorEdit': 'true'},
        )
    except Exception:
        raise _upload_error('Confluence') from None
    attachments = response.get('results') if isinstance(response, dict) else None
    if (not isinstance(attachments, list) or len(attachments) != 1
            or not isinstance(attachments[0], dict)
            or attachments[0].get('title') != filename or not attachments[0].get('id')):
        raise _upload_error('Confluence')
    result = _result(file_id, data, filename, str(attachments[0]['id']))
    return {**result, 'content_id': content_id, 'page_updated': False,
            'confluence_storage': f'<ac:image><ri:attachment ri:filename="{filename}" /></ac:image>'}


def register_tools() -> None:
    for mcp, service, handler in (
        (jira_mcp, 'jira', upload_jira_chat_attachment),
        (confluence_mcp, 'confluence', upload_confluence_chat_attachment),
    ):
        mcp.tool(name='upload_chat_attachment',
                 tags={service, 'write', f'toolset:{service}_attachments'},
                 annotations={'title': 'Upload Chat Image', 'readOnlyHint': False,
                              'destructiveHint': False, 'idempotentHint': False})(check_write_access(handler))
