"""Tests for EXPLORE → ACT promotion rules."""
import unittest

from apps.cli.runtime.explore_promotion import (
    REASON_DISCOVERY_CAP_REACHED,
    REASON_EXPLICIT_MODEL_MODIFY_INTENT,
    REASON_EXPLORATION_BUDGET_EXHAUSTED,
    REASON_EXPLORE_TEXT_ONLY_STREAK,
    REASON_FOCUSED_WRITE_CONTEXT_READY,
    REASON_REPEATED_TARGET_READ,
    REASON_READ_ONLY_STAGNATION_THRESHOLD,
    REASON_REJECTED_WRITE_ATTEMPT,
    REASON_REPO_CONTEXT_SUFFICIENT,
    REASON_SIMPLE_WRITE_MIN_CONTEXT,
    ExploreTurnResult,
    ExplorationMetrics,
    should_promote_explore_to_act,
    text_signals_modify_intent,
)


class _Sess:
    def __init__(self, change_expectation: str = ""):
        self.change_expectation = change_expectation


class TestExplorePromotion(unittest.TestCase):
    def _m(
        self,
        *,
        discovery=0,
        cap=10,
        budget_rem=50,
        budget_ex=False,
        text_streak=0,
        unique_reads=0,
        target_file_count=0,
        relevant_read_hits=0,
        max_target_reads=0,
        focused_write=False,
        simple_write=False,
        policy=True,
    ) -> ExplorationMetrics:
        return ExplorationMetrics(
            discovery_action_count=discovery,
            discovery_cap=cap,
            budget_remaining=budget_rem,
            budget_exhausted=budget_ex,
            explore_text_only_streak=text_streak,
            unique_read_paths=unique_reads,
            target_file_count=target_file_count,
            relevant_read_hits=relevant_read_hits,
            max_target_read_count=max_target_reads,
            focused_write_task=focused_write,
            simple_write_task=simple_write,
            policy_allows_act=policy,
            max_text_only_streak=2,
            min_unique_reads_for_context=3,
            min_discovery_for_context=2,
        )

    def test_rejected_write_promotes_when_policy_allows(self):
        m = self._m()
        tr = ExploreTurnResult(kind="tools", rejected_write_tool_names=("write_file",))
        self.assertTrue(should_promote_explore_to_act(_Sess(), tr, m))
        self.assertEqual(m.last_promotion_reason, REASON_REJECTED_WRITE_ATTEMPT)

    def test_rejected_write_no_promote_when_should_not_write(self):
        m = self._m()
        tr = ExploreTurnResult(kind="tools", rejected_write_tool_names=("write_file",))
        self.assertFalse(should_promote_explore_to_act(_Sess("should_not_write"), tr, m))
        self.assertEqual(m.last_promotion_reason, "")

    def test_budget_exhausted(self):
        m = self._m(budget_rem=0, budget_ex=True)
        tr = ExploreTurnResult(kind="text_only")
        self.assertTrue(should_promote_explore_to_act(_Sess(), tr, m))
        self.assertEqual(m.last_promotion_reason, REASON_EXPLORATION_BUDGET_EXHAUSTED)

    def test_discovery_cap(self):
        m = self._m(discovery=10, cap=10)
        tr = ExploreTurnResult(kind="tools", executed_tool_names=("read_file",))
        self.assertTrue(should_promote_explore_to_act(_Sess(), tr, m))
        self.assertEqual(m.last_promotion_reason, REASON_DISCOVERY_CAP_REACHED)

    def test_stagnation_precheck(self):
        m = self._m()
        tr = ExploreTurnResult(kind="precheck_stagnation", stagnation_reason="loop")
        self.assertTrue(should_promote_explore_to_act(_Sess(), tr, m))
        self.assertEqual(m.last_promotion_reason, REASON_READ_ONLY_STAGNATION_THRESHOLD)

    def test_repo_context_sufficient(self):
        m = self._m(unique_reads=3, discovery=2)
        tr = ExploreTurnResult(kind="tools", executed_tool_names=("read_file",))
        self.assertTrue(should_promote_explore_to_act(_Sess(), tr, m))
        self.assertEqual(m.last_promotion_reason, REASON_REPO_CONTEXT_SUFFICIENT)

    def test_focused_write_promotes_after_relevant_read(self):
        m = self._m(
            discovery=1,
            unique_reads=1,
            target_file_count=1,
            relevant_read_hits=1,
            focused_write=True,
        )
        tr = ExploreTurnResult(kind="tools", executed_tool_names=("read_file",))
        self.assertTrue(should_promote_explore_to_act(_Sess(), tr, m))
        self.assertEqual(m.last_promotion_reason, REASON_FOCUSED_WRITE_CONTEXT_READY)

    def test_simple_write_promotes_with_min_context(self):
        m = self._m(
            discovery=2,
            unique_reads=1,
            target_file_count=1,
            simple_write=True,
        )
        tr = ExploreTurnResult(kind="tools", executed_tool_names=("ls", "read_file"))
        self.assertTrue(should_promote_explore_to_act(_Sess(), tr, m))
        self.assertEqual(m.last_promotion_reason, REASON_SIMPLE_WRITE_MIN_CONTEXT)

    def test_repeated_target_read_promotes(self):
        m = self._m(
            discovery=1,
            unique_reads=1,
            target_file_count=1,
            max_target_reads=2,
            focused_write=True,
        )
        tr = ExploreTurnResult(kind="tools", executed_tool_names=("read_file",))
        self.assertTrue(should_promote_explore_to_act(_Sess(), tr, m))
        self.assertEqual(m.last_promotion_reason, REASON_REPEATED_TARGET_READ)

    def test_modify_intent_in_text(self):
        m = self._m()
        tr = ExploreTurnResult(
            kind="text_only",
            assistant_text="I will edit the file app/foo.ts to add the handler.",
        )
        self.assertTrue(should_promote_explore_to_act(_Sess(), tr, m))
        self.assertEqual(m.last_promotion_reason, REASON_EXPLICIT_MODEL_MODIFY_INTENT)

    def test_text_only_streak(self):
        m = self._m(text_streak=2)
        tr = ExploreTurnResult(kind="text_only", assistant_text="Still exploring the codebase.")
        self.assertTrue(should_promote_explore_to_act(_Sess(), tr, m))
        self.assertEqual(m.last_promotion_reason, REASON_EXPLORE_TEXT_ONLY_STREAK)

    def test_text_signals_modify_intent_negative(self):
        self.assertFalse(text_signals_modify_intent("ok"))
        self.assertFalse(text_signals_modify_intent(""))


if __name__ == "__main__":
    unittest.main()
