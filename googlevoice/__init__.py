"""
This project aims to bring the power of the Google Voice API to
the Python language in a simple,
easy-to-use manner. Currently it allows one to place calls, send sms,
download voicemails/recorded messages, and search the various
folders of Google Voice Accounts.
Use the Python API or command line script to schedule
calls, check for new received calls/sms,
or even sync recorded voicemails/calls.
"""

from .util import Folder, Message, Phone
from .voice import Voice

__all__ = ['Voice', 'Phone', 'Message', 'Folder']
