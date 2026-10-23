#!/usr/bin/env bash
# Inside a manylinux_2_28 container (glibc 2.28: Debian 10+, Ubuntu 20.04+, CentOS / Rocky / Alma 8+,
# Alibaba Cloud Linux 3): build the standalone executable. Usage: packaging/build-linux.sh <name>
set -euo pipefail
cd "$(dirname "$0")/.."
curl -LsSf https://astral.sh/uv/install.sh | env INSTALLER_NO_MODIFY_PATH=1 sh
export PATH="$HOME/.local/bin:$PATH"
curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
. "$HOME/.cargo/env"
# uv's Python (python-build-standalone) has the shared libpython PyInstaller needs; manylinux's own does not
uv venv -q --python 3.12 /tmp/venv
. /tmp/venv/bin/activate
uv pip install -q . pyinstaller
packaging/build.sh "$1"
