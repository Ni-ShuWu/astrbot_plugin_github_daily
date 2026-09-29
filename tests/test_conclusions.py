"""Tests for configurable conclusion dictionaries and message rendering."""

from __future__ import annotations

import json
import random
import unittest
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

from github_daily.conclusions import (
    DEFAULT_CONCLUSIONS,
    ROTATING_CONCLUSIONS,
    ConclusionPicker,
)
from github_daily.config import PluginConfig
from github_daily.service import ContributionService


class ConclusionConfigurationTests(unittest.TestCase):
    def test_schema_defaults_match_the_existing_rotating_pools(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        for status, pool in ROTATING_CONCLUSIONS.items():
            field = schema[f"conclusion_{status}_sentences"]
            self.assertEqual(field["type"], "dict")
            self.assertEqual(tuple(field["default"].values()), pool)
            self.assertEqual(field["default"]["default"], DEFAULT_CONCLUSIONS[status])

    def test_legacy_config_uses_the_original_defaults(self) -> None:
        config = PluginConfig.from_mapping({"random_conclusion": False})

        for status, pool in ROTATING_CONCLUSIONS.items():
            self.assertEqual(config.conclusion_pool(status), pool)
            self.assertEqual(
                config.fixed_conclusion(status), DEFAULT_CONCLUSIONS[status]
            )

    def test_custom_entries_are_single_line_strings_and_deduplicated(self) -> None:
        config = PluginConfig.from_mapping(
            {
                "conclusion_coding_sentences": {
                    "default": "  自定义\n固定\t文案  ",
                    "line_2": "第二句\r\n继续",
                    "duplicate": " 第二句  继续 ",
                    "not_text": 123,
                    "blank": " \n ",
                }
            }
        )

        self.assertEqual(
            config.conclusion_pool("coding"), ("自定义 固定 文案", "第二句 继续")
        )
        self.assertEqual(config.fixed_conclusion("coding"), "自定义 固定 文案")
        self.assertEqual(
            config.conclusion_pool("active"), ROTATING_CONCLUSIONS["active"]
        )

    def test_removed_default_uses_first_entry_and_empty_pool_falls_back(self) -> None:
        config = PluginConfig.from_mapping(
            {"conclusion_idle_sentences": {"line_1": "自定义空闲状态"}}
        )
        self.assertEqual(config.fixed_conclusion("idle"), "自定义空闲状态")

        empty_config = PluginConfig.from_mapping({"conclusion_idle_sentences": {}})
        self.assertEqual(empty_config.conclusion_pool("idle"), ())
        self.assertEqual(
            empty_config.fixed_conclusion("idle"), DEFAULT_CONCLUSIONS["idle"]
        )

    def test_to_dict_keeps_custom_values_and_masks_token(self) -> None:
        config = PluginConfig.from_mapping(
            {
                "github_token": "secret-token",
                "conclusion_active_sentences": {"default": "自定义活动文案"},
            }
        )
        exported = config.to_dict()

        self.assertEqual(exported["github_token"], "***")
        self.assertEqual(
            exported["conclusion_active_sentences"], {"default": "自定义活动文案"}
        )


class ConclusionRenderingTests(unittest.TestCase):
    @staticmethod
    def make_service(config: PluginConfig) -> ContributionService:
        service = ContributionService.__new__(ContributionService)
        service.config = config
        service._conclusions = ConclusionPicker(
            {status: config.conclusion_pool(status) for status in DEFAULT_CONCLUSIONS},
            rng=random.Random(3),
        )
        return service

    @staticmethod
    def make_result(status: str) -> SimpleNamespace:
        return SimpleNamespace(
            summary=SimpleNamespace(
                total_count=3,
                code_count=1,
                ordinary_count=2,
                latest_activity=None,
            ),
            account=SimpleNamespace(label="tester", username="tester"),
            window_hours=24,
            status=status,
            stale=False,
        )

    def test_fixed_mode_uses_custom_default_in_each_status(self) -> None:
        entries = {
            "coding": "自定义写代码固定文案",
            "active": "自定义活动固定文案",
            "idle": "自定义空闲固定文案",
        }
        raw = {
            f"conclusion_{status}_sentences": {"default": sentence}
            for status, sentence in entries.items()
        }
        service = self.make_service(PluginConfig.from_mapping(raw))

        for status, sentence in entries.items():
            with self.subTest(status=status):
                self.assertIn(
                    f"结论：{sentence}", service.format_result(self.make_result(status))
                )

    def test_rotation_uses_only_the_custom_pool_for_that_status(self) -> None:
        config = PluginConfig.from_mapping(
            {
                "random_conclusion": True,
                "conclusion_coding_sentences": {
                    "default": "coding one",
                    "line_2": "coding two",
                },
                "conclusion_active_sentences": {"default": "active only"},
            }
        )
        service = self.make_service(config)
        coding_messages = [
            service.format_result(self.make_result("coding")) for _ in range(4)
        ]
        coding_sentences = [
            message.split("结论：", 1)[1].splitlines()[0] for message in coding_messages
        ]

        self.assertEqual(set(coding_sentences), {"coding one", "coding two"})
        self.assertTrue(
            all(left != right for left, right in pairwise(coding_sentences))
        )
        active_message = service.format_result(self.make_result("active"))
        self.assertIn("结论：active only", active_message)

    def test_empty_custom_pool_falls_back_in_rotation_mode(self) -> None:
        config = PluginConfig.from_mapping(
            {"random_conclusion": True, "conclusion_coding_sentences": {}}
        )
        service = self.make_service(config)
        message = service.format_result(self.make_result("coding"))
        self.assertIn(f"结论：{DEFAULT_CONCLUSIONS['coding']}", message)


if __name__ == "__main__":
    unittest.main()
