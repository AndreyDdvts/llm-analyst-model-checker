"""Unit: Expression → TLA expression (и проверка типов)."""

import unittest

from analyst_agent import expressions as ex

VT = {
    "status": ("enum", frozenset({"Created", "Approved", "Cancelled", "Executed"})),
    "approved": ("bool",),
    "attempts": ("int",),
}


def tla(raw):
    return ex.to_tla(ex.parse(raw))


class ToTLA(unittest.TestCase):
    def test_literals(self):
        self.assertEqual(tla("Created"), '"Created"')
        self.assertEqual(tla(True), "TRUE")
        self.assertEqual(tla(False), "FALSE")
        self.assertEqual(tla(3), "3")
        self.assertEqual(tla(-2), "(-2)")

    def test_var_and_comparisons(self):
        self.assertEqual(tla({"eq": [{"var": "status"}, "Created"]}), '(status = "Created")')
        self.assertEqual(tla({"neq": [{"var": "approved"}, True]}), "(approved # TRUE)")
        self.assertEqual(tla({"le": [{"var": "attempts"}, 3]}), "(attempts <= 3)")
        self.assertEqual(tla({"gt": [{"var": "attempts"}, 0]}), "(attempts > 0)")

    def test_in(self):
        self.assertEqual(
            tla({"in": [{"var": "status"}, ["Created", "Approved"]]}),
            '(status \\in {"Created", "Approved"})',
        )

    def test_logic(self):
        self.assertEqual(
            tla({"and": [{"eq": [{"var": "status"}, "Executed"]}, {"eq": [{"var": "approved"}, True]}]}),
            '((status = "Executed") /\\ (approved = TRUE))',
        )
        self.assertEqual(tla({"or": [True, False]}), "(TRUE \\/ FALSE)")
        self.assertEqual(tla({"and": [True]}), "TRUE")
        self.assertEqual(tla({"not": {"var": "approved"}}), "(~approved)")
        self.assertEqual(tla({"implies": [{"var": "approved"}, False]}), "(approved => FALSE)")

    def test_arith(self):
        self.assertEqual(tla({"add": [{"var": "attempts"}, 1]}), "(attempts + 1)")
        self.assertEqual(tla({"sub": [{"var": "attempts"}, 1]}), "(attempts - 1)")


class Parse(unittest.TestCase):
    def test_errors(self):
        bad = [
            {"foo": [1, 2]},                       # неизвестный оператор
            {"eq": [1]},                           # арность
            {"eq": [1, 2], "ne": [1, 2]},          # два ключа
            {"in": [{"var": "status"}, []]},       # пустое множество
            {"in": [{"var": "status"}, [{"var": "x"}]]},
            {"var": 5},
            1.5,
            None,
            [1, 2],
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(ex.ExprError):
                    ex.parse(raw)

    def test_variables_of(self):
        node = ex.parse({"and": [{"eq": [{"var": "status"}, "Executed"]}, {"var": "approved"}]})
        self.assertEqual(ex.variables_of(node), {"status", "approved"})


class TypeCheck(unittest.TestCase):
    def tc(self, raw):
        return ex.typecheck(ex.parse(raw), VT)

    def test_ok(self):
        self.assertEqual(self.tc({"eq": [{"var": "status"}, "Created"]}), ("bool",))
        self.assertEqual(self.tc({"add": [{"var": "attempts"}, 1]}), ("int",))
        self.assertEqual(self.tc({"in": [{"var": "status"}, ["Created"]]}), ("bool",))

    def test_errors(self):
        bad = [
            {"eq": [{"var": "status"}, "Draft"]},          # значение вне enum
            {"eq": [{"var": "status"}, True]},             # enum vs bool
            {"eq": [{"var": "nope"}, True]},               # нет переменной
            {"and": [{"var": "attempts"}]},                # int в логике
            {"lt": [{"var": "status"}, 1]},                # сравнение enum
            {"in": [{"var": "status"}, ["Created", "X"]]},
            {"in": [{"var": "approved"}, [True]]},
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(ex.ExprError):
                    self.tc(raw)


class Evaluate(unittest.TestCase):
    def test_eval(self):
        s = {"status": "Cancelled", "approved": True, "attempts": 2}
        self.assertTrue(ex.evaluate(ex.parse({"eq": [{"var": "approved"}, True]}), s))
        self.assertFalse(ex.evaluate(ex.parse({"in": [{"var": "status"}, ["Created", "Approved"]]}), s))
        self.assertEqual(ex.evaluate(ex.parse({"add": [{"var": "attempts"}, 1]}), s), 3)
        self.assertTrue(ex.evaluate(ex.parse({"implies": [False, False]}), s))


if __name__ == "__main__":
    unittest.main()
