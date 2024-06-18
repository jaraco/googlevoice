import configparser
import os

from . import settings


class Config(configparser.ConfigParser):
    """
    ``ConfigParser`` subclass that looks into your home folder for a file named
    ``.gvoice`` and parses configuration data from it.
    """

    def __init__(self, filename: str = os.path.expanduser('~/.gvoice')):
        self.fname = filename

        if not os.path.exists(self.fname):
            try:
                with open(self.fname, 'w', encoding='utf-8') as f:
                    f.write(settings.DEFAULT_CONFIG)
            except OSError:
                return

        configparser.ConfigParser.__init__(self)

        try:
            self.read([self.fname], encoding='utf-8')
        except OSError:
            return

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
        with open(self.fname, 'w', encoding='utf-8') as f:
            self.write(f)

    forwardingNumber = property(lambda self: self.get('forwardingNumber'))
    email = property(lambda self: self.get('email', 'auth'))
    password = property(lambda self: self.get('password', 'auth'))
    smsKey = property(lambda self: self.get('smsKey', 'auth'))
    secret = property(lambda self: self.get('secret'))


config = Config()
