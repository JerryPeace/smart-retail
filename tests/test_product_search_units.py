"""商品語意搜尋 POC — 純函式單元測試.

涵蓋範圍：
  - load_products_os.py  : detect_format, extract_sources
  - embed_products_os.py : build_embed_text (含 strip_html)
  - verify_search_os.py  : load_golden_set（approved gate）

設計原則（對齊 tests/test_etl_units.py 慣例）：
  - no DB / no network / no Docker
  - 三支腳本以 importlib.util.spec_from_file_location 載入
    （腳本有 if __name__ == "__main__": guard，import 時零 IO）
  - 不測 OpenSearch / Bedrock I/O（已拍板：I/O 驗證走 curl/_count 手動判準）
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from textwrap import dedent

import pytest

# ---------- 腳本模組載入輔助 ----------

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts" / "etl"


def _load_script(name: str):
    """以 importlib 安全載入腳本模組（不觸發 __main__ guard）."""
    path = _SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 模組層級只載入一次（避免每個 test 重複 IO）
_load = _load_script
_load_mod = {}


def _mod(name: str):
    if name not in _load_mod:
        _load_mod[name] = _load(name)
    return _load_mod[name]


# ---------- detect_format / extract_sources ----------

class TestDetectFormat:
    def _fn(self):
        return _mod("load_products_os.py").detect_format

    def test_plain_array_detected(self):
        raw = [{"martId": 1001, "martName": "保健飲品"}]
        assert self._fn()(raw) == "plain_array"

    def test_search_hits_detected(self):
        raw = [{"_index": "products", "_id": "1001", "_source": {"martId": 1001}}]
        assert self._fn()(raw) == "search_hits"

    def test_empty_list_raises(self):
        with pytest.raises(ValueError):
            self._fn()([])

    def test_non_list_raises(self):
        with pytest.raises(ValueError):
            self._fn()({"martId": 1001})  # dict 不是 list

    def test_unknown_keys_raises(self):
        # 第一個元素是 dict 但無 martId 也無 _source
        with pytest.raises(ValueError):
            self._fn()([{"someRandomKey": "value"}])

    def test_non_dict_element_raises(self):
        with pytest.raises(ValueError):
            self._fn()(["not_a_dict"])


class TestExtractSources:
    def _fn(self):
        return _mod("load_products_os.py").extract_sources

    def test_plain_array_passthrough(self):
        raw = [
            {"martId": 1, "martName": "A"},
            {"martId": 2, "martName": "B"},
        ]
        result = self._fn()(raw)
        assert len(result) == 2
        assert result[0]["martId"] == 1
        assert result[1]["martName"] == "B"

    def test_search_hits_unwrapped(self):
        raw = [
            {"_index": "p", "_id": "1", "_source": {"martId": 1, "martName": "A"}},
            {"_index": "p", "_id": "2", "_source": {"martId": 2, "martName": "B"}},
        ]
        result = self._fn()(raw)
        assert len(result) == 2
        assert result[0]["martId"] == 1
        assert result[1]["martName"] == "B"

    def test_unknown_format_raises(self):
        with pytest.raises(ValueError):
            self._fn()([{"noMartId": True}])

    def test_plain_array_preserves_all_fields(self):
        raw = [{"martId": 99, "price": 999.0, "isSearchable": 1}]
        result = self._fn()(raw)
        assert result[0]["price"] == 999.0
        assert result[0]["isSearchable"] == 1

    def test_search_hits_missing_source_skipped(self):
        """_source 缺失的 hit 應被略過（不 crash）."""
        raw = [
            {"_id": "1", "_source": {"martId": 1}},
            {"_id": "2"},   # 無 _source
        ]
        # detect_format 會看第一個元素有 _source → search_hits
        result = self._fn()(raw)
        assert len(result) == 1
        assert result[0]["martId"] == 1


# ---------- build_embed_text ----------

class TestBuildEmbedText:
    def _fn(self):
        return _mod("embed_products_os.py").build_embed_text

    def test_none_fields_produce_no_literal_none(self):
        """None 欄位不得在輸出中留下字面字串 'None'."""
        source = {
            "martName": "靈芝王",
            "feature": None,
            "keyword": None,
            "categoryLevel1Name": None,
            "categoryLevel2Name": None,
            "categoryLevel3Name": None,
        }
        text = self._fn()(source)
        assert "None" not in text
        assert "靈芝王" in text

    def test_missing_fields_produce_no_literal_none(self):
        """欄位完全缺失（dict 中無 key）也不得輸出 'None'."""
        source = {"martName": "保健品"}
        text = self._fn()(source)
        assert "None" not in text

    def test_html_stripped(self):
        source = {
            "martName": "測試商品",
            "feature": "<b>特色</b> 補充<br/>說明",
            "keyword": None,
        }
        text = self._fn()(source)
        assert "<b>" not in text
        assert "<br/>" not in text
        assert "特色" in text
        assert "補充" in text

    def test_multiple_spaces_compressed(self):
        source = {
            "martName": "商品   A",
            "feature": "特色  說明   文字",
        }
        text = self._fn()(source)
        assert "  " not in text  # 不應有連續兩個以上空白

    def test_truncated_to_50000_chars(self):
        mod = _mod("embed_products_os.py")
        long_text = "x" * 100_000
        source = {"martName": long_text}
        text = mod.build_embed_text(source)
        assert len(text) <= mod.MAX_EMBED_CHARS

    def test_truncate_boundary_exact(self):
        mod = _mod("embed_products_os.py")
        limit = mod.MAX_EMBED_CHARS
        source = {"martName": "a" * limit}
        text = mod.build_embed_text(source)
        assert len(text) == limit

    def test_all_fields_assembled(self):
        source = {
            "martName": "靈芝王",
            "feature": "增強免疫",
            "keyword": "保健",
            "categoryLevel1Name": "葡萄王",
            "categoryLevel2Name": "健康食品",
            "categoryLevel3Name": "飲料",
        }
        text = self._fn()(source)
        assert "靈芝王" in text
        assert "增強免疫" in text
        assert "保健" in text
        assert "葡萄王" in text
        assert "健康食品" in text
        assert "飲料" in text

    def test_keyword_7pct_null_scenario(self):
        """模擬約 7% keyword 為 null 的真實情境."""
        source = {
            "martName": "iPhone 15",
            "feature": "最新款手機",
            "keyword": None,   # 7% 案例
            "categoryLevel1Name": "通訊",
        }
        text = self._fn()(source)
        assert "None" not in text
        assert "iPhone 15" in text
        assert "最新款手機" in text

    def test_empty_source_returns_empty_string(self):
        text = self._fn()({})
        assert text == ""

    def test_html_in_feature_fully_removed(self):
        """純 HTML 的 feature（如只有 tag 無文字）輸出應無 tag 殘留."""
        source = {
            "martName": "A",
            "feature": "<div><p></p></div>",
        }
        text = self._fn()(source)
        assert "<" not in text
        assert ">" not in text


# ---------- strip_html 子函式 ----------

class TestStripHtml:
    def _fn(self):
        return _mod("embed_products_os.py").strip_html

    def test_basic_tag_removed(self):
        assert "<b>" not in self._fn()("<b>文字</b>")

    def test_empty_string(self):
        assert self._fn()("") == ""

    def test_pure_html_no_text(self):
        result = self._fn()("<div><p></p></div>")
        assert "<" not in result

    def test_text_preserved(self):
        result = self._fn()("<b>保健</b>飲品")
        assert "保健" in result
        assert "飲品" in result


# ---------- golden set loader（approved gate）----------

class TestLoadGoldenSet:
    def _fn(self):
        return _mod("verify_search_os.py").load_golden_set

    def _write_yaml(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "golden_set.yaml"
        p.write_text(dedent(content), encoding="utf-8")
        return p

    def test_approved_status_loads_successfully(self, tmp_path):
        p = self._write_yaml(
            tmp_path,
            """
            meta:
              status: approved
              approved_by: tester
              approved_at: "2026-06-12"
            queries:
              - id: q01
                query: 靈芝保健飲
                category: lexical_overlap
                expected_mart_ids: ["123456"]
                rationale: 詞面命中
            """,
        )
        data = self._fn()(p)
        assert data["meta"]["status"] == "approved"
        assert len(data["queries"]) == 1

    def test_draft_status_exits_with_nonzero(self, tmp_path):
        p = self._write_yaml(
            tmp_path,
            """
            meta:
              status: draft
              approved_by: null
              approved_at: null
            queries:
              - id: q01
                query: 靈芝保健飲
                category: lexical_overlap
                expected_mart_ids: ["123456"]
                rationale: 詞面命中
            """,
        )
        with pytest.raises(SystemExit) as exc_info:
            self._fn()(p)
        assert exc_info.value.code != 0

    def test_missing_status_exits(self, tmp_path):
        """meta 存在但無 status 欄位時應拒跑."""
        p = self._write_yaml(
            tmp_path,
            """
            meta:
              approved_by: null
            queries: []
            """,
        )
        with pytest.raises(SystemExit) as exc_info:
            self._fn()(p)
        assert exc_info.value.code != 0

    def test_approved_preserves_queries(self, tmp_path):
        p = self._write_yaml(
            tmp_path,
            """
            meta:
              status: approved
              approved_by: tester
              approved_at: "2026-06-12"
            queries:
              - id: q01
                query: 增強免疫力的飲料
                category: non_overlap
                expected_mart_ids: ["111", "222"]
                rationale: 語意對應靈芝/人蔘飲品
              - id: q02
                query: 靈芝保健飲
                category: lexical_overlap
                expected_mart_ids: ["333"]
                rationale: 詞面命中
            """,
        )
        data = self._fn()(p)
        assert len(data["queries"]) == 2
        assert data["queries"][0]["id"] == "q01"
        assert data["queries"][1]["category"] == "lexical_overlap"
