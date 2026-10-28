#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "garmin-fit-sdk",
#   "tomli>=1.2; python_version < '3.11'",
#   "tzdata>=2025.2; platform_system == 'Windows'",
#   "tzlocal>=5.3,<6",
# ]
# ///
"""athlete.db v0 入口 shim：实现已迁至 src/openathlete/db/athlete_db.py。

`uv run ./athlete_db.py init|ingest|anchor|config|stats|rebuild-load` 行为不变；
包安装后等价命令是 `openathlete-mcp db ...`。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from openathlete.db.athlete_db import *  # noqa: F401,F403,E402

if __name__ == "__main__":
    sys.exit(main())
