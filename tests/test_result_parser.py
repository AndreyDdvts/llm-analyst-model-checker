"""Unit: TLC trace → VerificationResult (разбор вывода TLC)."""

import unittest

from analyst_agent.result_parser import TLCOutcome, parse_tlc_output, parse_value

VIOLATION = """\
TLC2 Version 2.19
Starting... (2026-10-12 01:42:27)
Computing initial states...
Finished computing initial states: 1 distinct state generated at 2026-10-12 01:42:27.
Error: Invariant MC_Prop is violated.
Error: The behavior up to this point is:
State 1: <Initial predicate>
/\\ cancelled = FALSE
/\\ approved = FALSE
/\\ status = "Created"

State 2: <Approve line 6, col 12 to line 6, col 103 of module BehaviorModel>
/\\ cancelled = FALSE
/\\ approved = TRUE
/\\ status = "Approved"

State 3: <Cancel line 7, col 11 to line 7, col 118 of module BehaviorModel>
/\\ cancelled = TRUE
/\\ approved = TRUE
/\\ status = "Cancelled"

State 4: <Execute line 8, col 12 to line 8, col 90 of module BehaviorModel>
/\\ cancelled = TRUE
/\\ approved = TRUE
/\\ status = "Executed"

6 states generated, 6 distinct states found, 1 states left on queue.
The depth of the complete state graph search is 4.
Finished in 00s at (2026-10-12 01:42:27)
"""

DEADLOCK = """\
Error: Deadlock reached.
Error: The behavior up to this point is:
State 1: <Initial predicate>
/\\ status = "New"
/\\ attempts = 0

State 2: <Submit line 20, col 5 to line 24, col 30 of module BehaviorModel>
/\\ status = "Processing"
/\\ attempts = 1

1,234 states generated, 1,001 distinct states found, 2 states left on queue.
The depth of the complete state graph search is 3.
"""

OK = """\
Model checking completed. No error has been found.
  Estimates of the probability that TLC did not check all reachable states
  because two distinct states had the same fingerprint:
  calculated (optimistic):  val = 3.3E-19
162 states generated, 37 distinct states found, 0 states left on queue.
The depth of the complete state graph search is 8.
Finished in 00s at (2026-10-12 01:42:28)
"""

INITIAL_VIOLATION = """\
Computing initial states...
Error: Invariant MC_Prop is violated by the initial state:
x = -1

Finished in 00s at (2026-10-12 01:52:50)
"""

ERROR = """\
Error: TLC threw an unexpected exception.
java.lang.RuntimeException: Attempted to compare string "a" with integer 1
Finished in 00s
"""


class Values(unittest.TestCase):
    def test_parse_value(self):
        self.assertIs(parse_value("TRUE"), True)
        self.assertIs(parse_value("FALSE"), False)
        self.assertEqual(parse_value("-3"), -3)
        self.assertEqual(parse_value('"Created"'), "Created")
        with self.assertRaises(ValueError):
            parse_value("<<1, 2>>")


class Traces(unittest.TestCase):
    def test_invariant_violation(self):
        p = parse_tlc_output(VIOLATION, ["status", "approved", "cancelled"])
        self.assertEqual(p.outcome, TLCOutcome.INVARIANT_VIOLATED)
        self.assertEqual(p.invariant, "MC_Prop")
        steps = p.trace.steps
        self.assertEqual([s.number for s in steps], [0, 1, 2, 3])
        self.assertEqual(p.trace.transitions(), ["Approve", "Cancel", "Execute"])
        self.assertIsNone(steps[0].transition)
        self.assertEqual(steps[0].state, {"status": "Created", "approved": False, "cancelled": False})
        self.assertEqual(list(steps[0].state), ["status", "approved", "cancelled"])  # порядок модели
        self.assertEqual(steps[2].changes, {"status": ["Approved", "Cancelled"], "cancelled": [False, True]})
        self.assertEqual(steps[3].state, {"status": "Executed", "approved": True, "cancelled": True})
        self.assertEqual(p.statistics.states_generated, 6)
        self.assertEqual(p.statistics.distinct_states, 6)
        self.assertEqual(p.statistics.depth, 4)

    def test_deadlock(self):
        p = parse_tlc_output(DEADLOCK)
        self.assertEqual(p.outcome, TLCOutcome.DEADLOCK)
        self.assertEqual(p.trace.transitions(), ["Submit"])
        self.assertEqual(p.trace.steps[-1].state, {"status": "Processing", "attempts": 1})
        self.assertEqual(p.statistics.states_generated, 1234)
        self.assertEqual(p.statistics.distinct_states, 1001)

    def test_no_error(self):
        p = parse_tlc_output(OK)
        self.assertEqual(p.outcome, TLCOutcome.NO_ERROR)
        self.assertIsNone(p.trace)
        self.assertEqual((p.statistics.states_generated, p.statistics.distinct_states, p.statistics.depth),
                         (162, 37, 8))

    def test_violation_in_initial_state_single_var(self):
        p = parse_tlc_output(INITIAL_VIOLATION)
        self.assertEqual(p.outcome, TLCOutcome.INVARIANT_VIOLATED)
        # TLC печатает состояние без заголовка "State 1:", а одну переменную — без '/\'
        self.assertEqual(len(p.trace.steps), 1)
        self.assertEqual(p.trace.steps[0].state, {"x": -1})
        self.assertIsNone(p.trace.steps[0].transition)

    def test_engine_error(self):
        p = parse_tlc_output(ERROR)
        self.assertEqual(p.outcome, TLCOutcome.ERROR)
        self.assertIn("unexpected exception", p.error_message)

    def test_garbage(self):
        p = parse_tlc_output("")
        self.assertEqual(p.outcome, TLCOutcome.ERROR)


if __name__ == "__main__":
    unittest.main()
