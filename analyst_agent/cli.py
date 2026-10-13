"""Командная строка.

  python -m analyst_agent validate MODEL.json
  python -m analyst_agent compile  MODEL.json [--property P1]
  python -m analyst_agent check    MODEL.json [--property P1 ...] [--explain]
  python -m analyst_agent analyze  "текст требований" | --file req.txt [--interactive]
  python -m analyst_agent demo
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import validator
from .compiler import compile_model
from .model_checker import CheckerConfig, ModelChecker
from .schema import ResultStatus


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _checker(args) -> ModelChecker:
    return ModelChecker(CheckerConfig(
        timeout_sec=args.timeout,
        state_limit=args.state_limit or None,
        keep_dir=args.keep_dir,
        jar=args.jar,
    ))


def cmd_validate(args) -> int:
    model, issues = validator.validate(_load(args.model))
    print(json.dumps({
        "valid": model is not None and not validator.has_errors(issues),
        "issues": [i.model_dump() for i in issues],
    }, ensure_ascii=False, indent=2))
    return 0 if model is not None and not validator.has_errors(issues) else 2


def cmd_compile(args) -> int:
    model, issues = validator.validate(_load(args.model))
    if model is None or validator.has_errors(issues):
        print(json.dumps([i.model_dump() for i in issues], ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    prop = next((p for p in model.properties if p.id == args.property), None) if args.property else None
    spec = compile_model(model, prop)
    print(spec.tla)
    print("---- cfg ----")
    print(spec.cfg)
    return 0


def cmd_check(args) -> int:
    raw = _load(args.model)
    report = _checker(args).check(raw, args.property or None)
    if args.explain:
        from .explainer import render_explanation
        from .schema import BehavioralModel
        model = None
        if not any(r.status == ResultStatus.MODEL_INVALID for r in report.results):
            model = BehavioralModel.model_validate(raw)
        for r in report.results:
            print(render_explanation(model, r) if model else (r.message or ""), end="\n\n")
            if r.status == ResultStatus.MODEL_INVALID:
                for i in r.issues:
                    print(f"- [{i.code}] {i.path}: {i.message}")
    else:
        out = [r.model_dump(mode="json", exclude_none=True) for r in report.results]
        print(json.dumps(out[0] if len(out) == 1 else out, ensure_ascii=False, indent=2))
    statuses = {r.status for r in report.results}
    if ResultStatus.MODEL_INVALID in statuses:
        return 2
    if statuses & {ResultStatus.ENGINE_ERROR, ResultStatus.TIMEOUT, ResultStatus.STATE_LIMIT_REACHED}:
        return 3
    return 1 if ResultStatus.PROPERTY_VIOLATED in statuses else 0


def cmd_analyze(args) -> int:
    from .agent import AnalystAgent
    from .llm import AnthropicClient, LLMError

    text = Path(args.file).read_text(encoding="utf-8") if args.file else args.text
    if not text:
        print("Нужен текст требований или --file", file=sys.stderr)
        return 2
    try:
        llm = AnthropicClient(model=args.llm_model) if args.llm_model else AnthropicClient()
    except LLMError as e:
        print(f"{e}. Для офлайн-демонстрации используйте: python -m analyst_agent demo", file=sys.stderr)
        return 2
    agent = AnalystAgent(llm, checker=_checker(args))
    while True:
        turn = agent.ask(text)
        for e in turn.events:
            res = e.output.get("results") if isinstance(e.output, dict) else None
            summary = ", ".join(f"{r.get('property_id')}: {r['status']}" for r in res) if res else json.dumps(e.output, ensure_ascii=False)[:200]
            print(f"[{e.name}{' — заблокировано' if e.blocked else ''}] {summary}", file=sys.stderr)
        print(turn.text)
        if args.save_model and turn.model:
            Path(args.save_model).write_text(json.dumps(turn.model, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"(модель сохранена в {args.save_model})", file=sys.stderr)
        if not args.interactive:
            return 0
        try:
            text = input("\n> ").strip()
        except EOFError:
            return 0
        if not text or text.lower() in {"exit", "quit", "выход"}:
            return 0


def cmd_demo(args) -> int:
    from .demo import run_demo
    run_demo(checker=_checker(args))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="analyst_agent", description="Агент-аналитик + Model Checker (TLA+/TLC)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def engine_opts(p):
        p.add_argument("--timeout", type=float, default=60.0, help="таймаут TLC, с")
        p.add_argument("--state-limit", type=int, default=1_000_000, help="лимит различных состояний (0 — без лимита)")
        p.add_argument("--keep-dir", help="сохранить сгенерированные .tla/.cfg и лог TLC в каталог")
        p.add_argument("--jar", help="путь к tla2tools.jar")

    p = sub.add_parser("validate", help="валидация модели")
    p.add_argument("model")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("compile", help="показать сгенерированный TLA+")
    p.add_argument("model")
    p.add_argument("--property")
    p.set_defaults(fn=cmd_compile)

    p = sub.add_parser("check", help="проверить модель в TLC")
    p.add_argument("model")
    p.add_argument("--property", action="append")
    p.add_argument("--explain", action="store_true", help="текстовое объяснение вместо JSON")
    engine_opts(p)
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("analyze", help="агент: требования на естественном языке → проверка → объяснение")
    p.add_argument("text", nargs="?")
    p.add_argument("--file")
    p.add_argument("--interactive", action="store_true")
    p.add_argument("--save-model")
    p.add_argument("--llm-model")
    engine_opts(p)
    p.set_defaults(fn=cmd_analyze)

    p = sub.add_parser("demo", help="офлайн-демо основного сценария (без LLM API)")
    engine_opts(p)
    p.set_defaults(fn=cmd_demo)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
