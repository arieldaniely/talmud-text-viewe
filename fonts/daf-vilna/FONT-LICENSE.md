# Daf Vilna font license

SPDX-License-Identifier: GPL-3.0-only

The Daf Vilna Gemara, Rashi and Tosafot font binaries and their corresponding
design and build sources are licensed under the GNU General Public License,
version 3. See [COPYING](COPYING). This grant covers the original material in
the Daf Vilna project. Reference-edition font binaries and third-party fonts
are outside its scope.

When you distribute a modified font, license it under GPLv3 and make the
Corresponding Source available as required by GPLv3. This includes the
editable outlines and the scripts and inputs needed to build the font.
Section 2 governs whether output from running a covered work is itself
covered.

To build, use Python 3.10 or later, install `requirements.txt`, and run
`python build_fonts.py` in this directory. The build writes TTF and WOFF2
files to `fonts/v4`. The build requires no reference font binaries.
