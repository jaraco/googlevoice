.. _scripts:


Command Line
============

The package is runnable as a module::

    python -m googlevoice <command>

Commands
--------

::

    login              One-time browser sign-in; saves a portable session.
    check              Report whether the saved session is still valid.
    number             Print your Google Voice number.

  Reading
    inbox [-n N]       List the N most recent text conversations (default 20).
    folder NAME [-n N] List a folder: inbox|calls|voicemail|spam|archive.
    thread NUMBER [-n N]  Show up to N recent messages with NUMBER (default 50).
    voicemail [-n N]   List voicemails with their transcripts.
    calls [--type T]   List call records: missed|placed|received|recorded.
    search QUERY       Full-text search across conversations.

  Managing a conversation (by NUMBER; no browser needed)
    archive / unarchive NUMBER
    spam / unspam NUMBER
    block / unblock NUMBER
    read / unread NUMBER
    download NUMBER [--dir D]   Save the conversation's voicemail audio.

  Sending (launches a browser)
    send NUMBER TEXT   Send an SMS. NUMBER may be comma-separated for a group.

The reading commands accept ``--json`` for machine-readable output. Numbers may
be formatted or bare -- ``+1 (208) 555-1234``, ``208-555-1234`` and
``2085551234`` are all accepted (a missing country code defaults to +1). If the
saved session has expired, a command offers to sign in and then retries.

Examples
--------

::

    $ python -m googlevoice login
    $ python -m googlevoice number
    +12085551234
    $ python -m googlevoice inbox -n 5
    $ python -m googlevoice voicemail
    $ python -m googlevoice archive +12085550000
    $ python -m googlevoice send +12085550000 "Hello from the command line"
    Sent.
