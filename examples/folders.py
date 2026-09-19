"""Print the conversations in each folder view.

Modernized from the original project's ``folders.py``.  The legacy per-folder
feeds (inbox/starred/spam/trash/...) are now selectors on a single
``threads()`` call; Archive and Spam each hold SMS, calls and voicemail.

Run ``python -m googlevoice.auth login`` once first to create a session.
"""

from googlevoice import Voice
from googlevoice.voice import Folder


def run():
    voice = Voice()  # loads ~/.googlevoice/session.json

    for name, selector in [
        ('Inbox', Folder.INBOX),
        ('Calls', Folder.CALLS),
        ('Voicemail', Folder.VOICEMAIL),
        ('Spam', Folder.SPAM),
        ('Archive', Folder.ARCHIVE),
    ]:
        print(f'{name}:')
        for thread in voice.threads(selector, count=20):
            print(f'    {thread.contact}: {thread.latest_text!r}')
        print()


__name__ == '__main__' and run()
