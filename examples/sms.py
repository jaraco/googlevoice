"""Send an SMS via Google Voice.

Run ``python -m googlevoice.auth login`` once first to create a session.
"""

from googlevoice import Voice


def run():
    voice = Voice()  # loads ~/.googlevoice/session.json

    phone_number = input('Number to send message to (e.g. +12085551234): ')
    text = input('Message text: ')

    voice.send_sms(phone_number, text)
    print('Message sent.')


__name__ == '__main__' and run()
