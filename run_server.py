"""Streamlit launcher for Windows: uses the selector event loop to avoid the
Proactor 'WinError 64' accept-crash bug, then runs the weather app."""
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from streamlit.web import cli as stcli

if __name__ == "__main__":
    sys.argv = [
        "streamlit", "run", "app.py",
        "--server.headless=true",
        "--server.port=8765",
        "--server.fileWatcherType=none",
        "--server.enableStaticServing=true",
        "--browser.gatherUsageStats=false",
    ]
    sys.exit(stcli.main())
