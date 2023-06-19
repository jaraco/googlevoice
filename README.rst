.. image:: https://img.shields.io/pypi/v/googlevoice.svg
   :target: https://pypi.org/project/googlevoice

.. image:: https://img.shields.io/pypi/pyversions/googlevoice.svg

.. image:: https://github.com/jaraco/googlevoice/workflows/tests/badge.svg
   :target: https://github.com/jaraco/googlevoice/actions?query=workflow%3A%22tests%22
   :alt: tests

.. image:: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/charliermarsh/ruff/main/assets/badge/v2.json
    :target: https://github.com/astral-sh/ruff
    :alt: Ruff

.. image:: https://img.shields.io/badge/code%20style-black-000000.svg
   :target: https://github.com/psf/black
   :alt: Code style: Black

.. image:: https://readthedocs.org/projects/googlevoice/badge/?version=latest
   :target: https://googlevoice.readthedocs.io/en/latest/?badge=latest

.. image:: https://img.shields.io/badge/skeleton-2023-informational
   :target: https://blog.jaraco.com/skeleton


Python Google Voice Library

Based on pygooglevoice by Joe McCall & Justin Quick.

This project is *essentially broken* as the login API has changed and a new technique for logging in has not been developed. Please see `issue 8 <https://github.com/jaraco/googlevoice/issues/8>`_ for details.


Exposing the Google Voice "API" to the Python language
-------------------------------------------------------

Google Voice for Python Allows you to place calls, send sms, download voicemail, and check the various folders of your Google Voice Account.
You can use the Python API or command line script to schedule calls, check for new recieved calls/sms, or even sync your recorded voicemails/calls.
