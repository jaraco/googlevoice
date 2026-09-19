"""Download the audio of every voicemail to the current directory.

Modernized from the original project's ``download-mp3.py``.

Run ``python -m googlevoice.auth login`` once first to create a session.

.. note::
   The media URL the audio is fetched from is *inferred* (see the protocol spec
   in ``googlevoice/voice.py``).  If downloads fail, run
   ``python -m googlevoice capture``, play a voicemail in the web app, and
   update ``_MEDIA_URL_KEYS`` to match the real request.
"""

from googlevoice import Voice
from googlevoice.util import DownloadError


def run():
    download_dir = '.'
    voice = Voice()  # loads ~/.googlevoice/session.json

    for vm in voice.voicemails(count=20):
        if not vm.has_audio:
            continue
        try:
            print('Saved', vm.download(download_dir))
        except DownloadError as err:
            print(f'Could not download {vm.id}: {err}')


__name__ == '__main__' and run()
