"""Этап 3 Model Checker Tool: запуск SANY и TLC отдельным subprocess.

Процесс запускается в собственной группе процессов, поэтому при timeout
JVM гарантированно принудительно завершается (SIGKILL всей группе).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class EngineNotFound(RuntimeError):
    pass


@dataclass
class ProcessOutput:
    returncode: Optional[int]
    output: str
    timed_out: bool
    duration_sec: float


def find_tla_jar(explicit: Optional[str] = None) -> Path:
    candidates = [
        explicit,
        os.environ.get("TLA2TOOLS_JAR"),
        str(PACKAGE_ROOT / "tools" / "tla2tools.jar"),
        str(Path.home() / ".tla" / "tla2tools.jar"),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    raise EngineNotFound(
        "tla2tools.jar не найден. Укажите TLA2TOOLS_JAR или выполните scripts/download_tla.sh"
    )


def find_java(explicit: Optional[str] = None) -> str:
    java = explicit or os.environ.get("JAVA_BIN") or shutil.which("java")
    if not java:
        raise EngineNotFound("Java 11+ не найдена (нужна для TLA+ Tools)")
    return java


def _run(cmd: list[str], cwd: Path, timeout: float) -> ProcessOutput:
    start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        return ProcessOutput(proc.returncode, out or "", False, time.monotonic() - start)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        out, _ = proc.communicate()
        return ProcessOutput(None, out or "", True, time.monotonic() - start)


class TLCRunner:
    def __init__(
        self,
        jar: Optional[str] = None,
        java: Optional[str] = None,
        workers: int = 1,
        heap_mb: int = 1024,
    ):
        self.jar = find_tla_jar(jar)
        self.java = find_java(java)
        self.workers = workers
        self.heap_mb = heap_mb

    def _java(self) -> list[str]:
        return [self.java, f"-Xmx{self.heap_mb}m", "-XX:+UseParallelGC", "-cp", str(self.jar)]

    def run_sany(self, workdir: Path, module: str, timeout: float = 60) -> ProcessOutput:
        """Синтаксический и семантический разбор сгенерированного TLA+."""
        return _run(self._java() + ["tla2sany.SANY", f"{module}.tla"], workdir, timeout)

    def run_tlc(
        self,
        workdir: Path,
        module: str,
        check_deadlock: bool,
        timeout: float,
    ) -> ProcessOutput:
        cmd = self._java() + [
            "tlc2.TLC",
            "-config", f"{module}.cfg",
            "-workers", str(self.workers),
            "-metadir", str(workdir / "states"),
            "-cleanup",
            "-noGenerateSpecTE",
        ]
        if not check_deadlock:
            cmd.append("-deadlock")  # флаг ОТКЛЮЧАЕТ проверку deadlock
        cmd.append(f"{module}.tla")
        return _run(cmd, workdir, timeout)
