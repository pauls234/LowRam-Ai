#!/bin/bash
# LowRAM AI Studio - double-click in Finder to start it from the source folder.
#
# The first start sets everything up (Python environment + components) and
# takes a minute; later starts open the app right away. Keep this Terminal
# window open while you use LowRAM AI; closing the app window stops it.
# Needs Python 3.10+ from https://www.python.org/downloads/
cd "$(dirname "$0")" || exit 1
exec /bin/bash ./start.sh "$@"
