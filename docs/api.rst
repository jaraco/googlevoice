.. _api:


API Reference
=============

.. automodule:: googlevoice

Voice
-----

Reading from Google Voice (account info, conversations, messages) is a plain
HTTP client and needs no browser.

.. autoclass:: googlevoice.Voice
   :members:

Thread
------

.. autoclass:: googlevoice.Thread
   :members:

Message
-------

.. autoclass:: googlevoice.Message
   :members:

Credentials
-----------

.. autoclass:: googlevoice.Credentials
   :members:

BrowserSender
-------------

Sending SMS requires browser-minted anti-abuse tokens, so it is handled
separately by driving the real web app.

.. autoclass:: googlevoice.browser.BrowserSender
   :members:

Authentication
--------------

.. automodule:: googlevoice.auth
   :members: browser_login, session_is_valid, save_session, load_session
