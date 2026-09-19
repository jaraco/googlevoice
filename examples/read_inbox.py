"""Print recent Google Voice conversations and their latest message.

Run ``python -m googlevoice.auth login`` once first to create a session.
"""

from googlevoice import Voice


def run():
    voice = Voice()  # loads ~/.googlevoice/session.json
    print(f'Google Voice number: {voice.number}\n')

    for thread in voice.inbox(count=20):
        flag = ' ' if thread.read else '*'  # * = unread
        latest = thread.latest
        when = latest.start_time.strftime('%Y-%m-%d %H:%M') if latest else '?'
        print(f'{flag} [{when}] {thread.contact}: {thread.latest_text!r}')


__name__ == '__main__' and run()
