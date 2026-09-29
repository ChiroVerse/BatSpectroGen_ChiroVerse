#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "BatSpectroGen is not installed yet."
  echo "Run ./Install_BatSpectroGen_macOS_Linux.sh first."
  exit 1
fi

exec .venv/bin/python BatSpectroGen.py
