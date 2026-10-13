"""Agent tests: natural language → BehavioralModel → Model Checker → объяснение.

Офлайн-тесты используют ScriptedLLM (роль LLM — записанные ответы), при
этом Model Checker (TLC) запускается по-настоящему. Живой тест с Claude
запускается при ANTHROPIC_API_KEY и RUN_LIVE_LLM=1.
"""

import json
import os
import unittest

from analyst_agent.agent import AnalystAgent, logic_fingerprint
from analyst_agent.demo import FOLLOW_UP_TEXT, REQUIREMENTS_TEXT, compose_answer, scripted_llm
from analyst_agent.llm import LLMResponse, ScriptedLLM, last_tool_result, tool_call
from analyst_agent.schema import ResultStatus

from .helpers import buggy, fixed, requires_tlc


@requires_tlc
class MainScenario(unittest.TestCase):
    """Основной сценарий постановки, два хода диалога."""

    @classmethod
    def setUpClass(cls):
        cls.agent = AnalystAgent(scripted_llm())
        cls.turn1 = cls.agent.ask(REQUIREMENTS_TEXT)
        cls.turn2 = cls.agent.ask(FOLLOW_UP_TEXT)

    def test_turn1_counterexample(self):
        r = self.turn1.report.by_id("P1")
        self.assertEqual(r.status, ResultStatus.PROPERTY_VIOLATED)
        self.assertEqual(r.counterexample.transitions(), ["Approve", "Cancel", "Execute"])

    def test_turn1_business_explanation(self):
        text = self.turn1.text
        # бизнес-сценарий, а не только «invariant violated»
        for phrase in ("Заявка согласована", "Пользователь отменяет заявку", "Система исполняет заявку",
                       "approved=true", "Cancel", "не изменяет approved"):
            self.assertIn(phrase, text)
        self.assertIn("Уточняющие вопросы", text)
        self.assertIn("R2", text)  # связь с исходным требованием

    def test_turn1_model_not_changed(self):
        self.assertEqual(len(self.turn1.checks), 1)
        self.assertEqual(logic_fingerprint(self.turn1.model), logic_fingerprint(buggy()))

    def test_tool_result_contains_explanation_aids(self):
        out = self.turn1.checks[0].output
        p1 = next(r for r in out["results"] if r["property_id"] == "P1")
        aids = p1["explanation_aids"]
        self.assertTrue(any("Cancel" in h and "approved" in h for h in aids["diagnostic_hints"]))
        self.assertTrue(aids["questions"])

    def test_turn2_fix_confirmed_by_user_holds(self):
        self.assertTrue(all(r.status == ResultStatus.PROPERTY_HOLDS for r in self.turn2.report.results))
        self.assertIn("было: НАРУШЕНО", self.turn2.text)


@requires_tlc
class NoAutoFixPolicy(unittest.TestCase):
    def test_second_check_with_changed_model_is_blocked(self):
        llm = ScriptedLLM([
            tool_call("check_behavior", model=buggy()),
            tool_call("check_behavior", model=fixed()),  # попытка «молча» исправить
            lambda msgs: LLMResponse(text=f"blocked={last_tool_result(msgs).get('error')}"),
        ])
        turn = AnalystAgent(llm).ask(REQUIREMENTS_TEXT)
        self.assertEqual(len(turn.checks), 1)
        self.assertTrue(turn.events[1].blocked)
        self.assertEqual(turn.text, "blocked=POLICY_NO_AUTOFIX")
        self.assertEqual(turn.report.by_id("P1").status, ResultStatus.PROPERTY_VIOLATED)

    def test_same_model_other_property_allowed(self):
        llm = ScriptedLLM([
            tool_call("check_behavior", model=buggy(), property_ids=["P1"]),
            tool_call("check_behavior", model=buggy(), property_ids=["P3"]),
            LLMResponse(text="ok"),
        ])
        turn = AnalystAgent(llm).ask("...")
        self.assertEqual(len(turn.checks), 2)

    def test_fixing_invalid_model_is_allowed(self):
        broken = buggy()
        broken["initial"]["status"] = "Draft"
        llm = ScriptedLLM([
            tool_call("check_behavior", model=broken),
            tool_call("check_behavior", model=buggy(), property_ids=["P1"]),
            LLMResponse(text="done"),
        ])
        turn = AnalystAgent(llm).ask("...")
        self.assertEqual(turn.checks[0].output["results"][0]["status"], "MODEL_INVALID")
        self.assertEqual(turn.checks[1].output["results"][0]["status"], "PROPERTY_VIOLATED")


class AgentMechanics(unittest.TestCase):
    def test_validate_tool_and_unknown_tool(self):
        llm = ScriptedLLM([
            tool_call("validate_model", model={"variables": []}),
            tool_call("rm_rf"),
            LLMResponse(text="fin"),
        ])
        turn = AnalystAgent(llm).ask("x")
        self.assertFalse(turn.events[0].output["valid"])
        self.assertIn("error", turn.events[1].output)
        self.assertEqual(turn.text, "fin")

    def test_step_limit(self):
        llm = ScriptedLLM([tool_call("validate_model", model={}) for _ in range(5)])
        turn = AnalystAgent(llm, max_steps=3).ask("x")
        self.assertIn("лимит шагов", turn.text)

    def test_history_has_tool_results(self):
        llm = ScriptedLLM([tool_call("validate_model", model=buggy()), LLMResponse(text="ok")])
        agent = AnalystAgent(llm)
        agent.ask("x")
        roles = [m["role"] for m in agent.messages]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])
        self.assertEqual(agent.messages[2]["content"][0]["type"], "tool_result")

    def test_compose_answer_on_holds(self):
        res = {"results": [{"property_id": "P1", "status": "PROPERTY_HOLDS"}]}
        text = compose_answer(res, fixed())
        self.assertIn("выполняется", text)


@unittest.skipUnless(os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("RUN_LIVE_LLM") == "1",
                     "живой тест LLM: задайте ANTHROPIC_API_KEY и RUN_LIVE_LLM=1")
@requires_tlc
class LiveLLM(unittest.TestCase):
    def test_natural_language_to_counterexample(self):
        from analyst_agent.llm import AnthropicClient
        agent = AnalystAgent(AnthropicClient())
        turn = agent.ask(
            "Заявка после согласования может быть исполнена. Пользователь может отменить заявку до "
            "исполнения. Согласование фиксируется признаком approved, отмена — признаком cancelled; "
            "исполнение разрешено, если approved = true. Проверь, что отменённая заявка не может быть исполнена."
        )
        self.assertTrue(turn.checks, "агент должен вызвать check_behavior")
        violated = [r for r in turn.report.results if r.status == ResultStatus.PROPERTY_VIOLATED and r.counterexample]
        self.assertTrue(violated, json.dumps(turn.checks[-1].output, ensure_ascii=False)[:2000])
        path = violated[0].counterexample.transitions()
        self.assertTrue(any("ancel" in t for t in path) and any("xecut" in t for t in path), path)
        self.assertTrue(turn.text.strip())


if __name__ == "__main__":
    unittest.main()
