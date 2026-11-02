# -*- coding: utf-8 -*-
"""upsert_many.written_names 契约单测（二期 P4：文档一键生成的数据回路）。

Agent 拿 written_names 逐接口生成用例，语义必须锁死：
- 新建 / 覆盖（同名 overwrite）/ 同名已存在 skipped —— 三种都算"有效登记"要回带；
- 被端点冲突拦截的**不算**（库里那个端点属于别人，按名生成会拿到错误契约）。
写面只有 api_def_store 这一个函数，直接打 tmp 数据文件即可，无需起 HTTP。
    .venv/Scripts/python.exe -m pytest unit_tests/test_api_def_writeback.py -v
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.api import api_def_store  # noqa: E402


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(api_def_store, "DATA_FILE", tmp_path / "api_defs.json")
    return tmp_path


def _rec(name, method="GET", path=None):
    return api_def_store.validate(name=name, module="测试", method=method,
                                  path=path or f"/api/{name}")


def test_written_names_新建与同名skipped都回带(isolated):
    api_def_store.upsert_many([_rec("接口A")], overwrite=False)          # 先落一条
    stats = api_def_store.upsert_many([_rec("接口A"), _rec("接口B")], overwrite=False)
    assert stats["created"] == 1 and stats["skipped"] == 1
    # 同名已存在（skipped）也算有效登记：接口库里确有这条定义，能据它生成用例
    assert sorted(stats["written_names"]) == ["接口A", "接口B"]


def test_written_names_覆盖同名也算(isolated):
    api_def_store.upsert_many([_rec("接口A")], overwrite=False)
    stats = api_def_store.upsert_many([_rec("接口A", method="POST")], overwrite=True)
    assert stats["updated"] == 1 and stats["written_names"] == ["接口A"]


def test_written_names_端点冲突拦截不算(isolated):
    api_def_store.upsert_many([_rec("接口A", path="/api/dup")], overwrite=False)
    stats = api_def_store.upsert_many(
        [_rec("接口B", path="/api/dup"), _rec("接口C", path="/api/ok")], overwrite=False)
    assert stats["skipped"] == 1 and len(stats["conflicts"]) == 1
    assert stats["written_names"] == ["接口C"]          # 冲突的接口B绝不回带
