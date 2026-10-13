"""Этап 5–6 Model Checker Tool: разбор вывода TLC.

Преобразует текстовый вывод TLC в структуру: исход проверки, трасса
(с восстановленными именами переходов) и статистика.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .schema import Statistics, Trace, TraceStep


class TLCOutcome(str, Enum):
    NO_ERROR = "NO_ERROR"
    INVARIANT_VIOLATED = "INVARIANT_VIOLATED"
    DEADLOCK = "DEADLOCK"
    ERROR = "ERROR"  # ошибка движка / вычисления / разбора


@dataclass
class ParsedTLC:
    outcome: TLCOutcome
    invariant: Optional[str] = None
    trace: Optional[Trace] = None
    statistics: Statistics = field(default_factory=Statistics)
    error_message: Optional[str] = None


RE_INVARIANT = re.compile(r"^Error: Invariant (\S+) is violated")
RE_DEADLOCK = re.compile(r"^Error: Deadlock reached\.")
RE_STATE = re.compile(r"^State (\d+): <(.*)>\s*$")
RE_ASSIGN = re.compile(r"^(?:/\\\s+)?([A-Za-z][A-Za-z0-9_]*) = (.+?)\s*$")
RE_STATS = re.compile(
    r"^([\d,]+) states generated, ([\d,]+) distinct states found, ([\d,]+) states left on queue"
)
RE_DEPTH = re.compile(r"^The depth of the complete state graph search is (\d+)")
NO_ERROR = "Model checking completed. No error has been found."


def parse_value(text: str) -> Any:
    t = text.strip()
    if t == "TRUE":
        return True
    if t == "FALSE":
        return False
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return t[1:-1]
    raise ValueError(f"Неподдерживаемое значение в трассе TLC: {text!r}")


def _action_name(label: str) -> Optional[str]:
    if label.startswith("Initial predicate"):
        return None
    m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", label)
    return m.group(1) if m else label


def parse_trace(lines: list[str], var_order: Optional[list[str]] = None) -> Trace:
    steps: list[TraceStep] = []
    cur: Optional[dict] = None
    for line in lines:
        m = RE_STATE.match(line)
        if m:
            if cur is not None:
                steps.append(cur)
            cur = {"label": m.group(2), "state": {}}
            continue
        if cur is None:
            continue
        a = RE_ASSIGN.match(line.strip())
        if a:
            cur["state"][a.group(1)] = parse_value(a.group(2))
        elif not line.strip():
            steps.append(cur)
            cur = None
    if cur is not None:
        steps.append(cur)

    result: list[TraceStep] = []
    prev: Optional[dict] = None
    for i, s in enumerate(steps):
        state = s["state"]
        if var_order:
            state = {k: state[k] for k in var_order if k in state}
        changes = {}
        if prev is not None:
            changes = {k: [prev.get(k), v] for k, v in state.items() if prev.get(k) != v}
        result.append(TraceStep(
            number=i,  # нумерация с 0, как в постановке (TLC нумерует с 1)
            transition=_action_name(s["label"]) if i > 0 else None,
            state=state,
            changes=changes,
        ))
        prev = state
    return Trace(steps=result)


def parse_statistics(text: str) -> Statistics:
    st = Statistics()
    for line in text.splitlines():
        m = RE_STATS.match(line.strip())
        if m:
            st.states_generated = int(m.group(1).replace(",", ""))
            st.distinct_states = int(m.group(2).replace(",", ""))
        d = RE_DEPTH.match(line.strip())
        if d:
            st.depth = int(d.group(1))
    return st


def parse_tlc_output(text: str, var_order: Optional[list[str]] = None) -> ParsedTLC:
    lines = text.splitlines()
    stats = parse_statistics(text)

    for idx, line in enumerate(lines):
        m = RE_INVARIANT.match(line)
        if m or RE_DEADLOCK.match(line):
            trace_lines = lines[idx + 1:]
            if "by the initial state" in line:
                # TLC печатает начальное состояние без заголовка "State 1:"
                trace_lines = ["State 1: <Initial predicate>"] + trace_lines
            # трасса заканчивается на строке статистики
            for j, tl in enumerate(trace_lines):
                if RE_STATS.match(tl.strip()) or tl.startswith("Finished in"):
                    trace_lines = trace_lines[:j]
                    break
            return ParsedTLC(
                outcome=TLCOutcome.INVARIANT_VIOLATED if m else TLCOutcome.DEADLOCK,
                invariant=m.group(1) if m else None,
                trace=parse_trace(trace_lines, var_order),
                statistics=stats,
            )

    if any(l.strip() == NO_ERROR for l in lines):
        return ParsedTLC(outcome=TLCOutcome.NO_ERROR, statistics=stats)

    errs = [l for l in lines if l.startswith("Error") or "***" in l or "Exception" in l]
    msg = "\n".join(errs[:10]) if errs else "\n".join(lines[-15:])
    return ParsedTLC(outcome=TLCOutcome.ERROR, statistics=stats, error_message=msg.strip())
