"""根据实时+12小时窗口指标动态计算多空资金配比。"""

import time
from collections import deque

import config
from utils.logger import get_logger

logger = get_logger()


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class PositionBiasEngine:
    """输出 long/short 资金配比，默认中性 50/50。"""

    def __init__(self):
        self.lookback_hours = max(1, int(getattr(config, "SIDE_LOOKBACK_HOURS", 12)))
        self.lookback_sec = self.lookback_hours * 3600
        # 多留 2 小时缓冲，便于窗口计算
        self._history = deque(maxlen=int(self.lookback_sec / 30) + 300)

    def _first_num(self, obj, keys):
        if obj is None:
            return None
        if isinstance(obj, (int, float)):
            return float(obj)
        if isinstance(obj, list):
            # 历史列表取最新一条
            for item in reversed(obj):
                val = self._first_num(item, keys)
                if val is not None:
                    return val
            return None
        if isinstance(obj, dict):
            for k in keys:
                if k in obj:
                    try:
                        return float(obj[k])
                    except Exception:
                        pass
            # 递归进入常见子字段
            for k in ("data", "list", "items", "records", "result"):
                if k in obj:
                    val = self._first_num(obj[k], keys)
                    if val is not None:
                        return val
        return None

    def _extract_oi(self, indicators):
        oi_obj = indicators.get("coinglass_oi")
        oi_val = self._first_num(
            oi_obj,
            (
                "openInterest",
                "open_interest",
                "oi",
                "openInterestUsd",
                "openInterestUSDT",
                "value",
            ),
        )
        if oi_val is not None:
            return oi_val
        return self._first_num(indicators.get("binance_oi"), ("value", "oi", "openInterest"))

    def _extract_top_ratio(self, indicators):
        ls = indicators.get("coinglass_long_short")
        val = self._first_num(
            ls,
            (
                "topLongShortRatio",
                "top_long_short_ratio",
                "topTraderLongShortRatio",
                "topTraderRatio",
                "longShortRatio",
                "ratio",
            ),
        )
        if val is not None:
            return val
        return self._first_num(indicators.get("binance_top_long_short_ratio"), ("ratio", "value", "longShortRatio"))

    def _extract_account_ratio(self, indicators):
        # 优先 Coinglass 账户人数比；其次 Binance long_short_ratio
        ls = indicators.get("coinglass_long_short")
        val = self._first_num(
            ls,
            (
                "longShortAccountRatio",
                "accountLongShortRatio",
                "long_short_account_ratio",
                "account_ratio",
            ),
        )
        if val is not None:
            return val
        val = self._first_num(indicators.get("binance_global_long_short_ratio"), ("ratio", "longShortRatio", "value"))
        if val is not None:
            return val
        return self._first_num(indicators.get("long_short_ratio"), ("ratio", "longShortRatio", "value"))

    def update(self, indicators):
        now = time.time()
        price = float(indicators.get("current_price", 0) or 0)
        if price <= 0:
            return
        item = {
            "ts": now,
            "price": price,
            "oi": self._extract_oi(indicators),
            "top_ratio": self._extract_top_ratio(indicators),
            "account_ratio": self._extract_account_ratio(indicators),
        }
        self._history.append(item)

        # 清理超过窗口+2小时的记录
        cutoff = now - (self.lookback_sec + 7200)
        while self._history and self._history[0]["ts"] < cutoff:
            self._history.popleft()

    def _find_baseline(self, now):
        if not self._history:
            return None
        target = now - self.lookback_sec
        baseline = self._history[0]
        for item in self._history:
            if item["ts"] <= target:
                baseline = item
            else:
                break
        return baseline

    def compute(self):
        if not getattr(config, "DYNAMIC_SIDE_ALLOCATION_ENABLED", False):
            return {
                "long_ratio": 0.5,
                "short_ratio": 0.5,
                "score": 0.0,
                "confidence": 0.0,
                "reason": "disabled",
                "components": {},
            }
        if not self._history:
            return {
                "long_ratio": 0.5,
                "short_ratio": 0.5,
                "score": 0.0,
                "confidence": 0.0,
                "reason": "no_history",
                "components": {},
            }

        now = time.time()
        latest = self._history[-1]
        base = self._find_baseline(now)
        if not base:
            base = latest

        # ✅ 修复：饱和阈值从 config 读取，价格动量默认 5%（原 2% 对 BTC 太敏感）
        price_sat = float(getattr(config, "SIDE_PRICE_MOM_SATURATION", 0.05))
        oi_sat = float(getattr(config, "SIDE_OI_MOM_SATURATION", 0.05))
        ratio_sat = float(getattr(config, "SIDE_RATIO_SATURATION", 0.2))

        price_mom = 0.0
        if base["price"] > 0:
            price_mom = _clamp((latest["price"] / base["price"] - 1.0) / price_sat, -1.0, 1.0)

        oi_mom = 0.0
        if latest["oi"] and base["oi"] and base["oi"] > 0:
            oi_mom = _clamp((latest["oi"] / base["oi"] - 1.0) / oi_sat, -1.0, 1.0)

        top_bias = 0.0
        if latest["top_ratio"]:
            top_bias = _clamp((latest["top_ratio"] - 1.0) / ratio_sat, -1.0, 1.0)

        acc_bias = 0.0
        if latest["account_ratio"]:
            acc_bias = _clamp((latest["account_ratio"] - 1.0) / ratio_sat, -1.0, 1.0)

        w_price = float(getattr(config, "SIDE_WEIGHT_PRICE_MOM", 0.35))
        w_oi = float(getattr(config, "SIDE_WEIGHT_OI_MOM", 0.20))
        w_top = float(getattr(config, "SIDE_WEIGHT_TOP_RATIO", 0.25))
        w_acc = float(getattr(config, "SIDE_WEIGHT_ACCOUNT_RATIO", 0.20))

        # 缺失数据时自动降权，避免假信号
        score_num = 0.0
        score_den = 0.0
        score_num += w_price * price_mom
        score_den += abs(w_price)
        if latest["oi"] and base["oi"]:
            score_num += w_oi * oi_mom
            score_den += abs(w_oi)
        if latest["top_ratio"]:
            score_num += w_top * top_bias
            score_den += abs(w_top)
        if latest["account_ratio"]:
            score_num += w_acc * acc_bias
            score_den += abs(w_acc)

        score = (score_num / score_den) if score_den > 0 else 0.0
        score = _clamp(score, -1.0, 1.0)

        lo = float(getattr(config, "SIDE_MIN_LONG_RATIO", 0.20))
        hi = float(getattr(config, "SIDE_MAX_LONG_RATIO", 0.80))
        lo = _clamp(lo, 0.0, 0.5)
        hi = _clamp(hi, 0.5, 1.0)
        if lo >= hi:
            lo, hi = 0.2, 0.8
        long_ratio = _clamp(0.5 + score * ((hi - lo) / 2.0), lo, hi)
        short_ratio = 1.0 - long_ratio
        confidence = _clamp(score_den / (abs(w_price) + abs(w_oi) + abs(w_top) + abs(w_acc) + 1e-10), 0.0, 1.0)

        return {
            "long_ratio": round(long_ratio, 4),
            "short_ratio": round(short_ratio, 4),
            "score": round(score, 4),
            "confidence": round(confidence, 4),
            "reason": "ok",
            "components": {
                "price_mom_12h": round(price_mom, 4),
                "oi_mom_12h": round(oi_mom, 4),
                "top_ratio_bias": round(top_bias, 4),
                "account_ratio_bias": round(acc_bias, 4),
            },
        }

