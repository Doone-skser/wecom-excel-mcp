#!/usr/bin/env bash
# Build the standalone executable into dist/ with the current Python environment (the package must be installed):
#   pip install . pyinstaller && packaging/build.sh wecom-excel-mcp-linux-x86_64
set -euo pipefail
name="${1:-wecom-excel-mcp}"
cd "$(dirname "$0")/.."
# --collect-data skips .py files, and site/wecom-mcp.py is data here (the client the server hands out)
site="$(python -c 'import os, wecom_excel_mcp; print(os.path.join(os.path.dirname(wecom_excel_mcp.__file__), "site"))')"
pyinstaller --noconfirm --clean --onefile --name "$name" --distpath dist --workpath build/pyinstaller --specpath build \
  --collect-data wecom_excel_mcp --collect-submodules wecom_excel_mcp \
  --collect-submodules uvicorn --collect-submodules mcp.server --collect-submodules mcp.shared --copy-metadata mcp --copy-metadata wecom-excel-mcp \
  --add-data "$site$(python -c 'import os; print(os.pathsep)')wecom_excel_mcp/site" \
  packaging/entry.py
