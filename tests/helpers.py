import copy
import json
import shutil
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def buggy() -> dict:
    return load("order_buggy.json")


def fixed() -> dict:
    return load("order_fixed.json")


def clone(d: dict) -> dict:
    return copy.deepcopy(d)


def tlc_available() -> bool:
    try:
        from analyst_agent.tlc_runner import find_java, find_tla_jar
        find_tla_jar()
        find_java()
        return shutil.which("java") is not None
    except Exception:
        return False


requires_tlc = unittest.skipUnless(tlc_available(), "нужны Java и tla2tools.jar")
