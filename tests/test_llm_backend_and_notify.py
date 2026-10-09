"""llm_backend / notify: 只靠环境变量配置, 未配置时降级而不是报错。"""
from __future__ import annotations

import pytest

from personal_intel_loop import llm_backend, notify

_ALL_LLM_ENV = [
    f"{prefix}_{suffix}"
    for prefix in ("PIL_LLM", "PIL_LLM_FALLBACK", "PIL_LOCAL_LLM")
    for suffix in ("BASE_URL", "MODEL", "API_KEY", "API_KEY_FILE")
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ALL_LLM_ENV + ["PIL_NOTIFY_CMD", "PIL_SMTP_HOST", "PIL_NOTIFY_EMAIL_TO"]:
        monkeypatch.delenv(name, raising=False)


def test_unconfigured_tiers():
    assert all(not llm_backend.is_configured(t) for t in llm_backend.TIERS)
    with pytest.raises(llm_backend.LLMUnavailable):
        llm_backend.chat("hi")
    assert llm_backend.chat_first_available("hi") is None


def test_key_file_is_read(monkeypatch, tmp_path):
    key = tmp_path / "key.txt"
    key.write_text("  secret-value \n", "utf-8")
    monkeypatch.setenv("PIL_LLM_BASE_URL", "http://example.invalid/v1/")
    monkeypatch.setenv("PIL_LLM_MODEL", "m")
    monkeypatch.setenv("PIL_LLM_API_KEY_FILE", str(key))
    cfg = llm_backend.tier_config("primary")
    assert cfg == {"base_url": "http://example.invalid/v1", "model": "m", "api_key": "secret-value"}


def test_chat_posts_openai_compatible_request(monkeypatch):
    import requests

    monkeypatch.setenv("PIL_LOCAL_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("PIL_LOCAL_LLM_MODEL", "local-m")
    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"model": "local-m", "choices": [{"message": {"content": " 你好 "}, "finish_reason": "stop"}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    r = llm_backend.chat("hi", tier="local", max_tokens=10, reasoning_effort="high")
    assert r == {"text": "你好", "model": "local-m", "backend": "local", "finish_reason": "stop"}
    assert seen["url"] == "http://127.0.0.1:9/v1/chat/completions"
    assert "Authorization" not in seen["headers"]
    assert seen["body"]["reasoning_effort"] == "high" and seen["body"]["max_tokens"] == 10


def test_notify_none_and_unconfigured():
    assert notify.send("x", "none") is True
    assert notify.send("x", "command") is False
    assert notify.send("x", "email") is False
    with pytest.raises(ValueError):
        notify.send("x", "carrier-pigeon")


def test_notify_command_receives_text_on_stdin(monkeypatch, tmp_path):
    out = tmp_path / "out.txt"
    monkeypatch.setenv("PIL_NOTIFY_CMD", f'cat > "{out}"; printf "%s" "$PIL_NOTIFY_SUBJECT" >> "{out}"')
    assert notify.send("正文", "command", subject="标题") is True
    assert out.read_text("utf-8") == "正文标题"
