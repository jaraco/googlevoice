import asyncio
import json
import os
import re
import time
import types

import pytest
import responses

from googlevoice import Credentials, Voice, auth
from googlevoice import _browserlock as bl
from googlevoice.__main__ import main
from googlevoice.auth import API_BASE, load_session, sapisid_hash, save_session
from googlevoice.util import LoginError, Message, Thread
from googlevoice.voice import Folder, _thread_id_for, normalize_number

FAKE_COOKIES = [
    {'name': 'SAPISID', 'value': 'sapisid-val', 'domain': '.google.com', 'path': '/'},
    {
        'name': '__Secure-1PAPISID',
        'value': '1p-val',
        'domain': '.google.com',
        'path': '/',
    },
    {
        'name': '__Secure-3PAPISID',
        'value': '3p-val',
        'domain': '.google.com',
        'path': '/',
    },
    {'name': 'SID', 'value': 'sid-val', 'domain': '.google.com', 'path': '/'},
]

ACCOUNT_RESPONSE = {'account': {'primaryDid': '+12085551234', 'phones': {}}}
THREADS_RESPONSE = {
    'thread': [
        {
            'id': 't.+12085550000',
            'read': False,
            'isText': True,
            'headingContactsPhoneNumberKey': ['+12085550000'],
            'item': [
                {
                    'id': 'abc123',
                    'startTime': '1781375718535',
                    'did': '+12085551234',
                    'contact': {'phoneNumber': '+12085550000', 'name': 'Pat'},
                    'type': 'smsIn',
                    # real SMS also carry a coarseType -- incoming must not be
                    # fooled by it (it is never 'callTypeIncoming' for SMS).
                    'coarseType': 'callTypeSmsIn',
                    'messageText': 'hello there',
                }
            ],
        }
    ]
}


# A thread holding a voicemail (with audio + transcript) and a missed call --
# exercises the type filters and download.
VOICEMAIL_THREAD = {
    'thread': [
        {
            'id': 't.+12085559999',
            'read': True,
            'isText': False,
            'headingContactsPhoneNumberKey': ['+12085559999'],
            'item': [
                {
                    'id': 'vm1',
                    'startTime': '1781375718535',
                    'did': '+12085551234',
                    'contact': {'phoneNumber': '+12085559999'},
                    'type': 'voicemail',
                    'coarseType': 'callTypeVoicemail',
                    # Google leaves messageText empty on every voicemail and
                    # puts the transcript in scored word tokens instead.
                    'messageText': '',
                    'transcriptStatus': 'received',
                    'transcript': {
                        'confidence': 0.7193037,
                        'wordTokens': [
                            {'word': 'hey', 'confidence': 0.71},
                            {'word': 'call', 'confidence': 0.72},
                            {'word': 'me', 'confidence': 0.73},
                            {'word': 'back', 'confidence': 0.74},
                        ],
                    },
                    'duration': 10,
                    'recordingUrl': 'https://example.test/vm1.mp3',
                },
                {
                    'id': 'm1',
                    'startTime': '1781375710000',
                    'contact': {'phoneNumber': '+12085559999'},
                    'type': 'missed',
                    'coarseType': 'callTypeMissed',
                },
            ],
        }
    ]
}


@pytest.fixture
def voice():
    # auto_login off so a mocked 401 doesn't try to launch a real browser
    return Voice(credentials=Credentials(FAKE_COOKIES), auto_login=False)


class TestAuth:
    def test_sapisid_hash_format(self):
        value = sapisid_hash(FAKE_COOKIES, now=1_700_000_000)
        # All three labels present, each "<label> <ts>_<40-hex-sha1>".
        assert 'SAPISIDHASH 1700000000_' in value
        assert 'SAPISID1PHASH 1700000000_' in value
        assert 'SAPISID3PHASH 1700000000_' in value
        assert re.search(r'SAPISIDHASH \d+_[0-9a-f]{40}', value)

    def test_session_roundtrip(self, tmp_path):
        path = tmp_path / 'session.json'
        save_session(FAKE_COOKIES, path)
        assert load_session(path) == FAKE_COOKIES
        # written atomically (no temp file left behind) and owner-only
        assert [p.name for p in tmp_path.iterdir()] == ['session.json']
        if os.name == 'posix':
            assert path.stat().st_mode & 0o777 == 0o600

    def test_save_session_keeps_the_old_file_if_the_write_fails(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / 'session.json'
        save_session(FAKE_COOKIES, path)

        def disk_full(*args, **kwargs):
            raise OSError('disk full')

        monkeypatch.setattr(auth.os, 'replace', disk_full)
        with pytest.raises(OSError):
            save_session([], path)
        assert load_session(path) == FAKE_COOKIES
        assert [p.name for p in tmp_path.iterdir()] == ['session.json']

    def test_requests_session_preserves_cookie_domain(self):
        jar = Credentials(FAKE_COOKIES).requests_session().cookies
        # Dotted domain must be preserved so cookies reach clients6.google.com.
        assert jar.get('SAPISID', domain='.google.com') == 'sapisid-val'


class TestVoiceReads:
    @responses.activate
    def test_number(self, voice):
        responses.post(API_BASE + 'account/get', json=ACCOUNT_RESPONSE)
        assert voice.number == '+12085551234'

    @responses.activate
    def test_threads_parsed(self, voice):
        responses.post(API_BASE + 'api2thread/list', json=THREADS_RESPONSE)
        threads = voice.inbox()
        assert len(threads) == 1
        thread = threads[0]
        assert isinstance(thread, Thread)
        assert thread.id == 't.+12085550000'
        assert thread.read is False
        assert thread.contact == '+12085550000'
        assert thread.latest_text == 'hello there'
        assert thread.latest.incoming is True

    @responses.activate
    def test_thread_by_number(self, voice):
        single = {'thread': THREADS_RESPONSE['thread'][0]}
        responses.post(API_BASE + 'api2thread/get', json=single)
        found = voice.thread('+12085550000')
        assert found is not None
        assert found.id == 't.+12085550000'
        # request asks for the normalized thread id
        assert json.loads(responses.calls[-1].request.body)[0] == 't.+12085550000'

    @responses.activate
    def test_thread_not_found(self, voice):
        responses.post(API_BASE + 'api2thread/get', json={})  # no 'thread'
        assert voice.thread('+19998887777') is None

    @responses.activate
    def test_threads_messages_param(self, voice):
        responses.post(API_BASE + 'api2thread/list', json=THREADS_RESPONSE)
        voice.threads(messages=100)
        body = json.loads(responses.calls[-1].request.body)
        assert body[2] == 100  # messages-per-thread slot

    @responses.activate
    def test_thread_accepts_formatted_number(self, voice):
        responses.post(API_BASE + 'api2thread/get', json={'thread': {'id': 't.x'}})
        assert voice.thread('(208) 555-0000') is not None
        # formatted number is normalized before becoming the thread id
        assert json.loads(responses.calls[-1].request.body)[0] == 't.+12085550000'

    @responses.activate
    def test_login_error_on_401(self, voice):
        from googlevoice.util import LoginError

        responses.post(API_BASE + 'account/get', status=401, body='nope')
        with pytest.raises(LoginError):
            voice.account()

    @responses.activate
    def test_auto_login_refreshes_and_retries(self, monkeypatch):
        import googlevoice.voice as voicemod

        # First account/get 401s, then (after a "refresh") succeeds.
        responses.post(API_BASE + 'account/get', status=401, body='nope')
        responses.post(API_BASE + 'account/get', json=ACCOUNT_RESPONSE)

        calls = []

        def fake_login(*a, **k):
            calls.append(1)
            return Credentials(FAKE_COOKIES)

        monkeypatch.setattr(voicemod.auth, 'browser_login', fake_login)
        v = Voice(credentials=Credentials(FAKE_COOKIES), auto_login=True)
        assert v.number == '+12085551234'
        assert calls == [1]  # refreshed exactly once


class TestSend:
    @responses.activate
    def test_send_sms_body_shape(self, voice):
        responses.post(API_BASE + 'api2thread/sendsms', json={})
        voice.send_sms('+12085550000', 'hi', recaptcha=['tok', None, None, 'tok2'])
        body = json.loads(responses.calls[-1].request.body)
        assert len(body) == 11
        assert body[4] == 'hi'
        assert body[5] == 't.+12085550000'
        assert isinstance(body[8], list) and len(body[8]) == 1  # [nonce]
        assert body[10] == ['tok', None, None, 'tok2']

    def test_thread_id_for(self):
        assert _thread_id_for('+12085550000') == 't.+12085550000'
        assert _thread_id_for('(208) 555-0000') == 't.+12085550000'  # normalized
        assert _thread_id_for('t.+12085550000') == 't.+12085550000'
        assert _thread_id_for('g.Group Message.x') == 'g.Group Message.x'
        assert _thread_id_for('c.PCIFABCDEF') == 'c.PCIFABCDEF'  # call id passthrough


class TestNumbers:
    def test_normalize_number(self):
        assert normalize_number('+1 (208) 555-0123') == '+12085550123'
        assert normalize_number('208-555-0123') == '+12085550123'
        assert normalize_number('2085550123') == '+12085550123'
        assert normalize_number('12085550123') == '+12085550123'
        assert normalize_number('+447911123456') == '+447911123456'  # intl untouched
        assert normalize_number('22000') == '22000'  # short code untouched
        assert normalize_number('t.+12085550123') == 't.+12085550123'  # id untouched


class TestMessage:
    def test_message_attributes(self):
        msg = Message(THREADS_RESPONSE['thread'][0]['item'][0])
        assert msg.text == 'hello there'
        assert msg.type == 'smsIn'
        assert msg.incoming is True
        assert msg.phone_number == '+12085550000'
        assert msg.start_time is not None

    def test_incoming_direction(self):
        # SMS: type is authoritative even though a (non-Incoming) coarseType
        # is present -- the bug this guards against.
        assert Message({'type': 'smsIn', 'coarseType': 'callTypeSmsIn'}).incoming
        assert not Message({'type': 'smsOut', 'coarseType': 'callTypeSmsOut'}).incoming
        # calls/voicemail: direction from coarseType
        assert not Message({'type': 'sip', 'coarseType': 'callTypeOutgoing'}).incoming
        assert Message({'type': 'sip', 'coarseType': 'callTypeIncoming'}).incoming
        assert Message({'type': 'missed', 'coarseType': 'callTypeMissed'}).incoming
        assert Message({'type': 'voicemail', 'recordingUrl': 'x'}).incoming

    def test_as_dict(self):
        thread = Thread(None, THREADS_RESPONSE['thread'][0])
        d = thread.as_dict()
        assert d['id'] == 't.+12085550000'
        assert d['contact'] == '+12085550000'
        assert d['read'] is False
        msg = d['messages'][0]
        assert msg['text'] == 'hello there'
        assert msg['incoming'] is True
        # start_time is ISO-8601 with a UTC offset
        assert msg['start_time'].endswith('+00:00')


class TestFolders:
    @responses.activate
    def test_folder_selectors(self, voice):
        responses.post(API_BASE + 'api2thread/list', json=THREADS_RESPONSE)
        for call, expected in [
            (voice.calls, Folder.CALLS),
            (voice.inbox, Folder.INBOX),
            (voice.spam, Folder.SPAM),
            (voice.archived, Folder.ARCHIVE),
        ]:
            call()
            assert json.loads(responses.calls[-1].request.body)[0] == expected


class TestTypeFilters:
    @responses.activate
    def test_voicemails(self, voice):
        responses.post(API_BASE + 'api2thread/list', json=VOICEMAIL_THREAD)
        vms = voice.voicemails()
        assert len(vms) == 1
        assert vms[0].is_voicemail
        # messageText is empty on voicemails; the transcript words stand in.
        assert vms[0].text == 'hey call me back'
        assert vms[0].transcript == 'hey call me back'
        assert vms[0].transcript_status == 'received'
        assert vms[0].transcript_confidence == pytest.approx(0.7193037)
        assert vms[0].duration == 10  # seconds
        assert vms[0].as_dict()['duration'] == 10
        assert vms[0].as_dict()['text'] == 'hey call me back'
        assert vms[0].has_audio
        assert vms[0].recording_url == 'https://example.test/vm1.mp3'
        # voicemails come from the Voicemail folder
        assert json.loads(responses.calls[0].request.body)[0] == Folder.VOICEMAIL

    def test_voicemail_without_transcript(self):
        # transcriptStatus 'processingFailure': no transcript object at all.
        vm = Message({
            'id': 'vm9',
            'type': 'voicemail',
            'coarseType': 'callTypeVoicemail',
            'messageText': '',
            'transcriptStatus': 'processingFailure',
            'duration': 2,
            'recordingUrl': 'https://example.test/vm9.mp3',
        })
        assert vm.is_voicemail
        assert vm.transcript is None
        assert vm.text == ''  # the API's own empty body, not None
        assert vm.transcript_status == 'processingFailure'
        assert vm.transcript_confidence is None
        assert vm.duration == 2
        assert vm.as_dict()['duration'] == 2
        assert vm.as_dict()['text'] == ''

    def test_sms_text_and_duration(self):
        # SMS keep messageText and have no duration.
        sms = Message({'type': 'smsIn', 'messageText': 'hi there'})
        assert sms.text == 'hi there'
        assert sms.transcript is None
        assert sms.duration is None
        assert sms.as_dict()['duration'] is None

    def test_call_duration_is_seconds(self):
        call = Message({
            'type': 'sip',
            'coarseType': 'callTypeOutgoing',
            'duration': 37,
        })
        assert call.duration == 37
        # numbers arrive as strings at times; anything unparseable is None
        assert Message({'type': 'sip', 'duration': '12'}).duration == 12
        assert Message({'type': 'sip', 'duration': 'n/a'}).duration is None
        assert Message({'type': 'sip', 'duration': None}).duration is None

    @responses.activate
    def test_missed(self, voice):
        responses.post(API_BASE + 'api2thread/list', json=VOICEMAIL_THREAD)
        missed = voice.missed()
        assert len(missed) == 1
        assert missed[0].type == 'missed'
        assert json.loads(responses.calls[0].request.body)[0] == Folder.CALLS


class TestThreadActions:
    """thread/batchupdateattributes -- body [[[values, mask, 1]]]."""

    @responses.activate
    def test_archive_sets_index_5(self, voice):
        responses.post(API_BASE + 'thread/batchupdateattributes', json={})
        voice.archive('+12085550000')
        body = json.loads(responses.calls[-1].request.body)
        values, mask, tail = body[0][0]
        assert values == ['t.+12085550000', None, None, None, None, 1]
        assert mask == [None, None, None, None, None, 1]
        assert tail == 1

    @responses.activate
    def test_unarchive_sets_index_5_to_zero(self, voice):
        responses.post(API_BASE + 'thread/batchupdateattributes', json={})
        voice.unarchive('+12085550000')
        values = json.loads(responses.calls[-1].request.body)[0][0][0]
        assert values == ['t.+12085550000', None, None, None, None, 0]

    @responses.activate
    def test_spam_block_read_indices(self, voice):
        responses.post(API_BASE + 'thread/batchupdateattributes', json={})
        for action, index in [
            (voice.mark_spam, 2),
            (voice.block, 1),
            (voice.mark_read, 3),
        ]:
            action('+12085550000')
            values = json.loads(responses.calls[-1].request.body)[0][0][0]
            assert values[index] == 1 and len(values) == index + 1

    @responses.activate
    def test_thread_shortcut_calls_voice(self, voice):
        responses.post(API_BASE + 'thread/batchupdateattributes', json={})
        thread = Thread(voice, THREADS_RESPONSE['thread'][0])
        thread.mark_spam()
        values = json.loads(responses.calls[-1].request.body)[0][0][0]
        assert values == ['t.+12085550000', None, 1]  # index 2 = spam

    def test_delete_not_implemented(self, voice):
        with pytest.raises(NotImplementedError):
            voice.delete('+12085550000')


class TestDownload:
    @responses.activate
    def test_download_saves_audio(self, voice, tmp_path):
        responses.post(API_BASE + 'api2thread/list', json=VOICEMAIL_THREAD)
        responses.get('https://example.test/vm1.mp3', body=b'ID3audio')
        vm = voice.voicemails()[0]
        path = vm.download(str(tmp_path))
        assert path.endswith('vm1.mp3')
        assert (tmp_path / 'vm1.mp3').read_bytes() == b'ID3audio'

    @responses.activate
    def test_download_without_audio_raises(self, voice):
        from googlevoice.util import DownloadError

        responses.post(API_BASE + 'api2thread/list', json=THREADS_RESPONSE)
        msg = voice.inbox()[0].latest  # an SMS, no audio
        with pytest.raises(DownloadError):
            msg.download()


class TestSearch:
    @responses.activate
    def test_search_body_and_parse(self, voice):
        responses.post(API_BASE + 'api2thread/search', json=THREADS_RESPONSE)
        results = voice.search('hello', count=50)
        assert len(results) == 1
        body = json.loads(responses.calls[-1].request.body)
        assert body[0] == 'hello' and body[1] == 50


class TestProfileLock:
    @pytest.mark.skipif(bl.fcntl is None, reason='POSIX advisory locks only')
    def test_busy_profile_raises(self, tmp_path):
        held = bl.ProfileLock(tmp_path)
        held.acquire()
        try:
            with pytest.raises(bl.BrowserBusyError) as exc:
                bl.ProfileLock(tmp_path).acquire()  # same profile, already locked
            assert str(os.getpid()) in str(exc.value)  # error names the holder
        finally:
            held.release()
        # released -> can acquire again
        again = bl.ProfileLock(tmp_path)
        again.acquire()
        again.release()

    @pytest.mark.skipif(os.name != 'posix', reason='symlink / os.kill semantics')
    def test_clears_stale_singleton(self, tmp_path):
        # Chrome's lock owned by a dead pid should be removed.
        (tmp_path / 'SingletonLock').symlink_to(f'somehost-{0x7FFFFFFF}')
        assert os.path.lexists(tmp_path / 'SingletonLock')
        bl.clear_stale_singleton(tmp_path)
        assert not os.path.lexists(tmp_path / 'SingletonLock')


class TestCLI:
    @responses.activate
    def test_inbox_json(self, monkeypatch, capsys):
        responses.post(API_BASE + 'api2thread/list', json=THREADS_RESPONSE)
        monkeypatch.setattr(
            'googlevoice.__main__.Voice',
            lambda: Voice(credentials=Credentials(FAKE_COOKIES)),
        )
        main(['inbox', '--json'])
        data = json.loads(capsys.readouterr().out)
        assert data[0]['id'] == 't.+12085550000'
        assert data[0]['messages'][0]['text'] == 'hello there'


# --------------------------------------------------------------------------- #
# Browser session helpers -- exercised against fakes standing in for nodriver's
# Tab/Browser (real CDP command objects, no Chrome).
# --------------------------------------------------------------------------- #
VOICE_URL = 'https://voice.google.com/u/0/messages'
SIGNED_OUT_URL = 'https://workspace.google.com/products/voice/'


def _run(coro):
    return asyncio.run(coro)


def _browser_cookie(name, domain='.google.com', expires=-1, **extra):
    """A stand-in for ``cdp.network.Cookie`` as ``browser.cookies.get_all()`` yields."""
    base = {
        'name': name,
        'value': f'{name}-val',
        'domain': domain,
        'path': '/',
        'secure': True,
        'http_only': True,
        'same_site': None,
        'expires': expires,
    }
    base.update(extra)
    return types.SimpleNamespace(**base)


class FakeTab:
    """Records the CDP commands sent and the navigations made."""

    def __init__(self, urls=()):
        self.urls = list(urls)  # what settled_url should report, in order
        self.sent = []  # decoded CDP requests ({'method', 'params'})
        self.gets = []

    async def send(self, cmd):
        request = next(cmd)  # a CDP command is a generator yielding its request
        self.sent.append(request)
        try:
            cmd.send({'success': True})
        except StopIteration as stop:
            return stop.value
        return None

    async def get(self, url):
        self.gets.append(url)

    def params(self, method):
        return [r['params'] for r in self.sent if r['method'] == method]


class FakeBrowser:
    def __init__(self, cookies=(), *, close_ok=True):
        self._cookies = list(cookies)
        self.cookies = types.SimpleNamespace(get_all=self._get_all)
        self.close_ok = close_ok
        self.stopped = False
        self.closed = False
        self.sent = []
        self._process = types.SimpleNamespace(wait=self._wait)
        self._process_pid = 4242

    async def _get_all(self):
        return self._cookies

    async def _wait(self):
        return 0

    async def send(self, cmd):
        if not self.close_ok:
            raise ConnectionError('CDP gone')
        self.sent.append(next(cmd))

    async def aclose(self):
        self.closed = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def nodriver():
    """The CDP command objects come from nodriver, which CI does not install."""
    return pytest.importorskip('nodriver')


@pytest.fixture
def scripted_urls(monkeypatch):
    """Make ``settled_url`` report the tab's scripted URLs instead of polling."""

    async def fake_settled(tab, **_):
        return tab.urls.pop(0) if len(tab.urls) > 1 else tab.urls[0]

    monkeypatch.setattr(auth, 'settled_url', fake_settled)


class TestBrowserSession:
    @pytest.mark.parametrize(
        ('url', 'expected'),
        [
            (VOICE_URL, True),
            ('https://voice.google.com/', True),
            ('https://voice.google.com/about', False),
            ('https://voice.google.com/about/', False),
            (SIGNED_OUT_URL, False),
            ('https://accounts.google.com/v3/signin/identifier?x=1', False),
            ('', False),
            (None, False),
        ],
    )
    def test_is_signed_in_url(self, url, expected):
        assert auth.is_signed_in_url(url) is expected

    def test_settled_url_waits_out_redirects(self):
        states = iter([
            'loading https://voice.google.com/',
            'complete https://voice.google.com/',
            'complete https://accounts.google.com/x',
            'complete https://accounts.google.com/x',
            'complete never-reached',
        ])
        tab = types.SimpleNamespace()

        async def evaluate(expr, await_promise=False):
            return next(states)

        tab.evaluate = evaluate
        assert _run(auth.settled_url(tab, poll=0)) == 'https://accounts.google.com/x'

    def test_inject_cookies_pins_session_cookies_and_skips_expired(self, nodriver):
        now = time.time()
        cookies = [
            # session cookie on a domain -> pinned with an expiry, Domain kept
            {'name': 'SID', 'value': 's', 'domain': '.google.com', 'expires': -1},
            # persistent -> expiry preserved
            {
                'name': 'NID',
                'value': 'n',
                'domain': '.google.com',
                'path': '/',
                'expires': now + 100,
                'same_site': 'None',
                'secure': True,
            },
            # host-only -> set via URL, no Domain attribute (``__Host-`` rule)
            {
                'name': '__Host-GAPS',
                'value': 'g',
                'domain': 'accounts.google.com',
                'path': '/',
                'expires': None,
            },
            # already expired -> skipped entirely (0 is the epoch, not "session")
            {'name': 'OLD', 'value': 'o', 'domain': '.google.com', 'expires': now - 5},
            {'name': 'EPOCH', 'value': 'e', 'domain': '.google.com', 'expires': 0},
        ]
        tab = FakeTab()
        assert _run(auth.inject_cookies(tab, cookies, ttl=1000)) == 3
        by_name = {p['name']: p for p in tab.params('Network.setCookie')}
        assert set(by_name) == {'SID', 'NID', '__Host-GAPS'}
        assert by_name['SID']['domain'] == '.google.com'
        assert now + 900 < float(by_name['SID']['expires']) <= now + 1100
        assert float(by_name['NID']['expires']) == pytest.approx(now + 100)
        assert by_name['NID']['sameSite'] == 'None'
        assert 'domain' not in by_name['__Host-GAPS']
        assert by_name['__Host-GAPS']['url'] == 'https://accounts.google.com/'
        assert by_name['__Host-GAPS']['secure'] is True  # the prefix demands it
        assert float(by_name['__Host-GAPS']['expires']) > now

    def test_inject_cookies_legacy_records_keep_http_only(self, nodriver):
        # Files written before the harvester stored the flag: infer it for the
        # known HttpOnly login cookies, leave the script-readable ones alone.
        cookies = [
            {'name': 'HSID', 'value': 'h', 'domain': '.google.com', 'expires': -1},
            {'name': 'SAPISID', 'value': 'p', 'domain': '.google.com', 'expires': -1},
            {
                'name': 'SID',
                'value': 's',
                'domain': '.google.com',
                'expires': -1,
                'http_only': False,
            },
        ]
        tab = FakeTab()
        assert _run(auth.inject_cookies(tab, cookies)) == 3
        by_name = {p['name']: p for p in tab.params('Network.setCookie')}
        assert by_name['HSID']['httpOnly'] is True
        assert by_name['SAPISID']['httpOnly'] is False
        assert by_name['SID']['httpOnly'] is False

    def test_inject_cookies_host_only_scheme_follows_secure_flag(self, nodriver):
        cookies = [
            {
                'name': 'plain',
                'value': 'x',
                'domain': 'voice.google.com',
                'path': '/u/',
                'secure': False,
                'expires': -1,
            },
            {
                'name': 'COMPASS',
                'value': 'c',
                'domain': 'voice.google.com',
                'secure': True,
                'expires': -1,
            },
        ]
        tab = FakeTab()
        assert _run(auth.inject_cookies(tab, cookies)) == 2
        by_name = {p['name']: p for p in tab.params('Network.setCookie')}
        assert by_name['plain']['url'] == 'http://voice.google.com/u/'
        assert by_name['plain']['secure'] is False
        assert by_name['COMPASS']['url'] == 'https://voice.google.com/'

    def test_inject_cookies_skips_malformed_records(self, nodriver):
        cookies = [
            {'value': 'no-name', 'domain': '.google.com'},
            {'name': 'BAD', 'value': 'b', 'domain': '.google.com', 'expires': 'soon'},
            {
                'name': 'ODD',
                'value': 'o',
                'domain': '.google.com',
                'same_site': 'unspecified',
                'expires': -1,
            },
            {'name': 'OK', 'value': 'k', 'domain': '.google.com', 'expires': -1},
        ]
        tab = FakeTab()
        assert _run(auth.inject_cookies(tab, cookies)) == 2
        by_name = {p['name']: p for p in tab.params('Network.setCookie')}
        assert set(by_name) == {'ODD', 'OK'}
        assert 'sameSite' not in by_name['ODD']  # unknown value dropped, cookie kept

    def test_ensure_signed_in_uses_profile_when_already_signed_in(
        self, scripted_urls, tmp_path
    ):
        tab = FakeTab([VOICE_URL])
        landed = _run(auth.ensure_signed_in(FakeBrowser(), tab, session_path=tmp_path))
        assert landed == VOICE_URL
        assert tab.sent == [] and tab.gets == []

    def test_ensure_signed_in_injects_saved_session_and_reloads(
        self, nodriver, scripted_urls, tmp_path
    ):
        session = save_session(FAKE_COOKIES, tmp_path / 'session.json')
        tab = FakeTab([SIGNED_OUT_URL, VOICE_URL])
        landed = _run(auth.ensure_signed_in(FakeBrowser(), tab, session_path=session))
        assert landed == VOICE_URL
        assert tab.gets == [auth.ORIGIN]  # reloaded after injecting
        names = {p['name'] for p in tab.params('Network.setCookie')}
        assert names == {c['name'] for c in FAKE_COOKIES}

    def test_ensure_signed_in_rejects_an_account_without_a_number(self, scripted_urls):
        tab = FakeTab(['https://voice.google.com/u/0/signup'])
        with pytest.raises(LoginError, match='no Google Voice number'):
            _run(auth.ensure_signed_in(FakeBrowser(), tab, session_path=None))

    def test_ensure_signed_in_without_session_file(self, scripted_urls, tmp_path):
        tab = FakeTab([SIGNED_OUT_URL])
        with pytest.raises(LoginError, match='googlevoice login'):
            _run(
                auth.ensure_signed_in(
                    FakeBrowser(), tab, session_path=tmp_path / 'missing.json'
                )
            )
        assert tab.gets == []

    def test_ensure_signed_in_when_saved_session_is_dead(
        self, nodriver, scripted_urls, tmp_path
    ):
        session = save_session(FAKE_COOKIES, tmp_path / 'session.json')
        tab = FakeTab([SIGNED_OUT_URL, SIGNED_OUT_URL])
        with pytest.raises(LoginError, match='no longer works'):
            _run(auth.ensure_signed_in(FakeBrowser(), tab, session_path=session))

    def test_ensure_signed_in_profile_only_mode(self, scripted_urls, tmp_path):
        tab = FakeTab([SIGNED_OUT_URL])
        with pytest.raises(LoginError):
            _run(auth.ensure_signed_in(FakeBrowser(), tab, session_path=None))
        assert tab.sent == []

    def test_harvest_keeps_google_cookies_only(self):
        browser = FakeBrowser([
            _browser_cookie('SID'),
            _browser_cookie('other', domain='.example.com'),
            _browser_cookie('__Host-GAPS', domain='accounts.google.com', expires=5.0),
        ])
        got = _run(auth.harvest_cookies(browser))
        assert [c['name'] for c in got] == ['SID', '__Host-GAPS']
        assert got[0]['expires'] == -1 and got[0]['http_only'] is True
        assert got[1]['domain'] == 'accounts.google.com'

    def test_refresh_session_requires_essential_cookies(self, tmp_path):
        path = tmp_path / 'session.json'
        ok = lambda cookies: True
        partial = FakeBrowser([_browser_cookie('SID')])
        assert _run(auth.refresh_session(partial, path, validate=ok)) is False
        assert not path.exists()
        # a name whose value is empty does not count as present
        hollow = FakeBrowser([
            _browser_cookie(n, value='') for n in auth.ESSENTIAL_COOKIES
        ])
        assert _run(auth.refresh_session(hollow, path, validate=ok)) is False
        assert not path.exists()
        full = FakeBrowser([_browser_cookie(n) for n in auth.ESSENTIAL_COOKIES])
        assert _run(auth.refresh_session(full, path, validate=ok)) is True
        assert {c['name'] for c in load_session(path)} == auth.ESSENTIAL_COOKIES

    def test_refresh_session_keeps_the_file_when_cookies_do_not_authenticate(
        self, tmp_path
    ):
        path = tmp_path / 'session.json'
        save_session(FAKE_COOKIES, path)
        probed = []
        browser = FakeBrowser([_browser_cookie(n) for n in auth.ESSENTIAL_COOKIES])

        def validate(cookies):
            probed.append({c['name'] for c in cookies})
            return False

        assert _run(auth.refresh_session(browser, path, validate=validate)) is False
        assert probed == [auth.ESSENTIAL_COOKIES]
        assert load_session(path) == FAKE_COOKIES  # untouched

    def test_refresh_session_probes_account_get_by_default(self, tmp_path, monkeypatch):
        probes = []
        monkeypatch.setattr(
            auth,
            'session_is_valid',
            lambda cookies: probes.append(len(cookies)) or True,
        )
        browser = FakeBrowser([_browser_cookie(n) for n in auth.ESSENTIAL_COOKIES])
        assert _run(auth.refresh_session(browser, tmp_path / 's.json')) is True
        assert probes == [len(auth.ESSENTIAL_COOKIES)]

    def test_close_browser_prefers_graceful_close(self, nodriver):
        browser = FakeBrowser()
        _run(auth.close_browser(browser))
        assert browser.sent[0]['method'] == 'Browser.close'
        assert browser.closed and not browser.stopped
        assert browser._process is None

    def test_close_browser_falls_back_to_stop(self, nodriver):
        browser = FakeBrowser(close_ok=False)
        _run(auth.close_browser(browser))
        assert browser.stopped

    def test_close_browser_bounds_a_hung_cdp_close(self, nodriver):
        browser = FakeBrowser()

        async def hang(cmd):
            next(cmd)
            await asyncio.sleep(3600)

        browser.send = hang
        _run(auth.close_browser(browser, timeout=0.05))
        assert browser.stopped

    def test_close_browser_kills_a_chrome_that_ignores_sigterm(self, nodriver):
        class Stubborn:
            def __init__(self):
                self.returncode = None
                self.killed = False
                self._dead = asyncio.Event()

            def kill(self):
                self.killed = True
                self.returncode = -9
                self._dead.set()

            async def wait(self):
                await self._dead.wait()
                return self.returncode

        browser = FakeBrowser(close_ok=False)
        browser._process = process = Stubborn()
        _run(auth.close_browser(browser, timeout=0.05))
        assert browser.stopped and process.killed

    def test_sender_close_refreshes_session_then_quits(
        self, nodriver, tmp_path, monkeypatch
    ):
        from googlevoice.browser import BrowserSender

        monkeypatch.setattr(auth, 'session_is_valid', lambda cookies: True)
        path = tmp_path / 'session.json'
        sender = BrowserSender(tmp_path / 'profile', session_path=path)
        sender._loop = asyncio.new_event_loop()
        sender._browser = FakeBrowser([
            _browser_cookie(n) for n in auth.ESSENTIAL_COOKIES
        ])
        sender._tab = object()
        sender._signed_in = True
        browser = sender._browser
        sender.close()
        assert sender._browser is None and sender._tab is None
        assert path.exists()  # rotated cookies saved for next time
        assert browser.sent[0]['method'] == 'Browser.close'
        sender._loop.close()

    def test_sender_close_releases_the_lock_on_interrupt(self, tmp_path):
        from googlevoice.browser import BrowserSender

        sender = BrowserSender(tmp_path / 'profile', session_path=None)
        sender._loop = asyncio.new_event_loop()
        sender._browser = browser = FakeBrowser()
        released = []
        sender._lock = types.SimpleNamespace(release=lambda: released.append(True))

        async def interrupted(browser):
            raise KeyboardInterrupt

        sender._shutdown = interrupted
        with pytest.raises(KeyboardInterrupt):
            sender.close()
        assert released == [True]
        assert browser.stopped
        assert sender._browser is None and sender._lock is None
        sender._loop.close()
