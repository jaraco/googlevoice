"""Search your Google Voice conversations.

Modernized from the original project's ``search.py`` (the web app's top search
box).

Run ``python -m googlevoice.auth login`` once first to create a session.
"""

from googlevoice import Voice


def run():
    voice = Voice()  # loads ~/.googlevoice/session.json

    query = input('Search for: ')
    for thread in voice.search(query, count=20):
        print(f'{thread.contact}: {thread.latest_text!r}')


__name__ == '__main__' and run()
