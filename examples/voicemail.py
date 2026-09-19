"""List voicemails and their transcripts.

Modernized from the original project's ``voicemail.py``: the legacy
``voice.voicemail().messages`` feed is gone; voicemails are now the inbox
messages whose ``type`` is voicemail, and the transcript is the message text.

Run ``python -m googlevoice.auth login`` once first to create a session.
"""

from googlevoice import Voice


def run():
    voice = Voice()  # loads ~/.googlevoice/session.json

    for vm in voice.voicemails(count=20):
        when = vm.start_time.strftime('%Y-%m-%d %H:%M') if vm.start_time else '?'
        print(f'[{when}] from {vm.phone_number}:')
        print(f'    {vm.text!r}\n')


__name__ == '__main__' and run()
