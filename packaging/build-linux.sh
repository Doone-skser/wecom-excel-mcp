#!/usr/bin/env bash
# Inside a manylinux_2_28 container (AlmaLinux 8, glibc 2.28: Debian 10+, Ubuntu 20.04+, CentOS / Rocky / Alma 8+,
# Alibaba Cloud Linux 3): build the standalone executable. Usage: packaging/build-linux.sh <name>
set -euo pipefail
cd "$(dirname "$0")/.."
# PyInstaller needs a Python with a shared libpython: the system python3.12 has one, manylinux's /opt/python
# and uv's python-build-standalone do not
dnf install -y -q python3.12 python3.12-devel python3.12-pip >/dev/null
curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
. "$HOME/.cargo/env"
python3.12 -m venv /tmp/venv
. /tmp/venv/bin/activate
pip install -q --upgrade pip
pip install -q . pyinstaller
packaging/build.sh "$1"
