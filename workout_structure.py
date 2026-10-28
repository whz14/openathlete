#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""兼容 shim：实现已迁至 src/openathlete/structure.py（包名 openathlete.structure）。

保留本文件是为了既有入口（fit_laps.py、dev/ 脚本、肌肉记忆里的 import workout_structure）
继续可用；新代码请直接 import openathlete.structure。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from openathlete.structure import *  # noqa: F401,F403,E402
