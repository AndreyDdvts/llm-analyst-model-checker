"""Model Checker Tool — детерминированный компонент, LLM не используется.

    Validator → TLA+ Compiler → (SANY) → TLC Runner → Result Parser

Точка входа: check_behavior(model, property_ids=None, ...)
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import BaseModel

from . import validator
from .compiler import compile_model
from .result_parser import TLCOutcome, parse_tlc_output
from .schema import (
    BehavioralModel,
    Property,
    PropertyType,
    Requirement,
    ResultStatus,
    Trace,
    ValidationIssue,
    VerificationResult,
)
from .tlc_runner import EngineNotFound, TLCRunner

HOLDS_DISCLAIMER = (
    "Свойство не нарушено в исследованной конечной модели при заданных "
    "ограничениях. Это не доказательство корректности production-системы."
)


class CheckReport(BaseModel):
    model_name: Optional[str] = None
    issues: list[ValidationIssue] = []
    results: list[VerificationResult] = []

    def by_id(self, pid: str) -> Optional[VerificationResult]:
        return next((r for r in self.results if r.property_id == pid), None)


@dataclass
class CheckerConfig:
    timeout_sec: float = 60.0
    state_limit: Optional[int] = 1_000_000
    run_sany: bool = True
    workers: int = 1
    heap_mb: int = 1024
    jar: Optional[str] = None
    java: Optional[str] = None
    keep_dir: Optional[str] = None  # сохранить сгенерированные .tla/.cfg/.log


def _related_requirements(model: BehavioralModel, prop: Property, trace: Optional[Trace]) -> list[Requirement]:
    ids: list[str] = list(prop.source)
    if trace:
        tr_by_name = {t.name: t for t in model.transitions}
        for name in trace.transitions():
            t = tr_by_name.get(name)
            if t:
                ids.extend(t.source)
    reqs = {r.id: r for r in model.requirements}
    seen, out = set(), []
    for i in ids:
        if i in reqs and i not in seen:
            seen.add(i)
            out.append(reqs[i])
    return out


class ModelChecker:
    def __init__(self, config: Optional[CheckerConfig] = None, runner: Optional[TLCRunner] = None):
        self.config = config or CheckerConfig()
        self._runner = runner

    @property
    def runner(self) -> TLCRunner:
        if self._runner is None:
            c = self.config
            self._runner = TLCRunner(jar=c.jar, java=c.java, workers=c.workers, heap_mb=c.heap_mb)
        return self._runner

    # ------------------------------------------------------------------
    def check(
        self,
        raw_model: Union[dict, BehavioralModel],
        property_ids: Optional[list[str]] = None,
    ) -> CheckReport:
        model, issues = validator.validate(raw_model)
        name = model.name if model else (raw_model.get("name") if isinstance(raw_model, dict) else None)

        if model is not None and property_ids:
            known = {p.id for p in model.properties}
            for pid in property_ids:
                if pid not in known:
                    issues.append(ValidationIssue(code="unknown_property", path="property_ids",
                                                  message=f"Свойство '{pid}' не найдено в модели"))
        if model is not None and not model.properties:
            issues.append(ValidationIssue(code="no_properties", path="properties",
                                          message="В модели нет ни одного свойства для проверки"))

        if model is None or validator.has_errors(issues):
            return CheckReport(model_name=name, issues=issues, results=[VerificationResult(
                status=ResultStatus.MODEL_INVALID,
                issues=[i for i in issues if i.severity == "error"],
                message="Модель не прошла валидацию; проверка не выполнялась.",
            )])

        props = [p for p in model.properties if not property_ids or p.id in property_ids]
        results = [self.check_property(model, p) for p in props]
        return CheckReport(model_name=model.name, issues=issues, results=results)

    # ------------------------------------------------------------------
    def check_property(self, model: BehavioralModel, prop: Property) -> VerificationResult:
        cfg = self.config
        base = VerificationResult(status=ResultStatus.ENGINE_ERROR, property_id=prop.id, property_type=prop.type)
        try:
            runner = self.runner
        except EngineNotFound as e:
            base.message = str(e)
            return base

        spec = compile_model(model, prop, state_limit=cfg.state_limit)

        try:
            if cfg.keep_dir:
                workdir = Path(cfg.keep_dir) / f"{_safe(model.name)}__{_safe(prop.id)}"
                workdir.mkdir(parents=True, exist_ok=True)
                return self._run_in(workdir, model, prop, spec, base)
            with tempfile.TemporaryDirectory(prefix="mc_") as d:
                return self._run_in(Path(d), model, prop, spec, base)
        except Exception as e:  # сбой запуска процесса, ФС и т.п.
            base.status = ResultStatus.ENGINE_ERROR
            base.message = f"Не удалось выполнить TLC: {type(e).__name__}: {e}"
            return base

    def _run_in(self, workdir: Path, model, prop, spec, base: VerificationResult) -> VerificationResult:
        cfg = self.config
        (workdir / f"{spec.module_name}.tla").write_text(spec.tla, encoding="utf-8")
        (workdir / f"{spec.module_name}.cfg").write_text(spec.cfg, encoding="utf-8")

        if cfg.run_sany:
            sany = self.runner.run_sany(workdir, spec.module_name, timeout=min(cfg.timeout_sec, 120))
            if sany.timed_out:
                base.status = ResultStatus.TIMEOUT
                base.message = f"Разбор TLA+ (SANY) не завершился за {cfg.timeout_sec} с; проверка не выполнена."
                return base
            if sany.returncode != 0 or "*** Errors" in sany.output:
                base.message = "SANY отклонил сгенерированный TLA+ (ошибка компилятора):\n" + sany.output[-2000:]
                return base

        run = self.runner.run_tlc(workdir, spec.module_name, spec.check_deadlock, cfg.timeout_sec)
        if cfg.keep_dir:
            (workdir / "tlc.log").write_text(run.output, encoding="utf-8")
        base.duration_sec = round(run.duration_sec, 3)

        if run.timed_out:
            parsed = parse_tlc_output(run.output)
            base.status = ResultStatus.TIMEOUT
            base.statistics = parsed.statistics
            base.message = (f"TLC не завершился за {cfg.timeout_sec} с и был принудительно остановлен. "
                            "Результат НЕ означает, что свойство выполняется.")
            return base

        var_order = [v.name for v in model.variables]
        parsed = parse_tlc_output(run.output, var_order)
        base.statistics = parsed.statistics
        limit_hit = (
            cfg.state_limit is not None
            and parsed.statistics.distinct_states is not None
            and parsed.statistics.distinct_states > cfg.state_limit
        )

        if parsed.outcome == TLCOutcome.ERROR:
            base.message = "TLC завершился с ошибкой:\n" + (parsed.error_message or "")
            return base
        if parsed.outcome == TLCOutcome.INVARIANT_VIOLATED and parsed.invariant != "MC_Prop":
            base.message = f"Нарушен служебный инвариант {parsed.invariant} (ошибка компилятора)."
            return base

        found = parsed.outcome in (TLCOutcome.INVARIANT_VIOLATED, TLCOutcome.DEADLOCK)
        if prop.type == PropertyType.reachable:
            if found:
                base.status = ResultStatus.PROPERTY_HOLDS
                base.witness = parsed.trace
                base.message = "Целевое состояние достижимо; приведён путь-свидетель."
            elif limit_hit:
                return self._limit(base)
            else:
                base.status = ResultStatus.PROPERTY_VIOLATED
                base.message = ("Целевое состояние недостижимо: ни одна допустимая последовательность "
                                "переходов в конечной модели к нему не приводит.")
        else:
            if found:
                base.status = ResultStatus.PROPERTY_VIOLATED
                base.counterexample = parsed.trace
                base.message = ("Найдено достижимое состояние без разрешённых переходов (deadlock)."
                                if prop.type == PropertyType.deadlock_free
                                else "Найден путь, нарушающий свойство.")
            elif limit_hit:
                return self._limit(base)
            else:
                base.status = ResultStatus.PROPERTY_HOLDS
                base.message = HOLDS_DISCLAIMER
        base.related_requirements = _related_requirements(
            model, prop, base.counterexample or base.witness)
        return base

    def _limit(self, base: VerificationResult) -> VerificationResult:
        base.status = ResultStatus.STATE_LIMIT_REACHED
        base.message = (f"Достигнут лимит в {self.config.state_limit} различных состояний; "
                        "пространство исследовано не полностью. Результат НЕ означает, что свойство выполняется.")
        return base


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:60] or "x"


def check_behavior(
    model: Union[dict, BehavioralModel],
    property_ids: Optional[list[str]] = None,
    config: Optional[CheckerConfig] = None,
) -> CheckReport:
    """Функция-инструмент, которую вызывает агент."""
    return ModelChecker(config).check(model, property_ids)
