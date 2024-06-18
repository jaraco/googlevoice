import configparser
import contextlib
import pathlib

from . import settings


class Config(configparser.ConfigParser):
    """
    ``ConfigParser`` subclass that looks into your home folder for a file named
    ``.gvoice`` and parses configuration data from it.
    """

    def __init__(self, filename: str = '~/.gvoice'):
        self.fname = pathlib.Path(filename).expanduser()

        if not self.fname.exists():
            with contextlib.suppress(OSError):
                self.fname.write_text(settings.DEFAULT_CONFIG, encoding='utf-8')

        configparser.ConfigParser.__init__(self)

        with contextlib.suppress(OSError):
            self.read([self.fname], encoding='utf-8')

    def get(self, option, section='gvoice', **kwargs):
        try:
            return (
                configparser.ConfigParser.get(self, section, option, **kwargs).strip()
                or None
            )
        except configparser.NoOptionError:
            return

    def set(self, option, value, section='gvoice'):
        return configparser.ConfigParser.set(self, section, option, value)

    @property
    def phoneType(self):
        try:
            return int(self.get('phoneType'))
        except TypeError:
            return

    def save(self):
        with self.fname.open('w', encoding='utf-8') as f:
            self.write(f)

    forwardingNumber = property(lambda self: self.get('forwardingNumber'))
    email = property(lambda self: self.get('email', 'auth'))
    password = property(lambda self: self.get('password', 'auth'))
    smsKey = property(lambda self: self.get('smsKey', 'auth'))
    secret = property(lambda self: self.get('secret'))


config = Config()
