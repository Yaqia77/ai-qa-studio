"""pytest 配置：自定义命令行参数、用例加载、Session 级fixture。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from loguru import logger  # noqa: E402

from core.context import Context, get_env_config, load_config  # noqa: E402
from core.excel_loader import load as load_excel  # noqa: E402
from core.excel_loader import load_secret_vars  # noqa: E402
from core.http_client import HttpExecutor  # noqa: E402
from core.log_setup import setup_logging  # noqa: E402

_LOAD_CACHE: dict = {}
_COLL_LOG_READY = False


def pytest_addoption(parser):
    parser.addoption("--env", default=None, help="环境名（对应 config.yaml 的 env 段，默认取 default_env）")
    parser.addoption("--file", default=str(ROOT / "data" / "api_cases.xlsx"), help="用例 Excel 文件或目录")
    parser.addoption("--module", default=None, help="按模块名筛选（包含匹配）")
    parser.addoption("--priority", default=None, help="按优先级筛选，逗号分隔，如 P0,P1")
    parser.addoption("--keyword", default=None, help="按关键字筛选（匹配 用例编号/接口名称/用例描述/URL）")
    parser.addoption("--cases", default=None, help="按用例编号精确选择，逗号分隔（优先于 --module/--priority/--keyword）")
    parser.addoption("--vars", default=None, help='运行前变量覆盖，JSON 对象字符串（覆盖同名 secret/Excel/YAML 变量，优先级低于运行时提取）')
    parser.addoption("--workers", default=None, help="UI 批次并行 worker 数（>1 时按浏览器分组×分片打 xdist_group；缺省 1=串行）")


def _parse_vars(raw):
    """解析 --vars JSON 为 dict；非法输入直接终止收集期报错，避免静默忽略覆盖。"""
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        raise pytest.UsageError(f"--vars 不是合法 JSON：{raw!r}")
    if not isinstance(obj, dict):
        raise pytest.UsageError("--vars 必须是 JSON 对象（键值映射）")
    out = {}
    for k, v in obj.items():
        out[str(k)] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return out


def _load_cases(file_arg: str):
    key = str(Path(file_arg).resolve())
    if key not in _LOAD_CACHE:
        p = Path(file_arg)
        if p.suffix.lower() in (".json", ".apjson"):
            # UI/App 项目走 JSON 用例，无 Excel 全局变量：给一个空壳供 context fixture 复用
            _LOAD_CACHE[key] = _UiData()
        else:
            _LOAD_CACHE[key] = load_excel(p)
    return _LOAD_CACHE[key]


class _UiData:
    """非 Excel 文件（UI/App）下 excel_data 的替身：无 Excel 全局变量表，variables 恒空。"""
    cases: list = []
    variables: dict = {}


def _load_ui_cases(file_arg: str) -> list:
    from backend.ui import ui_case_store
    return ui_case_store.load_cases(Path(file_arg))


def _load_app_cases(file_arg: str) -> list:
    from backend.app import app_case_store
    return app_case_store.load_cases(Path(file_arg))


def _load_dep_map() -> dict:
    """读用例依赖映射（data/dependencies.json，平台「配置依赖」写入）；缺失/损坏返回空。"""
    dep_file = ROOT / "data" / "dependencies.json"
    if not dep_file.exists():
        return {}
    try:
        data = json.loads(dep_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _expand_deps(selected: list, all_cases: list, dep_map: dict) -> list:
    """条件执行时自动纳入被依赖的用例（传递闭包，仅限 run=True）。

    解决痛点：按模块/优先级筛选时，提取 token 的登录用例没被选中，
    下游用例报「变量未定义」。依赖被停用（run=N）时不纳入，
    尊重用户的显式停用意图（也符合停用用例不进报告的既有约定）。
    """
    by_id = {c.case_id: c for c in all_cases}
    picked = {c.case_id for c in selected}
    out = list(selected)
    stack = [c.case_id for c in selected]
    while stack:
        cid = stack.pop()
        for d in dep_map.get(cid) or []:
            if d in picked:
                continue
            dep_case = by_id.get(d)
            if dep_case is not None and dep_case.run:
                picked.add(d)
                out.append(dep_case)
                stack.append(d)
    return out


def _topo_order(selected: list, dep_map: dict) -> list:
    """稳定拓扑排序：依赖用例排到前面，无依赖关系时保持 Excel 行序。

    手工编辑的 dependencies.json 可能成环：沿链带 chain 集合，命中即断开，
    不崩不丢（成环时顺序按行序，运行时由阻塞检查兜底）。
    """
    picked = {c.case_id for c in selected}
    by_id = {c.case_id: c for c in selected}
    ordered: list = []
    done: set = set()

    def emit(cid: str, chain: set) -> None:
        if cid in done or cid in chain:
            return
        chain.add(cid)
        for d in dep_map.get(cid) or []:
            if d in picked:
                emit(d, chain)
        chain.discard(cid)
        if cid not in done:
            done.add(cid)
            ordered.append(by_id[cid])

    for c in selected:
        emit(c.case_id, set())
    return ordered


def pytest_generate_tests(metafunc):
    """按 Excel 行顺序生成参数化用例（严格保持顺序，保证接口依赖可用）。

    声明了依赖（dependencies.json）时：条件执行自动纳入未选中的启用依赖用例，
    并把全部选中用例拓扑排序到依赖先行的顺序（--cases 精确选择不补全，仅排序）。
    """
    if "ui_case" in metafunc.fixturenames:
        _generate_ui_cases(metafunc)
        return
    if "app_case" in metafunc.fixturenames:
        _generate_app_cases(metafunc)
        return
    if "api_case" not in metafunc.fixturenames:
        return
    data = _load_cases(metafunc.config.getoption("--file"))

    module = metafunc.config.getoption("--module")
    priority = metafunc.config.getoption("--priority")
    keyword = metafunc.config.getoption("--keyword")
    cases_opt = metafunc.config.getoption("--cases")
    pri_set = {p.strip().upper() for p in priority.split(",") if p.strip()} if priority else None
    id_set = {c.strip() for c in cases_opt.split(",") if c.strip()} if cases_opt else None

    selected = []
    for case in data.cases:
        if id_set is not None:
            # 精确选择：仅按编号命中，保持 Excel 行序（依赖顺序优先于勾选顺序）
            if case.case_id in id_set:
                selected.append(case)
            continue
        # 按条件/全量执行时排除「是否执行=N」的用例：它们不该被收集，
        # 否则会在 test_api 里被 pytest.skip，进而在报告中留下「已跳过」条目污染结果。
        # （--cases 精确选择是显式意图，走上面的分支不受此限。）
        if not case.run:
            continue
        if module and module not in case.module:
            continue
        if pri_set and case.priority.upper() not in pri_set:
            continue
        if keyword:
            haystack = " ".join([case.case_id, case.api_name, case.case_desc, case.url])
            if keyword not in haystack:
                continue
        selected.append(case)

    if not selected:
        hint = "--cases" if id_set is not None else "--file/--module/--priority/--keyword"
        raise pytest.UsageError(f"没有匹配到任何用例，请检查 {hint} 筛选条件")

    dep_map = _load_dep_map()
    if dep_map:
        if id_set is None:
            # 条件/全量执行：补全启用状态的依赖用例（--cases 精确选择保持现状不补全）
            selected = _expand_deps(selected, data.cases, dep_map)
        selected = _topo_order(selected, dep_map)
    metafunc.parametrize("api_case", selected, ids=[c.case_id for c in selected])


def _generate_ui_cases(metafunc):
    """UI 用例参数化（来自项目 JSON）：筛选口径与 api 一致（run/module/priority/keyword/cases）。

    依赖拓扑排序与补全沿用 dependencies.json（与接口用例同一机制）。
    """
    file_arg = metafunc.config.getoption("--file")
    cases = _load_ui_cases(file_arg)

    module = metafunc.config.getoption("--module")
    priority = metafunc.config.getoption("--priority")
    keyword = metafunc.config.getoption("--keyword")
    cases_opt = metafunc.config.getoption("--cases")
    pri_set = {p.strip().upper() for p in priority.split(",") if p.strip()} if priority else None
    id_set = {c.strip() for c in cases_opt.split(",") if c.strip()} if cases_opt else None

    selected = []
    for c in cases:
        cid = c.get("case_id", "")
        if id_set is not None:
            if cid in id_set:
                selected.append(c)
            continue
        if not c.get("run", True):
            continue
        if module and module not in str(c.get("module", "")):
            continue
        if pri_set and str(c.get("priority", "")).upper() not in pri_set:
            continue
        if keyword:
            kw = keyword.strip().lower()
            hay = " ".join([str(c.get(f, "")) for f in ("case_id", "name", "desc")]).lower()
            if kw not in hay:
                continue
        selected.append(c)

    if not selected:
        hint = "--cases" if id_set is not None else "--file/--module/--priority/--keyword"
        raise pytest.UsageError(f"没有匹配到任何 UI 用例，请检查 {hint} 筛选条件")

    dep_map = _load_dep_map()
    if dep_map:
        selected = _ui_topo_order(selected, dep_map)

    # 并行：workers>1 时按浏览器分组×分片打 xdist_group（依赖护栏在 parallel_params 内）；
    # workers=1（缺省）走原参数化调用，收集产物形状不变。
    workers = _workers_opt(metafunc.config)
    if workers > 1:
        metafunc.parametrize("ui_case", parallel_params(selected, workers, dep_map))
    else:
        metafunc.parametrize("ui_case", selected, ids=[c.get("case_id", "") for c in selected])


def _generate_app_cases(metafunc):
    """App 用例参数化（来自项目 .apjson）：筛选与依赖拓扑口径与 UI 完全一致（run/module/priority/keyword/cases）。

    当前仅搭脚手架：用例可被收集，执行由 test_app.py 统一 skip（Appium 驱动未接入）。
    """
    file_arg = metafunc.config.getoption("--file")
    cases = _load_app_cases(file_arg)

    module = metafunc.config.getoption("--module")
    priority = metafunc.config.getoption("--priority")
    keyword = metafunc.config.getoption("--keyword")
    cases_opt = metafunc.config.getoption("--cases")
    pri_set = {p.strip().upper() for p in priority.split(",") if p.strip()} if priority else None
    id_set = {c.strip() for c in cases_opt.split(",") if c.strip()} if cases_opt else None

    selected = []
    for c in cases:
        cid = c.get("case_id", "")
        if id_set is not None:
            if cid in id_set:
                selected.append(c)
            continue
        if not c.get("run", True):
            continue
        if module and module not in str(c.get("module", "")):
            continue
        if pri_set and str(c.get("priority", "")).upper() not in pri_set:
            continue
        if keyword:
            kw = keyword.strip().lower()
            hay = " ".join([str(c.get(f, "")) for f in ("case_id", "name", "desc")]).lower()
            if kw not in hay:
                continue
        selected.append(c)

    if not selected:
        hint = "--cases" if id_set is not None else "--file/--module/--priority/--keyword"
        raise pytest.UsageError(f"没有匹配到任何 App 用例，请检查 {hint} 筛选条件")

    dep_map = _load_dep_map()
    if dep_map:
        selected = _ui_topo_order(selected, dep_map)
    metafunc.parametrize("app_case", selected, ids=[c.get("case_id", "") for c in selected])


def _ui_topo_order(selected: list, dep_map: dict) -> list:
    """UI/App 用例依赖拓扑排序（依赖先行，其余保持 JSON 原序）。"""
    by_id = {c.get("case_id"): c for c in selected}
    picked = set(by_id)
    ordered: list = []
    done: set = set()

    def emit(cid, chain):
        if cid in done or cid in chain:
            return
        chain.add(cid)
        for d in dep_map.get(cid) or []:
            if d in picked:
                emit(d, chain)
        chain.discard(cid)
        if cid not in done:
            done.add(cid)
            ordered.append(by_id[cid])

    for c in selected:
        emit(c.get("case_id"), set())
    return ordered


# ==================== 并行：分组×分片（纯函数，供单测直接喂用例列表） ====================


def _workers_opt(config) -> int:
    """读 --workers；非法一律回 1（串行）：并行是加速器不是正确性前提，退化必须无害。"""
    try:
        return max(1, int(config.getoption("--workers") or 1))
    except (TypeError, ValueError):
        return 1


def _ui_spec_group(case: dict) -> str:
    """xdist 分组键：与 UiRunner 取池口径逐字一致（engine.run：browser 缺省 chromium、headless 缺省 True），
    保证同一束浏览器钉在同一 worker。channel 不进键（内核回退在 launch 层，不改变池 spec_key）。"""
    browser = str(case.get("browser") or "chromium").lower()
    headless = bool(case.get("headless", True))
    return f"{browser}|{headless}"


def build_shard_plan(selected: list, workers: int) -> list:
    """按 spec 分组后，每组拆 k=min(workers, 组内条数) 片轮转分配（收集期确定、跨进程可复现）。

    返回 [(xdist_group名, [用例dict...])...]；全批浏览器束启动数==分片总数（并行验收口径）。
    """
    groups: dict = {}
    for c in selected:
        groups.setdefault(_ui_spec_group(c), []).append(c)
    plan = []
    for gname, cases in groups.items():
        k = max(1, min(int(workers), len(cases)))
        shards: list = [[] for _ in range(k)]
        for i, c in enumerate(cases):
            shards[i % k].append(c)
        for j, sh in enumerate(shards):
            plan.append((f"{gname}#{j}", sh))
    return plan


def _deps_involved(ids: set, dep_map: dict) -> bool:
    """本批选中用例是否卷入依赖（作为 key 声明了依赖，或被他人在批内依赖）。"""
    for cid in ids:
        if dep_map.get(cid):
            return True
    for cid, deps in (dep_map or {}).items():
        if cid in ids and any(d in ids for d in (deps or [])):
            return True
    return False


def _ensure_collect_logging() -> None:
    """收集期日志需要文件 sink（护栏降级/分组计划要落 logs/ 供冒烟核验）；每进程最多初始化一次。"""
    global _COLL_LOG_READY
    if _COLL_LOG_READY:
        return
    _COLL_LOG_READY = True
    try:
        setup_logging(load_config().get("log", {}).get("level", "INFO"))
    except Exception:  # noqa: BLE001
        pass


def parallel_params(selected: list, workers: int, dep_map: dict) -> list:
    """并行收集产物：每条用例带 xdist_group mark 的 pytest.param 列表（纯函数可单测）。

    依赖护栏：命中 dependencies.json → 整批钉进同一分片（软降级串行）：loadgroup 下同片
    同 worker 按收集序（已拓扑）执行，case_results 登记表与阻塞检查语义完整保留。
    """
    ids = {str(c.get("case_id", "")) for c in selected}
    note = ""
    if _deps_involved(ids, dep_map or {}):
        plan = [("ui-serial#0", list(selected))]
        note = "（含依赖用例，软降级为串行：整批钉同一 worker，保持拓扑序与结果登记表语义）"
    else:
        plan = build_shard_plan(selected, workers)
    _ensure_collect_logging()
    logger.info("[并行] worker 上限 {}，分组分片计划：{}{}", workers,
                "；".join(f"{g} → {len(cs)} 条" for g, cs in plan), note)
    group_of: dict = {}  # id(case) -> 片名：按 selected 原序产出 param，不扰动拓扑序/行序
    for gname, cases in plan:
        for c in cases:
            group_of[id(c)] = gname
    return [
        pytest.param(c, marks=[pytest.mark.xdist_group(group_of[id(c)])],
                     id=str(c.get("case_id", "")))
        for c in selected
    ]


@pytest.fixture(scope="session")
def app_config(request):
    """加载 config.yaml 并初始化日志。"""
    config = load_config()
    env_name = request.config.getoption("--env")
    env_cfg = get_env_config(config, env_name)
    setup_logging(config.get("log", {}).get("level", "INFO"))
    return {"config": config, "env": env_cfg}


@pytest.fixture(scope="session")
def excel_data(request):
    """Excel 解析结果（用例 + 全局变量），进程内缓存。"""
    return _load_cases(request.config.getoption("--file"))


@pytest.fixture(scope="session")
def context(request, app_config, excel_data):
    """Session 级共享变量池。

    优先级：运行时提取值 > 凭证文件 secret_vars > 用例 Excel 全局变量 > YAML 配置。
    """
    yaml_vars = {"base_url": app_config["env"].get("base_url", "")}
    secret_path = Path(app_config["config"].get("secret_vars_file", "data/secret_vars.xlsx"))
    if not secret_path.is_absolute():
        secret_path = ROOT / secret_path
    merged_vars = dict(excel_data.variables)
    merged_vars.update(load_secret_vars(secret_path))  # 凭证文件覆盖用例文件同名变量
    overrides = _parse_vars(request.config.getoption("--vars"))
    if overrides:
        merged_vars.update(overrides)  # 运行前覆盖：高于 secret/Excel/YAML，低于运行时提取
    return Context(yaml_vars, merged_vars)


@pytest.fixture(scope="session")
def executor(app_config):
    """Session 级 HTTP 执行器。"""
    env = app_config["env"]
    return HttpExecutor(
        base_url=env.get("base_url", ""),
        default_headers=env.get("default_headers"),
        default_timeout=env.get("timeout", 15),
        verify_ssl=env.get("verify_ssl", True),
    )


@pytest.fixture(scope="session")
def dep_map():
    """加载用例依赖映射（data/dependencies.json）。"""
    dep_file = ROOT / "data" / "dependencies.json"
    if not dep_file.exists():
        return {}
    try:
        with open(dep_file, encoding="utf-8") as fp:
            return json.load(fp)
    except (json.JSONDecodeError, OSError):
        return {}


@pytest.fixture(scope="session")
def case_results():
    """Session 级用例结果登记表，供依赖链检查。"""
    return {}  # {case_id: "passed" | "failed" | "skipped" | "error"}


@pytest.fixture(scope="session")
def ui_session_registry():
    """UI 登录态会话的进程内登记表（登录态复用）。

    后端批次是 subprocess 起 main.py（每批一个 pytest 进程），故「过期就地重采」只在本进程内去重：
    同一会话在本进程最多重采一次，避免一批 100 条引用同一会话的用例触发 100 次重登。
    结构由 backend.ui.session_collect 自管理：{"_locks": {key: Lock}, "_renewed": {key: True|err}}。
    """
    return {"_locks": {}, "_renewed": {}}


@pytest.fixture(scope="session")
def ui_browser_pool():
    """UI 浏览器实例池（执行提速）。

    生命周期归单 pytest 进程（批次 = subprocess 起 main.py，每批一进程）：
    同 (browser, headless) 分组只 launch 一次，每条用例仅新开 context（隔离语义不变），
    批次收尾 close_all 统一回收，不留长驻浏览器进程。
    采集链路不注入本池（session_collect 直接执行 UiRunner 无 pool），崩溃不牵连批次。
    """
    from core.engines.web.browser_pool import BrowserPool

    pool = BrowserPool()
    try:
        yield pool
    finally:
        pool.close_all()
