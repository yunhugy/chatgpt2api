"""注册代理池（订阅式）：从本地文件或 URL 拉取代理列表，按任务轮换使用。

适配 yukkcat/chatgpt2api v2.7.1-rc.3 注册机（新增模块，挂载注入）。

特性：
- 来源支持：本地文件路径（推荐，容器内挂载只读）、file:// URL、http(s) URL
- 后台线程按 refresh_interval 定时刷新；刷新失败保留旧列表
- 线程安全；next_proxy() 循环轮换
- 模块级函数接口：init_proxy_pool() / next_proxy() / status()
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
        self._fail_counts: dict[str, int] = {}
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

    # ---------- 消费 ----------
    def next_proxy(self) -> str:
        with self._lock:
            total = len(self._proxies)
            if not total:
                return ""
            for _ in range(total):
                proxy = self._proxies[self._index % total]
                self._index += 1
                if _host_of(proxy) not in self._blacklist:
                    return proxy
            # 极端情况：全部 IP 被拉黑 —— 清空黑名单重新开始，保证注册机不停摆。
            # 但这通常意味着代理源整体失效，必须留下醒目告警，避免静默空转烧额度。
            self._blacklist.clear()
            self._fail_counts.clear()
            self._exhausted_rounds += 1
            print(
                f"[proxy-pool] ⚠️ 代理池 {total} 条全部被拉黑，已清空黑名单重新轮换"
                f"（第 {self._exhausted_rounds} 次）；池子可能已整体失效，请检查代理源",
                flush=True,
            )
            proxy = self._proxies[self._index % total]
            self._index += 1
            return proxy

    def report(self, proxy: str, ok: bool) -> None:
        """注册任务实测反馈（IP 级）：失败累计 2 次拉黑整个 IP，同 IP 全端口一锅端。"""
        proxy = str(proxy or "").strip()
        if not proxy:
            return
        host = _host_of(proxy)
        if not host:
            return
        with self._lock:
            if ok:
                return
            count = self._fail_counts.get(host, 0) + 1
            self._fail_counts[host] = count
            if count >= 2 and host not in self._blacklist:
                self._blacklist.add(host)
                print(f"[proxy-pool] 已拉黑失败 IP: {host}（累计失败 {count} 次，同 IP 全端口一锅端）", flush=True)

    def size(self) -> int:
        with self._lock:
            return len(self._proxies)

    def status(self) -> dict:
        with self._lock:
            return {
                "source": self._source,
                "size": len(self._proxies),
                "index": self._index,
                "last_refresh": self._last_refresh,
                "last_error": self._last_error,
                "interval": self._interval,
                "blacklist": len(self._blacklist),
                "fail_counts": len(self._fail_counts),
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
    """取下一个代理（循环轮换）；池为空时返回空字符串。"""
    return _POOL.next_proxy()


def status() -> dict:
    return _POOL.status()


def report(proxy: str, ok: bool) -> None:
    """任务结果反馈：成功清零；失败累计（2 次自动拉黑）。"""
    _POOL.report(proxy, ok)
