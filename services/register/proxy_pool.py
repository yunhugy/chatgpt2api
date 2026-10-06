"""注册代理池（订阅式）：从本地文件或 URL 拉取代理列表，按任务轮换使用。

来源支持：
- 本地文件路径（推荐，容器内挂载只读）
- file:// URL
- http(s) URL（后台线程按 refresh_interval 定时刷新，刷新失败保留旧列表）

调度策略（按实测质量分档轮换）：
- 已验证档：成功过且没有失败记录的出口，优先复用
- 未使用档：还没跑过任务的出口
- 风险档：失败过的出口（失败 1 次即降权，退到未使用档之后）
- 同一 host 失败 2 次进入黑名单，不再分配；成功后清零并解除黑名单
- 全部出口都被拉黑时清空黑名单重新轮换，并打印醒目告警 + 计数（避免静默空转）

线程安全；模块级函数接口：init_proxy_pool() / next_proxy() / status() / report()
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

try:
    from curl_cffi import requests as _requests
except Exception:  # pragma: no cover
    _requests = None


def _host_of(proxy: str) -> str:
    """从 'scheme://host:port' 提取 host（IP）用于 IP 级黑名单；解析失败返回原串。"""
    text = str(proxy or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    if text.startswith("["):  # IPv6: [::1]:8080
        end = text.find("]")
        if end != -1:
            return text[: end + 1]
    if ":" in text:
        return text.rsplit(":", 1)[0]
    return text


# 分档：数值越小越优先
TIER_VERIFIED = 0  # 成功过，无失败记录
TIER_FRESH = 1  # 未使用过
TIER_RISKY = 2  # 失败过（降权）


class ProxyPool:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proxies: list[str] = []
        self._index = 0
        self._source = ""
        self._interval = 300
        self._last_refresh = 0.0
        self._last_error = ""
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats: dict[str, dict[str, int]] = {}
        self._blacklist: set[str] = set()
        self._exhausted_rounds = 0

    # ---------- 加载 ----------
    def _read_text(self) -> str:
        src = self._source
        if src.startswith("/") or src.startswith("file://"):
            path = src[7:] if src.startswith("file://") else src
            return Path(path).read_text(encoding="utf-8", errors="ignore")
        if _requests is None:
            raise RuntimeError("curl_cffi unavailable")
        resp = _requests.get(src, timeout=20, impersonate="chrome")
        return resp.text or ""

    def refresh(self) -> int:
        if not self._source:
            return 0
        try:
            text = self._read_text()
            lines = []
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("//"):
                    continue
                lines.append(line)
        except Exception as exc:
            self._last_error = str(exc)
            return 0
        if lines:
            with self._lock:
                self._proxies = lines
                self._last_refresh = time.time()
                self._last_error = ""
        return len(lines)

    def set_source(self, source: str, refresh_interval: int = 300) -> int:
        self._source = str(source or "").strip()
        try:
            self._interval = max(30, int(refresh_interval or 300))
        except Exception:
            self._interval = 300
        count = self.refresh()
        self._ensure_thread()
        return count

    # ---------- 调度 ----------
    def _tier(self, host: str) -> int:
        stat = self._stats.get(host)
        if stat and stat.get("ok", 0) > 0 and stat.get("fail", 0) == 0:
            return TIER_VERIFIED
        if not stat:
            return TIER_FRESH
        return TIER_RISKY

    def next_proxy(self) -> str:
        with self._lock:
            total = len(self._proxies)
            if not total:
                return ""
            # 从当前游标开始扫，按档位优先挑第一个可用的出口（档内仍是轮换）
            for tier in (TIER_VERIFIED, TIER_FRESH, TIER_RISKY):
                for offset in range(total):
                    proxy = self._proxies[(self._index + offset) % total]
                    if _host_of(proxy) in self._blacklist:
                        continue
                    if self._tier(_host_of(proxy)) != tier:
                        continue
                    self._index = (self._index + offset + 1) % total
                    return proxy
            # 极端情况：全部出口被拉黑 —— 清空黑名单重新开始，保证注册机不停摆。
            # 但这通常意味着代理源整体失效，必须留下醒目告警，避免静默空转烧额度。
            self._blacklist.clear()
            self._stats.clear()
            self._exhausted_rounds += 1
            print(
                f"[proxy-pool] ⚠️ 代理池 {total} 条全部被拉黑，已清空黑名单重新轮换"
                f"（第 {self._exhausted_rounds} 次）；池子可能已整体失效，请检查代理源",
                flush=True,
            )
            proxy = self._proxies[self._index % total]
            self._index = (self._index + 1) % total
            return proxy

    def report(self, proxy: str, ok: bool) -> None:
        """注册任务实测反馈（IP 级）。

        成功：清零失败记录并解除拉黑，该出口升入「已验证」档优先复用。
        失败：累计失败次数，失败 1 次即降权到「风险」档，累计 2 次拉黑整个 IP。
        """
        proxy = str(proxy or "").strip()
        if not proxy:
            return
        host = _host_of(proxy)
        if not host:
            return
        with self._lock:
            stat = self._stats.setdefault(host, {"ok": 0, "fail": 0})
            if ok:
                stat["ok"] = stat.get("ok", 0) + 1
                stat["fail"] = 0
                self._blacklist.discard(host)
                return
            stat["fail"] = stat.get("fail", 0) + 1
            if stat["fail"] >= 2 and host not in self._blacklist:
                self._blacklist.add(host)
                print(
                    f"[proxy-pool] 已拉黑失败 IP: {host}"
                    f"（累计失败 {stat['fail']} 次，同 IP 全端口一锅端）",
                    flush=True,
                )

    def size(self) -> int:
        with self._lock:
            return len(self._proxies)

    def status(self) -> dict:
        with self._lock:
            verified = fresh = risky = 0
            for proxy in self._proxies:
                host = _host_of(proxy)
                if host in self._blacklist:
                    continue
                tier = self._tier(host)
                if tier == TIER_VERIFIED:
                    verified += 1
                elif tier == TIER_FRESH:
                    fresh += 1
                else:
                    risky += 1
            return {
                "source": self._source,
                "size": len(self._proxies),
                "index": self._index,
                "last_refresh": self._last_refresh,
                "last_error": self._last_error,
                "interval": self._interval,
                "blacklist": len(self._blacklist),
                "tracked": len(self._stats),
                "failed": sum(1 for s in self._stats.values() if s.get("fail", 0) > 0),
                "verified": verified,
                "fresh": fresh,
                "risky": risky,
                "exhausted_rounds": self._exhausted_rounds,
            }

    # ---------- 后台刷新 ----------
    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="proxy-pool-refresh", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                self.refresh()
            except Exception:
                pass


_POOL = ProxyPool()


def init_proxy_pool(proxy_url: str = "", refresh_interval: int = 300) -> int:
    """初始化/刷新代理池；返回当前池内条数。proxy_url 为空时不改变现有配置。"""
    url = str(proxy_url or "").strip()
    if not url:
        return _POOL.size()
    return _POOL.set_source(url, refresh_interval)


def next_proxy() -> str:
    """取下一个代理（按质量分档轮换）；池为空时返回空字符串。"""
    return _POOL.next_proxy()


def status() -> dict:
    return _POOL.status()


def report(proxy: str, ok: bool) -> None:
    """任务结果反馈：成功升档并清零；失败降权，2 次拉黑。"""
    _POOL.report(proxy, ok)
