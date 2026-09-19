"""
Browser-driven SMS sending for Google Voice.

Reading from Google Voice is a plain HTTP call (see :mod:`googlevoice.voice`),
but *sending* is gated behind anti-abuse tokens -- a reCAPTCHA Enterprise token
and a BotGuard token -- that can only be produced by executing Google's
obfuscated JavaScript in a real browser. Rather than try to forge those
(infeasible) or pay a captcha service (which can't generate BotGuard tokens and
would score as a bot anyway), :class:`BrowserSender` simply drives the real
``voice.google.com`` web app via ``nodriver``: the page mints valid tokens
itself, bound to your live session.

Usage::

    from googlevoice.browser import BrowserSender

    with BrowserSender() as sender:           # reuses ~/.googlevoice/chrome-profile
        sender.send_sms('+12085551234', 'Hello from Python!')

Sign in once with ``python -m googlevoice login``. The browser signs in from
the saved ``session.json`` whenever its profile has been signed out (Chrome
drops the login cookies on a clean exit), and refreshes that file on close.
Sending therefore needs Chrome installed and running; reading does not.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ._browserlock import BrowserBusyError, ProfileLock
from .auth import (
    DEFAULT_DIR,
    DEFAULT_PROFILE_DIR,
    DEFAULT_SESSION_PATH,
    ORIGIN,
    REFRESH_TIMEOUT,
    browser_launch_args,
    close_browser,
    ensure_signed_in,
    headless_default,
    refresh_session,
)
from .util import APIError
from .voice import normalize_number

# Re-exported so callers can ``from googlevoice.browser import BrowserBusyError``.
__all__ = ['BrowserBusyError', 'BrowserSender', 'capture_api_calls']

log = logging.getLogger(__name__)

# A JS helper namespace installed into the page. Google Voice buries its
# controls in nested Shadow DOM, so every lookup recurses through shadow roots
# rather than use a flat CSS selector.
_HELPERS_JS = r"""
window.__gv = (function () {
  function deep(sel, root, acc) {
    root = root || document; acc = acc || [];
    root.querySelectorAll(sel).forEach(e => acc.push(e));
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) deep(sel, e.shadowRoot, acc); });
    return acc;
  }
  const vis = e => e && e.offsetParent !== null && !e.disabled;
  return {
    clickAria(label) {
      const el = deep('[aria-label]').find(e => vis(e) && e.getAttribute('aria-label') === label);
      if (!el) return 'MISS'; el.click(); return 'OK';
    },
    clickClass(cls) {
      const el = deep('.' + cls).find(vis);
      if (!el) return 'MISS'; el.click(); return 'OK';
    },
    focus(placeholder) {
      const el = deep('input, textarea').find(
        e => vis(e) && (e.placeholder || '').includes(placeholder));
      if (!el) return 'MISS';
      el.scrollIntoView(); el.click(); el.focus(); return 'OK';
    },
  };
})(); 'installed'
"""


class BrowserSender:
    """
    Sends Google Voice SMS by driving the real web app in a Chrome instance
    (via ``nodriver``). Reuses the profile created by ``python -m googlevoice
    login`` and, when that profile has been signed out, signs in again from the
    saved ``session_path`` (``None`` disables this and requires a signed-in
    profile).
    """

    def __init__(
        self,
        profile_dir=DEFAULT_PROFILE_DIR,
        *,
        session_path=DEFAULT_SESSION_PATH,
        headless: bool | None = None,
        timeout: float = 60,
        wait: bool = False,
    ):
        self.profile_dir = profile_dir
        self.session_path = session_path
        # headless=None -> auto: headed if a display is available, else headless
        # (a headed Chrome can't start on a display-less server/container).
        self.headless = headless_default() if headless is None else headless
        self.timeout = timeout
        # wait=False -> raise BrowserBusyError if the profile is already in use;
        # wait=True -> queue until it frees up.
        self.wait = wait
        self._browser = None
        self._tab = None
        self._loop = None
        self._lock = None
        self._signed_in = False
        self._statuses: list[int] = []

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def __enter__(self) -> BrowserSender:  # noqa: PYI034
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        import nodriver as uc

        # One Chrome per profile: take the lock (clearing any stale one) before
        # launching, so concurrent use fails fast instead of cryptically.
        self._lock = ProfileLock(self.profile_dir, wait=self.wait)
        self._lock.acquire()
        try:
            self._loop = uc.loop()
            self._loop.run_until_complete(self._start())
        except BaseException:
            self.close()  # release the lock / stop a half-started browser
            raise

    async def _start(self) -> None:
        import nodriver as uc
        from nodriver import cdp

        self._browser = await uc.start(
            headless=self.headless,
            user_data_dir=str(self.profile_dir),
            browser_args=browser_launch_args(),
        )
        self._tab = await self._browser.get(ORIGIN)

        def _on_response(ev):
            if 'sendsms' in ev.response.url:
                self._statuses.append(int(ev.response.status))

        self._tab.add_handler(cdp.network.ResponseReceived, _on_response)
        await self._tab.send(cdp.network.enable())

        # Signed-out profile -> sign in from session.json; raises LoginError
        # if neither works.
        await ensure_signed_in(self._browser, self._tab, session_path=self.session_path)
        self._signed_in = True

    def close(self) -> None:
        try:
            if self._browser is not None:
                browser, self._browser, self._tab = self._browser, None, None
                try:
                    self._loop.run_until_complete(self._shutdown(browser))
                except BaseException as err:
                    log.debug('graceful shutdown failed (%s); terminating', err)
                    with contextlib.suppress(Exception):
                        browser.stop()
                    if not isinstance(err, Exception):
                        raise  # Ctrl-C and cancellation still propagate
        finally:
            # Whatever happened above, the profile must not stay locked.
            if self._lock is not None:
                self._lock.release()
                self._lock = None

    async def _shutdown(self, browser) -> None:
        # Keep the portable session current with Google's rotated cookies, then
        # let Chrome quit cleanly so the profile is written out.  Both steps are
        # bounded: a wedged Chrome or network must not hang close().
        if self._signed_in and self.session_path is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    refresh_session(browser, self.session_path), REFRESH_TIMEOUT
                )
        await close_browser(browser)

    # ------------------------------------------------------------------ #
    # sending
    # ------------------------------------------------------------------ #
    def send_sms(
        self, recipient: str | list[str], text: str, *, thread_id: str | None = None
    ) -> None:
        """
        Send ``text`` by driving the web app's "new message" composer, which
        routes correctly for both new and existing conversations.

        ``recipient`` may be a single number (E.164, e.g. ``+12085551234``) or a
        list/comma-separated string of numbers -- pass more than one to start a
        **group message**. ``thread_id`` is accepted for API symmetry with
        :meth:`googlevoice.Voice.send_sms` but is not needed here.
        """
        if self._browser is None:
            raise RuntimeError(
                'BrowserSender not started; use it as a context manager.'
            )
        if isinstance(recipient, str):
            recipient = recipient.split(',')
        recipients = [normalize_number(r) for r in recipient if r.strip()]
        if not recipients:
            raise APIError('No recipient given.')
        self._loop.run_until_complete(self._send(recipients, text))

    async def _type(self, text: str) -> None:
        """Type ``text`` into the focused element with real (trusted) keystrokes."""
        from nodriver import cdp

        for ch in text:
            await self._tab.send(
                cdp.input_.dispatch_key_event(type_='keyDown', text=ch, key=ch)
            )
            await self._tab.send(cdp.input_.dispatch_key_event(type_='keyUp', key=ch))
            await self._sleep(0.04)

    async def _press_enter(self) -> None:
        """Press Enter on the focused element (sends the message)."""
        from nodriver import cdp

        for kind in ('keyDown', 'keyUp'):
            await self._tab.send(
                cdp.input_.dispatch_key_event(
                    type_=kind,
                    key='Enter',
                    code='Enter',
                    windows_virtual_key_code=13,
                    native_virtual_key_code=13,
                )
            )

    async def _action(self, expr: str):
        """Evaluate ``expr``; reinstall the JS helpers and retry once if needed."""
        try:
            return await self._tab.evaluate(expr, await_promise=False)
        except Exception:  # noqa: BLE001 - a navigation wipes the helpers; reinstall
            await self._tab.evaluate(_HELPERS_JS, await_promise=False)
            return await self._tab.evaluate(expr, await_promise=False)

    async def _poll(self, expr: str, *, tries: int = 20, delay: float = 0.5) -> str:
        """Call ``expr`` until it returns ``'OK'`` or attempts run out."""
        res = 'MISS'
        for _ in range(tries):
            res = await self._action(expr)
            if res == 'OK':
                return res
            await self._sleep(delay)
        return res

    async def _add_recipient(self, number: str) -> None:
        """Type one number into the recipient field and pick its suggestion."""
        # The Material autocomplete only opens its "Send to <number>" suggestion
        # in response to genuine key events, not a synthetic value set.
        if await self._poll("window.__gv.focus('Type a name or phone number')") != 'OK':
            raise APIError('Could not find the recipient field.')
        await self._type(number)
        await self._sleep(1.5)  # let the autocomplete populate
        if await self._poll("window.__gv.clickClass('send-to-label')") != 'OK':
            raise APIError(
                f'No "Send to {number}" suggestion appeared; '
                'is the number valid and textable?'
            )
        await self._sleep(1)

    async def _send(self, recipients: list[str], text: str) -> None:
        before = len(self._statuses)
        await self._browser.get(f'{ORIGIN}/u/0/messages')
        await self._sleep(5)
        await self._tab.evaluate(_HELPERS_JS, await_promise=False)

        # 1. open the "new message" composer
        if await self._poll("window.__gv.clickAria('Send new message')") != 'OK':
            raise APIError('Could not open the new-message composer.')
        await self._sleep(1)

        # 2. add each recipient in turn (>1 number => a group message)
        for number in recipients:
            await self._add_recipient(number)

        # 3. focus the compose box, type the message with real keystrokes, and
        #    press Enter to send (the new-message composer sends on Enter).
        if await self._poll("window.__gv.focus('Type a message')") != 'OK':
            raise APIError('Could not find the compose box.')
        await self._type(text)
        await self._sleep(0.5)
        await self._press_enter()

        # 4. confirm the send actually went through
        for _ in range(int(self.timeout * 2)):
            await self._sleep(0.5)
            if len(self._statuses) > before:
                status = self._statuses[-1]
                if status == 200:
                    log.info('sent to %s', ', '.join(recipients))
                    return
                raise APIError(f'sendsms returned HTTP {status}')
        raise APIError('Timed out waiting for the message to send.')

    async def _sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)


DEFAULT_CAPTURE_PATH = DEFAULT_DIR / 'capture.jsonl'


def capture_api_calls(
    profile_dir=DEFAULT_PROFILE_DIR,
    *,
    seconds: float = 180,
    headless: bool | None = None,
    wait: bool = False,
    out_path=DEFAULT_CAPTURE_PATH,
) -> list[dict]:
    """
    Open the signed-in web app and record every ``voiceclient`` POST (endpoint
    and decoded body) for ``seconds`` while you drive it by hand.

    This is the maintenance tool used to reverse-engineer the endpoints in
    :mod:`googlevoice.voice`; rerun it if Google changes the web API.  Perform
    an action in the browser and read the captured ``endpoint  body`` to update
    the relevant constant (``Folder``, ``MessageType``, the ``_Attr`` indices
    for ``thread/batchupdateattributes``, or ``_MEDIA_URL_KEYS``).  Each request
    is **written immediately** (one JSON object per line) to ``out_path`` *and*
    printed; writing+flushing per line means the log survives buffering or a
    killed process -- you can ``tail -f`` it live.

    Returns the captured calls as ``[{'endpoint', 'url', 'body'}, ...]``.
    """
    import nodriver as uc

    if headless is None:
        headless = headless_default()
    lock = ProfileLock(profile_dir, wait=wait)
    lock.acquire()
    try:
        return uc.loop().run_until_complete(
            _capture_async(
                profile_dir, seconds=seconds, headless=headless, out_path=out_path
            )
        )
    finally:
        lock.release()


async def _capture_async(profile_dir, *, seconds, headless, out_path) -> list[dict]:
    import asyncio
    import json
    import pathlib

    import nodriver as uc
    from nodriver import cdp

    out_path = pathlib.Path(out_path).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sink = out_path.open('w', encoding='utf-8')

    calls: list[dict] = []
    browser = await uc.start(
        headless=headless,
        user_data_dir=str(profile_dir),
        # keep cross-origin iframes in the page process so their requests surface
        # on the page target rather than a separate one we'd miss.
        browser_args=[
            *browser_launch_args(),
            '--disable-features=IsolateOrigins,site-per-process',
        ],
    )
    try:
        tab = await browser.get(ORIGIN)

        static_ext = (
            '.js',
            '.css',
            '.png',
            '.jpg',
            '.jpeg',
            '.gif',
            '.svg',
            '.woff',
            '.woff2',
            '.ico',
            '.webp',
            '.map',
        )

        def _on_request(ev):
            req = ev.request
            url = req.url or ''
            path = url.split('?', 1)[0].lower()
            if path.endswith(static_ext):
                return  # static asset
            if any(
                s in url
                for s in (
                    'gstatic.com',
                    'google-analytics.com',
                    '/gen_204',
                    '/jserror',
                    '/ulog',
                    'play.google.com/log',
                )
            ):
                return  # telemetry / analytics
            # Keep every POST (writes can go to any host / RPC), plus GETs that
            # look like API calls -- so we catch whichever mechanism each action
            # uses, even a worker or a non-clients6 host.
            api_like = any(
                s in url for s in ('voiceclient', 'api2thread', '/voice/', '/$rpc/')
            )
            if req.method != 'POST' and not api_like:
                return
            if 'voiceclient/' in url:
                endpoint = url.split('voiceclient/', 1)[1].split('?', 1)[0]
            else:
                endpoint = f'{req.method} ' + url.split('//', 1)[-1].split('?', 1)[0]
            raw = req.post_data
            try:
                body = json.loads(raw) if raw else None
            except (ValueError, TypeError):
                body = raw
            rec = {'endpoint': endpoint, 'url': req.url, 'body': body}
            calls.append(rec)
            sink.write(json.dumps(rec) + '\n')
            sink.flush()  # the log is the source of truth -- survive kill/buffering
            print(f'  {endpoint}\n    {json.dumps(body)}', flush=True)

        tab.add_handler(cdp.network.RequestWillBeSent, _on_request)
        # max_post_data_size makes Chrome include request bodies inline in the
        # event (otherwise post_data is None for non-trivial bodies).
        await tab.send(cdp.network.enable(max_post_data_size=1 << 20))

        # Attach to any other targets too (extra tabs, web/service workers) so a
        # request a worker issues is captured, not just the main page's.
        for other in list(getattr(browser, 'targets', [])):
            if other is tab:
                continue
            try:
                other.add_handler(cdp.network.RequestWillBeSent, _on_request)
                await other.send(cdp.network.enable(max_post_data_size=1 << 20))
            except Exception as err:  # noqa: BLE001 - not all targets support Network
                log.debug('skip target %s: %s', other, err)

        # A signed-out profile records nothing useful; sign in from session.json.
        await ensure_signed_in(browser, tab)

        print(
            f'>>> Recording voiceclient calls to {out_path} for {seconds:.0f}s.',
            flush=True,
        )
        print(
            '>>> In the browser: archive / spam / block / delete / search / play voicemail.',
            flush=True,
        )
        await asyncio.sleep(seconds)
    finally:
        await close_browser(browser)
        sink.close()
    print(f'>>> Captured {len(calls)} call(s).', flush=True)
    return calls
