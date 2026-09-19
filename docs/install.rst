.. _install:


Installation
============

::

    pip install googlevoice

Reading from Google Voice (account, conversations, messages) then needs only
``requests``.

The one-time browser sign-in and sending SMS additionally require Google Chrome
to be installed plus the ``nodriver`` package::

    pip install googlevoice[browser]

Once you have signed in (see :ref:`auth`), the saved session is portable: copy
``~/.googlevoice/session.json`` to any machine and reads work there with just
``requests`` -- no browser needed.
