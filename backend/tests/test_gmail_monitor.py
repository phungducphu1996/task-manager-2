from datetime import date
import base64

from sqlalchemy import select

from app.gmail_monitor import parse_eml_bytes, poll_gmail_and_notify, run_gmail_daily_digest, save_and_enqueue_gmail_event
from app.models import GmailMonitorEvent, IntegrationConfig, NotificationEvent


SALE_EML = b"""From: Etsy Transactions <transaction@etsy.com>
To: lidude56169@gmail.com
Subject: You made a sale on Etsy - Dispatch by May 21 - [US$39.40, Order #4059129411]
Date: Sun, 10 May 2026 18:00:20 +0000
Message-ID: <sale-message-id@example.test>
Content-Type: text/plain; charset="utf-8"

Congratulations on your Etsy order for 1 item from ubtxsi3s3ljmsh5m.

View the invoice:
http://www.etsy.com/your/orders/4059129411

------------------------------------------------------
Order Details
------------------------------------------------------

Shop:               Ngoc Nguyen Ha
Buyer:              ubtxsi3s3ljmsh5m
    --------------------------------------

Transaction ID:     5072985897
Item:               Custom Super Mario Baseball Jersey, Mario Family Matching Shirts
Package: Single Jersey
Personalization: Waluigi - Name: Leo / Size L
Quantity:           1
Item price:         US$56.64

--------------------------------------
Order Total:        US$39.40

Delivery Address:
<address >
<span class='name'>Leonel Brito</span><br/><span class='first-line'>7065 SW 19th St</span>
</address>

* Email britoleonel00@gmail.com
"""


MESSAGE_EML = b"""From: Etsy <no-reply@account.etsy.com>
To: lidude56169@gmail.com
Subject: Re: Etsy Conversation with JordonTheDash
Date: Tue, 12 May 2026 03:38:06 +0000
Message-ID: <message-id@example.test>
Content-Type: text/plain; charset="utf-8"

JordonTheDash
sent you a message

*****************************************************************
JordonTheDash
sent you a message
*****************************************************************

View
message (
https://ablink.account.etsy.com/message-link
)
"""


HELP_REQUEST_EML = b"""From: Etsy Conversations <conversations@mail.etsy.com>
To: lidude56169@gmail.com
Subject: Ashlee needs help with an order they placed
Date: Wed, 13 May 2026 03:00:57 +0000
Message-ID: <help-request@example.test>
Content-Type: text/plain; charset="utf-8"

Hi Ngoc, Ashlee sent you a message.
<p> You need help with: My order hasn't arrived<br />
<br />
Your ideal resolution: replace<br />
<br />
Note for seller: Hello,<br />
I have yet to receive my order. I have an upcoming vacation planned and I need these jerseys soon.<br />
Ashlee </p>
<p>Quick tip: Try responding to Ashlee as soon as you can.</p>
<p> Reply here https://www.etsy.com/conversations/1671839391?ref=proteus_convo_sender_headline_link&utm_source=convo&utm_medium=trans_email&utm_campaign=convo_html#last </p>
"""


SALE_WITH_THUMB_EML = b"""From: Etsy Transactions <transaction@etsy.com>
To: lidude56169@gmail.com
Subject: You made a sale on Etsy - Dispatch by May 21 - [US$130.13, Order #4054388316]
Date: Sun, 10 May 2026 19:00:20 +0000
Message-ID: <sale-thumb-message-id@example.test>
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="etsy-boundary"

--etsy-boundary
Content-Type: text/plain; charset="utf-8"

Shop:               Hazel Shop
Buyer:              buyer123
Transaction ID:     5072985898
Item:               Personalized Jersey
Quantity:           1
Item price:         US$130.13
Order Total:        US$130.13

--etsy-boundary
Content-Type: text/html; charset="utf-8"

<html><body>
<img src="https://i.etsystatic.com/12345/r/il_570xN.1234567890_abcd.jpg" alt="Personalized Jersey" />
</body></html>

--etsy-boundary--
"""


def test_parse_etsy_sale_email_extracts_order_fields() -> None:
    parsed = parse_eml_bytes(SALE_EML)

    assert parsed is not None
    assert parsed.event_type == 'sale'
    assert parsed.order_id == '4059129411'
    assert parsed.order_total == 'US$39.40'
    assert parsed.order_total_cents == 3940
    assert parsed.order_currency == 'US'
    assert parsed.dispatch_by == 'May 21'
    assert parsed.buyer_username == 'ubtxsi3s3ljmsh5m'
    assert parsed.buyer_name == 'Leonel Brito'
    assert parsed.buyer_email == 'britoleonel00@gmail.com'
    assert len(parsed.items) == 1
    assert parsed.items[0].quantity == 1
    assert parsed.items[0].details['Package'] == 'Single Jersey'
    assert parsed.items[0].details['Personalization'] == 'Waluigi - Name: Leo / Size L'


def test_parse_etsy_sale_extracts_shop_and_thumbnail_from_html() -> None:
    parsed = parse_eml_bytes(SALE_WITH_THUMB_EML)

    assert parsed is not None
    assert parsed.event_type == 'sale'
    assert parsed.shop == 'Hazel Shop'
    assert parsed.thumbnail_url == 'https://i.etsystatic.com/12345/r/il_570xN.1234567890_abcd.jpg'


def test_parse_etsy_conversation_email_extracts_message_link() -> None:
    parsed = parse_eml_bytes(MESSAGE_EML)

    assert parsed is not None
    assert parsed.event_type == 'message'
    assert parsed.message_sender_name == 'JordonTheDash'
    assert parsed.message_url == 'https://ablink.account.etsy.com/message-link'


def test_parse_etsy_help_request_extracts_issue_note_and_reply_link() -> None:
    parsed = parse_eml_bytes(HELP_REQUEST_EML)

    assert parsed is not None
    assert parsed.event_type == 'message'
    assert parsed.message_sender_name == 'Ashlee'
    assert parsed.message_issue == "My order hasn't arrived"
    assert parsed.message_resolution == 'replace'
    assert parsed.message_note == (
        'Hello,\nI have yet to receive my order. I have an upcoming vacation planned and I need these jerseys soon.\nAshlee'
    )
    assert parsed.message_url == (
        'https://www.etsy.com/conversations/1671839391?ref=proteus_convo_sender_headline_link&utm_source=convo&utm_medium=trans_email&utm_campaign=convo_html#last'
    )


def test_save_and_enqueue_gmail_event_dedupes_by_gmail_message_id(db_session) -> None:
    parsed = parse_eml_bytes(SALE_EML, gmail_message_id='gmail-sale-1', gmail_thread_id='thread-1')
    assert parsed is not None

    event, created = save_and_enqueue_gmail_event(db_session, parsed)
    duplicate, duplicate_created = save_and_enqueue_gmail_event(db_session, parsed)
    db_session.commit()

    assert created is True
    assert duplicate_created is False
    assert duplicate.id == event.id
    stored = db_session.scalar(select(GmailMonitorEvent).where(GmailMonitorEvent.gmail_message_id == 'gmail-sale-1'))
    assert stored is not None
    assert stored.notification_event_id is not None
    notification = db_session.get(NotificationEvent, stored.notification_event_id)
    assert notification is not None
    assert notification.event_type == 'gmail_sale_new'
    assert '[ETSY CÓ SALE MỚI]' in notification.payload['message']
    assert 'Đơn #4059129411' in notification.payload['message']
    assert 'Shop: Ngoc Nguyen Ha' in notification.payload['message']
    assert 'Link:' not in notification.payload['message']


def test_gmail_daily_digest_summarizes_sales_and_messages(db_session) -> None:
    sale = parse_eml_bytes(SALE_EML, gmail_message_id='gmail-sale-digest')
    message = parse_eml_bytes(MESSAGE_EML, gmail_message_id='gmail-message-digest')
    assert sale is not None
    assert message is not None
    save_and_enqueue_gmail_event(db_session, sale)
    save_and_enqueue_gmail_event(db_session, message)
    db_session.commit()

    result = run_gmail_daily_digest(db_session, target_date=date(2026, 5, 11))

    assert result['event_count'] == 1
    notification = db_session.get(NotificationEvent, result['notification_event_id'])
    assert notification is not None
    assert notification.event_type == 'gmail_daily_digest'
    assert '[ETSY TỔNG HỢP 11/05/2026]' in notification.payload['message']
    assert 'Sale mới: 1' in notification.payload['message']
    assert 'Theo shop:' in notification.payload['message']
    assert 'Ngoc Nguyen Ha: 1 sale' in notification.payload['message']
    assert 'US$39.40' in notification.payload['message']


def test_poll_gmail_and_notify_reads_imap_app_password_messages(db_session, monkeypatch) -> None:
    class FakeIMAP:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def login(self, address: str, password: str):
            assert address == 'etsy@example.com'
            assert password == 'app-password'
            return 'OK', [b'logged in']

        def select(self, mailbox: str, readonly: bool = False):
            assert mailbox == 'INBOX'
            assert readonly is True
            return 'OK', [b'3']

        def uid(self, command: str, *args):
            criteria = ' '.join(str(arg) for arg in args)
            if command == 'SEARCH':
                if 'transaction@etsy.com' in criteria:
                    return 'OK', [b'9']
                if 'no-reply@account.etsy.com' in criteria:
                    return 'OK', [b'8']
                if 'conversations@mail.etsy.com' in criteria:
                    return 'OK', [b'7']
                return 'OK', [b'']
            if command == 'FETCH':
                uid = str(args[0])
                if uid == '9':
                    return 'OK', [(b'9 (RFC822 {1}', SALE_EML)]
                if uid == '8':
                    return 'OK', [(b'8 (RFC822 {1}', MESSAGE_EML)]
                if uid == '7':
                    return 'OK', [(b'7 (RFC822 {1}', HELP_REQUEST_EML)]
            return 'NO', []

        def close(self):
            return 'OK', [b'closed']

        def logout(self):
            return 'BYE', [b'logout']

    import app.gmail_monitor as gmail_monitor

    gmail_monitor.settings.gmail_address = 'etsy@example.com'
    gmail_monitor.settings.gmail_app_password = 'app-password'
    gmail_monitor.settings.gmail_imap_host = 'imap.gmail.com'
    gmail_monitor.settings.gmail_imap_port = 993
    gmail_monitor.settings.gmail_imap_mailbox = 'INBOX'
    gmail_monitor.settings.gmail_sale_from_addresses = 'transaction@etsy.com'
    gmail_monitor.settings.gmail_sale_subject = 'You made a sale on Etsy'
    gmail_monitor.settings.gmail_message_from_addresses = 'no-reply@account.etsy.com,conversations@mail.etsy.com'
    gmail_monitor.settings.gmail_poll_max_results = 10
    monkeypatch.setattr(gmail_monitor.imaplib, 'IMAP4_SSL', FakeIMAP)
    monkeypatch.setattr('app.notifications._call_worker', lambda payload: (True, 200, '{"ok":true}', None))

    result = poll_gmail_and_notify(db_session)

    assert result['fetched'] == 3
    assert result['created'] == 3
    assert result['detected'] == {'sale': 1, 'message': 2}
    assert result['dispatch']['sent'] == 3
    stored_events = db_session.scalars(select(GmailMonitorEvent).order_by(GmailMonitorEvent.gmail_message_id)).all()
    assert len(stored_events) == 3
    assert {event.gmail_message_id for event in stored_events} == {
        'imap:INBOX:7',
        'imap:INBOX:8',
        'imap:INBOX:9',
    }


def test_poll_gmail_and_notify_reads_gmail_api_oauth_messages(db_session, monkeypatch) -> None:
    import app.gmail_monitor as gmail_monitor

    class FakeResponse:
        def __init__(self, payload: dict, status_code: int = 200) -> None:
            self.payload = payload
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise gmail_monitor.httpx.HTTPStatusError('bad', request=None, response=None)

        def json(self) -> dict:
            return self.payload

    def encoded(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')

    class FakeHTTPClient:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url: str, data: dict):
            assert data['grant_type'] == 'refresh_token'
            return FakeResponse({'access_token': 'access-token'})

        def get(self, url: str, headers: dict, params: dict | None = None):
            assert headers['Authorization'] == 'Bearer access-token'
            if url.endswith('/users/me/messages'):
                query = params['q']
                if 'transaction@etsy.com' in query:
                    return FakeResponse({'messages': [{'id': 'sale-1'}]})
                if 'no-reply@account.etsy.com' in query:
                    return FakeResponse({'messages': [{'id': 'message-1'}]})
                if 'conversations@mail.etsy.com' in query:
                    return FakeResponse({'messages': [{'id': 'message-2'}]})
                return FakeResponse({'messages': []})
            if url.endswith('/sale-1'):
                return FakeResponse({'id': 'sale-1', 'threadId': 'thread-sale', 'raw': encoded(SALE_EML)})
            if url.endswith('/message-1'):
                return FakeResponse({'id': 'message-1', 'threadId': 'thread-message-1', 'raw': encoded(MESSAGE_EML)})
            if url.endswith('/message-2'):
                return FakeResponse({'id': 'message-2', 'threadId': 'thread-message-2', 'raw': encoded(HELP_REQUEST_EML)})
            return FakeResponse({}, status_code=404)

    db_session.add(
        IntegrationConfig(
            key='gmail_zalo_monitor',
            payload={
                'gmail_oauth_client_id': 'client-id',
                'gmail_oauth_client_secret': 'client-secret',
                'gmail_oauth_refresh_token': 'refresh-token',
                'gmail_sale_from_addresses': 'transaction@etsy.com',
                'gmail_sale_subject': 'You made a sale on Etsy',
                'gmail_message_from_addresses': 'no-reply@account.etsy.com,conversations@mail.etsy.com',
                'gmail_poll_max_results': 10,
            },
        )
    )
    db_session.commit()
    monkeypatch.setattr(gmail_monitor.httpx, 'Client', FakeHTTPClient)
    monkeypatch.setattr('app.notifications._call_worker', lambda payload: (True, 200, '{"ok":true}', None))

    result = poll_gmail_and_notify(db_session)

    assert result['fetched'] == 3
    assert result['created'] == 3
    assert result['detected'] == {'sale': 1, 'message': 2}
    stored_events = db_session.scalars(select(GmailMonitorEvent).order_by(GmailMonitorEvent.gmail_message_id)).all()
    assert {event.gmail_message_id for event in stored_events} == {
        'gmailapi:message-1',
        'gmailapi:message-2',
        'gmailapi:sale-1',
    }


def test_gmail_monitor_paused_skips_poll_and_digest(db_session, monkeypatch) -> None:
    import app.gmail_monitor as gmail_monitor

    db_session.add(IntegrationConfig(key='gmail_zalo_monitor', payload={'enabled': False}))
    db_session.commit()
    monkeypatch.setattr(
        gmail_monitor,
        '_fetch_imap_messages',
        lambda config: (_ for _ in ()).throw(AssertionError('IMAP should not be called while paused')),
    )

    poll_result = poll_gmail_and_notify(db_session)
    digest_result = run_gmail_daily_digest(db_session, target_date=date(2026, 5, 11))

    assert poll_result['skipped'] is True
    assert digest_result['skipped'] is True
    assert poll_result['created'] == 0
    assert digest_result['notification_event_id'] is None


def test_admin_gmail_zalo_config_ui_masks_secrets_and_tests_delivery(client, monkeypatch) -> None:
    login = client.post('/auth/login', json={'username': 'trang', 'password': 'trang123'})
    token = login.json()['access_token']
    headers = {'Authorization': f'Bearer {token}'}
    calls: list[dict] = []

    def _success(payload: dict, **kwargs):
        calls.append({'payload': payload, 'kwargs': kwargs})
        return True, 200, '{"ok":true}', None

    monkeypatch.setattr('app.notifications._call_worker', _success)

    update = client.patch(
        '/admin/integrations/gmail-zalo',
        headers=headers,
        json={
            'gmail_address': 'etsy@example.com',
            'gmail_app_password': 'app-password',
            'gmail_oauth_client_id': 'oauth-client-id',
            'gmail_oauth_client_secret': 'oauth-client-secret',
            'gmail_oauth_redirect_uri': 'https://hazeleo.com/task-api/admin/integrations/gmail-zalo/oauth/callback',
            'enabled': False,
            'zalo_worker_url': 'http://worker.local',
            'zalo_worker_token': 'worker-token',
            'zalo_shared_secret': 'shared-secret',
            'zalo_group_id': 'zalo-group-from-ui',
        },
    )
    assert update.status_code == 200
    config = update.json()['config']
    assert config['enabled'] is False
    assert config['gmail_address'] == 'etsy@example.com'
    assert config['gmail_app_password_configured'] is True
    assert config['gmail_oauth_client_id'] == 'oauth-client-id'
    assert config['gmail_oauth_client_secret_configured'] is True
    assert config['gmail_oauth_redirect_uri'].endswith('/oauth/callback')
    assert 'app-password' not in str(config)
    assert 'oauth-client-secret' not in str(config)
    assert config['zalo_worker_token_configured'] is True
    assert config['zalo_shared_secret_configured'] is True

    test_send = client.post(
        '/admin/integrations/gmail-zalo/test-zalo',
        headers=headers,
        json={'message': 'hello group'},
    )
    assert test_send.status_code == 200
    assert test_send.json()['dispatch']['sent'] == 1
    assert calls
    assert calls[-1]['payload']['target_id'] == 'zalo-group-from-ui'
    assert calls[-1]['kwargs']['config']['zalo_worker_url'] == 'http://worker.local'


def test_admin_gmail_oauth_start_and_callback_store_refresh_token(client, monkeypatch, db_session) -> None:
    import app.main as main_module

    login = client.post('/auth/login', json={'username': 'trang', 'password': 'trang123'})
    token = login.json()['access_token']
    headers = {'Authorization': f'Bearer {token}'}
    redirect_uri = 'https://hazeleo.com/task-api/admin/integrations/gmail-zalo/oauth/callback'

    update = client.patch(
        '/admin/integrations/gmail-zalo',
        headers=headers,
        json={
            'gmail_oauth_client_id': 'oauth-client-id',
            'gmail_oauth_client_secret': 'oauth-client-secret',
            'gmail_oauth_redirect_uri': redirect_uri,
        },
    )
    assert update.status_code == 200

    start = client.post('/admin/integrations/gmail-zalo/oauth/start', headers=headers)
    assert start.status_code == 200
    auth_url = start.json()['auth_url']
    assert 'accounts.google.com' in auth_url
    stored = db_session.get(IntegrationConfig, 'gmail_zalo_monitor')
    assert stored is not None
    state = stored.payload['gmail_oauth_state']

    monkeypatch.setattr(
        main_module,
        'exchange_gmail_oauth_code',
        lambda config, code: {
            'refresh_token': 'refresh-token',
            'access_token': 'access-token',
            'email_address': 'etsy@example.com',
        },
    )
    callback = client.get(
        '/admin/integrations/gmail-zalo/oauth/callback',
        params={'code': 'oauth-code', 'state': state},
    )
    assert callback.status_code == 200

    db_session.expire_all()
    refreshed = db_session.get(IntegrationConfig, 'gmail_zalo_monitor')
    assert refreshed is not None
    assert refreshed.payload['gmail_oauth_refresh_token'] == 'refresh-token'
    assert refreshed.payload['gmail_oauth_email'] == 'etsy@example.com'
    assert 'gmail_oauth_state' not in refreshed.payload
