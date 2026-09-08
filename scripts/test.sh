#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
PYTHONPATH=apps python3 -m unittest discover -s tests -v
find scripts distro/alpine \( -name aports -o -name .work -o -name out \) -prune -o -name '*.sh' -exec sh -c '
  for file do
    case "$(head -n 1 "$file")" in *bash*) bash -n "$file";; *) sh -n "$file";; esac
  done
' sh {} +
python3 -c 'import xml.etree.ElementTree as E; E.parse("distro/alpine/overlay/etc/xdg/openbox/rc.xml")'
