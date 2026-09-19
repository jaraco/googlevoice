"""
Command-line interface for googlevoice.  Invoke with ``python -m googlevoice``.

    python -m googlevoice login                # one-time browser sign-in
    python -m googlevoice check                # is the saved session still valid?
    python -m googlevoice number               # print your Google Voice number

  Reading
    python -m googlevoice inbox [-n N]         # recent text conversations
    python -m googlevoice folder NAME [-n N]   # inbox|calls|voicemail|spam|archive
    python -m googlevoice thread NUMBER [-n N] # show messages with NUMBER
    python -m googlevoice voicemail [-n N]     # voicemails (with transcripts)
    python -m googlevoice calls [--type T]     # missed | placed | received | recorded
    python -m googlevoice search QUERY         # full-text search

  Acting on a conversation (by NUMBER) -- no browser needed
    python -m googlevoice archive/unarchive NUMBER
    python -m googlevoice spam/unspam NUMBER
    python -m googlevoice block/unblock NUMBER
    python -m googlevoice read/unread NUMBER
    python -m googlevoice download NUMBER [--dir D]   # save voicemail audio
    python -m googlevoice send NUMBER TEXT            # send an SMS (drives a browser)
                                                      # NUMBER may be comma-
                                                      # separated for a group

  Verifying the inferred endpoints
    python -m googlevoice capture [--seconds N]       # record real API calls

Add ``--json`` to the reading commands for machine-readable output.  Numbers may
be given formatted or bare (e.g. ``2085551234``); a missing country code
defaults to +1.  If the saved session is missing or expired, a command offers to
sign in (via the browser) and then retries automatically.
"""

import argparse
import json

from . import auth, util
from .voice import Folder, Voice

_FOLDERS = {
    'inbox': Folder.INBOX,
    'calls': Folder.CALLS,
    'voicemail': Folder.VOICEMAIL,
    'spam': Folder.SPAM,
    'archive': Folder.ARCHIVE,
}

# `calls --type X` -> Voice method returning Message records.
_CALL_TYPES = ('missed', 'placed', 'received', 'recorded')

# CLI verb -> (Voice method, help). All take a single NUMBER argument.
_ACTION_VERBS = {
    'archive': ('archive', 'Archive a conversation'),
    'unarchive': ('unarchive', 'Restore a conversation from the Archive'),
    'spam': ('mark_spam', 'Flag a conversation as spam'),
    'unspam': ('mark_not_spam', 'Clear the spam flag from a conversation'),
    'block': ('block', 'Block the other party'),
    'unblock': ('unblock', 'Unblock the other party'),
    'read': ('mark_read', 'Mark a conversation read'),
    'unread': ('mark_read', 'Mark a conversation unread'),
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m googlevoice', description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)

    sub.add_parser('login', help='Sign in via browser and save a portable session')
    sub.add_parser('check', help='Check whether the saved session still works')

    p_number = sub.add_parser('number', help='Print your Google Voice number')
    p_number.add_argument('--json', action='store_true', help='machine-readable output')

    p_inbox = sub.add_parser('inbox', help='List recent conversations')
    p_inbox.add_argument(
        '-n',
        '--count',
        type=int,
        default=20,
        help='how many conversations (default 20)',
    )
    p_inbox.add_argument('--json', action='store_true', help='machine-readable output')

    p_folder = sub.add_parser('folder', help='List a folder view')
    p_folder.add_argument('name', choices=sorted(_FOLDERS), help='which folder')
    p_folder.add_argument('-n', '--count', type=int, default=20)
    p_folder.add_argument('--json', action='store_true', help='machine-readable output')

    p_thread = sub.add_parser('thread', help='Show the conversation with a number')
    p_thread.add_argument('number', help='The other party, e.g. +12085551234')
    p_thread.add_argument(
        '-n', '--count', type=int, default=50, help='show up to N messages (default 50)'
    )
    p_thread.add_argument('--json', action='store_true', help='machine-readable output')

    p_vm = sub.add_parser('voicemail', help='List voicemails (with transcripts)')
    p_vm.add_argument('-n', '--count', type=int, default=20)
    p_vm.add_argument('--json', action='store_true', help='machine-readable output')

    p_calls = sub.add_parser('calls', help='List call records')
    p_calls.add_argument(
        '--type', choices=_CALL_TYPES, default='missed', help='(default missed)'
    )
    p_calls.add_argument('-n', '--count', type=int, default=20)
    p_calls.add_argument('--json', action='store_true', help='machine-readable output')

    p_search = sub.add_parser('search', help='Full-text search across conversations')
    p_search.add_argument('query', nargs='+', help='search terms')
    p_search.add_argument('-n', '--count', type=int, default=20)
    p_search.add_argument('--json', action='store_true', help='machine-readable output')

    for verb, spec in _ACTION_VERBS.items():
        p = sub.add_parser(verb, help=spec[1])
        p.add_argument('number', help='Conversation, e.g. +12085551234')

    p_dl = sub.add_parser(
        'download', help='Download voicemail audio from a conversation'
    )
    p_dl.add_argument('number', help='Conversation, e.g. +12085551234')
    p_dl.add_argument('--dir', default='.', help='destination directory (default .)')

    p_send = sub.add_parser('send', help='Send an SMS (launches a browser)')
    p_send.add_argument(
        'number',
        help='Recipient, e.g. +12085551234 (comma-separate two+ for a group)',
    )
    p_send.add_argument('text', nargs='+', help='Message text')
    p_send.add_argument(
        '--headless',
        action=argparse.BooleanOptionalAction,
        default=None,
        help='run Chrome headless (default: auto -- headless if no display)',
    )

    p_cap = sub.add_parser('capture', help='Record real API calls (verify endpoints)')
    p_cap.add_argument(
        '--seconds', type=float, default=180, help='how long to record (default 180)'
    )

    return parser


def _confirm(question: str) -> bool:
    try:
        return input(f'{question} [Y/n] ').strip().lower() in ('', 'y', 'yes')
    except EOFError:
        return False


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def _print_threads(threads, as_json: bool) -> None:
    if as_json:
        print(json.dumps([t.as_dict() for t in threads], indent=2))
        return
    for thread in threads:
        flag = ' ' if thread.read else '*'
        print(f'{flag} {thread.contact}: {thread.latest_text!r}')


def _print_messages(messages, as_json: bool) -> None:
    if as_json:
        print(json.dumps([m.as_dict() for m in messages], indent=2))
        return
    for msg in messages:
        when = (
            msg.start_time.astimezone().strftime('%Y-%m-%d %H:%M')
            if msg.start_time
            else '?'
        )
        # Calls, voicemails and recordings carry a length; SMS do not.
        length = f' ({msg.duration}s)' if msg.duration else ''
        print(f'[{when}] {msg.phone_number}{length}: {msg.text!r}')


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _cmd_number(args) -> None:
    number = Voice().number
    print(json.dumps({'number': number}) if args.json else number)


def _cmd_inbox(args) -> None:
    _print_threads(Voice().inbox(count=args.count), args.json)


def _cmd_folder(args) -> None:
    _print_threads(Voice().threads(_FOLDERS[args.name], count=args.count), args.json)


def _cmd_thread(args) -> None:
    thread = Voice().thread(args.number, messages=args.count)
    if thread is None:
        print(
            'null'
            if args.json
            else f'No conversation with {args.number} in recent threads.'
        )
        raise SystemExit(1)
    if args.json:
        print(json.dumps(thread.as_dict(), indent=2))
        return
    for msg in reversed(thread.messages):  # oldest first
        arrow = '<-' if msg.incoming else '->'
        # local time, matching the Google Voice web UI
        when = (
            msg.start_time.astimezone().strftime('%Y-%m-%d %H:%M')
            if msg.start_time
            else '?'
        )
        print(f'[{when}] {arrow} {msg.text!r}')


def _cmd_voicemail(args) -> None:
    _print_messages(Voice().voicemails(count=args.count), args.json)


def _cmd_calls(args) -> None:
    msgs = getattr(Voice(), args.type)(
        count=args.count
    )  # missed/placed/received/recorded
    _print_messages(msgs, args.json)


def _cmd_search(args) -> None:
    threads = Voice().search(' '.join(args.query), count=args.count)
    _print_threads(threads, args.json)


def _cmd_action(args) -> None:
    voice = Voice()
    if args.cmd == 'unread':
        voice.mark_read(args.number, read=False)
    else:
        getattr(voice, _ACTION_VERBS[args.cmd][0])(args.number)
    print(f'{args.cmd}: {args.number}')


def _cmd_download(args) -> None:
    voice = Voice()
    thread = voice.thread(args.number)
    if thread is None:
        print(f'No conversation with {args.number} in recent threads.')
        raise SystemExit(1)
    voicemails = thread.voicemails
    if not voicemails:
        print(f'No voicemail in the conversation with {args.number}.')
        raise SystemExit(1)
    for msg in voicemails:
        print('Saved', msg.download(args.dir))


def _cmd_send(args) -> None:
    from .browser import BrowserSender

    with BrowserSender(headless=args.headless) as sender:
        sender.send_sms(args.number, ' '.join(args.text))
    print('Sent.')


_COMMANDS = {
    'number': _cmd_number,
    'inbox': _cmd_inbox,
    'folder': _cmd_folder,
    'thread': _cmd_thread,
    'voicemail': _cmd_voicemail,
    'calls': _cmd_calls,
    'search': _cmd_search,
    'download': _cmd_download,
    'send': _cmd_send,
    **{verb: _cmd_action for verb in _ACTION_VERBS},
}


def _run(args) -> None:
    """Run a session-backed command (may raise on a missing/expired session)."""
    _COMMANDS[args.cmd](args)


def main(argv=None) -> None:
    args = _build_parser().parse_args(argv)

    if args.cmd == 'login':
        auth.browser_login()
        print('Done. The session is ready to use from any machine.')
        return

    if args.cmd == 'check':
        try:
            ok = auth.session_is_valid(auth.load_session())
        except auth.AuthError:
            ok = False
        print('Session is VALID.' if ok else 'Session is INVALID or missing.')
        raise SystemExit(0 if ok else 1)

    if args.cmd == 'capture':
        from .browser import capture_api_calls

        capture_api_calls(seconds=args.seconds)
        return

    try:
        _run(args)
    except (auth.AuthError, util.LoginError) as err:
        print(f'\n{err}\n')
        if _confirm('Sign in with a browser now and retry?'):
            auth.browser_login()
            _run(args)
        else:
            raise SystemExit(1) from None


if __name__ == '__main__':
    main()
