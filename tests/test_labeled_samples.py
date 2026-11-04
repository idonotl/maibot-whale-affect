"""在固定标注样本上评估规则分类器（技术路线 T02-T34 的规则部分、P3 验收）。

样本集中每个用例声明期望的 ``target`` 与 ``event``；本模块计算命中率，
并把误报（把非攻击判成攻击、把第三方判成机器人）单独列出。
"""

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import PACKAGE_NAME, PLUGIN_ROOT  # noqa: E402

rules_module = __import__(f"{PACKAGE_NAME}.classifier_rules", fromlist=["classifier_rules"])
models_module = __import__(f"{PACKAGE_NAME}.models", fromlist=["models"])
adapter_module = __import__(f"{PACKAGE_NAME}.host_adapter", fromlist=["host_adapter"])

NormalizedMessage = models_module.NormalizedMessage
Target = models_module.Target

FIXTURE_PATH = PLUGIN_ROOT / "fixtures" / "labeled_samples.json"

# 攻击类事件；用于单独统计「误报为攻击」的样本数。
ATTACK_EVENTS = frozenset({"mild_attack", "severe_attack"})

# 允许的等价事件：同一语义在规则层可能落到更宽的正向类别。
EVENT_EQUIVALENCE: Dict[str, frozenset] = {
    "praise": frozenset({"praise"}),
    "comfort": frozenset({"comfort"}),
    "apology": frozenset({"apology"}),
    "criticism": frozenset({"criticism"}),
    "affectionate_tease": frozenset({"affectionate_tease"}),
    "friendly_chat": frozenset({"friendly_chat"}),
    "mild_attack": frozenset({"mild_attack"}),
    "severe_attack": frozenset({"severe_attack"}),
}


def load_samples() -> List[Dict[str, Any]]:
    """读取固定标注样本。

    Returns:
        List[Dict[str, Any]]: 样本列表。

    Raises:
        FileNotFoundError: 样本文件缺失。
    """

    if not FIXTURE_PATH.is_file():
        raise FileNotFoundError(f"缺少标注样本: {FIXTURE_PATH}")
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("标注样本为空")
    return samples


def build_message(sample: Dict[str, Any]) -> NormalizedMessage:
    """把样本描述转换成归一化消息。

    Args:
        sample: 样本字典。

    Returns:
        NormalizedMessage: 归一化消息。
    """

    group_id = "123456" if sample.get("scene") == "group" else ""
    base = NormalizedMessage(
        platform="qq",
        bot_account_id="100000002",
        scope="",
        user_id="100000002" if sample.get("is_self") else "100000001",
        group_id=group_id,
        session_id="session-1",
        message_id=f"msg-{sample.get('id', 'x')}",
        text=str(sample.get("text") or ""),
        timestamp=1_760_000_000,
        is_at=bool(sample.get("at")),
        is_mentioned=bool(sample.get("at")),
        is_notify=bool(sample.get("is_notify")),
        is_command=bool(sample.get("is_command")),
        is_picture=bool(sample.get("is_picture")),
        is_emoji=bool(sample.get("is_emoji")),
    )
    # 自身回声在排除层处理，这里保持 user_id 等于机器人账号。
    return replace(base)


def classify_sample(classifier: Any, sample: Dict[str, Any]) -> Any:
    """对单个样本执行规则分类。

    Args:
        classifier: 规则分类器实例。
        sample: 样本字典。

    Returns:
        models.Classification: 分类结果。
    """

    message = build_message(sample)
    if adapter_module.is_excluded(message):
        return models_module.Classification(
            target=Target.OTHER.value,
            event=models_module.EventCategory.NEUTRAL.value,
            confidence=0.9,
            reason_code="excluded_by_adapter",
        )
    return classifier.classify(message)


def evaluate(classifier: Any, samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """在样本集上统计命中与误报。

    Args:
        classifier: 规则分类器实例。
        samples: 样本列表。

    Returns:
        Dict[str, Any]: 统计结果。
    """

    target_hits = 0
    event_checked = 0
    event_hits = 0
    false_attacks: List[str] = []
    missed_attacks: List[str] = []
    details: List[Tuple[str, str, str]] = []

    for sample in samples:
        result = classify_sample(classifier, sample)
        expected_target = str(sample.get("expected_target") or "")
        if result.target == expected_target:
            target_hits += 1
        else:
            details.append((str(sample["id"]), f"target={result.target}", f"expected={expected_target}"))

        expected_event = sample.get("expected_event")
        if expected_event:
            event_checked += 1
            allowed = EVENT_EQUIVALENCE.get(str(expected_event), frozenset({str(expected_event)}))
            if result.event in allowed:
                event_hits += 1
            else:
                details.append((str(sample["id"]), f"event={result.event}", f"expected={expected_event}"))
                if str(expected_event) in ATTACK_EVENTS:
                    missed_attacks.append(str(sample["id"]))
        # 期望非攻击却判成攻击，属于最危险的误报。
        if result.event in ATTACK_EVENTS and str(expected_event) not in ATTACK_EVENTS:
            false_attacks.append(str(sample["id"]))

    return {
        "total": len(samples),
        "target_hits": target_hits,
        "event_checked": event_checked,
        "event_hits": event_hits,
        "false_attacks": false_attacks,
        "missed_attacks": missed_attacks,
        "mismatches": details,
    }


class LabeledSamplesTest(unittest.TestCase):
    """固定样本集的整体验收。"""

    def setUp(self) -> None:
        self.classifier = rules_module.RulesClassifier()
        self.samples = load_samples()
        self.report = evaluate(self.classifier, self.samples)

    def test_fixture_covers_required_scenarios(self) -> None:
        ids = {str(sample["id"]) for sample in self.samples}
        for required in ("T02-nickname", "T04-nickname-not-exempt", "T05-negated-insult", "T17-group-no-target"):
            self.assertIn(required, ids)

    def test_target_accuracy(self) -> None:
        report = self.report
        ratio = report["target_hits"] / report["total"]
        self.assertGreaterEqual(
            ratio,
            1.0,
            f"对象判定命中 {report['target_hits']}/{report['total']}，不一致: {report['mismatches']}",
        )

    def test_event_accuracy(self) -> None:
        report = self.report
        self.assertGreater(report["event_checked"], 0)
        ratio = report["event_hits"] / report["event_checked"]
        self.assertGreaterEqual(
            ratio,
            1.0,
            f"事件判定命中 {report['event_hits']}/{report['event_checked']}，不一致: {report['mismatches']}",
        )

    def test_no_false_attack(self) -> None:
        self.assertEqual([], self.report["false_attacks"], "存在把非攻击误报为攻击的样本")

    def test_no_missed_attack(self) -> None:
        self.assertEqual([], self.report["missed_attacks"], "存在漏报的攻击样本")

    def test_report_is_json_serializable(self) -> None:
        payload = json.dumps(self.report, ensure_ascii=False)
        self.assertIn("target_hits", payload)


class ScenarioRegressionTest(unittest.TestCase):
    """逐条场景的固定断言（失败时能直接定位规格条目）。"""

    def setUp(self) -> None:
        self.classifier = rules_module.RulesClassifier()

    def _classify(self, sample_id: str) -> Any:
        sample = next(item for item in load_samples() if item["id"] == sample_id)
        return classify_sample(self.classifier, sample)

    def test_nickname_is_tease_not_attack(self) -> None:
        result = self._classify("T02-nickname")
        self.assertEqual("affectionate_tease", result.event)
        self.assertNotIn(result.event, ATTACK_EVENTS)

    def test_nickname_does_not_exempt_insult(self) -> None:
        result = self._classify("T04-nickname-not-exempt")
        self.assertEqual("severe_attack", result.event)

    def test_negated_insult_is_praise(self) -> None:
        result = self._classify("T05-negated-insult")
        self.assertEqual("praise", result.event)

    def test_self_report_is_not_scored(self) -> None:
        self.assertEqual(Target.OTHER.value, self._classify("T06-self-report").target)

    def test_quoted_insult_is_not_scored(self) -> None:
        self.assertEqual(Target.QUOTED.value, self._classify("T07-quoted-third-party").target)

    def test_correction_is_not_attack(self) -> None:
        result = self._classify("T08-correction")
        self.assertEqual("criticism", result.event)

    def test_object_shift_is_not_attack(self) -> None:
        result = self._classify("T09-object-shift")
        self.assertNotIn(result.event, ATTACK_EVENTS)
        self.assertEqual(Target.OTHER.value, result.target)

    def test_group_small_talk_is_ignored(self) -> None:
        self.assertEqual(Target.OTHER.value, self._classify("T17-group-no-target").target)

    def test_prompt_injection_is_plain_chat(self) -> None:
        result = self._classify("T22-prompt-injection")
        self.assertNotEqual("severe_attack", result.event)
        self.assertEqual(Target.BOT.value, result.target)

    def test_notify_and_echo_are_excluded(self) -> None:
        for sample_id in ("T32-notify", "T32-echo"):
            self.assertEqual(Target.OTHER.value, self._classify(sample_id).target, sample_id)

    def test_high_affinity_attack_still_counts(self) -> None:
        for sample_id in ("T34-high-affinity-attack", "T34-severe"):
            self.assertIn(self._classify(sample_id).event, ATTACK_EVENTS, sample_id)


class ExclusionBoundaryTest(unittest.TestCase):
    """适配层排除规则与规则分类器的一致性。"""

    def test_picture_and_command_are_excluded(self) -> None:
        for sample_id in ("M-picture", "M-command"):
            sample = next(item for item in load_samples() if item["id"] == sample_id)
            message = build_message(sample)
            self.assertTrue(adapter_module.is_excluded(message) or not message.text.strip(), sample_id)

    def test_group_requires_addressing(self) -> None:
        sample = next(item for item in load_samples() if item["id"] == "M-group-unaddressed-insult")
        result = classify_sample(rules_module.RulesClassifier(), sample)
        self.assertEqual(Target.OTHER.value, result.target)


if __name__ == "__main__":
    unittest.main()
