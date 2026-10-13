"""Unit: JSON validation → ожидаемый результат."""

import unittest

from analyst_agent import validator
from analyst_agent.model_checker import ModelChecker
from analyst_agent.schema import ResultStatus

from .helpers import buggy, clone, load


def codes(raw):
    _, issues = validator.validate(raw)
    return {i.code for i in issues if i.severity == "error"}


def warnings(raw):
    _, issues = validator.validate(raw)
    return {i.code for i in issues if i.severity == "warning"}


class ValidModels(unittest.TestCase):
    def test_examples_are_valid(self):
        for name in ("order_buggy.json", "order_fixed.json", "payment_retries.json"):
            with self.subTest(name=name):
                self.assertEqual(codes(load(name)), set())


class Variables(unittest.TestCase):
    def test_duplicate_variable(self):
        m = buggy()
        m["variables"].append({"name": "approved", "type": "boolean"})
        self.assertIn("duplicate_variable", codes(m))

    def test_integer_needs_finite_domain(self):
        m = buggy()
        m["variables"].append({"name": "attempts", "type": "integer", "min": 0})
        m["initial"]["attempts"] = 0
        self.assertIn("infinite_domain", codes(m))

    def test_enum_needs_values(self):
        m = buggy()
        m["variables"][0]["values"] = []
        self.assertIn("empty_domain", codes(m))

    def test_wrong_type_attributes(self):
        m = buggy()
        m["variables"][1]["values"] = ["x"]
        self.assertIn("type_mismatch", codes(m))

    def test_invalid_and_reserved_names(self):
        m = buggy()
        m["variables"][1]["name"] = "is approved"
        self.assertIn("invalid_name", codes(m))
        m = buggy()
        m["transitions"][0]["name"] = "Next"
        self.assertIn("reserved_name", codes(m))

    def test_unknown_type_is_schema_error(self):
        m = buggy()
        m["variables"][0]["type"] = "string"
        self.assertIn("schema", codes(m))


class Initial(unittest.TestCase):
    def test_missing_initial_value(self):
        m = buggy()
        del m["initial"]["cancelled"]
        self.assertIn("missing_initial", codes(m))

    def test_unknown_variable_with_trailing_space(self):
        # опечатка из постановки: "attempts " с пробелом
        m = buggy()
        m["variables"].append({"name": "attempts", "type": "integer", "min": 0, "max": 3})
        m["initial"]["attempts "] = 0
        c = codes(m)
        self.assertIn("unknown_variable", c)
        self.assertIn("missing_initial", c)

    def test_initial_out_of_domain(self):
        m = buggy()
        m["initial"]["status"] = "Draft"
        self.assertIn("initial_out_of_domain", codes(m))
        m = buggy()
        m["variables"].append({"name": "attempts", "type": "integer", "min": 0, "max": 3})
        m["initial"]["attempts"] = 5
        self.assertIn("initial_out_of_domain", codes(m))
        m = buggy()
        m["initial"]["approved"] = "no"
        self.assertIn("initial_out_of_domain", codes(m))


class Transitions(unittest.TestCase):
    def test_duplicate_transition(self):
        m = buggy()
        m["transitions"].append(clone(m["transitions"][0]))
        self.assertIn("duplicate_transition", codes(m))

    def test_guard_unknown_variable(self):
        m = buggy()
        m["transitions"][2]["guard"] = {"eq": [{"var": "aproved"}, True]}
        self.assertIn("invalid_expression", codes(m))

    def test_guard_must_be_boolean(self):
        m = buggy()
        m["transitions"][2]["guard"] = {"var": "status"}
        self.assertIn("type_mismatch", codes(m))

    def test_guard_enum_typo(self):
        m = buggy()
        m["transitions"][0]["guard"] = {"eq": [{"var": "status"}, "Craeted"]}
        self.assertIn("invalid_expression", codes(m))

    def test_effect_errors(self):
        m = buggy()
        m["transitions"][0]["effects"].append({"set": ["approvedd", True]})
        self.assertIn("unknown_variable", codes(m))
        m = buggy()
        m["transitions"][0]["effects"][1] = {"set": ["approved", "yes"]}
        self.assertIn("invalid_effect", codes(m))
        m = buggy()
        m["transitions"][0]["effects"][0] = {"set": ["status", "Done"]}
        self.assertIn("invalid_effect", codes(m))
        m = buggy()
        m["transitions"][0]["effects"].append({"set": ["status", "Created"]})
        self.assertIn("duplicate_assignment", codes(m))
        m = buggy()
        m["transitions"][0]["effects"].append({"assign": ["status", "Created"]})
        self.assertIn("invalid_effect", codes(m))

    def test_integer_effect_literal_out_of_domain(self):
        m = load("payment_retries.json")
        m["transitions"][0]["effects"][1] = {"set": ["attempts", 7]}
        self.assertIn("effect_out_of_domain", codes(m))

    def test_warnings(self):
        m = buggy()
        m["transitions"][0]["effects"] = []
        w = warnings(m)
        self.assertIn("no_effects", w)
        self.assertIn("constant_variable", w)  # approved больше никто не меняет
        m = buggy()
        m["transitions"][0]["source"] = ["R99"]
        self.assertIn("unknown_requirement", warnings(m))


class Properties(unittest.TestCase):
    def test_duplicate_property_id(self):
        m = buggy()
        m["properties"].append(clone(m["properties"][0]))
        self.assertIn("duplicate_property", codes(m))

    def test_expression_required(self):
        m = buggy()
        del m["properties"][0]["expression"]
        self.assertIn("invalid_property", codes(m))

    def test_deadlock_free_has_no_expression(self):
        m = buggy()
        m["properties"][3]["expression"] = True
        self.assertIn("invalid_property", codes(m))

    def test_terminal_only_for_deadlock(self):
        m = buggy()
        m["properties"][0]["terminal"] = True
        self.assertIn("invalid_property", codes(m))

    def test_property_typecheck(self):
        m = buggy()
        m["properties"][0]["expression"] = {"eq": [{"var": "status"}, "Canceled"]}
        self.assertIn("invalid_expression", codes(m))


class CheckerRejectsInvalid(unittest.TestCase):
    def test_model_invalid_status(self):
        m = buggy()
        m["initial"]["status"] = "Draft"
        report = ModelChecker().check(m)
        self.assertEqual(len(report.results), 1)
        self.assertEqual(report.results[0].status, ResultStatus.MODEL_INVALID)
        self.assertTrue(report.results[0].issues)

    def test_unknown_property_id(self):
        report = ModelChecker().check(buggy(), ["P42"])
        self.assertEqual(report.results[0].status, ResultStatus.MODEL_INVALID)

    def test_not_a_model(self):
        report = ModelChecker().check({"foo": 1})
        self.assertEqual(report.results[0].status, ResultStatus.MODEL_INVALID)


if __name__ == "__main__":
    unittest.main()
