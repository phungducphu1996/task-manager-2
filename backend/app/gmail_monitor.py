from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from hashlib import sha1
from html import unescape
import imaplib
import base64
import re
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import GmailMonitorEvent, IntegrationConfig, NotificationChannel, NotificationEvent
from .notifications import NotificationSpec, dispatch_due_notification_events, enqueue_notification_event

settings = get_settings()
GMAIL_ZALO_CONFIG_KEY = 'gmail_zalo_monitor'
GMAIL_OAUTH_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
GMAIL_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GMAIL_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GMAIL_API_URL = 'https://gmail.googleapis.com/gmail/v1'
DEFAULT_GMAIL_DAILY_DIGEST_TIME = '07:00'


def _string_list(value: str | None) -> list[str]:
    return [item.strip() for item in (value or '').split(',') if item.strip()]


def gmail_zalo_config(db: Session | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if db is not None:
        stored = db.get(IntegrationConfig, GMAIL_ZALO_CONFIG_KEY)
        if stored and isinstance(stored.payload, dict):
            payload = stored.payload
    return {
        'enabled': bool(payload.get('enabled', True)),
        'notify_sale_realtime': bool(payload.get('notify_sale_realtime', True)),
        'notify_message_realtime': bool(payload.get('notify_message_realtime', False)),
        'daily_digest_enabled': bool(payload.get('daily_digest_enabled', True)),
        'daily_digest_time': payload.get('daily_digest_time') or DEFAULT_GMAIL_DAILY_DIGEST_TIME,
        'gmail_address': payload.get('gmail_address') or settings.gmail_address,
        'gmail_app_password': payload.get('gmail_app_password') or settings.gmail_app_password,
        'gmail_oauth_client_id': payload.get('gmail_oauth_client_id') or settings.gmail_oauth_client_id,
        'gmail_oauth_client_secret': payload.get('gmail_oauth_client_secret') or settings.gmail_oauth_client_secret,
        'gmail_oauth_redirect_uri': payload.get('gmail_oauth_redirect_uri') or settings.gmail_oauth_redirect_uri,
        'gmail_oauth_refresh_token': payload.get('gmail_oauth_refresh_token'),
        'gmail_oauth_email': payload.get('gmail_oauth_email'),
        'gmail_oauth_connected_at': payload.get('gmail_oauth_connected_at'),
        'gmail_imap_host': payload.get('gmail_imap_host') or settings.gmail_imap_host,
        'gmail_imap_port': int(payload.get('gmail_imap_port') or settings.gmail_imap_port),
        'gmail_imap_mailbox': payload.get('gmail_imap_mailbox') or settings.gmail_imap_mailbox,
        'gmail_search_since_days': int(payload.get('gmail_search_since_days') or settings.gmail_search_since_days),
        'gmail_sale_from_addresses': payload.get('gmail_sale_from_addresses') or settings.gmail_sale_from_addresses,
        'gmail_sale_subject': payload.get('gmail_sale_subject') or settings.gmail_sale_subject,
        'gmail_message_from_addresses': payload.get('gmail_message_from_addresses') or settings.gmail_message_from_addresses,
        'gmail_poll_max_results': int(payload.get('gmail_poll_max_results') or settings.gmail_poll_max_results),
        'zalo_worker_url': payload.get('zalo_worker_url') or settings.zalo_worker_url,
        'zalo_worker_token': payload.get('zalo_worker_token') or settings.zalo_worker_token,
        'zalo_shared_secret': payload.get('zalo_shared_secret') or settings.zalo_shared_secret,
        'zalo_group_id': payload.get('zalo_group_id') or settings.zalo_group_id,
    }


class GmailMonitorError(RuntimeError):
    pass


class GmailMonitorConfigError(GmailMonitorError):
    pass


@dataclass(slots=True)
class ParsedGmailItem:
    transaction_id: str | None
    title: str
    quantity: int | None
    item_price: str | None
    details: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedGmailEvent:
    gmail_message_id: str
    event_type: str
    subject: str
    sender: str | None
    recipient: str | None
    received_at: datetime | None
    gmail_thread_id: str | None = None
    rfc_message_id: str | None = None
    snippet: str | None = None
    order_id: str | None = None
    order_total: str | None = None
    order_total_cents: int | None = None
    order_currency: str | None = None
    order_url: str | None = None
    thumbnail_url: str | None = None
    dispatch_by: str | None = None
    shop: str | None = None
    buyer_username: str | None = None
    buyer_name: str | None = None
    buyer_email: str | None = None
    items: list[ParsedGmailItem] = field(default_factory=list)
    message_sender_name: str | None = None
    message_url: str | None = None
    message_issue: str | None = None
    message_resolution: str | None = None
    message_note: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


def _clean_text(value: str | None) -> str:
    if not value:
        return ''
    value = unescape(value)
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r'\n{3,}', '\n\n', value)
    return value.strip()


def _strip_html_tags(value: str | None) -> str:
    if not value:
        return ''
    value = unescape(value)
    value = re.sub(r'<\s*br\s*/?\s*>', '\n', value, flags=re.IGNORECASE)
    value = re.sub(r'</\s*p\s*>', '\n', value, flags=re.IGNORECASE)
    value = re.sub(r'<\s*p[^>]*>', '\n', value, flags=re.IGNORECASE)
    value = re.sub(r'<[^>]+>', '', value)
    return _clean_text(value)


def _message_text(message: Message) -> str:
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() != 'text/plain':
                continue
            try:
                content = part.get_content()
            except LookupError:
                content = part.get_payload(decode=True).decode('utf-8', errors='replace')
            if content:
                parts.append(str(content))
    else:
        try:
            content = message.get_content()
        except LookupError:
            content = message.get_payload(decode=True).decode('utf-8', errors='replace')
        if content:
            parts.append(str(content))
    return _clean_text('\n\n'.join(parts))


def _message_html(message: Message) -> str:
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() != 'text/html':
                continue
            try:
                content = part.get_content()
            except LookupError:
                content = part.get_payload(decode=True).decode('utf-8', errors='replace')
            if content:
                parts.append(str(content))
    elif message.get_content_type() == 'text/html':
        try:
            content = message.get_content()
        except LookupError:
            content = message.get_payload(decode=True).decode('utf-8', errors='replace')
        if content:
            parts.append(str(content))
    return '\n\n'.join(parts)


def _parse_date_header(value: str | None, fallback: datetime | None = None) -> datetime | None:
    if not value:
        return fallback
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return fallback
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _amount_to_cents(value: str | None) -> tuple[str | None, int | None, str | None]:
    if not value:
        return None, None, None
    match = re.search(r'\b([A-Z]{2,4})\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)', value)
    if not match:
        match = re.search(r'\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)', value)
        if not match:
            return value.strip(), None, None
        amount = match.group(1)
        currency = None
    else:
        currency = match.group(1)
        amount = match.group(2)

    normalized = amount.replace(',', '')
    dollars, _, cents_raw = normalized.partition('.')
    cents = int(dollars) * 100 + int((cents_raw + '00')[:2])
    label = f'{currency or ""}${amount}'.strip()
    return label, cents, currency


def _first_match(pattern: str, text: str, flags: int = 0) -> str | None:
    match = re.search(pattern, text, flags)
    return _clean_text(match.group(1)) if match else None


def _compact_field(value: str | None) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    return re.sub(r'\n\s*\n+', '\n', cleaned).strip()


def _line_field(name: str, text: str) -> str | None:
    return _first_match(rf'^{re.escape(name)}:\s*(.+)$', text, re.MULTILINE)


def _line_fields(name: str, text: str) -> list[str]:
    return [
        _clean_text(match)
        for match in re.findall(rf'^\s*{re.escape(name)}:\s*(.+)$', text, flags=re.MULTILINE)
        if _clean_text(match)
    ]


def _extract_sale_shop(text: str, html: str) -> str | None:
    # Etsy's plain-text "Shop:" can be the seller/display name. The product
    # card in the HTML contains the storefront slug we want for routing.
    html_text = _clean_text(re.sub(r'<[^>]+>', '\n', html))
    html_shops = _line_fields('Shop', html_text)
    if html_shops:
        return html_shops[0]
    return _line_field('Shop', text)


def _extract_subject_sale(subject: str) -> tuple[str | None, str | None, str | None]:
    order_id = _first_match(r'Order\s+#([A-Za-z0-9_-]+)', subject, re.IGNORECASE)
    dispatch_by = _first_match(r'Dispatch by\s+(.+?)\s+-', subject, re.IGNORECASE)
    total = _first_match(r'\[(.+?),\s*Order\s+#', subject, re.IGNORECASE)
    return order_id, total, dispatch_by


def _extract_items(text: str) -> list[ParsedGmailItem]:
    blocks = re.findall(
        r'Transaction ID:\s*([^\n]+)\n(.*?)(?=\nTransaction ID:|\n-{5,}\nItem total:|\Z)',
        text,
        flags=re.DOTALL,
    )
    items: list[ParsedGmailItem] = []
    detail_fields = {'size', 'characters', 'personalization', 'package', 'color', 'style', 'name', 'number'}
    for transaction_id, block in blocks:
        title = ''
        quantity: int | None = None
        item_price: str | None = None
        details: dict[str, str] = {}
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith('Item:'):
                title = _clean_text(line.split(':', 1)[1])
                continue
            if line.startswith('Quantity:'):
                raw_quantity = line.split(':', 1)[1].strip()
                quantity = int(raw_quantity) if raw_quantity.isdigit() else None
                continue
            if line.startswith('Item price:'):
                item_price = _clean_text(line.split(':', 1)[1])
                continue
            field_match = re.match(r'([A-Za-z][A-Za-z0-9 /&\'-]{1,40}):\s*(.+)$', line)
            if field_match and field_match.group(1).strip().casefold() in detail_fields:
                details[_clean_text(field_match.group(1))] = _clean_text(field_match.group(2))
        if title:
            items.append(
                ParsedGmailItem(
                    transaction_id=_clean_text(transaction_id),
                    title=title,
                    quantity=quantity,
                    item_price=item_price,
                    details=details,
                )
            )
    return items


def _message_snippet(text: str) -> str | None:
    text = _strip_html_tags(text)
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('http') or set(line) <= {'*', '-'}:
            continue
        if line in {'Etsy', 'On', 'Sale', 'Gift Guide', 'Home &'}:
            continue
        lines.append(line)
        if len(lines) >= 3:
            break
    return ' / '.join(lines)[:500] if lines else None


def _extract_thumbnail_url(html: str) -> str | None:
    if not html:
        return None
    candidates = re.findall(r'<img\b[^>]*?\bsrc=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    for raw_url in candidates:
        url = unescape(raw_url).strip()
        normalized = url.casefold()
        if not normalized.startswith(('http://', 'https://')):
            continue
        if 'etsy' not in normalized and 'etsystatic' not in normalized:
            continue
        if any(token in normalized for token in ['pixel', 'tracking', 'spacer', 'logo', 'avatar']):
            continue
        return url
    return None


def _classify_email(subject: str, sender: str | None, text: str) -> str | None:
    normalized_subject = subject.casefold()
    normalized_sender = (sender or '').casefold()
    if 'you made a sale on etsy' in normalized_subject or 'transaction@etsy.com' in normalized_sender:
        return 'sale'
    if (
        'etsy conversation' in normalized_subject
        or 'sent you a message' in text.casefold()
        or 'conversations@mail.etsy.com' in normalized_sender
    ):
        return 'message'
    return None


def _extract_help_request_fields(text: str) -> dict[str, str | None]:
    clean = _strip_html_tags(text)
    sender = _first_match(r'Hi\s+[^,\n]+,\s*(.+?)\s+sent you a message\.', clean, re.IGNORECASE)
    issue = _first_match(r'You need help with:\s*(.+?)(?:\n+Your ideal resolution:|\Z)', clean, re.IGNORECASE | re.DOTALL)
    resolution = _first_match(r'Your ideal resolution:\s*(.+?)(?:\n+Note for seller:|\Z)', clean, re.IGNORECASE | re.DOTALL)
    note = _first_match(r'Note for seller:\s*(.+?)(?:\n+Quick tip:|\n+Buyers often|\Z)', clean, re.IGNORECASE | re.DOTALL)
    reply_url = _first_match(r'Reply here\s+(https?://\S+)', clean, re.IGNORECASE)
    return {
        'sender': _compact_field(sender),
        'issue': _compact_field(issue),
        'resolution': _compact_field(resolution),
        'note': _compact_field(note),
        'reply_url': _compact_field(reply_url),
    }


def parse_email_message(
    message: Message,
    *,
    gmail_message_id: str | None = None,
    gmail_thread_id: str | None = None,
    snippet: str | None = None,
    internal_date: datetime | None = None,
) -> ParsedGmailEvent | None:
    subject = _clean_text(str(message.get('Subject') or ''))
    sender = _clean_text(str(message.get('From') or '')) or None
    recipient = _clean_text(str(message.get('To') or '')) or None
    text = _message_text(message)
    html = _message_html(message)
    event_type = _classify_email(subject, sender, text)
    if event_type is None:
        return None

    rfc_message_id = _clean_text(str(message.get('Message-ID') or '')) or None
    fallback_id = rfc_message_id or sha1(f'{subject}\n{text[:500]}'.encode('utf-8')).hexdigest()
    parsed = ParsedGmailEvent(
        gmail_message_id=gmail_message_id or fallback_id.strip('<>'),
        gmail_thread_id=gmail_thread_id,
        rfc_message_id=rfc_message_id,
        event_type=event_type,
        subject=subject,
        sender=sender,
        recipient=recipient,
        received_at=_parse_date_header(str(message.get('Date') or ''), internal_date),
        snippet=_clean_text(snippet) or _message_snippet(text),
    )

    if event_type == 'sale':
        subject_order_id, subject_total, dispatch_by = _extract_subject_sale(subject)
        body_total = _line_field('Order Total', text)
        order_total, cents, currency = _amount_to_cents(body_total or subject_total)
        parsed.order_id = subject_order_id or _first_match(r'/orders/([0-9]+)', text)
        parsed.order_total = order_total
        parsed.order_total_cents = cents
        parsed.order_currency = currency
        parsed.dispatch_by = dispatch_by
        parsed.order_url = _first_match(r'(https?://(?:www\.)?etsy\.com/your/orders/[0-9]+)', text)
        parsed.thumbnail_url = _extract_thumbnail_url(html)
        parsed.shop = _extract_sale_shop(text, html)
        parsed.buyer_username = _line_field('Buyer', text)
        parsed.buyer_name = _first_match(r"<span class='name'>(.*?)</span>", text, re.DOTALL)
        parsed.buyer_email = _first_match(r'\*\s*Email\s+([^\s]+@[^\s]+)', text)
        parsed.items = _extract_items(text)
    else:
        help_request = _extract_help_request_fields(text)
        parsed.message_sender_name = help_request.get('sender')
        if not parsed.message_sender_name:
            parsed.message_sender_name = _first_match(r'Re:\s*Etsy Conversation with\s+(.+)$', subject, re.IGNORECASE)
        if not parsed.message_sender_name:
            parsed.message_sender_name = _first_match(
                r'^(?!Hi\s)[^\n]*?(.+?)\s+sent you a message',
                _strip_html_tags(text),
                re.IGNORECASE | re.DOTALL,
            )
        parsed.message_issue = help_request.get('issue')
        parsed.message_resolution = help_request.get('resolution')
        parsed.message_note = help_request.get('note')
        parsed.message_url = help_request.get('reply_url') or _first_match(
            r'View\s+message\s+\(\s*(https?://[^)\s]+)', text, re.IGNORECASE
        )
        if parsed.message_note:
            parsed.snippet = parsed.message_note[:500]

    parsed.raw_payload = {
        'plain_text_preview': text[:2000],
        'items': [asdict(item) for item in parsed.items],
        'message_sender_name': parsed.message_sender_name,
        'message_url': parsed.message_url,
        'message_issue': parsed.message_issue,
        'message_resolution': parsed.message_resolution,
        'message_note': parsed.message_note,
        'dispatch_by': parsed.dispatch_by,
        'shop': parsed.shop,
        'thumbnail_url': parsed.thumbnail_url,
        'buyer_email': parsed.buyer_email,
    }
    return parsed


def parse_eml_bytes(raw: bytes, **kwargs: Any) -> ParsedGmailEvent | None:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    return parse_email_message(message, **kwargs)


def _gmail_configured(config: dict[str, Any]) -> bool:
    return _gmail_oauth_configured(config) or bool(config.get('gmail_address') and config.get('gmail_app_password'))


def _gmail_oauth_configured(config: dict[str, Any]) -> bool:
    return bool(
        config.get('gmail_oauth_client_id')
        and config.get('gmail_oauth_client_secret')
        and config.get('gmail_oauth_refresh_token')
    )


def _require_gmail_config(config: dict[str, Any]) -> None:
    if not _gmail_configured(config):
        raise GmailMonitorConfigError('Gmail is not connected. Connect Gmail with OAuth in the admin UI.')


def gmail_oauth_configured(config: dict[str, Any]) -> bool:
    return bool(config.get('gmail_oauth_client_id') and config.get('gmail_oauth_client_secret'))


def gmail_oauth_connected(config: dict[str, Any]) -> bool:
    return _gmail_oauth_configured(config)


def build_gmail_oauth_url(config: dict[str, Any], *, state: str) -> str:
    if not gmail_oauth_configured(config):
        raise GmailMonitorConfigError('Gmail OAuth client ID and client secret are required.')
    redirect_uri = str(config.get('gmail_oauth_redirect_uri') or '').strip()
    if not redirect_uri:
        raise GmailMonitorConfigError('Gmail OAuth redirect URI is required.')
    params = {
        'client_id': str(config.get('gmail_oauth_client_id')),
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': GMAIL_OAUTH_SCOPE,
        'access_type': 'offline',
        'prompt': 'consent',
        'include_granted_scopes': 'true',
        'state': state,
    }
    return f'{GMAIL_AUTH_URL}?{urlencode(params)}'


def exchange_gmail_oauth_code(config: dict[str, Any], *, code: str) -> dict[str, Any]:
    if not gmail_oauth_configured(config):
        raise GmailMonitorConfigError('Gmail OAuth client ID and client secret are required.')
    redirect_uri = str(config.get('gmail_oauth_redirect_uri') or '').strip()
    if not redirect_uri:
        raise GmailMonitorConfigError('Gmail OAuth redirect URI is required.')

    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                GMAIL_TOKEN_URL,
                data={
                    'client_id': str(config.get('gmail_oauth_client_id')),
                    'client_secret': str(config.get('gmail_oauth_client_secret')),
                    'code': code,
                    'grant_type': 'authorization_code',
                    'redirect_uri': redirect_uri,
                },
            )
            response.raise_for_status()
            token = response.json()
            access_token = str(token.get('access_token') or '')
            email_address = None
            if access_token:
                profile_response = client.get(
                    f'{GMAIL_API_URL}/users/me/profile',
                    headers={'Authorization': f'Bearer {access_token}'},
                )
                if profile_response.status_code < 400:
                    profile = profile_response.json()
                    email_address = profile.get('emailAddress')
    except httpx.HTTPError as exc:
        raise GmailMonitorError(f'Gmail OAuth token exchange failed: {exc}') from exc

    if not token.get('refresh_token'):
        raise GmailMonitorError('Google did not return a refresh token. Try connecting again and approve offline access.')
    return {
        'refresh_token': token.get('refresh_token'),
        'access_token': token.get('access_token'),
        'expires_in': token.get('expires_in'),
        'email_address': email_address,
    }


def _refresh_gmail_access_token(config: dict[str, Any]) -> str:
    if not _gmail_oauth_configured(config):
        raise GmailMonitorConfigError('Gmail OAuth is not connected.')
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                GMAIL_TOKEN_URL,
                data={
                    'client_id': str(config.get('gmail_oauth_client_id')),
                    'client_secret': str(config.get('gmail_oauth_client_secret')),
                    'refresh_token': str(config.get('gmail_oauth_refresh_token')),
                    'grant_type': 'refresh_token',
                },
            )
            response.raise_for_status()
            token = response.json()
    except httpx.HTTPError as exc:
        raise GmailMonitorError(f'Gmail OAuth refresh failed: {exc}') from exc
    access_token = str(token.get('access_token') or '')
    if not access_token:
        raise GmailMonitorError('Gmail OAuth refresh did not return an access token.')
    return access_token


def _quote_imap_value(value: str) -> str:
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _imap_since_label(config: dict[str, Any]) -> str:
    days = max(1, int(config.get('gmail_search_since_days') or settings.gmail_search_since_days))
    since_date = datetime.now(ZoneInfo(settings.notify_timezone)).date() - timedelta(days=days)
    return since_date.strftime('%d-%b-%Y')


def _imap_uid_sort_key(uid: str) -> int:
    try:
        return int(uid)
    except ValueError:
        return 0


def _imap_search_uids(
    client: imaplib.IMAP4_SSL,
    *,
    config: dict[str, Any],
    from_addresses: list[str],
    subject: str | None = None,
) -> list[str]:
    seen: set[str] = set()
    uids: list[str] = []
    since = _imap_since_label(config)
    for sender in from_addresses:
        criteria = ['SINCE', since, 'FROM', _quote_imap_value(sender)]
        if subject:
            criteria.extend(['SUBJECT', _quote_imap_value(subject)])
        status, data = client.uid('SEARCH', None, *criteria)
        if status != 'OK':
            raise GmailMonitorError(f'Gmail IMAP search failed for {sender}: {status}')
        raw_uids = b' '.join(item for item in data if isinstance(item, bytes)).decode('ascii', errors='ignore')
        for uid in raw_uids.split():
            if uid in seen:
                continue
            seen.add(uid)
            uids.append(uid)
    max_results = max(1, int(config.get('gmail_poll_max_results') or settings.gmail_poll_max_results))
    return sorted(uids, key=_imap_uid_sort_key, reverse=True)[:max_results]


def _imap_fetch_raw(client: imaplib.IMAP4_SSL, uid: str) -> bytes | None:
    status, data = client.uid('FETCH', uid, '(RFC822)')
    if status != 'OK':
        raise GmailMonitorError(f'Gmail IMAP fetch failed for UID {uid}: {status}')
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def _fetch_imap_messages(config: dict[str, Any]) -> dict[str, list[tuple[str, bytes]]]:
    _require_gmail_config(config)
    client: imaplib.IMAP4_SSL | None = None
    result: dict[str, list[tuple[str, bytes]]] = {'sale': [], 'message': []}
    try:
        client = imaplib.IMAP4_SSL(str(config.get('gmail_imap_host') or settings.gmail_imap_host), int(config.get('gmail_imap_port') or settings.gmail_imap_port))
        client.login(str(config.get('gmail_address') or ''), str(config.get('gmail_app_password') or ''))
        mailbox = str(config.get('gmail_imap_mailbox') or settings.gmail_imap_mailbox)
        status, _ = client.select(mailbox, readonly=True)
        if status != 'OK':
            raise GmailMonitorError(f'Gmail IMAP select failed for mailbox {mailbox}: {status}')

        search_specs = {
            'sale': {
                'from_addresses': _string_list(str(config.get('gmail_sale_from_addresses') or '')),
                'subject': str(config.get('gmail_sale_subject') or '') or None,
            },
            'message': {
                'from_addresses': _string_list(str(config.get('gmail_message_from_addresses') or '')),
                'subject': None,
            },
        }
        for event_type, spec in search_specs.items():
            from_addresses = spec['from_addresses']
            if not from_addresses:
                continue
            for uid in _imap_search_uids(client, config=config, from_addresses=from_addresses, subject=spec['subject']):
                raw = _imap_fetch_raw(client, uid)
                if raw:
                    result[event_type].append((uid, raw))
        return result
    except imaplib.IMAP4.error as exc:
        raise GmailMonitorError(f'Gmail IMAP error: {exc}') from exc
    finally:
        if client is not None:
            try:
                client.close()
            except imaplib.IMAP4.error:
                pass
            try:
                client.logout()
            except imaplib.IMAP4.error:
                pass


def _gmail_api_query(config: dict[str, Any], *, from_address: str, subject: str | None = None) -> str:
    days = max(1, int(config.get('gmail_search_since_days') or settings.gmail_search_since_days))
    parts = [f'newer_than:{days}d', f'from:{from_address}']
    if subject:
        escaped = subject.replace('"', '\\"')
        parts.append(f'subject:"{escaped}"')
    return ' '.join(parts)


def _gmail_api_internal_date(value: str | int | None) -> datetime | None:
    if value is None:
        return None
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)


def _decode_gmail_api_raw(raw: str) -> bytes:
    padded = raw + '=' * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded.encode('ascii'))


def _fetch_gmail_api_messages(config: dict[str, Any]) -> dict[str, list[tuple[str, bytes, dict[str, Any]]]]:
    _require_gmail_config(config)
    access_token = _refresh_gmail_access_token(config)
    headers = {'Authorization': f'Bearer {access_token}'}
    result: dict[str, list[tuple[str, bytes, dict[str, Any]]]] = {'sale': [], 'message': []}
    max_results = max(1, int(config.get('gmail_poll_max_results') or settings.gmail_poll_max_results))
    search_specs = {
        'sale': {
            'from_addresses': _string_list(str(config.get('gmail_sale_from_addresses') or '')),
            'subject': str(config.get('gmail_sale_subject') or '') or None,
        },
        'message': {
            'from_addresses': _string_list(str(config.get('gmail_message_from_addresses') or '')),
            'subject': None,
        },
    }

    try:
        with httpx.Client(timeout=25.0) as client:
            for event_type, spec in search_specs.items():
                seen_ids: set[str] = set()
                for sender in spec['from_addresses']:
                    list_response = client.get(
                        f'{GMAIL_API_URL}/users/me/messages',
                        headers=headers,
                        params={
                            'q': _gmail_api_query(config, from_address=sender, subject=spec['subject']),
                            'maxResults': max_results,
                        },
                    )
                    list_response.raise_for_status()
                    messages = list_response.json().get('messages') or []
                    for item in messages:
                        message_id = str(item.get('id') or '')
                        if not message_id or message_id in seen_ids:
                            continue
                        seen_ids.add(message_id)
                        message_response = client.get(
                            f'{GMAIL_API_URL}/users/me/messages/{message_id}',
                            headers=headers,
                            params={'format': 'raw'},
                        )
                        message_response.raise_for_status()
                        message_payload = message_response.json()
                        raw = str(message_payload.get('raw') or '')
                        if not raw:
                            continue
                        result[event_type].append((message_id, _decode_gmail_api_raw(raw), message_payload))
                        if len(result[event_type]) >= max_results:
                            break
                    if len(result[event_type]) >= max_results:
                        break
    except httpx.HTTPError as exc:
        raise GmailMonitorError(f'Gmail API request failed: {exc}') from exc
    except (ValueError, TypeError) as exc:
        raise GmailMonitorError(f'Gmail API response could not be parsed: {exc}') from exc
    return result


def _event_key(event_type: str, gmail_message_id: str) -> str:
    token = gmail_message_id.strip('<>')
    if len(token) > 160:
        token = sha1(token.encode('utf-8')).hexdigest()
    return f'gmail:{event_type}:{token}'


def _truncate_line(value: str | None, limit: int = 420) -> str:
    cleaned = _clean_text(value)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + '...'


def _format_item_line(item: ParsedGmailItem, index: int) -> str:
    quantity = item.quantity if item.quantity is not None else 1
    suffix = f' - {item.item_price}' if item.item_price else ''
    details = ', '.join(f'{key}: {value}' for key, value in item.details.items())
    if details:
        suffix = f'{suffix} ({details})'
    return f'{index}. {quantity}x {_truncate_line(item.title, 140)}{suffix}'


def _format_realtime_message(parsed: ParsedGmailEvent) -> str:
    if parsed.event_type == 'sale':
        lines = ['[ETSY CÓ SALE MỚI]']
        if parsed.order_id:
            lines.append(f'Đơn #{parsed.order_id}')
        if parsed.order_total:
            lines.append(f'Tổng: {parsed.order_total}')
        if parsed.shop:
            lines.append(f'Shop: {parsed.shop}')
        buyer = parsed.buyer_name or parsed.buyer_username
        if buyer:
            lines.append(f'Khách: {buyer}')
        if parsed.buyer_username and parsed.buyer_username != buyer:
            lines.append(f'Username: {parsed.buyer_username}')
        if parsed.dispatch_by:
            lines.append(f'Cần ship: {parsed.dispatch_by}')
        if parsed.items:
            lines.extend(['', 'Sản phẩm:'])
            lines.extend(_format_item_line(item, index) for index, item in enumerate(parsed.items[:5], start=1))
            if len(parsed.items) > 5:
                lines.append(f'... còn {len(parsed.items) - 5} sản phẩm nữa')
        return '\n'.join(lines)

    sender = parsed.message_sender_name or parsed.sender or 'Etsy buyer'
    lines = ['[ETSY CÓ TIN NHẮN MỚI]', f'Từ: {sender}']
    if parsed.message_issue:
        lines.append(f'Vấn đề: {parsed.message_issue}')
    if parsed.message_resolution:
        lines.append(f'Mong muốn: {parsed.message_resolution}')
    if parsed.message_note:
        lines.extend(['', 'Nội dung:', _truncate_line(parsed.message_note, 700)])
    if parsed.snippet:
        if parsed.snippet != parsed.message_note:
            lines.extend(['', 'Tóm tắt:', _truncate_line(parsed.snippet, 500)])
    return '\n'.join(lines)


def _payload_for(parsed: ParsedGmailEvent, *, message: str) -> dict[str, Any]:
    return {
        'message': message,
        'context': {
            'source': 'gmail_monitor',
            'event_type': parsed.event_type,
            'gmail_message_id': parsed.gmail_message_id,
            'gmail_thread_id': parsed.gmail_thread_id,
            'rfc_message_id': parsed.rfc_message_id,
            'subject': parsed.subject,
            'sender': parsed.sender,
            'received_at': parsed.received_at.isoformat() if parsed.received_at else None,
            'order_id': parsed.order_id,
            'order_total': parsed.order_total,
            'shop': parsed.shop,
            'thumbnail_url': parsed.thumbnail_url,
            'buyer_username': parsed.buyer_username,
            'buyer_name': parsed.buyer_name,
            'order_url': parsed.order_url,
            'items': [asdict(item) for item in parsed.items],
            'message_issue': parsed.message_issue,
            'message_resolution': parsed.message_resolution,
            'message_note': parsed.message_note,
        },
    }


def _realtime_enabled(config: dict[str, Any], event_type: str) -> bool:
    if event_type == 'sale':
        return bool(config.get('notify_sale_realtime', True))
    if event_type == 'message':
        return bool(config.get('notify_message_realtime', False))
    return True


def save_and_enqueue_gmail_event(
    db: Session,
    parsed: ParsedGmailEvent,
    *,
    config: dict[str, Any] | None = None,
) -> tuple[GmailMonitorEvent, bool]:
    existing = db.scalar(
        select(GmailMonitorEvent).where(GmailMonitorEvent.gmail_message_id == parsed.gmail_message_id)
    )
    if existing:
        return existing, False

    config = config or gmail_zalo_config(db)
    notification_event = None
    if _realtime_enabled(config, parsed.event_type):
        message = _format_realtime_message(parsed)
        notification_event, _ = enqueue_notification_event(
            db,
            NotificationSpec(
                event_key=_event_key(parsed.event_type, parsed.gmail_message_id),
                event_type=f'gmail_{parsed.event_type}_new',
                channel=NotificationChannel.group,
                target_id=str(config.get('zalo_group_id') or '') or None,
                payload=_payload_for(parsed, message=message),
            ),
        )
    event = GmailMonitorEvent(
        gmail_message_id=parsed.gmail_message_id,
        gmail_thread_id=parsed.gmail_thread_id,
        rfc_message_id=parsed.rfc_message_id,
        event_type=parsed.event_type,
        source='gmail',
        sender=parsed.sender,
        recipient=parsed.recipient,
        subject=parsed.subject[:500],
        snippet=parsed.snippet,
        received_at=parsed.received_at,
        sale_order_id=parsed.order_id,
        sale_total_cents=parsed.order_total_cents,
        sale_currency=parsed.order_currency,
        buyer_name=parsed.buyer_name,
        buyer_username=parsed.buyer_username,
        order_url=parsed.order_url,
        notification_event_id=notification_event.id if notification_event else None,
        payload=parsed.raw_payload,
    )
    db.add(event)
    db.flush()
    return event, True


def poll_gmail_and_notify(db: Session) -> dict[str, Any]:
    config = gmail_zalo_config(db)
    if not config.get('enabled', True):
        return {
            'skipped': True,
            'reason': 'Gmail/Zalo monitor is paused.',
            'fetched': 0,
            'created': 0,
            'detected': {'sale': 0, 'message': 0},
            'dispatch': {'processed': 0, 'sent': 0, 'pending': 0, 'failed': 0},
        }
    use_gmail_api = _gmail_oauth_configured(config)
    messages_by_type = _fetch_gmail_api_messages(config) if use_gmail_api else _fetch_imap_messages(config)
    seen: set[str] = set()
    created = 0
    skipped = 0
    fetched = 0
    detected: dict[str, int] = {'sale': 0, 'message': 0}

    for expected_type, messages in messages_by_type.items():
        for message in messages:
            fetched += 1
            if use_gmail_api:
                uid, raw, metadata = message
                gmail_id = f'gmailapi:{uid}'
                gmail_thread_id = str(metadata.get('threadId') or '') or None
                snippet = str(metadata.get('snippet') or '') or None
                internal_date = _gmail_api_internal_date(metadata.get('internalDate'))
            else:
                uid, raw = message
                mailbox = str(config.get('gmail_imap_mailbox') or settings.gmail_imap_mailbox)
                gmail_id = f'imap:{mailbox}:{uid}'
                gmail_thread_id = None
                snippet = None
                internal_date = None
            if not gmail_id or gmail_id in seen:
                continue
            seen.add(gmail_id)
            parsed = parse_eml_bytes(
                raw,
                gmail_message_id=gmail_id,
                gmail_thread_id=gmail_thread_id,
                snippet=snippet,
                internal_date=internal_date,
            )
            if parsed is None or parsed.event_type != expected_type:
                skipped += 1
                continue
            _, is_created = save_and_enqueue_gmail_event(db, parsed, config=config)
            if is_created:
                created += 1
                detected[parsed.event_type] = detected.get(parsed.event_type, 0) + 1
            else:
                skipped += 1

    db.commit()
    dispatch = dispatch_due_notification_events(db) if created else {'processed': 0, 'sent': 0, 'pending': 0, 'failed': 0}
    return {
        'fetched': fetched,
        'created': created,
        'skipped': skipped,
        'detected': detected,
        'dispatch': dispatch,
    }


def _local_day_bounds(target_date: date) -> tuple[datetime, datetime]:
    tz = ZoneInfo(settings.notify_timezone)
    start = datetime.combine(target_date, time.min, tzinfo=tz)
    end = datetime.combine(target_date, time.max, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _format_money(cents: int, currency: str | None) -> str:
    prefix = f'{currency}$' if currency else '$'
    return f'{prefix}{cents / 100:,.2f}'


def _format_digest_message(events: list[GmailMonitorEvent], *, target_date: date) -> str:
    sales = [event for event in events if event.event_type == 'sale']
    messages = [event for event in events if event.event_type == 'message']
    total_by_currency: dict[str | None, int] = {}
    shop_summary: dict[str, dict[str, Any]] = {}
    for event in sales:
        shop = 'Không rõ shop'
        if isinstance(event.payload, dict):
            shop = str(event.payload.get('shop') or '').strip() or shop
        summary = shop_summary.setdefault(shop, {'count': 0, 'totals': {}})
        summary['count'] += 1
        if event.sale_total_cents is None:
            continue
        total_by_currency[event.sale_currency] = total_by_currency.get(event.sale_currency, 0) + event.sale_total_cents
        totals = summary['totals']
        totals[event.sale_currency] = totals.get(event.sale_currency, 0) + event.sale_total_cents

    lines = [
        f'[ETSY TỔNG HỢP {target_date:%d/%m/%Y}]',
        f'Sale mới: {len(sales)}',
        f'Tin nhắn mới: {len(messages)}',
    ]
    if total_by_currency:
        totals = ', '.join(_format_money(cents, currency) for currency, cents in sorted(total_by_currency.items(), key=lambda item: item[0] or ''))
        lines.append(f'Tổng sale: {totals}')

    if shop_summary:
        lines.extend(['', 'Theo shop:'])
        for shop, summary in sorted(shop_summary.items(), key=lambda item: item[0].casefold()):
            totals = summary['totals']
            total_label = ''
            if totals:
                total_label = ' | ' + ', '.join(
                    _format_money(cents, currency) for currency, cents in sorted(totals.items(), key=lambda item: item[0] or '')
                )
            lines.append(f'- {shop}: {summary["count"]} sale{total_label}')

    if sales:
        lines.extend(['', 'Top sale:'])
        for index, event in enumerate(sales[:8], start=1):
            total = _format_money(event.sale_total_cents, event.sale_currency) if event.sale_total_cents is not None else 'không rõ tổng'
            buyer = event.buyer_username or event.buyer_name or 'không rõ khách'
            order = f'#{event.sale_order_id}' if event.sale_order_id else event.subject
            shop = ''
            if isinstance(event.payload, dict) and event.payload.get('shop'):
                shop = f' | {event.payload["shop"]}'
            lines.append(f'{index}. {order} | {total} | {buyer}{shop}')

    if messages:
        lines.extend(['', 'Tin nhắn mới:'])
        for index, event in enumerate(messages[:8], start=1):
            sender = event.payload.get('message_sender_name') if isinstance(event.payload, dict) else None
            sender = sender or event.sender or 'Etsy buyer'
            lines.append(f'{index}. {sender}: {_truncate_line(event.snippet or event.subject, 160)}')

    return '\n'.join(lines)


def _parse_digest_time(value: Any) -> time:
    raw = str(value or DEFAULT_GMAIL_DAILY_DIGEST_TIME).strip()
    match = re.fullmatch(r'([01]?\d|2[0-3]):([0-5]\d)', raw)
    if not match:
        return time(7, 0)
    return time(int(match.group(1)), int(match.group(2)))


def _digest_skip_result(reason: str, *, target_date: date, event_count: int = 0) -> dict[str, Any]:
    return {
        'skipped': True,
        'reason': reason,
        'target_date': target_date.isoformat(),
        'event_count': event_count,
        'created': False,
        'notification_event_id': None,
        'dispatch': {'processed': 0, 'sent': 0, 'pending': 0, 'failed': 0},
    }


def run_gmail_daily_digest(db: Session, *, target_date: date | None = None) -> dict[str, Any]:
    config = gmail_zalo_config(db)
    now = datetime.now(ZoneInfo(settings.notify_timezone))
    is_manual_target = target_date is not None
    target_date = target_date or now.date()
    if not config.get('enabled', True):
        return _digest_skip_result('Gmail/Zalo monitor is paused.', target_date=target_date)
    if not config.get('daily_digest_enabled', True):
        return _digest_skip_result('Gmail daily digest is disabled.', target_date=target_date)
    if not is_manual_target:
        scheduled_time = _parse_digest_time(config.get('daily_digest_time'))
        scheduled_at = datetime.combine(target_date, scheduled_time, tzinfo=now.tzinfo)
        if now < scheduled_at:
            return _digest_skip_result(f'Gmail daily digest is scheduled for {scheduled_time:%H:%M}.', target_date=target_date)
        existing_digest = db.scalar(
            select(NotificationEvent).where(NotificationEvent.event_key == f'gmail:digest:{target_date.isoformat()}')
        )
        if existing_digest:
            return _digest_skip_result('Gmail daily digest already sent for this date.', target_date=target_date)
    start, end = _local_day_bounds(target_date)
    events = db.scalars(
        select(GmailMonitorEvent)
        .where(and_(GmailMonitorEvent.received_at >= start, GmailMonitorEvent.received_at <= end))
        .order_by(GmailMonitorEvent.received_at.desc(), GmailMonitorEvent.id.desc())
    ).all()

    message = _format_digest_message(events, target_date=target_date)
    notification_event, created = enqueue_notification_event(
        db,
        NotificationSpec(
            event_key=f'gmail:digest:{target_date.isoformat()}',
            event_type='gmail_daily_digest',
            channel=NotificationChannel.group,
            target_id=str(config.get('zalo_group_id') or '') or None,
            payload={
                'message': message,
                'context': {
                    'source': 'gmail_monitor',
                    'target_date': target_date.isoformat(),
                    'sale_count': sum(1 for event in events if event.event_type == 'sale'),
                    'message_count': sum(1 for event in events if event.event_type == 'message'),
                },
            },
        ),
    )
    db.commit()
    dispatch = dispatch_due_notification_events(db) if created else {'processed': 0, 'sent': 0, 'pending': 0, 'failed': 0}
    return {
        'target_date': target_date.isoformat(),
        'event_count': len(events),
        'created': created,
        'notification_event_id': notification_event.id,
        'dispatch': dispatch,
    }
