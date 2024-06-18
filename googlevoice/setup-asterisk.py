#!/usr/bin/env python
"""
Jacob Feisley, Ward Mundy, and Justin Quick
"""

from getpass import getpass
from sys import exit


def main():
    print(
        """This script installs Google Voice support on your PBX.
    You must have a system that is compatible with PBX in a Flash.
    WARNING: No error checking is provided.
    By using this script, you agree to assume ALL RISK.
    NO WARRANTY, EXPRESS OR IMPLIED, OF ANY KIND IS PROVIDED.

    If you make a typo while entering values below, press
    Ctrl-C and start over. Before starting, make sure you
    have removed any original [custom-gv] context.
    """
    )

    conf_default = '/etc/asterisk/extensions_custom.conf'
    conf = input(
        f"""Asterisk dialplan configuration file
        [Default {conf_default}]: """
    )

    if not conf.strip():
        conf = conf_default

    print(
        f"""
    Your Google Voice entries are stored in {conf}
    Edit that file and reload your Asterisk dialplan if you
    make future changes.
    """
    )

    settings = {
        'config': conf,
        'gvnum': input("10-digit Google Voice phone number (e.g. 9871234567): "),
        'acctname': input("Google Voice email address: "),
        'acctpass': getpass("Google Voice password: "),
        'ringback': input("11-digit Ring Back DID (e.g. 16781234567): "),
        'callpark': input("Parking Lot Magic Number: "),
    }

    input(
        """
    We are now ready to begin the installation.
    Confirm your entries below or press Ctrl-C to abort and try again.

    CONFIG: {config}
    GVNUM: {gvnum}
    ACCTNAME: {acctname}
    RINGBACK: {ringback}
    CALLPARK: {callpark}

    Hit Enter key to proceed
    """.format(**settings)
    )

    print(
        """
    Installing Google Voice support for your PBX. One moment please...
    """
    )

    content = r"""
    [custom-gv]
    exten => _X.,1,Wait(1)
    exten => _X.,n,Set(ACCTNAME=%(acctname)s)
    exten => _X.,n,Set(ACCTPASS=%(acctpass)s)
    exten => _X.,n,Set(RINGBACK=%(ringback)s)
    exten => _X.,n,Set(CALLPARK=%(callpark)s)
    exten => _X.,n,Playback(pls-wait-connect-call)
    exten => _X.,n,System(gvoice -b -e \${ACCTNAME} -p \
\${ACCTPASS} call \${EXTEN} \${RINGBACK})
    exten => _X.,n,Set(PARKINGEXTEN=\${CALLPARK})
    exten => _X.,n,Park()
    exten => _X.,n,ParkAndAnnounce(pbx-transfer:PARKED|45|Console/dsp)

    [custom-park]
    exten => s,1,Wait(4)
    exten => s,2,Set(GVNUM=%(gvnum)s)
    exten => s,3,Set(CALLPARK=%(callpark)s)
    exten => s,4,NoOp(**CALLERID: \${CALLERID(number)})
    exten => s,5,GotoIf($["${CALLERID(number)}"="${GVNUM}"]?6:7)
    exten => s,6,ParkedCall(\${CALLPARK})
    exten => s,7,Goto(from-trunk,gv-incoming,1)

    """

    try:
        fo = open(conf, 'a')
        fo.write(content % settings)
        fo.close()
    except OSError:
        print(f'Error opening file for writing: {conf}')
        exit(0)

    print(
        """
    Installation script is finished. Do NOT run it again on this system!

    You can now reload your Asterisk dialplan configuration with
    the following command:

        asterisk -rx "dialplan reload"

    For complete documentation, see the following Nerd Vittles article:

    http://nerdvittles.com/?p=635
    """
    )


__name__ == '__main__' and main()
