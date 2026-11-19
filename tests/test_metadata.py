from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live_relay.metadata import (
    StreamMetadata,
    apply_metadata_overrides,
    clean_tags,
    generate_metadata,
)
from live_relay.source import LiveSource


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.source = LiveSource(
            title="شرح بث مباشر للألعاب",
            urls=("https://source.example/private-stream",),
            headers=(),
            description="دليل عملي. https://source.example/private-link?token=secret",
            channel="قناة الألعاب",
            categories=("Gaming",),
            tags=("ألعاب", "بث مباشر"),
        )
        self.settings = SimpleNamespace(
            gemini_api_key="",
            gemini_model="gemini-3.8-flash",
            gemini_metadata_enabled=True,
            metadata_language="ar",
            youtube_category_id="20",
        )

    def test_falls_back_to_source_metadata_without_api_key(self):
        result = generate_metadata(self.settings, self.source)
        self.assertEqual(result.provider, "source")
        self.assertEqual(result.title, self.source.title)
        self.assertEqual(result.category_id, "20")
        self.assertIn("دليل عملي", result.description)
        self.assertEqual(result.tags, ("ألعاب", "بث مباشر"))

    def test_picks_best_title_from_candidates(self):
        self.settings.gemini_api_key = "test-api-secret"
        generated = {
            "title_candidates": [
                "بث مباشر: نهائي دوري الأبطال بين ريال مدريد وبرشلونة",
                "مباراة\u200f مباشرة\u200f الآن\u200f",
                "WATCH THE MATCH NOW!!!",
            ],
            "description": "وصف بث مفيد.",
            "keywords": ["دوري الأبطال", "بث مباشر", "كرة القدم"],
        }
        response = Mock(ok=True)
        response.json.return_value = {
            "steps": [
                {
                    "type": "model_output",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(generated, ensure_ascii=False),
                        }
                    ],
                }
            ]
        }
        with patch("live_relay.metadata.requests.post", return_value=response):
            result = generate_metadata(self.settings, self.source)

        self.assertEqual(result.provider, "gemini")
        self.assertIn("دوري الأبطال", result.title)
        self.assertIn("دوري الأبطال", result.tags[0])

    def test_prompt_requests_three_ranked_candidates(self):
        self.settings.gemini_api_key = "test-api-secret"
        captured = {}

        def fake_post(url, **kwargs):
            captured["body"] = kwargs["json"]
            return Mock(ok=False)

        with patch("live_relay.metadata.requests.post", side_effect=fake_post):
            generate_metadata(self.settings, self.source)

        body = captured["body"]
        self.assertIn("title_candidates", body["input"])
        self.assertIn(
            "title_candidates",
            body["response_format"]["schema"]["properties"],
        )

    def test_uses_stateless_structured_interaction_and_sanitizes_output(self):
        self.settings.gemini_api_key = "test-api-secret"
        generated = {
            "title": "عنوان جذاب " + "ع" * 110,
            "description": "وصف بث مفيد.",
            "keywords": ["ألعاب", "تحدي", "ألعاب", "بث مباشر"],
        }
        response = Mock(ok=True)
        response.json.return_value = {
            "steps": [
                {
                    "type": "model_output",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(generated, ensure_ascii=False),
                        }
                    ],
                }
            ]
        }
        with patch("live_relay.metadata.requests.post", return_value=response) as post:
            result = generate_metadata(self.settings, self.source)

        args, kwargs = post.call_args
        self.assertEqual(
            args[0], "https://generativelanguage.googleapis.com/v1beta/interactions"
        )
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "test-api-secret")
        self.assertEqual(kwargs["json"]["model"], "gemini-3.8-flash")
        self.assertFalse(kwargs["json"]["store"])
        self.assertEqual(
            kwargs["json"]["response_format"]["mime_type"], "application/json"
        )
        self.assertNotIn("test-api-secret", json.dumps(kwargs["json"]))
        self.assertNotIn("source.example", kwargs["json"]["input"])
        self.assertEqual(result.provider, "gemini")
        self.assertEqual(len(result.title), 100)
        self.assertEqual(result.description, "وصف بث مفيد.")
        self.assertEqual(result.tags, ("ألعاب", "تحدي", "بث مباشر"))

    def test_returns_source_metadata_if_gemini_response_is_invalid(self):
        self.settings.gemini_api_key = "test-api-secret"
        response = Mock(ok=True)
        response.json.return_value = {"steps": []}
        with (
            patch("live_relay.metadata.requests.post", return_value=response),
            self.assertLogs(level="WARNING") as captured,
        ):
            result = generate_metadata(self.settings, self.source)
        self.assertEqual(result.provider, "source")
        self.assertNotIn("test-api-secret", "\n".join(captured.output))

    def test_api_failure_does_not_block_fallback(self):
        self.settings.gemini_api_key = "test-api-secret"
        response = Mock(ok=False, status_code=503)
        with patch("live_relay.metadata.requests.post", return_value=response):
            result = generate_metadata(self.settings, self.source)
        self.assertEqual(result.provider, "source")
        self.assertEqual(result.title, self.source.title)

    def test_tags_are_deduplicated_and_limited(self):
        tags = clean_tags([" Gaming ", "gaming", "Live", "x" * 90], limit=12)
        self.assertEqual(tags, ("Gaming", "Live"))

    def test_disabled_gemini_uses_source_metadata(self):
        self.settings.gemini_metadata_enabled = False
        self.settings.gemini_api_key = "test-api-secret"
        with patch("live_relay.metadata.requests.post") as post:
            result = generate_metadata(self.settings, self.source)
        post.assert_not_called()
        self.assertEqual(result.provider, "source")


    def test_manual_overrides_replace_generated_metadata(self):
        base = generate_metadata(self.settings, self.source)
        self.settings.youtube_title_override = "عنوان مخصص للبث"
        self.settings.youtube_description_override = "وصف مخصص"
        self.settings.youtube_tags_override = "أخبار; بث مباشر، تحليل"

        result = apply_metadata_overrides(self.settings, base)

        self.assertEqual(result.title, "عنوان مخصص للبث")
        self.assertEqual(result.description, "وصف مخصص")
        self.assertEqual(result.tags, ("أخبار", "بث مباشر", "تحليل"))
        self.assertEqual(result.provider, "manual")

    def test_runtime_empty_overrides_restore_base_metadata(self):
        base = StreamMetadata("Gemini title", "Generated description", ("tag",))
        self.settings.youtube_title_override = "Configured title"

        result = apply_metadata_overrides(self.settings, base, {})

        self.assertIs(result, base)

if __name__ == "__main__":
    unittest.main()
