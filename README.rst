.. image:: https://img.shields.io/pypi/v/googlevoice.svg
   :target: https://pypi.org/project/googlevoice

.. image:: https://img.shields.io/pypi/pyversions/googlevoice.svg

.. image:: https://github.com/jaraco/googlevoice/actions/workflows/main.yml/badge.svg
   :target: https://github.com/jaraco/googlevoice/actions?query=workflow%3A%22tests%22
   :alt: tests

.. image:: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json
    :target: https://github.com/astral-sh/ruff
    :alt: Ruff

.. image:: https://readthedocs.org/projects/googlevoice/badge/?version=latest
   :target: https://googlevoice.readthedocs.io/en/latest/?badge=latest

.. image:: https://img.shields.io/badge/skeleton-2026-informational
   :target: https://blog.jaraco.com/skeleton


Python Google Voice Library

Based on pygooglevoice by Joe McCall & Justin Quick.

Expose the Google Voice "API" to Python: send and read SMS and list your
conversations.


Status
======

For years this library was broken: Google retired the legacy
``/voice/b/0/`` HTML endpoints it scraped, so login stopped working
(`issue #8 <https://github.com/jaraco/googlevoice/issues/8>`_).

It has now been **rewritten against the modern Google Voice web API** that
``voice.google.com`` itself uses. Authentication no longer needs your password:
you sign in once in a real browser, and the library reuses the resulting
session (which is portable to any machine).


Installation
============

::

    pip install googlevoice

The one-time browser login additionally needs Google Chrome installed and the
``nodriver`` package (``pip install nodriver``); after that, day-to-day use is
pure ``requests`` and needs no browser.


Authenticate (once)
====================

Google's login is JavaScript-heavy and bot-protected, so we drive a real
(undetected) Chrome to let you sign in normally — password, 2-step
verification, passkeys and captchas all just work::

    python -m googlevoice.auth login

A Chrome window opens. Sign in to the Google account that owns your Voice
number and wait on the inbox; login is detected automatically and your session
is saved to ``~/.googlevoice/session.json``.

**Sessions are portable.** That ``session.json`` is all the library needs.
Copy it to a headless server, a container, a Raspberry Pi — anywhere — and the
API works there with no browser and no further login. Re-run ``login`` only if
the session is revoked or expires::

    python -m googlevoice.auth check     # is my saved session still valid?

Keep ``session.json`` private: it grants access to your Google Voice account.


Usage
=====

.. code-block:: python

    from googlevoice import Voice

    voice = Voice()                     # loads ~/.googlevoice/session.json
    print(voice.number)                 # your Google Voice number, e.g. +12085551234

    # Read recent conversations (pure requests, no browser)
    for thread in voice.inbox():
        print(thread.contact, '->', thread.latest_text)

    # Read one conversation (numbers may be bare/formatted; +1 assumed)
    convo = voice.thread('208-555-0123', messages=100)
    for msg in reversed(convo.messages):       # oldest first
        print(msg.start_time, msg.text)

Voicemails (transcript in ``text``, length in ``duration``, audio via
``download``), call history, and full-text search are reads too:

.. code-block:: python

    for vm in voice.voicemails():
        print(vm.start_time, vm.text)        # transcript, or '' if Google failed
        print(vm.duration)                   # length in whole seconds
        vm.download('.')                     # save the .mp3

    voice.missed(); voice.placed(); voice.received()   # call records
    voice.search('invoice')                            # matching conversations

Managing a conversation is also a plain HTTP call (no browser): archive, spam,
block and read all work on a thread, a number, or a thread id. There is no
Trash -- in the modern web app, deletion is permanent.

.. code-block:: python

    convo.archive(); convo.unarchive()
    convo.mark_spam(); convo.mark_not_spam()
    convo.block(); convo.unblock()
    convo.mark_read(False)                   # mark unread

The command line mirrors all of this (``python -m googlevoice inbox``,
``voicemail``, ``calls --type missed``, ``search QUERY``,
``archive NUMBER`` ...); reading commands take ``--json`` for scripting.

If the saved session expires, a 401 triggers a one-time browser re-login that
auto-confirms from the saved profile and retries; disable with
``Voice(auto_login=False)``. Point at a non-default session file with
``Voice(session_path='/path/to/session.json')`` or pass
``Voice(credentials=...)`` (see ``googlevoice.auth.Credentials``).

Sending an SMS is gated by Google behind anti-abuse tokens (reCAPTCHA +
BotGuard) that can only be produced in a browser, so it is done by driving the
real web app:

.. code-block:: python

    from googlevoice.browser import BrowserSender

    with BrowserSender() as sender:
        sender.send_sms('+12085551234', 'Hello from Python!')
        sender.send_sms(['+12085551234', '+12085550000'], 'Group hello!')

Or from the command line: ``python -m googlevoice send +12085551234 "hi"``
(comma-separate the numbers for a group message).

The browser signs in from your saved ``session.json``, so it works on any
machine that has the session file and Chrome, and never asks you to log in
again while the session is valid. It also refreshes ``session.json`` when it
closes.


How it works
============

The modern web app authenticates each request to
``https://clients6.google.com/voice/v1/voiceclient/...`` with a
``SAPISIDHASH`` — a SHA-1 over the request timestamp, your Google ``SAPISID``
cookies, and the origin. It needs no OAuth flow and does not expire on its own
(the cookies do the work), which is what makes a saved session portable.

``googlevoice.auth`` harvests the Google cookies during the browser login and
computes that hash for every call; ``googlevoice.voice`` wraps the JSON
endpoints. See `issue #7 <https://github.com/jaraco/googlevoice/issues/7>`_ and
`issue #8 <https://github.com/jaraco/googlevoice/issues/8>`_ for the
reverse-engineering history.
