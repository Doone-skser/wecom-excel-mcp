"""Entry point of the standalone executable (PyInstaller)."""

import multiprocessing

from wecom_excel_mcp.server import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
