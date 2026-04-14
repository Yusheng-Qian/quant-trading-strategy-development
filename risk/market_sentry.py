# market_sentry.py - 30m 波动监控 + 12H 历史记忆
from collections import deque
import config
from utils.logger import get_logger

logger = get_logger()


class MarketSentry:
    """
    三维风控 - 维度2 & 维度3
    维度2: 监控当前 30min K线瞬时动能
    维度3: 维护 12H 滑动窗口(24根K线)历史记忆
    """

    def __init__(self):
        self._threshold = getattr(config, "SENTRY_VIOLENT_PCT", 0.008)
        self._window = getattr(config, "SENTRY_MEMORY_CANDLES", 24)
        self._candle_memory = deque(maxlen=self._window)
        self._current_violent = False
        self._any_12h_violent = False
        self._backfilled = False
        # 用 (倒数第2根收盘价, 倒数第3根收盘价) 的 tuple 做指纹，检测新K线关闭
        self._last_fingerprint = None

    def update(self, ws_feed):
        """每轮主循环调用。从 WSMarketData 读取 K 线数据。"""
        if not ws_feed:
            return self._result(0.0)

        klines = ws_feed.get_cached_klines()
        if not klines or len(klines.get("closes", [])) < 2:
            return self._result(0.0)

        closes = klines["closes"]

        # 维度2: 当前K线 vs 上一根收盘价
        cur = closes[-1]
        prev = closes[-2]
        change = abs(cur - prev) / prev if prev > 0 else 0
        self._current_violent = change >= self._threshold

        # 启动时从历史K线填充 memory（仅一次，在新K线检测之前）
        # ✅ 修复：仅在 backfill 实际成功后才设置 _backfilled=True
        if not self._backfilled and len(closes) >= 3:
            start = max(1, len(closes) - self._window - 1)
            for i in range(start, len(closes) - 1):
                ch = abs(closes[i] - closes[i - 1]) / closes[i - 1] if closes[i - 1] > 0 else 0
                self._candle_memory.append(ch)
            # 初始化指纹，避免首次检测误触发
            self._last_fingerprint = (closes[-2], closes[-3])
            self._backfilled = True

        # 维度3: 检测新K线关闭
        # 用 (closes[-2], closes[-3]) 做指纹：当新K线关闭时，旧的 closes[-1] 变成 closes[-2]，
        # 旧的 closes[-2] 变成 closes[-3]，所以这个 tuple 会变化。
        # 这比跟踪 len() 或单个值更可靠（deque 满后 len 不变，单值可能碰巧相等）。
        if len(closes) >= 3 and self._backfilled:
            fp = (closes[-2], closes[-3])
            if self._last_fingerprint is not None and fp != self._last_fingerprint:
                # 新K线关闭：closes[-2] 是刚关闭的K线，closes[-3] 是它的前一根
                ch = abs(closes[-2] - closes[-3]) / closes[-3] if closes[-3] > 0 else 0
                self._candle_memory.append(ch)
            self._last_fingerprint = fp

        self._any_12h_violent = any(ch >= self._threshold for ch in self._candle_memory)
        return self._result(change)

    def _result(self, change):
        return {
            "current_violent": self._current_violent,
            "any_12h_violent": self._any_12h_violent,
            "current_change_pct": round(change, 6),
            "violent_count_12h": sum(1 for ch in self._candle_memory if ch >= self._threshold),
        }

    @property
    def current_violent(self):
        return self._current_violent

    @property
    def any_12h_violent(self):
        return self._any_12h_violent

