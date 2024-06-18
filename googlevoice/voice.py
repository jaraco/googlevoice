import base64
import getpass
import importlib.metadata
import logging
import platform
import re

import requests

from . import settings, util
from .conf import config

qpat = re.compile(r'\?')

if settings.DEBUG:
    logging.basicConfig(level=logging.DEBUG)

log = logging.getLogger(__name__)


def _gen_user_agent():
    version = importlib.metadata.version('googlevoice')
    pyver = platform.python_version()
    return f'googlevoice/{version} Python/{pyver}'


class Voice:
    """
    Main voice instance for interacting with the Google Voice service
    Handles login/logout and most of the baser HTTP methods
    """

    user_agent = _gen_user_agent()

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': self.user_agent})

        for name in settings.FEEDS:
            setattr(self, name, self.__get_xml_page(name))

        setattr(self, 'message', self.__get_xml_page('message'))

    ######################
    # Some handy methods
    ######################
    @property
    def special(self):
        """
        Returns special identifier for your session (if logged in)
        """
        if getattr(self, '_special', None):
            return self._special
        pattern = re.compile(r"('_rnr_se':) '(.+)'")
        resp = self.session.get(settings.INBOX).text
        try:
            sp = pattern.search(resp).group(2)
        except AttributeError:
            sp = None
        self._special = sp
        return sp

    def login(self, email=None, passwd=None, smsKey=None):
        """
        Login to the service using your Google Voice account
        Credentials will be propmpted for if not given as args or in the
        ``~/.gvoice`` config file
        """
        if hasattr(self, '_special') and getattr(self, '_special'):
            return self

        email = email or config.email or input('Email address: ')
        passwd = passwd or config.password or getpass.getpass()

        content = self.__do_page('login').text
        # holy hackjob
        gxf = re.search(
            r"type=\"hidden\"\s+name=\"gxf\"\s+value=\"(.+)\"", content
        ).group(1)
        result = self.__do_page(
            'login_post', {'Email': email, 'Passwd': passwd, 'gxf': gxf}
        )

        if result.url.startswith(getattr(settings, "SMSAUTH")):
            content = self.__smsAuth(smsKey)

            try:
                smsToken = re.search(
                    r"name=\"smsToken\"\s+value=\"([^\"]+)\"", content
                ).group(1)
                content = self.__do_page(
                    'login', {'smsToken': smsToken, 'service': "grandcentral"}
                )
            except AttributeError:
                raise util.LoginError

            del smsKey, smsToken, gxf

        del email, passwd

        try:
            assert self.special
        except (AssertionError, AttributeError):
            raise util.LoginError

        return self

    def __smsAuth(self, smsKey=None):
        if smsKey is None:
            smsKey = config.smsKey

        if smsKey is None:
            from getpass import getpass

            smsPin = getpass("SMS PIN: ")
            content = self.__do_page('smsauth', {'smsUserPin': smsPin}).read()

        else:
            smsKey = base64.b32decode(re.sub(r' ', '', smsKey), casefold=True).encode(
                "hex"
            )
            content = self.__oathtoolAuth(smsKey)

            try_count = 1

            while (
                "The code you entered didn&#39;t verify." in content and try_count < 5
            ):
                sleep_seconds = 10
                try_count += 1
                print(
                    f'invalid code, retrying after {sleep_seconds} seconds (attempt {try_count})'
                )
                import time

                time.sleep(sleep_seconds)
                content = self.__oathtoolAuth(smsKey)

        del smsKey

        return content

    def __oathtoolAuth(self, smsKey):
        import commands

        smsPin = commands.getstatusoutput('oathtool --totp ' + smsKey)[1]
        content = self.__do_page('smsauth', {'smsUserPin': smsPin}).read()
        del smsPin
        return content

    def logout(self):
        """
        Logs out an instance and makes sure it does not still have a session
        """
        self.__do_page('logout')
        del self._special
        assert self.special is None
        return self

    def call(
        self,
        outgoingNumber,
        forwardingNumber=None,
        phoneType=None,
        subscriberNumber=None,
    ):
        """
        Make a call to an ``outgoingNumber`` from your
        ``forwardingNumber`` (optional).
        If you pass in your ``forwardingNumber``, please also pass
        in the correct ``phoneType``
        """
        if forwardingNumber is None:
            forwardingNumber = config.forwardingNumber
        if phoneType is None:
            phoneType = config.phoneType

        self.__validate_special_page(
            'call',
            {
                'outgoingNumber': outgoingNumber,
                'forwardingNumber': forwardingNumber,
                'subscriberNumber': subscriberNumber or 'undefined',
                'phoneType': phoneType,
                'remember': '1',
            },
        )

    __call__ = call

    def cancel(self, outgoingNumber=None, forwardingNumber=None):
        """
        Cancels a call matching outgoing and forwarding numbers (if given).
        Will raise an error if no matching call is being placed
        """
        self.__validate_special_page(
            'cancel',
            {
                'outgoingNumber': outgoingNumber or 'undefined',
                'forwardingNumber': forwardingNumber or 'undefined',
                'cancelType': 'C2C',
            },
        )

    @property
    def phones(self):
        """
        Returns a list of ``Phone`` instances attached to your account.
        """
        return [util.Phone(self, data) for data in self.contacts['phones'].values()]

    @property
    def settings(self):
        """
        Dict of current Google Voice settings
        """
        return util.AttrDict(self.contacts['settings'])

    def send_sms(self, phoneNumber, text):
        """
        Send an SMS message to a given ``phoneNumber`` with
        the given ``text`` message
        """
        self.__validate_special_page('sms', {'phoneNumber': phoneNumber, 'text': text})

    def search(self, query):
        """
        Search your Google Voice Account history for calls, voicemails, and sms
        Returns ``Folder`` instance containting matching messages
        """
        data = dict(q=query)
        return self.__get_xml_page('search', terms=data)()

    def archive(self, msg, archive=1):
        """
        Archive the specified message by removing it from the Inbox.
        """
        self.__messages_post('archive', msg, archive=archive)

    def delete(self, msg, trash=1):
        """
        Moves this message to the Trash. Use ``message.delete(0)``
        to move it out of the Trash.
        """
        self.__messages_post('delete', msg, trash=trash)

    def download(self, msg, adir=None):
        """
        Download a voicemail or recorded call MP3 matching the given ``msg``
        which can either be a ``Message`` instance, or a SHA1 identifier.
        Saves files to ``adir`` (defaults to current directory).
        Message hashes can be found in ``self.voicemail().messages`` for
        example.
        Returns location of saved file.
        """
        from os import getcwd, path

        if isinstance(msg, util.Message):
            msg = msg.id
        if adir is None:
            adir = getcwd()
        url = self.__resolve_page('download')
        url += msg
        try:
            resp = self.__do_url(url)
            resp.raise_for_status()
        except Exception:
            raise util.DownloadError
        fn = path.join(adir, f'{msg}.mp3')
        with open(fn, 'wb') as fo:
            fo.write(resp.content)
        return fn

    @property
    def contacts(self):
        """
        Partial data of your Google Account Contacts related to
        your Voice account.
        For a more comprehensive suite of APIs, check out
        http://code.google.com/apis/contacts/docs/1.0/developers_guide_python.html
        """
        if hasattr(self, '_contacts'):
            return self._contacts
        self._contacts = self.__get_xml_page('contacts')()
        return self._contacts

    ######################
    # Helper methods
    ######################

    def __resolve_page(self, page):
        return getattr(settings, page.upper())

    def __do_page(self, page, data=None, headers=None, terms=None):
        """
        Loads a page out of the settings and request it using requests.
        Return Response.
        """
        return self.__do_url(self.__resolve_page(page), data, headers, terms)

    def __do_url(self, url, data=None, headers=None, terms=None):
        log.debug('url is %s', url)
        log.debug('data is %s', data)
        method = 'POST' if data else 'GET'
        return self.session.request(
            method, url, data=data, params=terms or None, headers=headers
        )

    def __validate_special_page(self, page, data={}, **kwargs):
        """
        Validates a given special page for an 'ok' response
        """
        data.update(kwargs)
        util.load_and_validate(self.__do_special_page(page, data))

    _Phone__validate_special_page = __validate_special_page

    def __do_special_page(self, page, data=None, headers={}, terms={}):
        """
        Add self.special to request data
        """
        assert self.special, 'You must login before using this page'
        if isinstance(data, tuple):
            data += ('_rnr_se', self.special)
        elif isinstance(data, dict):
            data.update({'_rnr_se': self.special})
        return self.__do_page(page, data, headers, terms)

    _Phone__do_special_page = __do_special_page

    def __get_xml_page(self, page, data=None, headers={}, terms={}):
        """
        Return XMLParser instance generated from given page
        """

        def getter():
            page_name = f'XML_{page.upper()}'
            return self.__do_special_page(page_name, data, headers, terms).text

        return util.XMLParser(self, page, getter)

    def __messages_post(self, page, *msgs, **data):
        """
        Performs message operations, eg deleting,staring,moving
        """
        if len(msgs) != 1:
            raise NotImplementedError("Only supports one message")
        for msg in msgs:
            if isinstance(msg, util.Message):
                msg = msg.id
            data['messages'] = msg
        return self.__do_special_page(page, data)

    _Message__messages_post = __messages_post
