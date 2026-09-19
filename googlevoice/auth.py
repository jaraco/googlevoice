"""
Authentication for the modern Google Voice web API.

Google retired the legacy ``/voice/b/0/`` HTML/XML endpoints this library was
originally built on.  The current voice.google.com web app talks to

    https://clients6.google.com/voice/v1/voiceclient/...

and authenticates each request with a ``SAPISIDHASH`` -- a hash computed from
the Google login cookies, the request origin, and a timestamp.  It needs no
OAuth dance and never expires (the cookies do the work), which makes it ideal
for a *portable* credential.

This module splits authentication cleanly in two:

* **Authenticate once, with a browser** -- :func:`browser_login` drives a real
  (undetected) Chrome via ``nodriver`` so you can sign in to Google normally
  (password, 2FA, passkeys and captcha all just work).  It then harvests the
  Google cookies and writes them to a portable ``session.json``.

* **Operate anywhere, without a browser** -- :class:`Credentials` turns those
  saved cookies into the ``Authorization`` header every API call needs, using
  nothing but :mod:`requests` and :mod:`hashlib`.  Copy ``session.json`` to a
  headless server and it keeps working.

The only machine-specific requirement is that Chrome is installed, and only for
the one-time :func:`browser_login`.  Everything after that is pure ``requests``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import pathlib
import sys
import tempfile
import time
import urllib.parse

import requests

from .util import LoginError

log = logging.getLogger(__name__)

# The voice.google.com web client's public API key and origin, observed from
# its network traffic.  The key is a browser key (not a secret); it merely
# identifies the API project.
ORIGIN = 'https://voice.google.com'
API_KEY = 'AIzaSyDTYc1N4xiODyrQYK0Kl6g_y279LjYkrBg'
API_BASE = 'https://clients6.google.com/voice/v1/voiceclient/'

# A plausible desktop-Chrome UA.  The API authenticates on cookies + hash, not
# on this, but a real-looking UA avoids standing out.
USER_AGENT = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36'
)

# (header-label, cookie-name) pairs whose SHA1 forms the multi-part
# ``Authorization`` value the web app sends.  Any present subset is accepted by
# the server; we send all we have to match the real client exactly.
_HASH_COOKIES = (
    ('SAPISIDHASH', 'SAPISID'),
    ('SAPISID1PHASH', '__Secure-1PAPISID'),
    ('SAPISID3PHASH', '__Secure-3PAPISID'),
)

# Where the portable credentials live by default.  A directory (not the legacy
# ``~/.gvoice`` config *file*) so the one-time browser profile can sit beside it.
DEFAULT_DIR = pathlib.Path.home() / '.googlevoice'
DEFAULT_SESSION_PATH = DEFAULT_DIR / 'session.json'
DEFAULT_PROFILE_DIR = DEFAULT_DIR / 'chrome-profile'

# Cookies that signal a usable Google login (used to detect login completion).
ESSENTIAL_COOKIES = {'SID', 'SAPISID', '__Secure-3PSID'}


# --------------------------------------------------------------------------- #
# Browser launch helpers (shared by browser-driven features)
# --------------------------------------------------------------------------- #
def headless_default() -> bool:
    """
    Whether to run Chrome headless by default.

    On Linux a *headed* Chrome cannot start without a display server, so if
    neither ``DISPLAY`` nor ``WAYLAND_DISPLAY`` is set (a headless server,
    container, CI, or a tool that spawns us without a display) we must go
    headless. On macOS/Windows headed always works, so default to headed.
    """
    if sys.platform.startswith('linux'):
        return not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))
    return False


def browser_launch_args(*, no_sandbox: bool | None = None) -> list[str]:
    """
    Common ``browser_args`` for ``nodriver``/Chrome.

    ``--no-sandbox`` is added only when running as root (``no_sandbox=None``),
    since Chrome's sandbox can't initialize as root/in many containers and
    refuses to launch; on a normal user account it stays on (safer).
    """
    args = ['--no-first-run', '--no-default-browser-check']
    if no_sandbox is None:
        no_sandbox = os.name == 'posix' and getattr(os, 'geteuid', lambda: 1)() == 0
    if no_sandbox:
        args.append('--no-sandbox')
    return args


class AuthError(Exception):
    """Authentication failed, or the saved session is missing/expired."""


def _is_google_cookie(domain: str) -> bool:
    domain = domain.lstrip('.')
    return domain == 'google.com' or domain.endswith('.google.com')


# --------------------------------------------------------------------------- #
# Session (cookie) persistence
# --------------------------------------------------------------------------- #
def save_session(
    cookies: list[dict], path: pathlib.Path = DEFAULT_SESSION_PATH
) -> pathlib.Path:
    """
    Write harvested cookies to ``path`` as portable JSON (mode 0600).

    The file is created owner-only from its first byte and swapped into place
    atomically, so a crash mid-write can neither truncate the previous session
    nor leave the credentials world-readable for an instant.
    """
    path = pathlib.Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({'version': 1, 'cookies': cookies}, indent=2)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def load_session(path: pathlib.Path = DEFAULT_SESSION_PATH) -> list[dict]:
    """Load cookies previously saved by :func:`save_session`."""
    path = pathlib.Path(path).expanduser()
    if not path.exists():
        raise AuthError(
            f'No saved session at {path}. Run `python -m googlevoice.auth login` first.'
        )
    return json.loads(path.read_text(encoding='utf-8'))['cookies']


# --------------------------------------------------------------------------- #
# Credentials: cookies -> Authorization header
# --------------------------------------------------------------------------- #
def sapisid_hash(cookies: list[dict], *, now: float | None = None) -> str:
    """
    Build the multi-part ``SAPISIDHASH`` ``Authorization`` value.

    Each part is ``<label> <ts>_<sha1(f"{ts} {cookie} {ORIGIN}")>``.
    """
    ts = int(time.time() if now is None else now)
    by_name = {c['name']: c['value'] for c in cookies}
    parts = []
    for label, name in _HASH_COOKIES:
        secret = by_name.get(name)
        if secret:
            digest = hashlib.sha1(f'{ts} {secret} {ORIGIN}'.encode()).hexdigest()
            parts.append(f'{label} {ts}_{digest}')
    if not parts:
        raise AuthError(
            'No SAPISID cookies in the session -- it is invalid or expired. '
            'Re-run `python -m googlevoice.auth login`.'
        )
    return ' '.join(parts)


class Credentials:
    """
    Portable Google Voice credentials: a cookie jar plus the logic to sign
    requests to the modern API.  Construct from a saved session and hand to
    :class:`googlevoice.voice.Voice`.
    """

    def __init__(self, cookies: list[dict]):
        self.cookies = cookies

    @classmethod
    def load(cls, path: pathlib.Path = DEFAULT_SESSION_PATH) -> Credentials:
        """Load credentials from a saved ``session.json``."""
        return cls(load_session(path))

    def requests_session(self) -> requests.Session:
        """A :class:`requests.Session` preloaded with the Google cookies."""
        sess = requests.Session()
        for c in self.cookies:
            # Preserve the original (often dotted, e.g. ``.google.com``) domain
            # so the cookies are sent to the ``clients6.google.com`` subdomain.
            sess.cookies.set(
                c['name'], c['value'], domain=c['domain'], path=c.get('path', '/')
            )
        return sess

    def auth_headers(self) -> dict[str, str]:
        """Fresh request headers, including a freshly-timestamped SAPISIDHASH."""
        return {
            'Authorization': sapisid_hash(self.cookies),
            'Content-Type': 'application/json+protobuf',
            'X-Goog-Api-Key': API_KEY,
            'X-Goog-AuthUser': '0',
            # The logical origin travels in X-Origin (NOT Origin): the gapi
            # client proxies through an iframe on the API host, so a literal
            # ``Origin: voice.google.com`` against host ``clients6.google.com``
            # trips Google's cross-domain check ("Origin doesn't match Host").
            'X-Origin': ORIGIN,
            'X-Referer': ORIGIN,
            'X-Requested-With': 'XMLHttpRequest',
            'User-Agent': USER_AGENT,
        }


# --------------------------------------------------------------------------- #
# Login probe (pure requests) -- "are these cookies usable right now?"
# --------------------------------------------------------------------------- #
def session_is_valid(cookies: list[dict]) -> bool:
    """True if the cookies can authenticate a live ``account/get`` call."""
    try:
        creds = Credentials(cookies)
        resp = creds.requests_session().post(
            API_BASE + 'account/get',
            params={'alt': 'json', 'key': API_KEY},
            headers=creds.auth_headers(),
            data='[null,1]',
            timeout=30,
        )
        return resp.status_code == 200
    except (requests.RequestException, AuthError) as err:
        log.debug('session probe failed: %s', err)
        return False


# --------------------------------------------------------------------------- #
# Browser session helpers (shared by login, BrowserSender and Caller)
#
# The Chrome profile is NOT a reliable login store.  Chrome discards
# session-only cookies on every clean exit, and Google's login cookies end up
# session-only in the profile, so a profile that sent a text yesterday is
# signed out today ("sending a text makes me log in again").  The portable
# ``session.json`` is the real credential: every browser-driven feature checks
# the page it landed on and, if the profile is signed out, injects the saved
# cookies (pinned with an expiry so they survive the next clean exit) and
# reloads.  On close the saved session is refreshed with Google's rotated
# cookies and Chrome is asked to quit cleanly so the profile is flushed.
# --------------------------------------------------------------------------- #
# Expiry given to cookies the browser reported as session-only.  Chrome caps a
# cookie's lifetime at 400 days; a year keeps them well inside that.
SESSION_COOKIE_TTL = 365 * 86400

# Upper bound on refreshing session.json at shutdown (cookie harvest plus the
# ``account/get`` probe), so a wedged browser or network cannot hang close().
REFRESH_TIMEOUT = 30.0

# Google login cookies Chrome reports as HttpOnly (observed live).  Consulted
# only for records written before the harvester stored ``http_only``, so a
# restored legacy session does not expose them to page scripts.  Deliberately
# absent: SID, SAPISID, APISID, SIDCC and the ``__Secure-*PAPISID`` pair, which
# the web app reads from JavaScript.
_LEGACY_HTTP_ONLY = frozenset({
    'HSID',
    'SSID',
    'LSID',
    'NID',
    'S',
    'COMPASS',
    '__Secure-1PSID',
    '__Secure-3PSID',
    '__Secure-1PSIDTS',
    '__Secure-3PSIDTS',
    '__Secure-1PSIDRTS',
    '__Secure-3PSIDRTS',
    '__Secure-1PSIDCC',
    '__Secure-3PSIDCC',
    '__Host-1PLSID',
    '__Host-3PLSID',
    '__Host-GAPS',
    '__Host-GAPSTS',
})


def is_signed_in_url(url: str | None) -> bool:
    """
    True if ``url`` is inside the signed-in Voice web app.

    Signed out, ``voice.google.com`` bounces to ``accounts.google.com``, to its
    own ``/about`` marketing page, or to ``workspace.google.com``.
    """
    parts = urllib.parse.urlsplit(url or '')
    return parts.hostname == 'voice.google.com' and not parts.path.startswith('/about')


def needs_voice_number(url: str | None) -> bool:
    """True if ``url`` is Voice's number-signup flow: signed in, but no number yet."""
    return urllib.parse.urlsplit(url or '').path.rstrip('/').endswith('/signup')


def _check_has_number(url: str) -> None:
    if needs_voice_number(url):
        raise LoginError(
            'Signed in, but this Google account has no Google Voice number '
            f'(the browser landed on {url}). Pick a number at voice.google.com first.'
        )


async def harvest_cookies(browser) -> list[dict]:
    """The Google cookies currently in ``browser`` as portable dicts."""
    cookies = []
    for c in await browser.cookies.get_all():
        if not _is_google_cookie(c.domain):
            continue
        same_site = getattr(c, 'same_site', None)
        cookies.append({
            'name': c.name,
            'value': c.value,
            'domain': c.domain,
            'path': c.path or '/',
            'secure': bool(getattr(c, 'secure', False)),
            'http_only': bool(getattr(c, 'http_only', False)),
            'same_site': same_site.value if same_site else None,
            # -1 (or None) means a session cookie
            'expires': getattr(c, 'expires', None),
        })
    return cookies


def _cookie_params(c: dict, *, now: float, ttl: float) -> dict | None:
    """``Network.setCookie`` arguments for one saved cookie; None once expired."""
    from nodriver import cdp

    expires = c.get('expires')
    if expires is None or expires < 0:
        expires = now + ttl  # the browser had it as session-only: pin it
    elif expires < now:
        return None  # 0 is the epoch, not "session": expired like any past time
    name = c['name']
    path = c.get('path') or '/'
    http_only = c.get('http_only')
    if http_only is None:  # legacy record: the flag was never saved
        http_only = name in _LEGACY_HTTP_ONLY
    # Prefixed names are Secure by definition; Chrome rejects them otherwise.
    secure = bool(c.get('secure', False)) or name.startswith(('__Host-', '__Secure-'))
    kwargs = {
        'name': name,
        'value': c['value'],
        'path': path,
        'secure': secure,
        'http_only': bool(http_only),
        'expires': cdp.network.TimeSinceEpoch(expires),
    }
    same_site = c.get('same_site')
    if same_site:
        # An unknown SameSite value drops the attribute, not the cookie.
        with contextlib.suppress(ValueError):
            kwargs['same_site'] = cdp.network.CookieSameSite(same_site)
    domain = c['domain']
    if domain.startswith('.'):
        kwargs['domain'] = domain
    else:
        # A host-only cookie (e.g. ``__Host-…`` on accounts.google.com) must
        # carry no Domain attribute, so set it through its URL instead.  Chrome
        # marks a cookie set through an https URL Secure, so the scheme has to
        # follow the saved flag.
        scheme = 'https' if secure else 'http'
        kwargs['url'] = f'{scheme}://{domain}{path}'
    return kwargs


async def inject_cookies(
    tab, cookies: list[dict], *, ttl: float = SESSION_COOKIE_TTL
) -> int:
    """
    Set saved ``cookies`` in the browser via CDP; return how many took.

    Cookies the browser had reported as session-only get an expiry ``ttl``
    seconds out, so they persist across a clean Chrome exit.  Already-expired
    cookies are skipped.  A malformed or rejected cookie never aborts the rest.
    """
    from nodriver import cdp

    now = time.time()
    count = 0
    for c in cookies:
        try:
            kwargs = _cookie_params(c, now=now, ttl=ttl)
            if kwargs is None:
                continue
            ok = await tab.send(cdp.network.set_cookie(**kwargs))
        except Exception as err:  # noqa: BLE001 - one bad cookie must not sink the rest
            # Log the type only: an error message could echo the cookie's value.
            name = c.get('name') if isinstance(c, dict) else None
            log.debug('set_cookie %s failed (%s)', name, type(err).__name__)
            continue
        count += bool(ok)
    return count


async def settled_url(tab, *, timeout: float = 20.0, poll: float = 1.0) -> str:
    """
    The tab's URL once the document has loaded and stopped redirecting.

    Signed-out visits hop through two or three redirects, so a single early
    ``location.href`` read would report ``voice.google.com`` and be wrong.
    """
    deadline = time.monotonic() + timeout
    last = None
    while True:
        try:
            state = await tab.evaluate(
                "document.readyState + ' ' + location.href", await_promise=False
            )
        except Exception as err:  # noqa: BLE001 - navigating away destroys the context
            log.debug('page not readable yet: %s', err)
            state = None
        ready, _, url = (state or '').partition(' ')
        if ready == 'complete' and url and url == last:
            return url
        last = url if ready == 'complete' else None
        if time.monotonic() >= deadline:
            return last or url or ''
        await asyncio.sleep(poll)


async def ensure_signed_in(
    browser,
    tab,
    *,
    session_path: pathlib.Path | None = DEFAULT_SESSION_PATH,
    url: str = ORIGIN,
) -> str:
    """
    Make sure ``tab`` is inside the signed-in Voice web app; return its URL.

    If the profile is signed out, inject the cookies saved at ``session_path``
    and reload ``url``.  Raises :class:`~googlevoice.util.LoginError` when
    neither the profile nor the saved session works (``session_path=None``
    skips the injection).
    """
    landed = await settled_url(tab)
    if is_signed_in_url(landed):
        _check_has_number(landed)
        return landed
    hint = 'Run `python -m googlevoice login` to sign in again.'
    if session_path is None:
        raise LoginError(f'Browser profile is not signed in to Google Voice. {hint}')
    try:
        cookies = load_session(session_path)
    except AuthError as err:
        raise LoginError(
            'Not signed in to Google Voice: the browser profile is signed out '
            f'and the saved session could not be loaded ({err}). {hint}'
        ) from None
    count = await inject_cookies(tab, cookies)
    log.info('profile signed out; injected %d saved cookies', count)
    await tab.get(url)
    landed = await settled_url(tab)
    if not is_signed_in_url(landed):
        raise LoginError(
            'Not signed in to Google Voice: the browser profile is signed out '
            f'and the saved session at {session_path} no longer works. {hint}'
        )
    _check_has_number(landed)
    return landed


async def refresh_session(
    browser,
    session_path: pathlib.Path = DEFAULT_SESSION_PATH,
    *,
    validate=None,
) -> bool:
    """
    Overwrite ``session_path`` with the browser's current Google cookies.

    Google rotates some login cookies while the web app runs; saving them keeps
    the portable session fresh.  The file is only replaced by a set that carries
    the essential login cookies *with values* and that authenticates a live
    ``account/get`` (``validate``, by default :func:`session_is_valid`).
    Anything less returns False and leaves the saved session alone.
    """
    if validate is None:
        validate = session_is_valid
    cookies = await harvest_cookies(browser)
    present = {c['name'] for c in cookies if c['value']}
    if not ESSENTIAL_COOKIES <= present:
        return False
    if not await asyncio.to_thread(validate, cookies):
        log.info("the browser's cookies do not authenticate; keeping the saved session")
        return False
    save_session(cookies, session_path)
    return True


async def _reap(process, timeout: float) -> None:
    """Wait for Chrome to exit after SIGTERM; SIGKILL it if it will not."""
    try:
        await asyncio.wait_for(process.wait(), timeout)
    except Exception:  # noqa: BLE001 - still running: escalate
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout)


async def close_browser(browser, *, timeout: float = 10.0) -> None:
    """
    Quit Chrome cleanly and wait for it to exit; escalate if it will not.

    ``nodriver``'s ``stop()`` only sends SIGTERM and returns at once, which on
    some platforms leaves the profile marked as crashed and its cookie store
    unflushed.  Asking Chrome to close over CDP lets it write the profile out
    first.  Every step is bounded by ``timeout``, so a wedged Chrome can neither
    hang the caller nor outlive the profile lock: after a failed graceful close
    it is terminated, then killed, and waited for.
    """
    from nodriver import cdp

    process = getattr(browser, '_process', None)

    async def graceful() -> None:
        await browser.send(cdp.browser.close())
        if process is not None:
            await process.wait()

    try:
        await asyncio.wait_for(graceful(), timeout)
    except Exception as err:  # noqa: BLE001 - teardown must never mask the real error
        log.debug('graceful browser close failed (%s); terminating instead', err)
        with contextlib.suppress(Exception):
            browser.stop()
        if process is not None:
            await _reap(process, timeout)
        return
    with contextlib.suppress(Exception):
        await browser.aclose()
    browser._process = None
    browser._process_pid = None


# --------------------------------------------------------------------------- #
# Browser login (one-time, needs Chrome)
# --------------------------------------------------------------------------- #
async def _browser_login_async(
    profile_dir: pathlib.Path, *, headless: bool, timeout: float, poll: float
):
    import nodriver as uc

    profile_dir = pathlib.Path(profile_dir).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)

    browser = await uc.start(
        headless=headless,
        user_data_dir=str(profile_dir),
        browser_args=browser_launch_args(),
    )
    try:
        tab = await browser.get(ORIGIN)
        print('>>> A Chrome window opened. Sign in to the Google account that')
        print('>>> owns your Google Voice number, then wait on the Voice inbox.')
        print('>>> Detecting login automatically...')

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cookies = await harvest_cookies(browser)
            names = {c['name'] for c in cookies}
            # Confirm by actually authenticating, not just by cookie presence.
            if ESSENTIAL_COOKIES <= names and session_is_valid(cookies):
                print('>>> Login confirmed (account/get succeeded).')
                # Pin the login in the profile too, so it outlives a clean exit.
                await inject_cookies(tab, cookies)
                return cookies
            await asyncio.sleep(poll)
        raise AuthError(f'Timed out after {timeout:.0f}s waiting for login.')
    finally:
        await close_browser(browser)


def browser_login(
    session_path: pathlib.Path = DEFAULT_SESSION_PATH,
    profile_dir: pathlib.Path = DEFAULT_PROFILE_DIR,
    *,
    headless: bool = False,
    timeout: float = 300,
    poll: float = 3,
) -> Credentials:
    """
    Open a browser, let the user sign in to Google, then persist the session.

    Returns ready-to-use :class:`Credentials`; also writes ``session_path``.
    """
    import nodriver as uc

    cookies = uc.loop().run_until_complete(
        _browser_login_async(profile_dir, headless=headless, timeout=timeout, poll=poll)
    )
    saved = save_session(cookies, session_path)
    print(f'>>> Saved {len(cookies)} cookies to {saved}')
    return Credentials(cookies)


# --------------------------------------------------------------------------- #
# CLI: python -m googlevoice.auth login | check
# --------------------------------------------------------------------------- #
def _main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog='python -m googlevoice.auth')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_login = sub.add_parser('login', help='Sign in via browser and save a session')
    p_login.add_argument('--session', default=str(DEFAULT_SESSION_PATH))
    p_login.add_argument('--profile', default=str(DEFAULT_PROFILE_DIR))
    p_login.add_argument('--headless', action='store_true')
    p_login.add_argument('--timeout', type=float, default=300)

    p_check = sub.add_parser('check', help='Verify a saved session still works')
    p_check.add_argument('--session', default=str(DEFAULT_SESSION_PATH))

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format='%(levelname)s %(name)s: %(message)s'
    )

    if args.cmd == 'login':
        browser_login(
            session_path=pathlib.Path(args.session),
            profile_dir=pathlib.Path(args.profile),
            headless=args.headless,
            timeout=args.timeout,
        )
        print('>>> Done. The session is ready to use from any machine.')
    elif args.cmd == 'check':
        ok = session_is_valid(load_session(pathlib.Path(args.session)))
        print('Session is VALID.' if ok else 'Session is INVALID or expired.')
        raise SystemExit(0 if ok else 1)


if __name__ == '__main__':
    _main()
