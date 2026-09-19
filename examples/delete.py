"""Tidy the inbox by archiving conversations you have already read.

Modernized from the original project's ``delete.py`` (which deleted read SMS).
The modern web app has no Trash -- deletion is permanent -- so the everyday
inbox-tidying action is Archive. ``mark_spam()`` and ``block()`` are also
available.

Run ``python -m googlevoice.auth login`` once first to create a session.
"""

from googlevoice import Voice


def run():
    voice = Voice()  # loads ~/.googlevoice/session.json

    for thread in voice.inbox(count=20):
        if thread.read:
            print(f'Archiving {thread.contact}: {thread.latest_text!r}')
            thread.archive()
            # thread.mark_spam()   # or flag it as spam
            # thread.block()       # or block the sender


__name__ == '__main__' and run()
