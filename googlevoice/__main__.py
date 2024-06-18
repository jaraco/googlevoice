"""
Googlevoice interactive application. Invoke with python -m googlevoice.
"""

import functools
from atexit import register
from optparse import OptionParser
from pprint import pprint
from sys import exit

from googlevoice.util import LoginError
from googlevoice.voice import Voice

parser = OptionParser(
    usage='''gvoice [options] commands
    Where commands are

    login (li) - log into the voice service
    logout (lo) - log out of the service and make sure session is deleted
    help

    Voice Commands
        call (c) - call an outgoing number from a forwarding number
        cancel (cc) - cancel a particular call
        download (d) - download mp3 message given id hash
        send_sms (s) - send sms messages

    Folder Views
        search (se)
        inbox (i)
        voicemail (v)
        starred (st)
        all (a)
        spam (sp)
        trash (t)
        voicemail (v)
        sms (sm)
        recorded (r)
        placed (p)
        received (re)
        missed (m)'''
)
parser.add_option(
    "-e", "--email", dest="email", default=None, help="Google Voice Account Email"
)
parser.add_option(
    "-p",
    "--password",
    dest='passwd',
    default=None,
    help='Your account password (prompted if blank)',
)
parser.add_option(
    "-b",
    "--batch",
    dest='batch',
    default=False,
    action="store_true",
    help='Batch operations, asking for no interactive input',
)


def login(email, passwd, batch):
    """
    Login Voice instance based on options and interactivity
    """
    global voice
    try:
        voice.login(email, passwd)
    except LoginError:
        if batch:
            print('Login failed.')
            exit(0)
        if input('Login failed. Retry?[Y/n] ').lower() in ('', 'y'):
            login(None, None, batch)
        else:
            exit(0)


def logout():
    global voice
    print('Logging out of voice...')
    voice.logout()


def pprint_folder(name):
    folder = getattr(voice, name)()
    print(folder)
    pprint(folder.messages, indent=4)


def run_interactive(voice, action, args):
    while 1:
        try:
            action = input('gvoice> ').lower().strip()
        except (EOFError, KeyboardInterrupt):
            exit(0)
        if not action:
            continue

        handle_action(voice, action)


action_aliases = dict(
    q='quit',
    exit='quit',
    li='login',
    lo='logout',
    c='call',
    cc='cancelcall',
    s='sendsms',
    se='search',
    d='download',
    t='trash',
    sp='spam',
    i='inbox',
    v='voicemail',
    a='all',
    st='starred',
    m='missed',
    re='received',
    r='recorded',
    sm='sms',
)


def call(voice):
    voice.call(
        input('Outgoing number: '),
        input('Forwarding number [optional]: ') or None,
        int(input('Phone type [1-Home, 2-Mobile, 3-Work, 7-Gizmo]:') or 2),
    )
    print('Calling...')


def send_sms(voice):
    voice.send_sms(input('Phone number: '), input('Message: '))
    print('Message Sent')


def search(voice):
    se = voice.search(input('Search query: '))
    print(se)
    pprint(se.messages)


def download(voice):
    print('MP3 downloaded to {}'.format(voice.download(input('Message sha1: '))))


def handle_action(voice, action):
    fn_map = dict(
        quit=functools.partial(exit, 0),
        login=login,
        logout=voice.logout,
        call=functools.partial(call, voice),
        cancelcall=voice.cancel,
        sendsms=send_sms,
        download=download,
        help=functools.partial(print, parser.usage),
    )
    folder_names = (
        'trash spam inbox voicemail all starred missed received recorded sms'.split()
    )
    fn_map.update(
        (name, functools.partial(pprint_folder, name)) for name in folder_names
    )
    pure_action = action_aliases.get(action, action)
    return fn_map.get(pure_action, lambda: None)()


def run_other(voice, action, args):
    if action == 'send_sms':
        try:
            num, args = args[0], args[1:]
        except Exception:
            print('Please provide a message')
            exit(0)
        args = (num, ' '.join(args))
    getattr(voice, action)(*args)


def main():
    options, args = parser.parse_args()

    try:
        action, args = args[0], args[1:]
    except IndexError:
        action = 'interactive'

    if action == 'help':
        print(parser.usage)
        exit(0)

    voice = Voice()
    login()

    register(logout)

    globals().get(f'run_{action}', run_other)(voice, action, args)


__name__ == '__main__' and main()
