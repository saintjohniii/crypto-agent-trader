from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg: dict[str, Any] = yaml.safe_load(f)

    if loop := os.getenv("LOOP_SECONDS"):
        cfg["trading"]["loop_seconds"] = int(loop)

    return cfg
