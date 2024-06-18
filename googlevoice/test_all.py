import os
import sys
import random
import string
import time
import itertools

import responses
import pytest
import faker

from googlevoice import Voice
from googlevoice import settings
from googlevoice import conf


fake = faker.Faker()


@pytest.fixture
def random_gxf():
    def random_alphanumeric():
        while True:
            yield random.choice(string.ascii_letters + string.digits + '-')

    token = '-'.join((''.join(itertools.islice(random_alphanumeric(), 34)),))
    timestamp = int(time.time() * 1000)
    return '{token}:{timestamp}'.format(**locals())


@pytest.fixture
def config(tmpdir):
    return conf.Config(str(tmpdir / 'test-config.ini'))


class TestVoice:
    @responses.activate
    def test_login(self, random_gxf):
        responses.add(
            responses.GET,
            settings.LOGIN,
            """
            ...
            <input type="hidden" name="gxf" value="{random_gxf}">
            ...
            """.format(**locals()),
        )
        responses.add(
            responses.POST,
            settings.LOGIN_POST,
        )
        responses.add(responses.GET, settings.INBOX, "'_rnr_se': 'special-value'")
        voice = Voice()
        voice.login(email=fake.email(), passwd=fake.password())
        assert voice.special == 'special-value'

    @pytest.fixture(scope='class')
    def voice(self):
        has_creds = conf.config.email and conf.config.password
        output_captured = getattr(sys.stdout, 'name') != '<stdout>'
        if not has_creds and output_captured:
            pytest.skip("Cannot run with output captured")

        voice = Voice()
        voice.login()
        return voice

    @pytest.fixture
    def outgoing(self):
        if getattr(sys.stdout, 'name') != '<stdout>':
            pytest.skip("Cannot run with output captured")
        outgoing = input('Outgoing number (blank to ignore call tests): ')
        if not outgoing:
            pytest.skip("no outgoing number provided")
        return outgoing

    @pytest.fixture
    def forwarding(self, outgoing):
        return input('Forwarding number [optional]: ') or None

    def test_1call(self, voice, outgoing, forwarding):
        voice.call(outgoing, forwarding)

    def test_sms(self, voice, outgoing, forwarding):
        voice.send_sms(outgoing, 'i sms u')

    def test_2cancel(self, voice, outgoing, forwarding):
        voice.cancel(outgoing, forwarding)

    def test_special(self, voice):
        assert voice.special

    def test_inbox(self, voice):
        assert voice.inbox

    def test_balance(self, voice):
        assert voice.settings['credits']

    def test_search(self, voice):
        assert len(voice.search('joe'))

    def test_disable_enable(self, voice):
        voice.phones[0].disable()
        voice.phones[0].enable()

    def test_download(self, voice, tmpdir):
        msg = list(voice.voicemail().messages)[0]
        fn = f'{msg.id}.mp3'
        with tmpdir.as_cwd():
            voice.download(msg)
            assert os.path.isfile(fn)

    def test_zlogout(self, voice):
        voice.logout()
        voice.special is None


class TestConfig:
    def test_defaults(self, config):
        assert config.forwardingNumber is None
        assert config.phoneType == 2

    def test_missing_key(self, config):
        assert config.get('wtf') is None

    def test_set_get(self, config):
        number = '+15555551212'
        config.set('forwardingNumber', number)
        assert config.forwardingNumber == number
        config.save()
        new_config = conf.Config(config.fname)
        assert new_config.forwardingNumber == number
