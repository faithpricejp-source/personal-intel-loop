from __future__ import annotations

from pathlib import Path

from personal_intel_loop.active_corpus import (
    _extract_object_type,
    _extract_summary_text,
    _parse_wikilinks,
    _resolve_object_file,
    collect_active_object_ids,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")


def test_parse_wikilinks_dedup_and_strip_alias(tmp_path):
    idx = tmp_path / "index.md"
    _write(
        idx,
        "- [[JDG_alpha]] | why_active: x\n"
        "- [[JDG_beta|display name]] | why_active: y\n"
        "- [[JDG_alpha]] dup\n"
        "- [[MON_gamma#anchor]] | why_active: z\n",
    )
    assert _parse_wikilinks(idx) == ["JDG_alpha", "JDG_beta", "MON_gamma"]


def test_collect_active_object_ids_merges_both_indices(tmp_path):
    jdg_idx = tmp_path / "jdg.md"
    mon_idx = tmp_path / "mon.md"
    _write(jdg_idx, "- [[JDG_a]]\n- [[DEC_b]]\n")
    _write(mon_idx, "- [[MON_c]]\n- [[JDG_a]] (dup)\n")
    ids = collect_active_object_ids(jdg_dec_index=jdg_idx, mon_index=mon_idx)
    assert ids == ["JDG_a", "DEC_b", "MON_c"]


def test_resolve_object_file_finds_subdir(tmp_path):
    vault = tmp_path / "vault"
    target = vault / "07 判断与决策" / "Judgments" / "JDG_alpha.md"
    _write(target, "body")
    # also add a decoy under archived tree that must be skipped
    _write(vault / "09 版本与归档" / "JDG_alpha.md", "archived")
    resolved = _resolve_object_file("JDG_alpha", vault_root=vault)
    assert resolved == target


def test_resolve_object_file_returns_none_for_template_placeholder(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    assert _resolve_object_file("JDG_对象", vault_root=vault) is None


def test_extract_summary_section_prefers_named_section():
    body = (
        "## 背景\n一些背景\n\n"
        "## 摘要\n核心判断是 X。\n第二行补充。\n\n"
        "## 其他\n无关段落。\n"
    )
    extracted = _extract_summary_text(body)
    assert extracted.startswith("## 摘要")
    assert "核心判断是 X" in extracted
    assert "无关段落" not in extracted


def test_extract_summary_falls_back_to_body_when_no_section():
    body = "没有标题段，只有一段文本。"
    assert _extract_summary_text(body) == body


def test_extract_object_type_prefers_frontmatter():
    assert _extract_object_type("JDG_x", "object_type: judgement") == "judgement"


def test_extract_object_type_falls_back_to_prefix():
    assert _extract_object_type("MON_x", "") == "MON"
    assert _extract_object_type("plain", "") == "plain"
