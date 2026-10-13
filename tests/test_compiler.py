"""Unit: BehavioralModel → TLA generation."""

import unittest

from analyst_agent.compiler import compile_model
from analyst_agent.schema import BehavioralModel

from .helpers import buggy, load


def model(raw=None):
    return BehavioralModel.model_validate(raw or buggy())


def prop(m, pid):
    return next(p for p in m.properties if p.id == pid)


class Structure(unittest.TestCase):
    def setUp(self):
        self.m = model()
        self.spec = compile_model(self.m, prop(self.m, "P1"))
        self.tla = self.spec.tla

    def test_header_and_variables(self):
        self.assertTrue(self.tla.startswith("---- MODULE BehaviorModel ----"))
        self.assertIn("EXTENDS Integers, TLC", self.tla)
        self.assertIn("VARIABLES status, approved, cancelled", self.tla)
        self.assertIn("vars == <<status, approved, cancelled>>", self.tla)
        self.assertTrue(self.tla.rstrip().endswith("===="))

    def test_init(self):
        self.assertIn(
            'Init ==\n    /\\ status = "Created"\n    /\\ approved = FALSE\n    /\\ cancelled = FALSE',
            self.tla,
        )

    def test_transition_approve(self):
        expected = (
            "Approve ==\n"
            '    /\\ (status = "Created")\n'
            '    /\\ status\' = "Approved"\n'
            "    /\\ approved' = TRUE\n"
            "    /\\ UNCHANGED <<cancelled>>"
        )
        self.assertIn(expected, self.tla)

    def test_transition_execute_does_not_check_status(self):
        self.assertIn("Execute ==\n    /\\ (approved = TRUE)\n    /\\ status' = \"Executed\"\n"
                      "    /\\ UNCHANGED <<approved, cancelled>>", self.tla)

    def test_next_and_spec(self):
        self.assertIn("Next ==\n    \\/ Approve\n    \\/ Cancel\n    \\/ Execute", self.tla)
        self.assertIn("Spec == Init /\\ [][Next]_vars", self.tla)

    def test_source_reference_comment(self):
        self.assertIn("\\* [R2] Пользователь отменяет заявку", self.tla)

    def test_cfg(self):
        self.assertIn("SPECIFICATION Spec", self.spec.cfg)
        self.assertIn("INVARIANT MC_Prop", self.spec.cfg)
        self.assertIn("INVARIANT MC_TypeOK", self.spec.cfg)
        self.assertFalse(self.spec.check_deadlock)


class Properties(unittest.TestCase):
    def setUp(self):
        self.m = model()

    def test_forbidden_state_is_negated(self):
        tla = compile_model(self.m, prop(self.m, "P1")).tla
        self.assertIn('MC_Prop == ~((status = "Executed") /\\ (cancelled = TRUE))', tla)

    def test_invariant_as_is(self):
        tla = compile_model(self.m, prop(self.m, "P2")).tla
        self.assertIn('MC_Prop == ((status = "Executed") => (approved = TRUE))', tla)

    def test_reachable_is_negated_goal(self):
        tla = compile_model(self.m, prop(self.m, "P3")).tla
        self.assertIn('MC_Prop == ~(status = "Executed")', tla)

    def test_deadlock_free_with_terminal(self):
        spec = compile_model(self.m, prop(self.m, "P4"))
        self.assertTrue(spec.check_deadlock)
        self.assertNotIn("MC_Prop", spec.cfg)
        self.assertIn('MC_Terminal == (status \\in {"Cancelled", "Executed"}) /\\ UNCHANGED vars', spec.tla)
        self.assertIn("\\/ MC_Terminal", spec.tla)

    def test_state_limit_constraint(self):
        spec = compile_model(self.m, prop(self.m, "P1"), state_limit=500)
        self.assertIn('MC_StateLimit == TLCGet("distinct") <= 500', spec.tla)
        self.assertIn("CONSTRAINT MC_StateLimit", spec.cfg)


class Integers(unittest.TestCase):
    def test_domain_and_implicit_bound_guard(self):
        m = model(load("payment_retries.json"))
        tla = compile_model(m, m.properties[0]).tla
        self.assertIn("MC_Domain_attempts == 0..3", tla)
        self.assertIn("attempts' = (attempts + 1)", tla)
        self.assertIn("attempts' \\in MC_Domain_attempts", tla)


if __name__ == "__main__":
    unittest.main()
