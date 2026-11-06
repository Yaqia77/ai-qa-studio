"""ai-qa-studio 多端自动化测试 CLI 入口。

用法示例:
    python main.py                                    # 使用 default_env + data/api_cases.xlsx
    python main.py --env test --file data/api_cases.xlsx
    python main.py --module 演示-文章CRUD              # 只跑指定模块
    python main.py --priority P0,P1                   # 只跑指定优先级
    python main.py --keyword 登录                      # 按关键字筛选
    python main.py --file proj_x.json --workers 2     # UI 批次并行（仅 web 引擎生效）
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import load_config  # noqa: E402
from core.engines import engine_for_file, get_engine  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Excel 驱动的接口自动化测试")
    parser.add_argument("--env", default=None, help="环境名，默认取 config.yaml 的 default_env")
    parser.add_argument("--file", default="data/api_cases.xlsx", help="用例 Excel 文件或目录（目录则合并所有 .xlsx）")
    parser.add_argument("--module", default=None, help="按模块名筛选（包含匹配）")
    parser.add_argument("--priority", default=None, help="按优先级筛选，逗号分隔，如 P0,P1")
    parser.add_argument("--keyword", default=None, help="按关键字筛选（用例编号/接口名称/用例描述/URL）")
    parser.add_argument("--cases", default=None, help="按用例编号精确选择，逗号分隔（优先于 --module/--priority/--keyword）")
    parser.add_argument("--vars", default=None, help="运行前变量覆盖，JSON 字符串")
    parser.add_argument("--workers", type=int, default=None,
                        help="UI 批次并行 worker 数（>=1，缺省读 config.yaml 的 ui.workers_default；非 UI 批次忽略）")
    parser.add_argument("--no-html", action="store_true", help="不生成 pytest-html 报告")
    parser.add_argument("--no-allure", action="store_true", help="不生成 allure 结果")
    return parser.parse_args()


def _effective_workers(requested, ui_cfg: dict) -> int:
    """并行度唯一折算点：请求值 > ui.workers_default > 1；双重上限：workers_max（缺省4）与本机核数。

    非法输入一律回到 1（串行）：并行是加速器不是正确性前提，退化必须无害。
    """
    def _int(v, d):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    n = _int(requested, _int((ui_cfg or {}).get("workers_default"), 1))
    n = max(1, min(n, _int((ui_cfg or {}).get("workers_max"), 4)))
    return max(1, min(n, os.cpu_count() or 1))


def main() -> None:
    args = parse_args()
    config = load_config()
    report_cfg = config.get("report", {})

    # 相对路径统一按项目根目录解析
    file_arg = Path(args.file)
    if not file_arg.is_absolute():
        file_arg = (ROOT / file_arg).resolve()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = ROOT / "reports" / f"run_{ts}"
    report_dir.mkdir(parents=True, exist_ok=True)

    # 按用例文件查注册表得到引擎，再按 engine.key 分发到对应 pytest 收集入口（.json → web，其余 → api）。
    # 无匹配回落 api，与旧硬编码 if/else 的分流结果完全一致；显式指定单文件避免两类收集器同批次互干扰。
    engine = engine_for_file(file_arg) or get_engine("api")
    test_target = str(ROOT / engine.test_module)
    print(f"[interface-auto] 引擎: {engine.key}（{engine.label}）→ {engine.test_module}")
    pytest_args = [test_target, "-v", "--tb=short"]

    # 并行：仅 web（UI）引擎拼 xdist 参数；依赖护栏与分组分片在 conftest 收集期决策。
    # loadgroup 下每个 xdist_group 钉在同一 worker 串行 → 束复用与依赖顺序都锁在片内。
    workers = _effective_workers(args.workers, config.get("ui") or {})
    if workers > 1 and engine.key == "web":
        pytest_args += ["-p", "xdist", "-n", str(workers), "--dist", "loadgroup", f"--workers={workers}"]
        print(f"[interface-auto] 并行 worker 数: {workers}（xdist loadgroup，按浏览器分组×分片；含依赖用例自动降级串行）")
    elif args.workers:
        print(f"[interface-auto] 并行仅支持 UI 项目（当前引擎 {engine.key}），本批次串行执行")

    if args.env:
        pytest_args += [f"--env={args.env}"]
    pytest_args += [f"--file={file_arg}"]
    if args.module:
        pytest_args += [f"--module={args.module}"]
    if args.priority:
        pytest_args += [f"--priority={args.priority}"]
    if args.keyword:
        pytest_args += [f"--keyword={args.keyword}"]
    if args.cases:
        pytest_args += [f"--cases={args.cases}"]
    if args.vars:
        pytest_args += [f"--vars={args.vars}"]
    if report_cfg.get("html", True) and not args.no_html:
        pytest_args += [f"--html={report_dir / 'report.html'}", "--self-contained-html"]
    if report_cfg.get("allure", True) and not args.no_allure:
        pytest_args += [f"--alluredir={report_dir / 'allure-results'}"]

    print(f"[interface-auto] 用例文件: {file_arg}")
    print(f"[interface-auto] 报告目录: {report_dir}")
    exit_code = pytest.main(pytest_args)

    print("=" * 60)
    html_report = report_dir / "report.html"
    if html_report.exists():
        print(f"[interface-auto] HTML 报告: {html_report}")
    allure_dir = report_dir / "allure-results"
    if allure_dir.exists():
        allure_cfg = ROOT / "allurerc.mjs"  # Allure 3 报告配置（reportLanguage=zh，打开即中文）
        cfg_arg = f' --config "{allure_cfg}"' if allure_cfg.exists() else ""
        print(f"[interface-auto] Allure 结果: {allure_dir}（执行 allure serve{cfg_arg} {allure_dir} 查看）")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
