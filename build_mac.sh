#!/bin/bash
# Build dist/News Analyzer.app and dist/News Analyzer.dmg for Apple Silicon Macs.
#
# The build Python sets the oldest macOS the app starts on. python.org 3.12 is built for macOS 11;
# Homebrew's Python and its libraries require the macOS version they were built on (26 here).
set -euo pipefail
cd "$(dirname "$0")"

PYTHON=${PYTHON:-/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12}
VENV=venv-build
APP="dist/News Analyzer.app"
DMG="dist/News Analyzer.dmg"

if [ ! -f _embedded_creds.py ]; then
    echo "_embedded_creds.py is missing: the app would start without Telegram API keys" >&2
    exit 1
fi
# A fresh venv every build: pip keeps an installed package of the same version even when
# requirements-build.txt pins another wheel of it (scipy)
"$PYTHON" -m venv --clear "$VENV"
"$VENV/bin/pip" install --quiet -r requirements-build.txt
"$VENV/bin/pyinstaller" --noconfirm --clean NewsAnalyzer.spec

# A dependency wheel built for a newer macOS raises the real minimum silently, so check every binary
"$VENV/bin/python" - "$APP" <<'EOF'
import os, plistlib, re, subprocess, sys

app = sys.argv[1]
with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
    promised = plistlib.load(f)["LSMinimumSystemVersion"]
too_new = []
for root, _dirs, files in os.walk(app):
    for name in files:
        path = os.path.join(root, name)
        if os.path.islink(path):
            continue
        with open(path, "rb") as f:
            if f.read(4) not in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
                continue
        load_commands = subprocess.run(["otool", "-arch", "arm64", "-l", path], capture_output=True, text=True).stdout
        m = re.search(r"minos (\S+)", load_commands)
        if m and tuple(map(int, m.group(1).split("."))) > tuple(map(int, promised.split("."))):
            too_new.append(f"  macOS {m.group(1)}: {os.path.relpath(path, app)}")
if too_new:
    sys.exit(f"These binaries need a newer macOS than LSMinimumSystemVersion {promised}:\n" + "\n".join(too_new))
EOF

# DMG layout as in 1.0: the app, a link to Applications and the install guide.
# The staging copy is an APFS clone, so it takes no space; the OS cleans the temp dir.
STAGE=$(mktemp -d)
cp -cR "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
install -m 644 packaging/install_ru.txt "$STAGE/УСТАНОВКА.txt"
hdiutil create -volname "News Analyzer" -srcfolder "$STAGE" -format UDZO -ov "$DMG"
echo "Built $DMG"
