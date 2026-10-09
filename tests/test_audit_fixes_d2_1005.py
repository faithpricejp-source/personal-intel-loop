"""审计 D 卷 D24–D27 的修复验证(2026-10-05, 见 audits/AUDIT_D.md)。

每条先复现(修复前失败)、修复后通过; 复现不出来的记「已不存在」不改代码。
"""
from __future__ import annotations

from pathlib import Path

from personal_intel_loop import risk_reference as rr

FIXTURE_REF = Path(__file__).parent / "fixtures" / "risk_ref"

US_ALIASES_ALL_LANGS = ["美国", "美國", "米国", "アメリカ合衆国", "United States", "USA", "U.S.", "America"]


# --------------------------------------------------------------------------- #
# D24: _cell_hit 别名误匹配 —— "U.S."→"us" 按子串命中其他国家
# --------------------------------------------------------------------------- #

def test_d24_cell_hit_us_alias_not_substring_of_other_countries():
    """"us"/"america" 这类归一化别名不得作为裸子串命中别国写法。"""
    for cell in ("Russia", "Austria", "Belarus", "South America", "Central America"):
        assert not rr._cell_hit(cell, US_ALIASES_ALL_LANGS), f"{cell} 不应命中美国别名"


def test_d24_cell_hit_us_aliases_still_hit_valid_spellings():
    """修复不得丢掉美国自己的合法写法(en 缩写/全名与 zh/ja)。"""
    for cell in ("United States", "U.S.", "USA", "US", "America", "United States of America", "美国", "米国", "アメリカ合衆国"):
        assert rr._cell_hit(cell, US_ALIASES_ALL_LANGS), f"{cell} 应命中美国别名"


def test_d24_district_block_excludes_foreign_rows_written_in_english(tmp_path):
    """district_time_lines 的国家单元格用英文国名时, 他国行不得混入美国参考块。"""
    root = tmp_path / "ref"
    (root / "official").mkdir(parents=True)
    (root / "official" / "district_time_lines.md").write_text(
        "# 涉及具体街区・时段の原文行\n"
        "\n"
        "| 国家 | 地区 | 标注 | 原文 | URL |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| Russia | Moscow | 外務省・夜間 | ロシア夜間の注意文。 | https://example.com/ru |\n"
        "| Austria | Vienna | 外務省・夜間 | オーストリア夜間の注意文。 | https://example.com/at |\n"
        "| United States | New York | 外務省・夜間 | 米国夜間の注意文。 | https://example.com/us |\n"
        "| U.S. | （全米） | FCDO・夜間 | Try not to walk alone at night. | https://example.com/us2 |\n",
        encoding="utf-8",
    )
    block = rr._district_lines_block(root, "unitedstates", "newyork", None)
    assert block
    assert "Russia" not in block and "Austria" not in block
    assert "米国夜間の注意文。" in block
    assert "Try not to walk alone at night." in block


# --------------------------------------------------------------------------- #
# D25: 无标注的半句硬切（_fit_text 兜底 / load_reference·load_home_reference 的 head）
# --------------------------------------------------------------------------- #

def test_d25_fit_text_first_line_over_limit_still_marks_truncation():
    """块的第一行(整段无换行)单独超预算时, 兜底硬切也要带截断标注。"""
    text = "这是一整段没有换行的超长正文，讲的是治安状况。" * 3 + "\n第二行"
    out = rr._fit_text(text, 30)
    assert "截断" in out or "省略" in out, "第一行单独超预算时也要带截断标注"
    assert len(out) <= 30, "带标注后总长仍不得超预算"


def test_d25_fit_text_tiny_limit_still_returns_content():
    """预算小到标注都放不下时也不能崩/返回空。"""
    out = rr._fit_text("abcdef", 3)
    assert out and len(out) <= 3


def test_d25_load_reference_head_over_max_chars_marks_truncation():
    """head 自己就超过 max_chars 时, 不得无标注裸切(丢掉全部来源与日期还不告知)。"""
    out = rr.load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF, max_chars=50)
    assert "截断" in out


def test_d25_load_home_reference_head_over_max_chars_marks_truncation():
    out = rr.load_home_reference(ref_dir=FIXTURE_REF, max_chars=50)
    assert "截断" in out


# --------------------------------------------------------------------------- #
# D26: hotspots 合法 JSON 但非 dict → load_reference 整体抛异常
# --------------------------------------------------------------------------- #

def test_d26_hotspot_block_non_dict_json_returns_empty(tmp_path):
    """`[]`/`null`/`3`/字符串都是合法 JSON, 与坏编码同属「文件不可用」→ 空串。"""
    root = tmp_path / "ref"
    (root / "hotspots").mkdir(parents=True)
    for payload in ("[]", "null", "3", '"str"'):
        (root / "hotspots" / "hotspots_nyc.json").write_text(payload, encoding="utf-8")
        assert rr._hotspot_block(root, "unitedstates", "newyork", None) == "", payload


def test_d26_load_reference_non_dict_hotspots_degrades_to_other_blocks(tmp_path):
    """hotspots 坏掉时其余参考块照常返回, 不能让整个 load_reference 抛异常。"""
    root = tmp_path / "ref"
    (root / "official").mkdir(parents=True)
    (root / "hotspots").mkdir(parents=True)
    (root / "official" / "unitedstates.md").write_text(
        "# x\n### 1. 危険レベル（当前）\n- 来源 URL: https://example.com/a\n\n原文:\n\nAAA\n",
        encoding="utf-8",
    )
    (root / "hotspots" / "hotspots_nyc.json").write_text("[]", encoding="utf-8")
    out = rr.load_reference("unitedstates", "newyork", ref_dir=root)
    assert "危険レベル" in out
