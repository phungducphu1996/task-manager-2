from __future__ import annotations

from urllib.parse import quote

import httpx

from .config import get_settings


class StorageError(RuntimeError):
    pass


def is_storage_enabled() -> bool:
    settings = get_settings()
    return bool(settings.supabase_url and settings.supabase_service_role_key and settings.supabase_storage_bucket)


def _headers(content_type: str | None = None) -> dict[str, str]:
    settings = get_settings()
    if not settings.supabase_service_role_key:
        raise StorageError('SUPABASE_SERVICE_ROLE_KEY is missing.')

    headers = {
        'Authorization': f'Bearer {settings.supabase_service_role_key}',
        'apikey': settings.supabase_service_role_key,
    }
    if content_type:
        headers['Content-Type'] = content_type
    return headers


def _base_url() -> str:
    settings = get_settings()
    if not settings.supabase_url:
        raise StorageError('SUPABASE_URL is missing.')
    return settings.supabase_url.rstrip('/')


def upload_bytes(object_path: str, content: bytes, content_type: str) -> None:
    settings = get_settings()
    if not settings.supabase_storage_bucket:
        raise StorageError('SUPABASE_STORAGE_BUCKET is missing.')

    encoded_path = quote(object_path, safe='/')
    url = f'{_base_url()}/storage/v1/object/{quote(settings.supabase_storage_bucket, safe="")}/{encoded_path}'
    headers = _headers(content_type)
    headers['x-upsert'] = 'true'

    with httpx.Client(timeout=60.0) as client:
        response = client.post(url, headers=headers, content=content)

    if response.status_code >= 400:
        raise StorageError(f'Upload failed ({response.status_code}): {response.text}')


def delete_object(object_path: str) -> None:
    settings = get_settings()
    if not settings.supabase_storage_bucket:
        raise StorageError('SUPABASE_STORAGE_BUCKET is missing.')

    encoded_path = quote(object_path, safe='/')
    url = f'{_base_url()}/storage/v1/object/{quote(settings.supabase_storage_bucket, safe="")}/{encoded_path}'

    with httpx.Client(timeout=30.0) as client:
        response = client.delete(url, headers=_headers())

    if response.status_code >= 400:
        raise StorageError(f'Delete failed ({response.status_code}): {response.text}')


def sign_object_url(object_path: str) -> str:
    settings = get_settings()
    if not settings.supabase_storage_bucket:
        raise StorageError('SUPABASE_STORAGE_BUCKET is missing.')

    encoded_path = quote(object_path, safe='/')
    url = f'{_base_url()}/storage/v1/object/sign/{quote(settings.supabase_storage_bucket, safe="")}/{encoded_path}'
    payload = {'expiresIn': settings.supabase_signed_url_expires_seconds}

    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, headers=_headers('application/json'), json=payload)

    if response.status_code >= 400:
        raise StorageError(f'Sign URL failed ({response.status_code}): {response.text}')

    body = response.json()
    signed = body.get('signedURL')
    if not signed or not isinstance(signed, str):
        raise StorageError('Invalid signed URL response from Supabase Storage.')

    if signed.startswith('http://') or signed.startswith('https://'):
        return signed
    return f'{_base_url()}{signed}'


def ensure_bucket_exists() -> None:
    settings = get_settings()
    if not settings.supabase_storage_bucket:
        raise StorageError('SUPABASE_STORAGE_BUCKET is missing.')

    bucket_id = settings.supabase_storage_bucket
    encoded_bucket = quote(bucket_id, safe='')
    get_url = f'{_base_url()}/storage/v1/bucket/{encoded_bucket}'

    with httpx.Client(timeout=30.0) as client:
        get_response = client.get(get_url, headers=_headers())

        if get_response.status_code == 200:
            return
        if get_response.status_code not in (400, 404):
            raise StorageError(f'Bucket lookup failed ({get_response.status_code}): {get_response.text}')

        create_url = f'{_base_url()}/storage/v1/bucket'
        payload = {'id': bucket_id, 'name': bucket_id, 'public': False}
        create_response = client.post(create_url, headers=_headers('application/json'), json=payload)

    if create_response.status_code in (200, 201):
        return

    body = create_response.text.lower()
    if create_response.status_code in (400, 409) and (
        'already exists' in body or 'duplicate' in body or 'bucket' in body
    ):
        return

    raise StorageError(f'Bucket create failed ({create_response.status_code}): {create_response.text}')
