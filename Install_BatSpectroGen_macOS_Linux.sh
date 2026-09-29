#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 was not found. Install Python 3.9 through 3.13, then try again."
  exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if (3, 9) <= sys.version_info[:2] < (3, 14) else 1)'; then
  echo "BatSpectroGen requires Python 3.9, 3.10, 3.11, 3.12, or 3.13."
  exit 1
fi

echo "Creating the BatSpectroGen environment..."
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip

if .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 9) else 1)'; then
  .venv/bin/python -m pip install 'numpy>=1.23,<2.1' 'matplotlib>=3.7,<3.10' 'soundfile>=0.12.1,<0.14'
else
  .venv/bin/python -m pip install 'numpy>=1.23' 'matplotlib>=3.7,<3.11' 'soundfile>=0.12.1'
fi

.venv/bin/python -c "import tkinter, numpy, matplotlib, soundfile, BatSpectroGen; print('BatSpectroGen is ready.')"
printf '\nSetup complete. Run ./Run_BatSpectroGen_macOS_Linux.sh to start.\n'
