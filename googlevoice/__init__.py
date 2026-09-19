"""
A Python client for the modern Google Voice web API.

Read your conversations, calls and voicemails (with transcripts and audio
download), search, and manage threads (archive, spam, block, mark read) -- all
as plain HTTP, no browser. Sending SMS (including group messages) is gated by
Google's anti-abuse tokens, so it is done by driving the real web app; see
:mod:`googlevoice.browser`.
"""

from .auth import Credentials
from .util import Message, Thread
from .voice import Voice

__all__ = ['Credentials', 'Message', 'Thread', 'Voice']
