# -*- coding: utf-8 -*-
"""并行执行：分组×分片与依赖护栏的纯逻辑单测（不真起浏览器、不跑 xdist）。

验收口径：
- 分片：组内 k=min(workers, 组内条数) 片轮转分配，用例不丢不重、组名含片号；
- 口径：分组键与 UiRunner 取池口径一致（browser 缺省 chromium、headless 缺省 True）；
- 护栏：批次命中 dependencies.json → 整批钉同一分片（软降级串行，序=收集序）；
- 折算：min(请求值, workers_max 缺省4, cpu_count)，非法输入回 1（串行无害退化）。

    .venv/Scripts/python.exe -m pytest unit_tests/test_ui_parallel.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(name: str, rel: str):
    """按文件加载无包结构的 conftest / main（顶层 import 均可安全重放）。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


conftest = _load("_ui_conftest", "tests/conftest.py")
main_mod = _load("_ui_main", "main.py")


def _case(cid: str, **over) -> dict:
    c = {"case_id": cid, "name": cid, "steps": []}
    c.update(over)
    return c


# ==================== 分组键口径 ====================


def test_spec_group_matches_runner_convention():
    """与 UiRunner.run 取池口径逐字一致：browser 缺省 chromium、headless 缺省 True、大小写归一。"""
    assert conftest._ui_spec_group(_case("a")) == "chromium|True"
    assert conftest._ui_spec_group(_case("a", browser="Chromium")) == "chromium|True"
    assert conftest._ui_spec_group(_case("a", headless=False)) == "chromium|False"
    assert conftest._ui_spec_group(_case("a", browser="firefox", headless=True)) == "firefox|True"
    # channel 不进键：内核回退是 launch 层细节，不改变分组（与池 spec_key=(browser,headless,channel or "") 中
    # 执行侧仅传 browser/headless 的现状一致）
    assert conftest._ui_spec_group(_case("a", channel="chrome")) == "chromium|True"


# ==================== 分组×分片 ====================


def test_single_group_shards_evenly_roundrobin():
    """单组 6 条 × workers 2 → 2 片各 3 条，轮转分配（偶数位片0、奇数位片1），不丢不重。"""
    cases = [_case(f"t{i}") for i in range(6)]
    plan = conftest.build_shard_plan(cases, 2)
    assert [g for g, _ in plan] == ["chromium|True#0", "chromium|True#1"]
    assert [[c["case_id"] for c in cs] for _, cs in plan] == [["t0", "t2", "t4"], ["t1", "t3", "t5"]]


def test_shard_count_is_min_workers_groupsize():
    """组内条数不足 worker 数时不产生空片：1 条 → 1 片；4 条 × 8 → 4 片各 1 条。"""
    plan = conftest.build_shard_plan([_case("x")], 4)
    assert len(plan) == 1 and len(plan[0][1]) == 1
    plan = conftest.build_shard_plan([_case(f"t{i}") for i in range(4)], 8)
    assert len(plan) == 4 and all(len(cs) == 1 for _, cs in plan)


def test_multi_group_plan_launch_bound():
    """多组：各组独立分片，分片总数 = Σ min(workers, 组内条数)（=并行批次的浏览器束启动上界）。"""
    cases = ([_case(f"a{i}", headless=True) for i in range(5)]
             + [_case(f"b{i}", headless=False) for i in range(2)])
    plan = conftest.build_shard_plan(cases, 3)
    assert [g for g, _ in plan] == ["chromium|True#0", "chromium|True#1", "chromium|True#2",
                                    "chromium|False#0", "chromium|False#1"]
    assert sum(len(cs) for _, cs in plan) == len(cases)  # 不丢
    ids = [c["case_id"] for _, cs in plan for c in cs]
    assert len(set(ids)) == len(ids)  # 不重


def test_plan_is_deterministic_across_processes():
    """收集期算法确定性：同一输入两次调用产出完全一致（controller 与各 worker 分别收集必须同 plan）。"""
    cases = [_case(f"t{i}", browser=("chromium" if i % 2 else "firefox")) for i in range(7)]
    assert conftest.build_shard_plan(cases, 3) == conftest.build_shard_plan(cases, 3)


# ==================== 依赖护栏（软降级串行） ====================


def test_deps_involved_detection():
    ids = {"a", "b", "c"}
    assert conftest._deps_involved(ids, {"a": ["x"]}) is True            # 声明了批外依赖也算卷入
    assert conftest._deps_involved(ids, {"x": ["b"]}) is False           # 批外用例依赖批内：与本批执行序无关
    assert conftest._deps_involved(ids, {"a": ["b"]}) is True            # 批内互相依赖
    assert conftest._deps_involved(ids, {"x": ["y"], "a": []}) is False  # 空依赖列表不算
    assert conftest._deps_involved(ids, {}) is False


def test_parallel_params_dep_guard_pins_all_to_one_shard():
    """命中依赖：整批钉 ui-serial#0，且保持收集序（拓扑序由 _ui_topo_order 先行保证）。"""
    cases = [_case("login"), _case("use"), _case("other")]
    params = conftest.parallel_params(cases, 3, {"use": ["login"]})
    groups = [p.marks[0].args[0] for p in params]
    assert groups == ["ui-serial#0"] * 3
    assert [p.id for p in params] == ["login", "use", "other"]


def test_parallel_params_no_dep_shards_with_marks():
    """无依赖：每条 param 带 xdist_group mark，片号来自分片函数；id=case_id。"""
    cases = [_case(f"t{i}") for i in range(4)]
    params = conftest.parallel_params(cases, 2, {})
    assert [p.id for p in params] == ["t0", "t1", "t2", "t3"]
    assert [p.marks[0].name for p in params] == ["xdist_group"] * 4
    assert [p.marks[0].args[0] for p in params] == ["chromium|True#0", "chromium|True#1"] * 2


# ==================== 并行度折算（main._effective_workers） ====================


def test_effective_workers_precedence_and_clamp():
    cpu = os.cpu_count() or 1
    assert main_mod._effective_workers(None, {}) == 1                       # 缺省串行（存量行为）
    assert main_mod._effective_workers(2, {"workers_default": 3}) == 2      # 请求值优先
    assert main_mod._effective_workers(None, {"workers_default": 2}) == 2   # 批次默认
    assert main_mod._effective_workers(9, {}) == min(4, cpu)                # 上限截断（workers_max 缺省 4）
    assert main_mod._effective_workers(0, {}) == 1                          # 0/负数回 1
    assert main_mod._effective_workers("x", {}) == 1                        # 非法回 1
    assert main_mod._effective_workers(3, {"workers_max": 2}) == 2          # 配置上限可调低


def test_effective_workers_cpu_capped():
    """机器核数是最终硬闸：workers_max 调再高也不超 cpu_count。"""
    cpu = os.cpu_count() or 1
    assert main_mod._effective_workers(64, {"workers_max": 64}) == cpu
