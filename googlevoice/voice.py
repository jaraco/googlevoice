"""
Client for the modern Google Voice web API.

The legacy ``/voice/b/0/`` HTML/XML endpoints this library was built on are
gone (see issues #7 and #8).  This is a ground-up reimplementation against the
JSON API the current voice.google.com web app uses:

    https://clients6.google.com/voice/v1/voiceclient/...

Authentication is handled by :mod:`googlevoice.auth` (a portable cookie
session, no browser needed at call time).  Typical use::

    from googlevoice import Voice
    v = Voice()                       # loads ~/.googlevoice/session.json
    print(v.number)                   # your Google Voice number
    for thread in v.threads():        # recent SMS/voicemail threads
        print(thread.contact, thread.latest_text)
    v.archive(thread)                 # archive / spam / block etc. all work
    v.send_sms('+15555551234', 'Hi')  # sending needs a browser (see below)

Run ``python -m googlevoice.auth login`` once first to create the session.

Reads and *state changes* (archive, spam, block, mark-read) are plain HTTP
calls.  Only *sending* is gated behind anti-abuse tokens that require a browser
(see :mod:`googlevoice.browser`).

The endpoint shapes here were reverse-engineered from the live web app's
network traffic (``python -m googlevoice capture``) and verified against a real
account.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from . import auth, util
from .auth import (
    API_BASE,
    API_KEY,
    DEFAULT_PROFILE_DIR,
    DEFAULT_SESSION_PATH,
    Credentials,
)

log = logging.getLogger(__name__)

# Country code assumed for bare national numbers (e.g. ``2085551234``). Change
# this for non-US accounts.
DEFAULT_COUNTRY_CODE = '+1'

# Thread-id prefixes that are already ids (not dialable numbers): ``t.`` single
# number, ``g.`` group, ``c.`` call/voicemail conversation.
_ID_PREFIXES = ('t.', 'g.', 'c.')


def normalize_number(number: str) -> str:
    """
    Normalize a phone number for use as a thread id / recipient.

    Strips common formatting (spaces, parens, dashes, dots) and, for bare
    national numbers, prepends :data:`DEFAULT_COUNTRY_CODE`. Already-E.164
    numbers, thread ids (``t.``/``g.``/``c.``), and short codes are passed
    through.

    >>> normalize_number('+1 (208) 555-0123')
    '+12085550123'
    >>> normalize_number('2085550123')
    '+12085550123'
    >>> normalize_number('22000')
    '22000'
    """
    n = number.strip()
    if n.startswith(_ID_PREFIXES):
        return n
    n = re.sub(r'[\s()\-.]', '', n)
    if n.startswith('+'):
        return n
    digits = n.lstrip('+')
    if len(digits) <= 6:  # short code (e.g. 22000) -- not a dialable number
        return n
    if len(digits) == 11 and digits.startswith('1'):
        return '+' + digits
    if len(digits) == 10:
        return DEFAULT_COUNTRY_CODE + digits
    return n


class Folder:
    """
    Selector for the ``folder`` slot (``body[0]``) of ``api2thread/list``.

    These integers were confirmed against a live account: the Archive and Spam
    views each hold all three conversation kinds (SMS, calls, voicemail).
    There is **no Trash folder** -- in the modern web app deletion is permanent.
    """

    CALLS = 1  # call history (placed / received / missed)
    INBOX = 2  # text-message conversations (the default Messages view)
    VOICEMAIL = 4  # voicemail conversations
    SPAM = 5  # spam (all kinds)
    ARCHIVE = 6  # archived conversations (all kinds)


class MessageType:
    """Values of a message's ``type`` field (verified against a live account)."""

    SMS_IN = 'smsIn'
    SMS_OUT = 'smsOut'
    SIP = 'sip'  # a placed or received call (direction is in ``coarseType``)
    MISSED = 'missed'  # a missed call
    VOICEMAIL = 'voicemail'  # a voicemail (``recordingUrl`` + transcript)


class CoarseType:
    """Values of a call's ``coarseType`` field (call direction)."""

    PLACED = 'callTypeOutgoing'  # verified
    RECEIVED = 'callTypeIncoming'
    MISSED = 'callTypeMissed'


# --------------------------------------------------------------------------- #
# State changes: thread/batchupdateattributes
# --------------------------------------------------------------------------- #
# A single endpoint changes a thread's state.  Its body is
#
#     [[[ values, mask, 1 ]]]
#
# where ``values[0]`` is the thread id and a flag (0/1) sits at one further
# index; ``mask`` is a parallel array with a ``1`` marking that index.  The
# index selects which attribute changes (verified by toggling each on a live
# thread and watching which folder it moved to):
class _Attr:
    BLOCK = 1  # block / unblock the other party
    SPAM = 2  # mark / unmark spam  (moves to/from the Spam folder)
    READ = 3  # mark read / unread
    ARCHIVE = 5  # archive / unarchive (moves to/from the Archive folder)


def _update_attr_body(thread_id: str, index: int, value: int) -> list:
    """Build the ``thread/batchupdateattributes`` body to set one attribute."""
    values = [thread_id] + [None] * (index - 1) + [value]
    mask = [None] * index + [1]
    return [[[values, mask, 1]]]


# Voicemail/recording audio travels in the message JSON under this key
# (verified: an authenticated GET of it returns the MP3).
_MEDIA_URL_KEYS = ('recordingUrl',)


class Voice:
    """
    Main entry point for the modern Google Voice API.

    Pass :class:`~googlevoice.auth.Credentials`, or leave it ``None`` to load
    the default saved session (``~/.googlevoice/session.json``).

    If ``auto_login`` is true (default), a call that Google rejects with HTTP
    401 (an expired session) triggers a one-time browser re-login -- which
    auto-confirms in seconds when the saved Chrome profile is still signed in --
    and the call is retried.  Set ``auto_login=False`` for headless servers
    with no browser.
    """

    def __init__(
        self,
        credentials: Credentials | None = None,
        *,
        session_path=DEFAULT_SESSION_PATH,
        profile_dir=DEFAULT_PROFILE_DIR,
        auto_login: bool = True,
    ):
        self._session_path = session_path
        self._profile_dir = profile_dir
        self._auto_login = auto_login
        self.credentials = credentials or Credentials.load(session_path)
        self._http = self.credentials.requests_session()

    # ----------------------------------------------------------------- #
    # Low-level call
    # ----------------------------------------------------------------- #
    def _call(
        self, endpoint: str, body: Any, *, alt: str = 'json', _retried=False
    ) -> Any:
        """POST ``body`` (a JSON-able protojson array) to ``endpoint``."""
        resp = self._http.post(
            API_BASE + endpoint,
            params={'alt': alt, 'key': API_KEY},
            headers=self.credentials.auth_headers(),
            data=json.dumps(body),
            timeout=30,
        )
        if resp.status_code == 401:
            if self._auto_login and not _retried and self._refresh_session():
                return self._call(endpoint, body, alt=alt, _retried=True)
            raise util.LoginError(
                'Google rejected the session (401). It may be expired -- '
                're-run `python -m googlevoice.auth login`.'
            )
        if resp.status_code != 200:
            raise util.APIError(
                f'{endpoint} -> HTTP {resp.status_code}: {resp.text[:300]}'
            )
        if alt == 'json':
            try:
                return resp.json()
            except ValueError as err:
                raise util.APIError(f'{endpoint}: bad JSON response') from err
        return resp.text

    def _refresh_session(self) -> bool:
        """Re-mint the session via a browser login; return True on success."""
        log.info('session expired -- refreshing via browser login')
        try:
            self.credentials = auth.browser_login(
                self._session_path, self._profile_dir, timeout=120
            )
        except Exception as err:  # noqa: BLE001 - nodriver missing, signed out, etc.
            log.warning('auto re-login failed: %s', err)
            return False
        self._http = self.credentials.requests_session()
        return True

    # ----------------------------------------------------------------- #
    # Account
    # ----------------------------------------------------------------- #
    def account(self) -> dict:
        """Raw account info (primary number, phones, settings)."""
        return self._call('account/get', [None, 1])['account']

    @property
    def number(self) -> str | None:
        """Your Google Voice number in E.164 form, e.g. ``+12085551234``."""
        return self.account().get('primaryDid')

    # ----------------------------------------------------------------- #
    # Reading conversations
    # ----------------------------------------------------------------- #
    def threads(
        self,
        folder: int = Folder.INBOX,
        count: int = 20,
        *,
        messages: int = 15,
        cursor: str | None = None,
    ) -> list[util.Thread]:
        """
        Return recent conversation :class:`~googlevoice.util.Thread` objects.

        ``count`` is how many conversations to return; ``messages`` is how many
        recent messages to include per conversation. ``cursor`` is the
        ``startTime`` of the oldest thread from a previous page (for paging).
        """
        body = [folder, count, messages, cursor, None, [None, 1, 1, 1]]
        data = self._call('api2thread/list', body)
        return [util.Thread(self, t) for t in data.get('thread', [])]

    def inbox(self, count: int = 20) -> list[util.Thread]:
        """Text-message conversations (the default Messages view)."""
        return self.threads(Folder.INBOX, count)

    def calls(self, count: int = 20) -> list[util.Thread]:
        """Call-history conversations (placed / received / missed)."""
        return self.threads(Folder.CALLS, count)

    def archived(self, count: int = 20) -> list[util.Thread]:
        """Archived conversations (all kinds). Restore with :meth:`unarchive`."""
        return self.threads(Folder.ARCHIVE, count)

    def spam(self, count: int = 20) -> list[util.Thread]:
        """Spam conversations (all kinds). Clear with :meth:`mark_not_spam`."""
        return self.threads(Folder.SPAM, count)

    def thread(self, recipient: str, *, messages: int = 100) -> util.Thread | None:
        """
        Return the single conversation with ``recipient`` (a number this library
        can normalize, or a thread id), with up to ``messages`` recent messages,
        or ``None`` if there is no such conversation.
        """
        tid = _thread_id_for(recipient)
        data = self._call('api2thread/get', [tid, messages, None, [None, 1, 1]])
        t = data.get('thread')
        return util.Thread(self, t) if t else None

    def search(self, query: str, *, count: int = 200) -> list[util.Thread]:
        """Full-text search across conversations (the web app's top search box)."""
        body = [query, count, None, None, None, [None, 1, 1, 1]]
        data = self._call('api2thread/search', body)
        return [util.Thread(self, t) for t in data.get('thread', [])]

    # ----------------------------------------------------------------- #
    # Message-type filters (voicemail and the call kinds)
    # ----------------------------------------------------------------- #
    def _messages(self, folder: int, count: int) -> list[util.Message]:
        return [m for t in self.threads(folder, count) for m in t.messages]

    def voicemails(self, count: int = 20) -> list[util.Message]:
        """Voicemail messages -- :attr:`Message.text` is the transcript and
        :meth:`Message.download` fetches the audio."""
        return [m for m in self._messages(Folder.VOICEMAIL, count) if m.is_voicemail]

    def missed(self, count: int = 20) -> list[util.Message]:
        """Missed-call records."""
        return [
            m
            for m in self._messages(Folder.CALLS, count)
            if m.type == MessageType.MISSED or m.coarse_type == CoarseType.MISSED
        ]

    def placed(self, count: int = 20) -> list[util.Message]:
        """Outgoing (placed) call records."""
        return [
            m
            for m in self._messages(Folder.CALLS, count)
            if m.coarse_type == CoarseType.PLACED
        ]

    def received(self, count: int = 20) -> list[util.Message]:
        """Answered incoming-call records."""
        return [
            m
            for m in self._messages(Folder.CALLS, count)
            if m.coarse_type == CoarseType.RECEIVED
        ]

    def recorded(self, count: int = 20) -> list[util.Message]:
        """Recorded calls / voicemails that have downloadable audio."""
        msgs = self._messages(Folder.CALLS, count) + self._messages(
            Folder.VOICEMAIL, count
        )
        return [m for m in msgs if m.has_audio]

    # ----------------------------------------------------------------- #
    # State changes (archive / spam / block / read) -- no browser needed
    # ----------------------------------------------------------------- #
    def _set_attr(self, target: util.Thread | str, index: int, value: int) -> dict:
        tid = target.id if isinstance(target, util.Thread) else _thread_id_for(target)
        return self._call(
            'thread/batchupdateattributes', _update_attr_body(tid, index, value)
        )

    def archive(self, target: util.Thread | str) -> dict:
        """Move a conversation to the Archive."""
        return self._set_attr(target, _Attr.ARCHIVE, 1)

    def unarchive(self, target: util.Thread | str) -> dict:
        """Restore an archived conversation."""
        return self._set_attr(target, _Attr.ARCHIVE, 0)

    def mark_spam(self, target: util.Thread | str) -> dict:
        """Flag a conversation as spam."""
        return self._set_attr(target, _Attr.SPAM, 1)

    def mark_not_spam(self, target: util.Thread | str) -> dict:
        """Clear the spam flag from a conversation."""
        return self._set_attr(target, _Attr.SPAM, 0)

    def block(self, target: util.Thread | str) -> dict:
        """Block the other party on a conversation."""
        return self._set_attr(target, _Attr.BLOCK, 1)

    def unblock(self, target: util.Thread | str) -> dict:
        """Unblock the other party on a conversation."""
        return self._set_attr(target, _Attr.BLOCK, 0)

    def mark_read(self, target: util.Thread | str, read: bool = True) -> dict:
        """Mark a conversation read (``read=False`` marks it unread)."""
        return self._set_attr(target, _Attr.READ, 1 if read else 0)

    def mark_all_read(self, folder: int = Folder.INBOX) -> dict:
        """Mark every conversation in ``folder`` as read."""
        return self._call('thread/markallread', [folder])

    def delete(self, target: util.Thread | str) -> dict:
        """
        Permanently delete a conversation.

        .. warning::
           **Not yet implemented.**  Deletion in the modern web app is
           *permanent* (there is no Trash), and its request shape was not among
           the captured calls, so it is intentionally not guessed here.  To add
           it safely, run ``python -m googlevoice capture``, delete a throwaway
           conversation, and wire the captured endpoint/body in.
        """
        raise NotImplementedError(
            'delete is permanent and its API shape is unverified; capture it '
            'first (see the docstring).'
        )

    # ----------------------------------------------------------------- #
    # Voicemail / recording download
    # ----------------------------------------------------------------- #
    def download(self, message: util.Message | dict, dest: str | None = None) -> str:
        """
        Download the voicemail/recording audio for ``message`` to ``dest`` (a
        directory, default: cwd) as ``<message id>.mp3`` and return the path.
        """
        import os

        data = message._data if isinstance(message, util.Message) else message
        url = next((data[k] for k in _MEDIA_URL_KEYS if data.get(k)), None)
        if not url:
            raise util.DownloadError('This message has no recording to download.')
        resp = self._http.get(url, headers=self.credentials.auth_headers(), timeout=60)
        if resp.status_code not in (200, 206):
            raise util.DownloadError(f'download -> HTTP {resp.status_code}')
        path = os.path.join(dest or os.getcwd(), f'{data.get("id", "voicemail")}.mp3')
        with open(path, 'wb') as fo:
            fo.write(resp.content)
        return path

    # ----------------------------------------------------------------- #
    # Sending
    # ----------------------------------------------------------------- #
    def send_sms(
        self,
        recipient: str,
        text: str,
        *,
        thread_id: str | None = None,
        recaptcha: list | None = None,
    ) -> dict:
        """
        Send an SMS ``text`` to ``recipient`` (E.164, e.g. ``+12085551234``).

        If ``thread_id`` is given the message is sent into that existing
        conversation (e.g. a ``g.Group Message.<id>`` group thread); otherwise
        it is derived from ``recipient`` (``t.<E.164>``).

        .. important::
           Unlike reads and state changes, Google gates *sending* behind
           reCAPTCHA Enterprise + BotGuard anti-abuse tokens (the final element
           of the body), which can only be produced by executing Google's
           JavaScript in a browser.  Prefer :class:`googlevoice.browser.
           BrowserSender`, which drives a browser to mint those and send.  This
           low-level method only sends if you pass a pre-minted ``recaptcha``
           payload (``["!<botguard-token>"]``); without it Google returns 429.
        """
        tid = thread_id or _thread_id_for(recipient)
        nonce = int(time.time() * 1000)
        # protojson body for api2thread/sendsms, verified from live traffic:
        # four leading nulls, text, thread id, two nulls, [nonce], null, then
        # the reCAPTCHA/anti-abuse token payload.
        body = [None, None, None, None, text, tid, None, None, [nonce], None, recaptcha]
        return self._call('api2thread/sendsms', body)


def _thread_id_for(recipient: str) -> str:
    """Map a recipient to a thread id (``t.<E.164>``); pass ids through."""
    normalized = normalize_number(recipient)
    if normalized.startswith(_ID_PREFIXES):
        return normalized
    return 't.' + normalized
