"""
Data wrappers and exceptions for the modern Google Voice API.

The legacy API returned XML wrapping JSON wrapping HTML; the modern API returns
plain JSON, so the old ``XMLParser`` machinery is gone.  These light wrappers
give attribute access over the JSON the ``voiceclient`` endpoints return.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class LoginError(Exception):
    """The session is missing, rejected, or expired."""


class APIError(Exception):
    """A voiceclient API call returned a non-200 response."""


class DownloadError(Exception):
    """A voicemail/recording could not be downloaded."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ms_to_datetime(ms: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Messages and threads
# --------------------------------------------------------------------------- #
class Message:
    """A single message (SMS, voicemail, or call) within a thread."""

    def __init__(self, data: dict, voice=None):
        self._data = data
        self.voice = voice

    @property
    def id(self) -> str | None:
        return self._data.get('id')

    @property
    def text(self) -> str | None:
        """The SMS body, or the voicemail transcript.

        Google only fills ``messageText`` for SMS. For a voicemail it is always
        the empty string and the transcript arrives as ``transcript``, a list of
        scored word tokens, so this joins those words back into a sentence.
        Voicemails whose ``transcript_status`` is not ``'received'`` have no
        transcript at all and keep the API's empty ``messageText``.
        """
        body = self._data.get('messageText')
        return body or self.transcript or body

    @property
    def transcript(self) -> str | None:
        """The voicemail transcript as a sentence, or None if there isn't one."""
        tokens = (self._data.get('transcript') or {}).get('wordTokens') or []
        words = [w.get('word') for w in tokens if w.get('word')]
        return ' '.join(words) if words else None

    @property
    def transcript_status(self) -> str | None:
        """``'received'`` when a transcript exists, e.g. ``'processingFailure'``
        when Google could not transcribe the voicemail."""
        return self._data.get('transcriptStatus')

    @property
    def transcript_confidence(self) -> float | None:
        """Google's confidence in the whole transcript, 0..1."""
        return (self._data.get('transcript') or {}).get('confidence')

    @property
    def type(self) -> str | None:
        """``smsIn``, ``smsOut``, ``sip`` (a call), ``missed``, ``voicemail``."""
        return self._data.get('type')

    @property
    def coarse_type(self) -> str | None:
        """For calls, the direction: ``callTypeOutgoing`` / ``callTypeIncoming``
        / ``callTypeMissed``."""
        return self._data.get('coarseType')

    @property
    def duration(self) -> int | None:
        """How long the call or voicemail ran, **in whole seconds**.

        Google reports this as an integer number of seconds (not milliseconds),
        and only for calls, voicemails and recordings. SMS have no duration and
        return None.
        """
        try:
            return int(self._data['duration'])
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def is_voicemail(self) -> bool:
        return 'voicemail' in (self.type or '').lower()

    @property
    def recording_url(self) -> str | None:
        """URL of the voicemail/recording audio, if any."""
        return self._data.get('recordingUrl')

    @property
    def has_audio(self) -> bool:
        """Whether this message carries downloadable voicemail/recording audio."""
        return bool(self.recording_url)

    def download(self, dest: str | None = None) -> str:
        """Download the voicemail/recording audio; see :meth:`Voice.download`."""
        if self.voice is None:
            raise DownloadError('Message has no Voice attached to download with.')
        return self.voice.download(self, dest)

    @property
    def incoming(self) -> bool:
        """
        True for things you received (incoming SMS, received or missed calls,
        voicemail); False for things you sent/placed.

        ``type`` is authoritative for SMS (``smsIn``/``smsOut``). For calls and
        voicemail, direction is in ``coarseType`` (``callTypeOutgoing`` = placed
        by you). Note SMS *also* carry a ``coarseType`` (``callTypeSmsIn`` /
        ``callTypeSmsOut``), so it must not be consulted first.
        """
        t = (self.type or '').lower()
        if t.startswith('sms'):
            return t.endswith('in')
        return (self.coarse_type or '').lower() != 'calltypeoutgoing'

    @property
    def phone_number(self) -> str | None:
        """The other party's number."""
        return (self._data.get('contact') or {}).get('phoneNumber')

    @property
    def did(self) -> str | None:
        """Your Google Voice number this message went through."""
        return self._data.get('did')

    @property
    def start_time(self) -> datetime | None:
        return _ms_to_datetime(self._data.get('startTime'))

    def as_dict(self) -> dict:
        """A plain, JSON-serializable view of this message."""
        when = self.start_time
        return {
            'id': self.id,
            'text': self.text,
            'type': self.type,
            'duration': self.duration,
            'incoming': self.incoming,
            'phone_number': self.phone_number,
            'did': self.did,
            'start_time': when.isoformat() if when else None,
        }

    def __repr__(self) -> str:
        arrow = '<-' if self.incoming else '->'
        return f'<Message {arrow} {self.phone_number}: {self.text!r}>'


class Thread:
    """A conversation: a list of :class:`Message` plus reply helpers."""

    def __init__(self, voice, data: dict):
        self.voice = voice
        self._data = data

    @property
    def id(self) -> str | None:
        """e.g. ``t.+12085551234`` (number), ``t.22000`` (short code)."""
        return self._data.get('id')

    @property
    def read(self) -> bool:
        return bool(self._data.get('read'))

    @property
    def is_text(self) -> bool:
        return bool(self._data.get('isText'))

    @property
    def messages(self) -> list[Message]:
        """Messages, newest first (as Google returns them)."""
        return [Message(m, self.voice) for m in self._data.get('item', [])]

    @property
    def voicemails(self) -> list[Message]:
        """Just the voicemail messages in this conversation."""
        return [m for m in self.messages if m.is_voicemail]

    @property
    def latest(self) -> Message | None:
        items = self._data.get('item') or []
        return Message(items[0], self.voice) if items else None

    @property
    def latest_text(self) -> str | None:
        return self.latest.text if self.latest else None

    @property
    def contact(self) -> str | None:
        """The recipient's phone number / identifier for this thread."""
        keys = self._data.get('headingContactsPhoneNumberKey') or []
        if keys:
            return keys[0]
        latest = self.latest
        return latest.phone_number if latest else None

    def reply(self, text: str) -> dict:
        """Send ``text`` back into this conversation."""
        return self.voice.send_sms(self.contact, text, thread_id=self.id)

    # Conversation actions -- thin shortcuts onto the matching Voice method.
    def archive(self) -> dict:
        """Move this conversation to the Archive."""
        return self.voice.archive(self)

    def unarchive(self) -> dict:
        """Restore this conversation from the Archive."""
        return self.voice.unarchive(self)

    def mark_spam(self) -> dict:
        """Flag this conversation as spam."""
        return self.voice.mark_spam(self)

    def mark_not_spam(self) -> dict:
        """Clear the spam flag from this conversation."""
        return self.voice.mark_not_spam(self)

    def block(self) -> dict:
        """Block the other party on this conversation."""
        return self.voice.block(self)

    def unblock(self) -> dict:
        """Unblock the other party on this conversation."""
        return self.voice.unblock(self)

    def mark_read(self, read: bool = True) -> dict:
        """Mark this conversation read (``read=False`` marks it unread)."""
        return self.voice.mark_read(self, read)

    def as_dict(self) -> dict:
        """A plain, JSON-serializable view of this conversation."""
        return {
            'id': self.id,
            'contact': self.contact,
            'read': self.read,
            'is_text': self.is_text,
            'messages': [m.as_dict() for m in self.messages],
        }

    def __repr__(self) -> str:
        return f'<Thread {self.id} ({self.contact}): {self.latest_text!r}>'
