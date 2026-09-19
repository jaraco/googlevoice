.. _auth:


Authentication
==============

Google Voice no longer supports password login from a script; the modern web
app authenticates with your Google session cookies. You sign in once in a real
browser, and the library reuses the saved session afterwards.

Sign in (once)
--------------

::

    python -m googlevoice.auth login

A Chrome window opens. Sign in to the Google account that owns your Voice
number -- password, 2-step verification, and passkeys all work normally -- and
wait on the inbox. Login is detected automatically and the session is written
to ``~/.googlevoice/session.json``.

Using and moving the session
----------------------------

That ``session.json`` is all the library needs to read your account. It is
**portable**: copy it to a headless server or container and it keeps working
with no browser. Check whether a saved session is still valid with::

    python -m googlevoice.auth check

Re-run ``login`` only if the session is revoked or expires.

.. warning::
   ``session.json`` grants access to your Google Voice account. Keep it
   private (it is created with ``0600`` permissions).

Sending
-------

Reading is browser-free, but **sending** SMS is gated by Google behind
anti-abuse tokens (reCAPTCHA + BotGuard) that can only be produced in a
browser. :class:`googlevoice.browser.BrowserSender` therefore drives the real
web app to send. Sending thus requires Chrome to be installed on the sending
machine.

The browser signs in from the saved ``session.json`` (Chrome forgets Google's
login cookies whenever it exits cleanly, so the browser profile alone cannot
keep you signed in). While the session is valid you are never asked to log in
again, and the session file is refreshed each time the browser closes.
