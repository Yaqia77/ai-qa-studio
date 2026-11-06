# -*- coding: utf-8 -*-
"""进程内浏览器实例池（执行提速的核心）。

现状痛点：UiRunner 每条用例独立 `sync_playwright().start() → launch → ... → 关闭`，
100 条用例即启停 100 次 playwright + 浏览器进程——浏览器冷启动是与用例复杂度无关的
固定开销，被 N 倍放大；偶发启动抖动还会把「环境噪声」记成「用例失败」。

本池只折叠「浏览器进程」这一层开销，**绝不折叠「context」这层隔离**：每条用例依旧
`new_context`（可带 storage_state），cookie/localStorage/会话 attach/extract 变量池、
base_url 解析语义与原先完全一致。

- BrowserBundle：一个 (共享 playwright, browser) 组合 + spec_key + dead 标记，生命周期归单 pytest 进程；
- BrowserPool：按 spec_key=(browser, headless, channel) 复用；`reap_after` 安全阀（跑满 N 条主动回收，
  防长驻内存/句柄膨胀）；`is_connected()` 探活 + `invalidate()` 覆盖「浏览器中途崩溃」；
- **playwright 实例全池唯一**：Playwright Sync API 在同一线程只允许存在一个存活实例，
  第二个 `sync_playwright().start()` 会抛「Sync API inside the asyncio loop」（实测复现，
  典型触发：headless 束存活时再起 headful 束）。headless 开关是 launch 参数而非实例属性，
  一个实例即可派生任意 (browser × headless × channel) 组合，故 bundle 共享池级 pw、
  各自只持有 browser 进程；束回收只关 browser，pw 由 close_all 统一停止；
- 启动口径复用 UiRunner._launch（自带内核缺失 → 自动回退本机 Chrome），全项目单一launch事实来源；
- launch_fn / pw_factory 可注入（单测喂假 driver，不真起浏览器）。

core 侧模块，不依赖 backend。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional, Tuple

from loguru import logger

# spec_key：(浏览器名, 无头开关, 通道)。channel 缺省空串=Playwright 默认解析
# （内核缺失回退本机 Chrome 的细节由 launch 层处理，不进键——同一 (browser, headless) 共用一束）。
SpecKey = Tuple[str, bool, str]


def _default_pw_start():
    """真启动：起全池唯一的 sync_playwright 实例。"""
    from playwright.sync_api import sync_playwright

    return sync_playwright().start()


def _default_launch(pw, browser: str, headless: bool, channel: Optional[str]):
    """在共享 pw 上按 UiRunner 口径 launch 一个 browser 进程（含本机 Chrome 回退）。"""
    from core.engines.web.engine import UiRunner  # 懒导入：launch 回退口径单一事实来源

    return UiRunner._launch(pw, browser, headless, channel=channel)


class BrowserBundle:
    """一束复用的浏览器实例：底层是 (共享 playwright, browser)，用例各自从它开新 context。"""

    def __init__(self, pw, browser, spec: SpecKey,
                 on_close: Optional[Callable[["BrowserBundle"], None]] = None):
        self.pw = pw                          # 池级共享实例：bundle 只引用，不负责停止
        self.browser = browser
        self.spec = spec
        self.dead = False                      # 执行期发现崩溃/断连由 UiRunner 置位
        self.closed = False                    # 本束是否已被回收（幂等守卫）
        self._on_close = on_close

    def new_context(self, storage_state: Optional[str] = None):
        """用例级隔离入口：每次全新 context（可加载登录态），隔离语义与原先逐字一致。"""
        if storage_state:
            return self.browser.new_context(storage_state=storage_state)
        return self.browser.new_context()

    def is_alive(self) -> bool:
        """探活：未被标 dead/回收，且 Playwright 报告连接仍在。任何异常按「已死」处理。"""
        if self.dead or self.closed:
            return False
        try:
            return bool(self.browser.is_connected())
        except Exception:  # noqa: BLE001 已断连的 driver 调 is_connected 也可能炸
            return False

    def close(self) -> None:
        """回收本束：只关 browser 进程，共享 pw 归池统一停止；幂等，失败只留日志不抛。"""
        if self.closed:
            return
        self.closed = True
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("关闭 browser 失败（忽略）: {}", e)
        if self._on_close is not None:
            try:
                self._on_close(self)
            except Exception:  # noqa: BLE001
                pass


class BrowserPool:
    """单 pytest 进程内的浏览器束登记表：共享 pw + 按 spec 复用 + reap 安全阀 + dead 重启。"""

    def __init__(self, reap_after: int = 200,
                 launch_fn: Optional[Callable[..., object]] = None,
                 pw_factory: Optional[Callable[[], object]] = None):
        # 每束最多连续服务 reap_after 条用例，到量主动回收重启（安全阀，防越跑越慢/崩）。
        self.reap_after = max(1, int(reap_after))
        self._launch_fn = launch_fn or _default_launch
        self._pw_factory = pw_factory or _default_pw_start
        self._table_mutex = threading.Lock()   # 保护登记表与共享 pw 的取用/重建
        self._spec_locks: dict[SpecKey, threading.Lock] = {}
        self._bundles: dict[SpecKey, BrowserBundle] = {}
        self._counts: dict[SpecKey, int] = {}
        self._pw = None                        # 全池唯一 playwright 实例（懒启动）
        self.launch_count = 0                  # 实际 launch 次数（验收：应等于分组数而非用例数）

    def _lock_for(self, spec: SpecKey) -> threading.Lock:
        with self._table_mutex:
            return self._spec_locks.setdefault(spec, threading.Lock())

    def _ensure_pw(self):
        with self._table_mutex:
            if self._pw is None:
                self._pw = self._pw_factory()
            return self._pw

    def _reset_pw(self) -> None:
        """丢弃共享 pw（疑似已死）：停旧实例并置空，下次 _ensure_pw 重建。"""
        with self._table_mutex:
            pw, self._pw = self._pw, None
        if pw is not None:
            try:
                pw.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning("停止疑似已死的 playwright 实例失败（忽略）: {}", e)

    def acquire(self, browser: str = "chromium", headless: bool = True,
                channel: Optional[str] = None) -> BrowserBundle:
        """取一束可用浏览器：命中且存活即复用；缺束/已死/到 reap 阈值则（重）启动。

        同 spec 串行启动（锁内双检），不同 spec 互不阻塞。所有 bundle 共享同一 pw；
        若在共享 pw 上启动失败（疑似实例失联），重建 pw 后重试一次，仍失败原样抛出，
        由调用方（UiRunner）统一折算成用例失败与中文指引。
        """
        spec = (str(browser or "chromium").lower(), bool(headless), channel or "")
        with self._lock_for(spec):
            bundle = self._bundles.get(spec)
            if bundle is not None:
                if not bundle.is_alive():
                    logger.warning("[浏览器池] spec={} 已失效，回收重启", spec)
                    self._retire(spec, bundle)
                elif self._counts.get(spec, 0) >= self.reap_after:
                    logger.info("[浏览器池] spec={} 已跑满 {} 条，安全阀回收重启", spec, self.reap_after)
                    self._retire(spec, bundle)
            bundle = self._bundles.get(spec)
            if bundle is None:
                br = self._launch_on_pw(spec)
                bundle = BrowserBundle(self._pw, br, spec, on_close=lambda b: self._forget(spec, b))
                with self._table_mutex:
                    self._bundles[spec] = bundle
                    self._counts[spec] = 0
                self.launch_count += 1
                logger.info("[浏览器池] 新启浏览器束 spec={}（累计启动 {} 次）", spec, self.launch_count)
            self._counts[spec] = self._counts.get(spec, 0) + 1
            return bundle

    def _launch_on_pw(self, spec: SpecKey):
        """在共享 pw 上 launch；失败疑似 pw 死 → 重建 pw 重试一次。"""
        pw = self._ensure_pw()
        try:
            return self._launch_fn(pw, browser=spec[0], headless=spec[1], channel=spec[2] or None)
        except Exception as e:  # noqa: BLE001
            if self._pw is None:
                raise  # 已在别处重置过，不再重试，直接上抛
            logger.warning("[浏览器池] 共享 playwright 实例上启动失败（{}），重建实例后重试一次",
                           str(e).splitlines()[0] if str(e) else type(e).__name__)
            self._reset_pw()
            return self._launch_fn(self._ensure_pw(), browser=spec[0], headless=spec[1], channel=spec[2] or None)

    def invalidate(self, bundle: BrowserBundle) -> None:
        """执行侧发现浏览器中途崩溃/断连的显式标记通道：置 dead，下次 acquire 触发重启。"""
        bundle.dead = True

    def close_all(self) -> None:
        """批次收尾：统一回收所有束并停止共享 pw（进程退出即调用，不留长驻浏览器进程）。"""
        with self._table_mutex:
            bundles = list(self._bundles.values())
            self._bundles.clear()
            self._counts.clear()
        for b in bundles:
            b.close()
        self._reset_pw()
        if bundles:
            logger.info("[浏览器池] 批次结束，已回收 {} 束浏览器", len(bundles))

    def _retire(self, spec: SpecKey, bundle: BrowserBundle) -> None:
        with self._table_mutex:
            if self._bundles.get(spec) is bundle:
                del self._bundles[spec]
                self._counts.pop(spec, None)
        bundle.close()

    def _forget(self, spec: SpecKey, bundle: BrowserBundle) -> None:
        """束自报已关闭（如外部直接 close）时清账，避免登记表悬挂死条目。"""
        with self._table_mutex:
            if self._bundles.get(spec) is bundle:
                del self._bundles[spec]
                self._counts.pop(spec, None)
