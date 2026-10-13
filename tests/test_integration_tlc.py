"""Integration: BehavioralModel → TLC → expected result (реальный запуск TLC)."""

import tempfile
import unittest
from pathlib import Path

from analyst_agent.model_checker import CheckerConfig, ModelChecker, check_behavior
from analyst_agent.schema import ResultStatus

from .helpers import buggy, fixed, load, requires_tlc


def counter_model(max_value: int, n_vars: int = 2) -> dict:
    names = [f"c{i}" for i in range(n_vars)]
    return {
        "name": "Counters",
        "variables": [{"name": n, "type": "integer", "min": 0, "max": max_value} for n in names],
        "initial": {n: 0 for n in names},
        "transitions": [
            {"name": f"Inc_{n}", "guard": True, "effects": [{"set": [n, {"add": [{"var": n}, 1]}]}]}
            for n in names
        ],
        "properties": [
            {"id": "INV", "type": "invariant", "expression": {"ge": [{"var": "c0"}, 0]}},
            {"id": "BAD", "type": "forbidden_state", "expression": {"eq": [{"var": "c0"}, 2]}},
        ],
    }


@requires_tlc
class AcceptanceTests(unittest.TestCase):
    def test_1_violation_detected(self):
        """Acceptance test №1: PROPERTY_VIOLATED, Created → Approve → Cancel → Execute."""
        r = check_behavior(buggy(), ["P1"]).results[0]
        self.assertEqual(r.status, ResultStatus.PROPERTY_VIOLATED)
        self.assertEqual(r.engine, "TLC")
        self.assertEqual(r.counterexample.transitions(), ["Approve", "Cancel", "Execute"])
        states = [s.state for s in r.counterexample.steps]
        self.assertEqual(states[0], {"status": "Created", "approved": False, "cancelled": False})
        self.assertEqual(states[1], {"status": "Approved", "approved": True, "cancelled": False})
        self.assertEqual(states[2], {"status": "Cancelled", "approved": True, "cancelled": True})
        self.assertEqual(states[3], {"status": "Executed", "approved": True, "cancelled": True})
        self.assertEqual({q.id for q in r.related_requirements}, {"R1", "R2", "R3"})
        self.assertGreater(r.statistics.distinct_states, 0)

    def test_2_fixed_model_holds(self):
        """Acceptance test №2: Cancel сбрасывает approved → PROPERTY_HOLDS."""
        r = check_behavior(fixed(), ["P1"]).results[0]
        self.assertEqual(r.status, ResultStatus.PROPERTY_HOLDS)
        self.assertIsNone(r.counterexample)
        self.assertIsNotNone(r.statistics.states_generated)
        self.assertIn("не доказательство", r.message)

    def test_all_properties_buggy(self):
        rep = check_behavior(buggy())
        st = {r.property_id: r.status for r in rep.results}
        self.assertEqual(st, {
            "P1": ResultStatus.PROPERTY_VIOLATED,
            "P2": ResultStatus.PROPERTY_HOLDS,
            "P3": ResultStatus.PROPERTY_HOLDS,
            "P4": ResultStatus.PROPERTY_HOLDS,
        })
        witness = rep.by_id("P3").witness
        self.assertEqual(witness.transitions(), ["Approve", "Execute"])


@requires_tlc
class PropertyTypes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rep = check_behavior(load("payment_retries.json"))

    def test_unreachable_is_violation(self):
        r = self.rep.by_id("P1")
        self.assertEqual(r.status, ResultStatus.PROPERTY_VIOLATED)
        self.assertIsNone(r.counterexample)

    def test_deadlock_found(self):
        r = self.rep.by_id("P2")
        self.assertEqual(r.status, ResultStatus.PROPERTY_VIOLATED)
        last = r.counterexample.steps[-1].state
        self.assertEqual(last, {"status": "Failed", "attempts": 3})
        self.assertEqual(r.counterexample.transitions()[:2], ["Submit", "Fail"])

    def test_invariant_with_integers_holds(self):
        self.assertEqual(self.rep.by_id("P3").status, ResultStatus.PROPERTY_HOLDS)

    def test_deadlock_without_terminal(self):
        m = buggy()
        m["properties"] = [{"id": "D", "type": "deadlock_free"}]
        r = check_behavior(m).results[0]
        # Cancelled без approved — нет разрешённых переходов
        self.assertEqual(r.status, ResultStatus.PROPERTY_VIOLATED)
        self.assertEqual(r.counterexample.steps[-1].state["status"], "Cancelled")

    def test_violation_in_initial_state(self):
        m = buggy()
        m["properties"] = [{"id": "X", "type": "invariant", "expression": {"eq": [{"var": "status"}, "Approved"]}}]
        r = check_behavior(m).results[0]
        self.assertEqual(r.status, ResultStatus.PROPERTY_VIOLATED)
        self.assertEqual(len(r.counterexample.steps), 1)
        self.assertEqual(r.counterexample.steps[0].state["status"], "Created")


@requires_tlc
class EngineLimits(unittest.TestCase):
    def test_state_limit_is_not_holds(self):
        cfg = CheckerConfig(state_limit=50, run_sany=False)
        r = ModelChecker(cfg).check(counter_model(100), ["INV"]).results[0]
        self.assertEqual(r.status, ResultStatus.STATE_LIMIT_REACHED)
        self.assertNotEqual(r.status, ResultStatus.PROPERTY_HOLDS)

    def test_violation_found_before_limit(self):
        cfg = CheckerConfig(state_limit=50, run_sany=False)
        r = ModelChecker(cfg).check(counter_model(100), ["BAD"]).results[0]
        self.assertEqual(r.status, ResultStatus.PROPERTY_VIOLATED)
        self.assertEqual(r.counterexample.steps[-1].state["c0"], 2)

    def test_timeout_is_not_holds(self):
        cfg = CheckerConfig(timeout_sec=1.5, state_limit=None, run_sany=False)
        r = ModelChecker(cfg).check(counter_model(9999, 3), ["INV"]).results[0]
        self.assertEqual(r.status, ResultStatus.TIMEOUT)

    def test_engine_error_when_java_missing(self):
        cfg = CheckerConfig(java="/nonexistent/java")
        r = ModelChecker(cfg).check(buggy(), ["P1"]).results[0]
        self.assertEqual(r.status, ResultStatus.ENGINE_ERROR)

    def test_keep_dir_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            ModelChecker(CheckerConfig(keep_dir=d)).check(buggy(), ["P1"])
            files = {p.name for p in Path(d).rglob("*") if p.is_file()}
            self.assertTrue({"BehaviorModel.tla", "BehaviorModel.cfg", "tlc.log"} <= files)


if __name__ == "__main__":
    unittest.main()
