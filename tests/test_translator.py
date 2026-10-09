"""Tests for translator.py."""
from personal_intel_loop.translator import needs_translation, translate_body


def _mock_llm(monkeypatch, *, text: str | None = None, exc: Exception | None = None,
              captured: dict | None = None) -> None:
    """伪造 llm_backend: 配好主力档, chat 返回给定文本或抛错。"""
    from personal_intel_loop import llm_backend

    monkeypatch.setenv("PIL_LLM_BASE_URL", "http://primary.invalid/v1")
    monkeypatch.setenv("PIL_LLM_MODEL", "model-x")
    monkeypatch.delenv("PIL_LLM_FALLBACK_BASE_URL", raising=False)

    def fake_chat(prompt, **kwargs):
        if captured is not None:
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
        if exc is not None:
            raise exc
        return {"text": text, "backend": "primary", "model": "model-x", "finish_reason": "stop"}

    monkeypatch.setattr(llm_backend, "chat", fake_chat)


class TestNeedsTranslation:
    def test_english_needs_translation(self):
        assert needs_translation("This is an English article about AI and technology.") is True

    def test_chinese_no_translation(self):
        assert needs_translation("这是一篇关于人工智能的中文文章，内容非常丰富。") is False

    def test_empty_no_translation(self):
        assert needs_translation("") is False
        assert needs_translation("   ") is False

    def test_mixed_mostly_english(self):
        # English body with a few Chinese words still needs translation
        text = "This article discusses AI trends. 人工智能 is transforming industries globally."
        assert needs_translation(text) is True

    def test_mixed_mostly_chinese(self):
        text = "这篇文章讨论了AI的趋势，AI is transforming industries globally，对社会影响深远。"
        assert needs_translation(text) is False

    def test_japanese_no_translation(self):
        # Japanese kana is also CJK-adjacent, should not be translated
        assert needs_translation("これは日本語のテキストです。人工知能について説明します。") is False


class TestTranslateBody:
    def test_empty_body_returns_none(self):
        assert translate_body("") is None
        assert translate_body("   ") is None

    def test_returns_none_when_llm_not_configured(self, monkeypatch):
        for name in ("PIL_LLM_BASE_URL", "PIL_LLM_MODEL", "PIL_LLM_FALLBACK_BASE_URL", "PIL_LLM_FALLBACK_MODEL"):
            monkeypatch.delenv(name, raising=False)
        assert translate_body("This is an English article.", title="Test") is None

    def test_returns_none_when_cloud_raises(self, monkeypatch):
        _mock_llm(monkeypatch, exc=RuntimeError("all models failed"))
        assert translate_body("This is an English article.", title="Test") is None

    def test_returns_none_on_empty_response(self, monkeypatch):
        _mock_llm(monkeypatch, text="   ")
        assert translate_body("This is an English article.", title="Test") is None

    def test_returns_cloud_translated_text_without_local_fallback(self, monkeypatch):
        captured: dict = {}
        _mock_llm(monkeypatch, text="这是一篇关于人工智能的英文文章。", captured=captured)
        result = translate_body("This is an English article about AI.", title="AI Article")
        assert result == "这是一篇关于人工智能的英文文章。"
        assert captured["kwargs"].get("tier") == "primary"

    def test_body_truncated_to_limit(self, monkeypatch):
        from personal_intel_loop.translator import TRANSLATE_BODY_LIMIT

        captured: dict = {}
        _mock_llm(monkeypatch, text="翻译结果", captured=captured)

        long_body = "x" * (TRANSLATE_BODY_LIMIT + 500)
        translate_body(long_body, title="Long Article")
        assert len(captured["prompt"]) < len(long_body) + 200
        assert "节选" in captured["prompt"]
