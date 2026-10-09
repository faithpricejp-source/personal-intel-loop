"""profile.py: 读 / 剥掉未接受提案 / 追加提案 / 列待接受。"""
from __future__ import annotations

from personal_intel_loop.profile import (
    PROPOSALS_HEADER,
    append_proposal,
    load_profile,
    pending_proposals,
    profile_body_for_prompt,
)

SAMPLE = """# 阅读偏好 profile

## 域饱和表
| AI 产业经济 | 饱和 | 只要含可检验预测的 |

## 待接受的修订
-
"""


def test_body_for_prompt_strips_pending_section(tmp_path):
    p = tmp_path / "reading_profile.md"
    p.write_text(SAMPLE, "utf-8")
    body = profile_body_for_prompt(p)
    assert "域饱和表" in body and PROPOSALS_HEADER not in body


def test_append_and_pending_roundtrip(tmp_path):
    p = tmp_path / "reading_profile.md"
    p.write_text(SAMPLE, "utf-8")
    assert pending_proposals(p) == []  # 孤立 '-' 占位不算
    append_proposal("[2026-08-26 · keep · rss:x] 依据: a → 目标: b → 建议: c", p)
    append_proposal("second", p)
    assert pending_proposals(p) == ["[2026-08-26 · keep · rss:x] 依据: a → 目标: b → 建议: c", "second"]
    text = load_profile(p)
    assert text.count(PROPOSALS_HEADER) == 1
    assert "\n-\n" not in text  # 占位被替换
    assert "域饱和表" in profile_body_for_prompt(p)  # 正文未被动


def test_append_creates_section_when_missing(tmp_path):
    p = tmp_path / "reading_profile.md"
    p.write_text("# profile\n\n正文\n", "utf-8")
    append_proposal("x", p)
    assert pending_proposals(p) == ["x"]
    assert profile_body_for_prompt(p).endswith("正文")


def test_missing_file_is_empty(tmp_path):
    p = tmp_path / "nope.md"
    assert load_profile(p) == "" and profile_body_for_prompt(p) == "" and pending_proposals(p) == []


def test_decide_proposal_tolerates_header_suffix(tmp_path):
    from personal_intel_loop.profile import decide_proposal

    p = tmp_path / "reading_profile.md"
    p.write_text("# p\n\n正文\n\n## 待接受的修订(后台提案区,每条带日期与依据;我接受后上移合并)\n- alpha\n- beta\n", "utf-8")
    assert decide_proposal("alpha", "accept", p) == 1
    text = p.read_text("utf-8")
    assert "## 已接受的修订" in text and "- alpha" in text
    assert pending_proposals(p) == ["beta"]
    assert decide_proposal("beta", "reject", p) == 0
