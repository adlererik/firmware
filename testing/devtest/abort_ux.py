# (c) Copyright 2020 by Coinkite Inc. This file is covered by license found in COPYING-CC.
#
# abort menu system and return to top menu
from main import pa, numpad

pa.setup(pa.pin)
pa.login()

from actions import goto_top_menu
goto_top_menu()

numpad.abort_ux()
