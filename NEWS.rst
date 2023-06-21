v3.1.0
======

Refreshed project metadata. Cleaned up lint.

Requires Python 3.7 or later.

Login is still non-functional.

v3.0.0
======

Refreshed project metadata. Now requires Python 3.6 or later.
Login still basically is non-functional.

2.0.5
=====

* #3: Fixed error in Delete message.

2.0.4
=====

* pettazz/pygooglevoice#10: Fix typo in ``googlevoice`` command.

2.0.3
=====

* pettazz/pygooglevoice#4: Removed multiple checks for sha-1
  hash, which failed in some cases when the Message IDs
  weren't of that form.

2.0.2
=====

Fixed more bugs in the implementation, revealed by an enhanced
test suite.

2.0.1
=====

* #2: Fixed bugs in login. Confirmed ``examples/sms.py`` works
  again.

2.0
===

Rebased code on upstream pygooglevoice.

Removed scripts, replaced with runnable modules invokable with
``python -m``:

 - googlevoice
 - googlevoice.interact
 - googlevoice.setup-asterisk

Added test suite via tox and pytest, although currently only imports
and lint are tested.

Automated releases via Travis-CI.

Renamed GitHub repo to jaraco/googlevoice.
