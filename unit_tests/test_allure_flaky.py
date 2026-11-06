# -*- coding: utf-8 -*-
"""观测与诊断 · flaky 结构化字段与 trace_file（构造临时 allure-results，不依赖真实执行）。

验收口径：
- list_cases 对含「UI重试轨迹」附件的批次结构化暴露 attempts/retried/flaky；
- flaky = 重试后通过（attempts>1 且 final_ok 且 status=passed）；重试仍失败 retried 但非 flaky；
- 存量无重试附件的用例缺省 attempts=1/retried=False/flaky=False（报告形状向后兼容）；
- case_detail 命中「UI失败trace」附件返回 trace_file（zip 源文件名），否则 None。

    .venv/Scripts/python.exe -m pytest unit_tests/test_allure_flaky.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.shared import allure_store  # noqa: E402


def _write_result(results_dir: Path, cid: str, status: str,
                  retry: dict | None = None, with_trace: bool = False) -> None:
    r = {
        "name": f"[P0] {cid} 用例描述",
        "status": status,
        "start": 1000,
        "stop": 2000,
        "statusDetails": {"message": "步骤失败" if status != "passed" else ""},
        "attachments": [],
    }
    if retry is not None:
        (results_dir / f"{cid}-retry.json").write_text(
            json.dumps(retry, ensure_ascii=False), encoding="utf-8")
        r["attachments"].append({"name": "UI重试轨迹", "source": f"{cid}-retry.json"})
    if with_trace:
        (results_dir / f"{cid}-trace.zip").write_bytes(b"PK\x03\x04zip")
        r["attachments"].append({"name": "UI失败trace", "source": f"{cid}-trace.zip"})
    (results_dir / f"{cid}-result.json").write_text(
        json.dumps(r, ensure_ascii=False), encoding="utf-8")


def _item(resp: dict, cid: str) -> dict:
    return next(x for x in resp["items"] if x["case_id"] == cid)


def test_flaky_structured_fields(tmp_path):
    # 重试后通过 → flaky
    _write_result(tmp_path, "ui_flaky", "passed",
                  retry={"attempts": 2, "final_ok": True,
                         "history": [{"attempt": 1, "ok": False}]})
    # 重试仍失败 → retried 但非 flaky
    _write_result(tmp_path, "ui_retried_fail", "failed",
                  retry={"attempts": 3, "final_ok": False,
                         "history": [{"attempt": 1, "ok": False}, {"attempt": 2, "ok": False}]})
    # 未重试 → 缺省字段
    _write_result(tmp_path, "ui_clean", "passed")

    resp = allure_store.list_cases(tmp_path)
    f = _item(resp, "ui_flaky")
    assert f["attempts"] == 2 and f["retried"] is True and f["flaky"] is True
    assert "〔重试 1 次后通过〕" in f["summary"]

    rf = _item(resp, "ui_retried_fail")
    assert rf["attempts"] == 3 and rf["retried"] is True and rf["flaky"] is False
    assert "〔重试 2 次仍未通过〕" in rf["summary"]

    c = _item(resp, "ui_clean")
    assert c["attempts"] == 1 and c["retried"] is False and c["flaky"] is False
    assert "重试" not in c["summary"]


def test_case_detail_trace_file(tmp_path):
    _write_result(tmp_path, "ui_with_trace", "failed", with_trace=True)
    _write_result(tmp_path, "ui_no_trace", "failed")

    d1 = allure_store.case_detail(tmp_path, "ui_with_trace")
    assert d1["trace_file"] == "ui_with_trace-trace.zip"

    d2 = allure_store.case_detail(tmp_path, "ui_no_trace")
    assert d2["trace_file"] is None


def test_flaky_shape_backward_compatible(tmp_path):
    """无重试附件的历史批次：新增字段有安全缺省，绝不 KeyError、summary 不追加标注。"""
    _write_result(tmp_path, "legacy", "passed")
    item = _item(allure_store.list_cases(tmp_path), "legacy")
    for key in ("attempts", "retried", "flaky"):
        assert key in item
    assert (item["attempts"], item["retried"], item["flaky"]) == (1, False, False)
