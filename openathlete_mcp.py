# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=2.2,<3",
#   "httpx>=0.27,<1",
#   "garmin-fit-sdk>=21.0",
#   "garth==0.4.38",
#   "tomli>=1.2; python_version < '3.11'",
#   "tzdata>=2025.2; platform_system == 'Windows'",
#   "tzlocal>=5.3,<6",
# ]
# ///
"""OpenAthlete 统一 stdio MCP 的源码入口。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from openathlete.mcp.server import main


if __name__ == "__main__":
    main()
