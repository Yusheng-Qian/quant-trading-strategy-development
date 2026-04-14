# strategy.py - 网格交易策略
"""
网格交易策略
✅ 市场状态判断
✅ 动态网格计算
✅ 交易记录
"""
from enum import Enum
import math
import os
import threading
import time
from datetime import datetime
import json
from utils.logger import get_logger
import config

logger = get_logger()
MIN_NOTIONAL = 130  # 每层最小名义价值(USDT) - 提高到130以降低手续费吞噬
ORDER_STEP_SIZE = getattr(config, 'ORDER_STEP_SIZE', 0.001)

class MarketState(Enum):
    RANGING = 1   # 震荡市
    TRENDING = 2  # 趋势市
    UNKNOWN = 3

class GridStrategy:
    """网格交易策略"""
    
    def __init__(self, symbol, total_capital, grid_capital_ratio, buffer_ratio, 
                 reserve_ratio, L, N, upper_pct, lower_pct, Tp, 
                 stop_loss_pct, max_trades_per_day, monthly_target):
        
        self.symbol = symbol
        self.total_capital = total_capital
        self.grid_capital = total_capital * grid_capital_ratio
        self.buffer_capital = total_capital * buffer_ratio
        self.reserve_capital = total_capital * reserve_ratio
        
        self.L = L
        self.N = N
        self.target_N = N
        self._N_original = N  # ✅ 保存原始网格层数，用于资金回升时恢复
        self.upper_pct = upper_pct
        self.lower_pct = lower_pct
        self.Tp = Tp
        self.stop_loss_pct = stop_loss_pct
        
        self.max_trades_per_day = max_trades_per_day
        self.monthly_target = monthly_target
        
        self.trades_today = []
        self.daily_pnl = 0.0
        self.monthly_pnl = 0.0
        self.last_grid_info = None
        self._last_rebuild_time = 0  # ✅ 修复：网格重建冷却期
        self._is_paused = False
        self._pause_reason = None
        self._position_cooldown_until = 0
        self._last_suggested_N = None
        self._n_change_candidate = None
        self._n_change_rounds = 0
        self._tp_change_candidate = None
        self._tp_change_rounds = 0
        
        # ✅ 修复1：持久化网格层数状态
        self._state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grid_strategy_state.json")
        self._load_state()
        self._market_state = MarketState.UNKNOWN
        self._lock = threading.Lock()
        self._has_position = False
        self.last_indicators = {}
        
        logger.info(f"[Strategy] 初始化 | 资金: ${total_capital} | 网格: {N} | 杠杆: {L}x")
    
    def evaluate_market(self, rsi=None, price_slope=None, ma200=None,
                        current_price=None, adx=None, vhf=None, adx_fast=None):
        """
        9.0: 市场状态判定已移至 DecisionEngine。
        此方法保留仅为向后兼容（scheduler/strategy_manager 可能调用）。
        实际决策由 DecisionEngine._determine_regime() 执行。
        """
        return self._market_state
    
    def compute_grid_dynamic(
        self,
        current_price,
        atr,
        smoothed_atr=None,
        available_capital=None,
        capital_multiplier=1.0,
        grid_bias=None,
        side_allocation=None,
        sentry_override=None,
        consecutive_fill_info=None,
    ):
        """动态计算网格（支持双向）

        Args:
            current_price: 当前价格
            atr: ATR
            smoothed_atr: 平滑 ATR
            available_capital: 当前可用资金（可选，优先用于避免冻结后超额下单）
            capital_multiplier: 仓位乘数（叠加在模式参数之上）
        """
        if smoothed_atr is None:
            smoothed_atr = atr

        capital_multiplier = max(0.0, float(capital_multiplier or 0.0))
        scaled_grid_capital = self.grid_capital * capital_multiplier
        effective_capital = scaled_grid_capital if available_capital is None else min(scaled_grid_capital, available_capital)

        min_notional_floor = float(getattr(config, "MIN_ORDER_NOTIONAL_EXCHANGE_FLOOR", 100))
        min_notional_eff = max(min_notional_floor, float(getattr(config, "SENTRY_MIN_ORDER_USDT", MIN_NOTIONAL)))
        min_layer_cap = float(getattr(config, "MIN_LAYER_CAPITAL_USDT", 130))
        if sentry_override:
            sf = sentry_override.get("min_order_usdt")
            if sf:
                sf = float(sf)
                min_notional_eff = max(min_notional_floor, sf)
                min_layer_cap = max(min_notional_floor, min(min_layer_cap, sf))

        if effective_capital < min_notional_eff:
            logger.warning(
                f"[Strategy] 可用网格资金 ${effective_capital:.2f} 低于最小名义价值 ${min_notional_eff:.0f}，暂停网格计算"
            )
            return None

        # 低资金保护：避免资金不足时反复尝试下单导致"撤单-重建"死循环
        # MIN_LAYER_CAPITAL_USDT 是每层最小名义价值，需除以杠杆得到保证金需求
        capital_for_layers = available_capital if available_capital is not None else effective_capital
        min_margin_per_layer = min_layer_cap / max(1, self.L)  # 名义价值 → 保证金
        max_layers_by_capital = int(capital_for_layers / min_margin_per_layer) if min_margin_per_layer > 0 else self.N
        if max_layers_by_capital < 1:
            logger.warning(
                f"[Strategy] 可用资金 ${capital_for_layers:.2f} 不足单层保证金下限 ${min_margin_per_layer:.0f} (名义${min_layer_cap}/{self.L}x)"
            )
            return None

        bidirectional = getattr(config, 'BIDIRECTIONAL_GRID', False)
        if grid_bias in ("long_only", "short_only"):
            # 方向偏置时，单边网格使用全部资金
            bidirectional = False

        long_ratio = 0.5
        short_ratio = 0.5
        if bidirectional and isinstance(side_allocation, dict):
            try:
                long_ratio = float(side_allocation.get("long_ratio", 0.5))
                short_ratio = float(side_allocation.get("short_ratio", 1.0 - long_ratio))
            except Exception:
                long_ratio, short_ratio = 0.5, 0.5
            if long_ratio <= 0 and short_ratio <= 0:
                long_ratio, short_ratio = 0.5, 0.5
            total_ratio = long_ratio + short_ratio
            if total_ratio <= 0:
                long_ratio, short_ratio = 0.5, 0.5
            else:
                long_ratio /= total_ratio
                short_ratio /= total_ratio

        long_capital = effective_capital * long_ratio if bidirectional else effective_capital
        short_capital = effective_capital * short_ratio if bidirectional else 0
        if grid_bias == "long_only":
            short_capital = 0
            long_capital = effective_capital
        elif grid_bias == "short_only":
            long_capital = 0
            short_capital = effective_capital

        # 根据最小名义价值限制层数
        # 双向时以较小一侧资金决定层数，避免一侧大量层被 MIN_NOTIONAL 过滤
        if bidirectional:
            active_caps = [c for c in (long_capital, short_capital) if c > 0]
            side_capital = min(active_caps) if active_caps else 0
        else:
            side_capital = effective_capital
        # 三维风控: sentry_override 作为 N 的上限钳制（不替换 adaptive 的值）
        target_n = int(getattr(self, "target_N", self.N) or self.N)  # adaptive 计算的值
        if sentry_override:
            sn = sentry_override.get("target_n")
            if sn is not None:
                # 用 min：adaptive 可以降 N（风控），coordinator 也可以降 N（波动），取更保守的
                target_n = min(target_n, int(sn))
        effective_N = target_n
        if side_capital > 0:
            # ✅ 修复3：预留10%缓冲区，防止价格波动导致频繁调整
            max_notional_per_side = side_capital * self.L * 0.9  # 90% 实际可用
            max_layers = int(max_notional_per_side / min_notional_eff)
            effective_N = max(1, min(target_n, max_layers, max_layers_by_capital))
            if effective_N != target_n:
                # 仅记录建议层数，真正部署成功后再写入 self.N，避免冷却期内状态抖动
                if self._last_suggested_N != effective_N:
                    logger.info(
                        f"[Strategy] 建议层数由 {target_n} 调整为 {effective_N}"
                        f" | side_cap=${side_capital:.0f} max_notional={max_layers} max_by_cap={max_layers_by_capital}"
                        f" | cap_for_layers=${capital_for_layers:.0f} min_layer=${min_layer_cap}"
                    )
                    self._last_suggested_N = effective_N

        # 名义预算约束层数：避免后续安全网缩仓后，层层跌破最小名义价值导致“部署中但0挂单”
        _risk_cap_for_n = float(getattr(config, "GRID_MAX_EXPOSURE_RATIO", 0.25))
        _total_equity_for_n = max(self.total_capital, effective_capital)
        _max_notional_per_side_for_n = _total_equity_for_n * self.L * _risk_cap_for_n
        _budget_limit_for_n = _max_notional_per_side_for_n * 0.82
        _long_budget_for_n = long_capital * self.L if long_capital > 0 else 0.0
        _short_budget_for_n = short_capital * self.L if short_capital > 0 else 0.0
        if _long_budget_for_n > _budget_limit_for_n and _long_budget_for_n > 0:
            _long_budget_for_n = _budget_limit_for_n
        if _short_budget_for_n > _budget_limit_for_n and _short_budget_for_n > 0:
            _short_budget_for_n = _budget_limit_for_n

        _active_budgets = [b for b in (_long_budget_for_n, _short_budget_for_n) if b > 0]
        if _active_budgets:
            _min_side_budget = min(_active_budgets)
            _max_layers_by_budget = int(_min_side_budget / max(min_notional_eff, 1e-9))
            _max_layers_by_budget = max(1, _max_layers_by_budget)
            if effective_N > _max_layers_by_budget:
                old_n = effective_N
                effective_N = _max_layers_by_budget
                logger.info(
                    f"[Strategy] 名义预算约束层数: N {old_n}->{effective_N} "
                    f"| min_side_budget=${_min_side_budget:.0f} min_notional=${min_notional_eff:.0f}"
                )

        upper_pct_eff = self.upper_pct
        lower_pct_eff = self.lower_pct
        if getattr(config, "AUTO_RANGE_BY_EFFECTIVE_N", True):
            base_n = max(1, int(self._N_original or self.N or 1))
            cur_n = max(1, int(effective_N or 1))
            if cur_n < base_n:
                ratio = base_n / cur_n
                power = float(getattr(config, "RANGE_BY_N_POWER", 0.5))
                max_mult = float(getattr(config, "RANGE_BY_N_MAX_MULT", 1.8))
                mult = min(max_mult, ratio ** power)
                upper_pct_eff = self.upper_pct * mult
                lower_pct_eff = self.lower_pct * mult
                logger.info(
                    f"[Strategy] 范围按层数放宽: N {base_n}->{cur_n} | 倍率 x{mult:.2f} | "
                    f"上界 {self.upper_pct*100:.2f}%->{upper_pct_eff*100:.2f}% | "
                    f"下界 {self.lower_pct*100:.2f}%->{lower_pct_eff*100:.2f}%"
                )
            else:
                upper_pct_eff = self.upper_pct
                lower_pct_eff = self.lower_pct

        # 三维风控: interval_mult 拉宽间距 + 非对称防御
        if sentry_override:
            iv_mult = sentry_override.get("interval_mult", 1.0)
            if iv_mult != 1.0:
                if sentry_override.get("asymmetric") == "active":
                    asym_w = getattr(config, "SENTRY_ASYMMETRIC_WIDEN", 1.8)
                    asym_t = getattr(config, "SENTRY_ASYMMETRIC_TIGHTEN", 0.7)
                    inds = getattr(self, "last_indicators", {}) or {}
                    ma20 = inds.get("ma20")
                    if ma20 and ma20 > 0:
                        if current_price > ma20:
                            # 暴涨: 上方卖单大幅拉宽(防空单拉爆), 下方买单正常
                            upper_pct_eff *= iv_mult * asym_w
                            lower_pct_eff *= asym_t
                        else:
                            # 暴跌: 下方买单大幅拉宽(拒绝接飞刀), 上方卖单收紧(加速平仓)
                            lower_pct_eff *= iv_mult * asym_w
                            upper_pct_eff *= asym_t
                    else:
                        # ma20 不可用，回退对称拉宽
                        upper_pct_eff *= iv_mult
                        lower_pct_eff *= iv_mult
                else:
                    upper_pct_eff *= iv_mult
                    lower_pct_eff *= iv_mult

        spacing = getattr(config, "GRID_SPACING", "arithmetic")
        spacing = spacing.lower().strip()
        if getattr(config, "AUTO_GRID_SPACING", False):
            vol_ratio = smoothed_atr / current_price if current_price > 0 else 0
            threshold = getattr(config, "GRID_SPACING_VOL_THRESHOLD", 0.012)
            spacing = "geometric" if vol_ratio >= threshold else "arithmetic"

        # ── ATR 范围上限：最外层不超过 MAX_GRID_ATR_MULT × ATR ──
        max_atr_mult = float(getattr(config, "MAX_GRID_ATR_MULT", 2.0))
        max_range = max_atr_mult * smoothed_atr if smoothed_atr > 0 else float("inf")

        # 等差网格：ATR 概率网格
        # 三层控制: ① step ≤ ATR  ② range ≤ max_atr_mult×ATR  ③ N ≥ MIN_GRID_N
        min_grid_n = int(getattr(config, "MIN_GRID_N", 3))

        if spacing == "arithmetic":
            atr_step = smoothed_atr / effective_N if effective_N > 0 else 0
            range_step = current_price * max(lower_pct_eff, upper_pct_eff) / effective_N if effective_N > 0 else 0
            grid_step = max(atr_step, range_step)

            # ① 步长钳制：step 不超过 ATR
            if smoothed_atr > 0 and grid_step > smoothed_atr:
                logger.info(
                    f"[Strategy] 步长ATR钳制: step {grid_step:.0f}->{smoothed_atr:.0f} "
                    f"(range_step={range_step:.0f} > ATR={smoothed_atr:.0f})"
                )
                grid_step = smoothed_atr

            # ② 范围钳制：最外层 ≤ max_range
            if grid_step > 0 and max_range < float("inf"):
                max_n_by_atr = int(max_range / grid_step)
                if max_n_by_atr < effective_N and max_n_by_atr >= 1:
                    effective_N = max_n_by_atr

            # ③ 最小层数保护：N 不低于 MIN_GRID_N，step 相应缩小
            if effective_N < min_grid_n and max_range < float("inf") and min_grid_n >= 1:
                new_step = max_range / min_grid_n
                if new_step > 0:
                    logger.info(
                        f"[Strategy] 最小层数保护: N {effective_N}->{min_grid_n} | "
                        f"step {grid_step:.0f}->{new_step:.0f} "
                        f"(每层 {new_step/smoothed_atr:.2f} ATR)"
                    )
                    effective_N = min_grid_n
                    grid_step = new_step

            P_low = current_price - effective_N * grid_step
            P_high = current_price + effective_N * grid_step
        # 等比网格：按百分比等比递进，风险更均匀
        elif spacing == "geometric":
            grid_step = 0  # 等比不使用线性步长
            # ATR 钳制：限制等比网格范围
            atr_ratio_cap = max_atr_mult * smoothed_atr / current_price if current_price > 0 else 1.0
            upper_pct_eff = min(upper_pct_eff, atr_ratio_cap)
            lower_pct_eff = min(lower_pct_eff, atr_ratio_cap)
            P_low = current_price * (1 - lower_pct_eff)
            P_high = current_price * (1 + upper_pct_eff)
        else:
            logger.warning(f"[Strategy] 未知 GRID_SPACING={spacing}，回退等差网格")
            atr_step = smoothed_atr / effective_N if effective_N > 0 else 0
            range_step = current_price * max(lower_pct_eff, upper_pct_eff) / effective_N if effective_N > 0 else 0
            grid_step = max(atr_step, range_step)
            if grid_step > 0 and max_range < float("inf"):
                max_n_by_atr = int(max_range / grid_step)
                if max_n_by_atr < effective_N and max_n_by_atr >= 1:
                    effective_N = max_n_by_atr
            P_low = current_price - effective_N * grid_step
            P_high = current_price + effective_N * grid_step
            spacing = "arithmetic"
        StopOutPrice = P_low * (1 - self.stop_loss_pct)
        has_short_side = bidirectional or grid_bias == "short_only"
        StopOutPriceUpper = P_high * (1 + self.stop_loss_pct) if has_short_side else None

        # ── 连续成交 spacing 扩张（只影响 Core 层）──
        _consec_info = consecutive_fill_info or {}
        _consec_count = int(_consec_info.get("count", 0))
        _consec_threshold = int(getattr(config, "CONSECUTIVE_FILL_SPACING_THRESHOLD", 2))
        _consec_k = float(getattr(config, "CONSECUTIVE_FILL_SPACING_K", 0.20))
        _consec_max = float(getattr(config, "CONSECUTIVE_FILL_SPACING_MAX_MULT", 3.0))
        _core_spacing_mult = 1.0
        if _consec_count >= _consec_threshold:
            _core_spacing_mult = min(_consec_max, 1 + _consec_k * _consec_count)
            logger.info(
                f"[Strategy] 连续成交扩张 | 同向连续={_consec_count} "
                f"| Core层间距×{_core_spacing_mult:.2f}"
            )

        # ── ATR 动态止盈 ──
        # 如果管线已设置 TP（Decision + Flow），直接使用，不再二次 ATR 计算
        if getattr(self, '_tp_set_by_pipeline', False):
            dynamic_tp = self.Tp
            self._tp_set_by_pipeline = False  # 消费后重置
        else:
            dynamic_tp = self._dynamic_tp_pct(atr, current_price)

        tiered_tp_enabled = getattr(config, 'TIERED_TP_ENABLED', False)
        tiered_tp_levels = getattr(config, 'TIERED_TP_LEVELS', [])

        grid = []

        # ── 概率加权资金分配：exp(-k × fill_score) ──
        _prob_weight_k = float(getattr(config, "GRID_PROB_WEIGHT_K", 1.0))
        # 风险2防护：连续成交≥3时增大k → 降低近层集中度，资金更均匀分散
        if _consec_count >= 3:
            _dampen = float(getattr(config, "CONSECUTIVE_FILL_WEIGHT_DAMPEN", 0.3))
            _prob_weight_k = _prob_weight_k * (1 + _dampen * (_consec_count - 2))
            logger.info(
                f"[Strategy] 连续成交权重分散 | k={_prob_weight_k:.2f} (连续{_consec_count}笔)"
            )
        _prob_weight_min = float(getattr(config, "GRID_PROB_WEIGHT_MIN", 0.05))
        _prob_weights_enabled = bool(getattr(config, "GRID_PROB_WEIGHTS_ENABLED", True))
        _target_zone_enabled = bool(getattr(config, "GRID_TARGET_ZONE_ALLOC_ENABLED", True))
        _target_zone_center = float(getattr(config, "GRID_TARGET_ZONE_CENTER", 0.45))
        _target_zone_sigma = max(0.05, float(getattr(config, "GRID_TARGET_ZONE_SIGMA", 0.22)))
        _target_zone_boost = max(0.0, float(getattr(config, "GRID_TARGET_ZONE_BOOST", 0.85)))
        _probe_alloc_mult = max(0.2, float(getattr(config, "GRID_PROBE_ALLOC_MULT", 0.90)))
        _tail_alloc_mult = max(0.1, float(getattr(config, "GRID_TAIL_ALLOC_MULT", 0.45)))

        def _calc_layer_weights(n_layers, step_or_ratio, price_base, is_geometric=False):
            """计算每层资金权重 (归一化)"""
            if not _prob_weights_enabled or n_layers <= 0 or smoothed_atr <= 0:
                return [1.0 / max(1, n_layers)] * n_layers
            weights = []
            for i in range(1, n_layers + 1):
                if is_geometric and step_or_ratio is not None:
                    lp = price_base * (step_or_ratio ** i)
                    dist = abs(price_base - lp)
                else:
                    dist = i * step_or_ratio
                fs = dist / smoothed_atr
                w = max(_prob_weight_min, math.exp(-_prob_weight_k * fs))

                # 目标区间分配：近价层保持中等，中段层加权，尾端层减权。
                if _target_zone_enabled and n_layers >= 3:
                    depth = (i - 1) / max(1, n_layers - 1)  # 0=最近层, 1=最远层
                    zone_peak = math.exp(-0.5 * ((depth - _target_zone_center) / _target_zone_sigma) ** 2)
                    zone_mult = 1.0 + _target_zone_boost * zone_peak
                    if i == 1:
                        zone_mult *= _probe_alloc_mult
                    if i == n_layers:
                        zone_mult *= _tail_alloc_mult
                    w = max(_prob_weight_min, w * zone_mult)
                weights.append(w)
            total = sum(weights)
            return [w / total for w in weights] if total > 0 else [1.0 / n_layers] * n_layers

        # ── 事前敞口约束：先算名义预算，再按权重分配 ──────────
        # 核心思想：用名义口径做预算 → 按权重分配名义 → 反推 size
        # 避免旧模式"先按保证金分配 → ×L膨胀 → 事后砍"的结构冲突
        risk_cap = float(getattr(config, "GRID_MAX_EXPOSURE_RATIO", 0.25))
        max_worst_dd = float(getattr(config, "GRID_MAX_WORST_DRAWDOWN", 0.15))
        total_equity = max(self.total_capital, effective_capital)

        # Step 1: 单侧最大名义预算 = equity × L × risk_cap
        max_notional_per_side = total_equity * self.L * risk_cap

        # 旧模式的单侧名义 = side_capital × L（保证金口径 → 名义口径）
        long_notional_budget = long_capital * self.L if long_capital > 0 else 0
        short_notional_budget = short_capital * self.L if short_capital > 0 else 0

        # 如果保证金分配已超出名义预算 → 在分配前钳制（而非事后缩减）
        # 预留 18% 余量给 ceil 取整 + MIN_NOTIONAL 地板膨胀
        budget_limit = max_notional_per_side * 0.82
        if long_notional_budget > budget_limit and long_notional_budget > 0:
            long_notional_budget = budget_limit
        if short_notional_budget > budget_limit and short_notional_budget > 0:
            short_notional_budget = budget_limit

        if long_notional_budget > 0 or short_notional_budget > 0:
            logger.info(
                f"[Strategy] 名义预算 | 多头: ${long_notional_budget:.0f} 空头: ${short_notional_budget:.0f} "
                f"| 上限: ${max_notional_per_side:.0f} (equity=${total_equity:.0f}×{self.L}x×{risk_cap})"
            )

        # ═══ 多头网格：当前价以下的买单 ═══
        if long_notional_budget > 0:
            if spacing == "geometric":
                ratio_down = (P_low / current_price) ** (1 / effective_N) if effective_N > 0 else 1
            else:
                ratio_down = None

            long_weights = _calc_layer_weights(
                effective_N,
                ratio_down if spacing == "geometric" else grid_step,
                current_price, is_geometric=(spacing == "geometric"),
            )

            for i in range(1, effective_N + 1):
                # 层类型：Probe(最近) / Core(中间) / Tail(最远)
                if i == 1:
                    _layer_type = "probe"
                elif i == effective_N:
                    _layer_type = "tail"
                else:
                    _layer_type = "core"

                # Core 层应用连续成交 spacing 扩张；Probe/Tail 不受影响
                _step_mult = _core_spacing_mult if _layer_type == "core" else 1.0
                buy_price = (
                    current_price * (ratio_down ** i)
                    if spacing == "geometric" else
                    current_price - i * grid_step * _step_mult
                )
                # Step 2: 按权重分配名义（而非保证金）
                layer_notional = long_notional_budget * long_weights[i - 1]
                layer_capital = layer_notional / self.L  # 反推保证金（仅供记录）
                # Step 3: 名义 → size
                size_btc = layer_notional / buy_price if buy_price > 0 else 0

                # 向上取整到 Binance 步长，确保名义价值 ≥ MIN_NOTIONAL
                size_btc = math.ceil(size_btc / ORDER_STEP_SIZE) * ORDER_STEP_SIZE
                notional = size_btc * buy_price

                if buy_price <= 0 or size_btc < ORDER_STEP_SIZE:
                    continue
                # 三维风控: 105 USDT 地板 — 低于地板则强制提升 size
                if notional < min_notional_eff:
                    size_btc = math.ceil((min_notional_eff / buy_price) / ORDER_STEP_SIZE) * ORDER_STEP_SIZE
                    notional = size_btc * buy_price
                    if notional < min_notional_eff:
                        logger.warning(
                            f"[Strategy] 多头L{i}: 名义 ${notional:.2f} < ${min_notional_eff}，跳过"
                        )
                        continue

                layer = self._build_layer(
                    layer_idx=i, side='long',
                    entry_price=buy_price, tp_pct=dynamic_tp,
                    layer_capital=layer_capital, size_btc=size_btc,
                    tiered_tp_enabled=tiered_tp_enabled,
                    tiered_tp_levels=tiered_tp_levels,
                )
                layer['layer_type'] = _layer_type
                grid.append(layer)

        # ═══ 空头网格：当前价以上的卖单 ═══
        if short_notional_budget > 0:
            if spacing == "geometric":
                ratio_up = (P_high / current_price) ** (1 / effective_N) if effective_N > 0 else 1
            else:
                ratio_up = None

            short_weights = _calc_layer_weights(
                effective_N,
                ratio_up if spacing == "geometric" else grid_step,
                current_price, is_geometric=(spacing == "geometric"),
            )

            for i in range(1, effective_N + 1):
                # 层类型：Probe(最近) / Core(中间) / Tail(最远)
                if i == 1:
                    _layer_type = "probe"
                elif i == effective_N:
                    _layer_type = "tail"
                else:
                    _layer_type = "core"

                # Core 层应用连续成交 spacing 扩张；Probe/Tail 不受影响
                _step_mult = _core_spacing_mult if _layer_type == "core" else 1.0
                sell_price = (
                    current_price * (ratio_up ** i)
                    if spacing == "geometric" else
                    current_price + i * grid_step * _step_mult
                )
                # Step 2: 按权重分配名义（而非保证金）
                layer_notional = short_notional_budget * short_weights[i - 1]
                layer_capital = layer_notional / self.L  # 反推保证金（仅供记录）
                # Step 3: 名义 → size
                size_btc = layer_notional / sell_price if sell_price > 0 else 0

                # 向上取整到 Binance 步长，确保名义价值 ≥ MIN_NOTIONAL
                size_btc = math.ceil(size_btc / ORDER_STEP_SIZE) * ORDER_STEP_SIZE
                notional = size_btc * sell_price

                if size_btc < ORDER_STEP_SIZE:
                    continue
                # 三维风控: 105 USDT 地板 — 低于地板则强制提升 size
                if notional < min_notional_eff:
                    size_btc = math.ceil((min_notional_eff / sell_price) / ORDER_STEP_SIZE) * ORDER_STEP_SIZE
                    notional = size_btc * sell_price
                    if notional < min_notional_eff:
                        logger.warning(
                            f"[Strategy] 空头L{i}: 名义 ${notional:.2f} < ${min_notional_eff}，跳过"
                        )
                        continue

                layer = self._build_layer(
                    layer_idx=-i, side='short',
                    entry_price=sell_price, tp_pct=dynamic_tp,
                    layer_capital=layer_capital, size_btc=size_btc,
                    tiered_tp_enabled=tiered_tp_enabled,
                    tiered_tp_levels=tiered_tp_levels,
                )
                layer['layer_type'] = _layer_type
                grid.append(layer)

        # ── 事后安全网：正常不应触发，仅防御 MIN_NOTIONAL 地板提升导致的溢出 ──
        if grid and total_equity > 0:
            def _calc_side_metrics():
                long_notional_local = 0.0
                long_worst_loss_local = 0.0
                short_notional_local = 0.0
                short_worst_loss_local = 0.0

                for layer in grid:
                    size = float(layer.get("size_btc", 0) or layer.get("amount", 0) or 0)
                    entry = float(layer.get("entry_price", 0) or layer.get("price", 0) or 0)
                    notional_val = size * entry
                    if layer.get("side") == "long":
                        long_notional_local += notional_val
                        long_worst_loss_local += size * (entry - P_low) if P_low < entry else 0
                    elif layer.get("side") == "short":
                        short_notional_local += notional_val
                        short_worst_loss_local += size * (P_high - entry) if P_high > entry else 0

                return (
                    long_notional_local,
                    short_notional_local,
                    long_worst_loss_local,
                    short_worst_loss_local,
                )

            long_notional, short_notional, long_worst_loss, short_worst_loss = _calc_side_metrics()

            # 取单边最大值（多空不会同时全部成交）
            max_side_notional = max(long_notional, short_notional)
            worst_dd = max(long_worst_loss, short_worst_loss)

            # 敞口安全网（事前约束后通常不触发）
            if max_side_notional > max_notional_per_side * 1.05 and max_side_notional > 0:
                scale = max_notional_per_side / max_side_notional
                reduced_pct = max(0.0, 1.0 - scale)
                logger.warning(
                    f"[Strategy] ⚠️ 敞口安全网: 单边名义${max_side_notional:.0f} > 上限${max_notional_per_side:.0f} "
                    f"| 缩放到 {scale:.0%} (实际缩减 {reduced_pct:.0%}, MIN_NOTIONAL地板溢出)"
                )
                for layer in grid:
                    old_size = float(layer.get("size_btc", 0) or layer.get("amount", 0) or 0)
                    # 降仓使用向下取整，避免缩仓后反而被步长向上抬高
                    new_size = math.floor(old_size * scale / ORDER_STEP_SIZE) * ORDER_STEP_SIZE
                    new_size = max(new_size, ORDER_STEP_SIZE)
                    layer["size_btc"] = new_size
                    layer["amount"] = new_size

                # 如果缩仓后仍明显超限，优先移除最外层 tail 层，直到贴近上限
                long_notional, short_notional, long_worst_loss, short_worst_loss = _calc_side_metrics()
                max_side_notional = max(long_notional, short_notional)
                overflow_side = "long" if long_notional >= short_notional else "short"
                trim_count = 0
                while max_side_notional > max_notional_per_side * 1.02:
                    side_layers = [l for l in grid if l.get("side") == overflow_side]
                    if len(side_layers) <= 1:
                        break
                    tail_layer = max(side_layers, key=lambda l: abs(int(l.get("layer", 0))))
                    grid.remove(tail_layer)
                    trim_count += 1
                    long_notional, short_notional, long_worst_loss, short_worst_loss = _calc_side_metrics()
                    max_side_notional = max(long_notional, short_notional)
                    overflow_side = "long" if long_notional >= short_notional else "short"

                if trim_count > 0:
                    logger.warning(
                        f"[Strategy] ⚠️ 敞口安全网补偿: 已移除 {trim_count} 个最外层以贴合上限 "
                        f"| 当前单边名义=${max_side_notional:.0f} 上限=${max_notional_per_side:.0f}"
                    )

            # 最坏回撤安全网（使用最新层信息重新评估）
            worst_dd = max(long_worst_loss, short_worst_loss)
            if worst_dd > total_equity * max_worst_dd and worst_dd > 0:
                dd_scale = (total_equity * max_worst_dd) / worst_dd
                logger.warning(
                    f"[Strategy] ⚠️ 回撤安全网: 最坏浮亏${worst_dd:.0f} > 上限${total_equity * max_worst_dd:.0f} "
                    f"(equity×{max_worst_dd}) | 缩减 {dd_scale:.0%}"
                )
                for layer in grid:
                    old_size = float(layer.get("size_btc", 0) or layer.get("amount", 0) or 0)
                    new_size = math.floor(old_size * dd_scale / ORDER_STEP_SIZE) * ORDER_STEP_SIZE
                    new_size = max(new_size, ORDER_STEP_SIZE)
                    layer["size_btc"] = new_size
                    layer["amount"] = new_size

        grid_info = {
            'P0': current_price,
            'P_low': round(P_low, 2),
            'P_high': round(P_high, 2),
            'StopOutPrice': round(StopOutPrice, 2),
            'StopOutPriceUpper': round(StopOutPriceUpper, 2) if StopOutPriceUpper else None,
            'bidirectional': bidirectional,
            'grid_step': round(grid_step, 2),
            'spacing': spacing,
            'atr': atr,
            'N': effective_N,
            'target_N': target_n,
            'Tp': dynamic_tp,
            'stop_loss_pct': self.stop_loss_pct,
            'upper_pct': upper_pct_eff,
            'lower_pct': lower_pct_eff,
            'effective_capital': round(effective_capital, 4),
            'capital_multiplier': round(capital_multiplier, 4),
            'grid': grid,
            'timestamp': datetime.now().isoformat()
        }
        if bidirectional:
            grid_info["long_ratio"] = round(long_ratio, 4)
            grid_info["short_ratio"] = round(short_ratio, 4)

        long_count = sum(1 for g in grid if g['side'] == 'long')
        short_count = sum(1 for g in grid if g['side'] == 'short')
        ratio_log = ""
        if bidirectional:
            ratio_log = f" | 资金配比 多:{long_ratio*100:.0f}% 空:{short_ratio*100:.0f}%"
        logger.info(
            f"[Strategy] 网格计算 | 类型: {'等比' if spacing == 'geometric' else '等差'} | "
            f"多头: {long_count}层 空头: {short_count}层 | "
            f"范围: ${P_low:.2f}-${P_high:.2f}{ratio_log}"
        )

        return grid_info

    def _dynamic_tp_pct(self, atr, price):
        """
        ATR 自适应止盈比例
        低波动 → TP 收窄（保成交率）
        高波动 → TP 放宽（抓大行情）
        """
        if not getattr(config, "DYNAMIC_TP_ENABLED", True):
            return self.Tp
        if not atr or atr <= 0 or not price or price <= 0:
            return self.Tp  # ATR 不可用时 fallback
        k = float(getattr(config, "DYNAMIC_TP_K", 1.08))
        tp_min = float(getattr(config, "DYNAMIC_TP_MIN", 0.004))
        tp_max = float(getattr(config, "DYNAMIC_TP_MAX", 0.020))
        raw = k * (atr / price)
        result = max(tp_min, min(tp_max, raw))
        logger.debug(
            f"[Strategy] 动态TP | ATR=${atr:.2f} ATR/P={atr/price:.4f} "
            f"raw={raw:.4f} → TP={result:.4f} ({result*100:.2f}%)"
        )
        return result

    def _build_layer(self, layer_idx, side, entry_price, tp_pct,
                     layer_capital, size_btc, tiered_tp_enabled, tiered_tp_levels):
        """构建单个网格层（多/空通用）"""
        # 多头: TP = entry * (1 + tp_pct)  → 卖出止盈
        # 空头: TP = entry * (1 - tp_pct)  → 买回止盈
        if side == 'long':
            tp_price = entry_price * (1 + tp_pct)
        else:
            tp_price = entry_price * (1 - tp_pct)

        layer = {
            'layer': layer_idx,
            'side': side,
            'entry_price': round(entry_price, 2),
            'tp_price': round(tp_price, 2),
            # 保留兼容字段
            'buy_price': round(entry_price, 2) if side == 'long' else round(tp_price, 2),
            'sell_price': round(tp_price, 2) if side == 'long' else round(entry_price, 2),
            'size_btc': round(size_btc, 6),
            'tiered_tp': False,
        }

        if tiered_tp_enabled and tiered_tp_levels:
            tp_levels = []
            total_est_profit = 0.0
            for level_idx, level in enumerate(tiered_tp_levels):
                lv_pct = level['pct']
                lv_ratio = level['ratio']
                if side == 'long':
                    lv_tp_price = entry_price * (1 + lv_pct)
                else:
                    lv_tp_price = entry_price * (1 - lv_pct)
                lv_size = size_btc * lv_ratio
                lv_profit = layer_capital * lv_ratio * lv_pct
                total_est_profit += lv_profit

                tp_levels.append({
                    'tp_level': level_idx + 1,
                    'tp_price': round(lv_tp_price, 2),
                    # 兼容字段
                    'sell_price': round(lv_tp_price, 2) if side == 'long' else round(entry_price, 2),
                    'buy_price': round(entry_price, 2) if side == 'long' else round(lv_tp_price, 2),
                    'size_btc': round(lv_size, 6),
                    'tp_pct': lv_pct,
                    'tp_ratio': lv_ratio,
                    'est_profit': round(lv_profit, 2),
                })

            layer['tiered_tp'] = True
            layer['tp_levels'] = tp_levels
            layer['est_profit'] = round(total_est_profit, 2)
        else:
            layer['est_profit'] = round(layer_capital * tp_pct, 2)

        return layer
    
    def should_rebuild_grid(self, new_grid_info, old_grid_info=None):
        """
        判断是否重建网格
        
        Args:
            new_grid_info: 新计算的网格参数
            old_grid_info: 旧网格参数（可选，默认使用 self.last_grid_info）
        """
        ref = old_grid_info if old_grid_info is not None else self.last_grid_info
        hard_cd = int(getattr(config, "MIN_REBUILD_INTERVAL_SEC", 300))
        local_cd_enabled = bool(getattr(config, "STRATEGY_APPLY_LOCAL_REBUILD_COOLDOWN", False))

        def _allow_rebuild(reason):
            if self._last_rebuild_time <= 0 or not local_cd_enabled:
                return True, reason
            elapsed_local = time.time() - self._last_rebuild_time
            if elapsed_local < hard_cd:
                logger.debug(
                    f"[Strategy] {reason} 但硬冷却期内 ({elapsed_local:.0f}s/{hard_cd}s)，跳过重建"
                )
                return False, "硬冷却期内，跳过重建"
            return True, reason
        
        if not ref:
            return True, "首次部署"
        
        old_price = ref['P0']
        new_price = new_grid_info['P0']
        price_change = abs(new_price - old_price) / old_price if old_price > 0 else 0
        
        if price_change > 0.02:
            return _allow_rebuild(f"价格变化 {price_change*100:.2f}%")
        
        old_atr = ref['atr']
        new_atr = new_grid_info['atr']
        atr_change = abs(new_atr - old_atr) / (old_atr + 1e-10)
        
        if atr_change > 0.30:
            return _allow_rebuild(f"ATR 变化 {atr_change*100:.2f}%")
        
        # 参数变化（自适应策略调整后重建，但有5分钟冷却期）
        for key in ['N', 'Tp', 'stop_loss_pct', 'upper_pct', 'lower_pct']:
            if ref.get(key) != new_grid_info.get(key):
                if key == "N":
                    old_n = int(ref.get("N") or self.N or 0)
                    new_n = int(new_grid_info.get("N") or old_n)
                    n_diff = abs(new_n - old_n)
                    min_diff = int(getattr(config, "N_REBUILD_MIN_DIFF", 2))
                    old_cap = float(ref.get("effective_capital", 0) or 0)
                    new_cap = float(new_grid_info.get("effective_capital", 0) or 0)
                    cap_change = abs(new_cap - old_cap) / (old_cap + 1e-10) if old_cap > 0 else 1.0
                    cap_thr = float(getattr(config, "N_REBUILD_CAPITAL_CHANGE_THRESHOLD", 0.15))

                    # 小幅变化：不立即重建，但走 debounce 路径
                    # 如果连续多轮都要求同方向变化，说明是真实需求
                    if n_diff < min_diff and cap_change < cap_thr:
                        small_confirm = int(getattr(config, "N_SMALL_CHANGE_CONFIRM_ROUNDS", 5))
                        candidate = (old_n, new_n)
                        if self._n_change_candidate == candidate:
                            self._n_change_rounds += 1
                        else:
                            self._n_change_candidate = candidate
                            self._n_change_rounds = 1

                        if self._n_change_rounds < small_confirm:
                            logger.debug(
                                f"[Strategy] 参数 N 小幅变化 {old_n}->{new_n}（Δ={n_diff}, "
                                f"确认 {self._n_change_rounds}/{small_confirm}）"
                            )
                            return False, "N 小幅变化，待确认"

                        # 连续 N 轮确认通过 → 允许重建
                        logger.info(
                            f"[Strategy] 参数 N 小幅变化 {old_n}->{new_n} 连续 {small_confirm} 轮确认，执行重建"
                        )
                        self._n_change_candidate = None
                        self._n_change_rounds = 0
                        return _allow_rebuild(f"N {old_n}->{new_n} 小幅确认重建")

                    # 连续确认，避免 3/5 来回抖动
                    candidate = (old_n, new_n)
                    if self._n_change_candidate != candidate:
                        self._n_change_candidate = candidate
                        self._n_change_rounds = 1
                        need_rounds = int(getattr(config, "N_CHANGE_CONFIRM_ROUNDS", 2))
                        if need_rounds > 1:
                            logger.debug(
                                f"[Strategy] 参数 N 待确认 {old_n}->{new_n} ({self._n_change_rounds}/{need_rounds})"
                            )
                            return False, "N 变更待确认"
                    else:
                        self._n_change_rounds += 1
                    need_rounds = int(getattr(config, "N_CHANGE_CONFIRM_ROUNDS", 2))
                    if self._n_change_rounds < need_rounds:
                        logger.debug(
                            f"[Strategy] 参数 N 待确认 {old_n}->{new_n} ({self._n_change_rounds}/{need_rounds})"
                        )
                        return False, "N 变更待确认"

                    elapsed = time.time() - self._last_rebuild_time
                    if old_n > 0 and new_n < old_n:
                        # 降层（风控收缩）应更快生效
                        fast_cd = int(getattr(config, "N_DOWNSIZE_REBUILD_COOLDOWN_SEC", 30))
                        if elapsed < fast_cd:
                            logger.debug(
                                f"[Strategy] 参数 N 下调但快速冷却期内 ({elapsed:.0f}s/{fast_cd}s)，跳过重建"
                            )
                            return False, "下调冷却期内，跳过重建"
                        self._n_change_candidate = None
                        self._n_change_rounds = 0
                        return _allow_rebuild(f"参数 N 下调 {old_n}->{new_n}")
                    elif old_n > 0 and new_n > old_n:
                        # 升层（风险放宽）保持保守节奏
                        slow_cd = int(getattr(config, "N_UPSIZE_REBUILD_COOLDOWN_SEC", 300))
                        if elapsed < slow_cd:
                            logger.debug(
                                f"[Strategy] 参数 N 上调但冷却期内 ({elapsed:.0f}s/{slow_cd}s)，跳过重建"
                            )
                            return False, "上调冷却期内，跳过重建"
                        self._n_change_candidate = None
                        self._n_change_rounds = 0
                        return _allow_rebuild(f"参数 N 上调 {old_n}->{new_n}")

                if key in ("Tp", "upper_pct", "lower_pct"):
                    old_v = ref.get(key) or 0
                    new_v = new_grid_info.get(key) or 0
                    if old_v > 0:
                        change_ratio = abs(new_v - old_v) / old_v
                        if key == "Tp":
                            threshold = float(getattr(config, "TP_REBUILD_CHANGE_THRESHOLD", 0.18))
                        else:
                            threshold = float(getattr(config, "COINGLASS_GRID_RANGE_THRESHOLD", 0.10))
                        if change_ratio < threshold:
                            logger.debug(
                                f"[Strategy] 参数 {key} 变化 {change_ratio*100:.1f}% < {threshold*100:.0f}%，忽略重建"
                            )
                            continue
                        if key == "Tp":
                            # TP 高频小抖动会导致反复"想重建→被执行层拦截"；增加确认轮次去噪
                            confirm_rounds = int(getattr(config, "TP_CHANGE_CONFIRM_ROUNDS", 3))
                            candidate = round(float(new_v), 6)
                            if self._tp_change_candidate == candidate:
                                self._tp_change_rounds += 1
                            else:
                                self._tp_change_candidate = candidate
                                self._tp_change_rounds = 1
                            if self._tp_change_rounds < confirm_rounds:
                                logger.debug(
                                    f"[Strategy] 参数 Tp 待确认 {old_v:.6f}->{new_v:.6f} "
                                    f"({self._tp_change_rounds}/{confirm_rounds})"
                                )
                                return False, "Tp 变更待确认"
                            self._tp_change_candidate = None
                            self._tp_change_rounds = 0
                elapsed = time.time() - self._last_rebuild_time
                if elapsed < 300:  # ✅ 修复：5分钟冷却期，防止参数抖动频繁重建
                    logger.debug(
                        f"[Strategy] 参数 {key} 变更但冷却期内 ({elapsed:.0f}s/300s)，跳过重建"
                    )
                    return False, "冷却期内，跳过重建"
                return _allow_rebuild(f"参数 {key} 变更")

        self._n_change_candidate = None
        self._n_change_rounds = 0
        self._tp_change_candidate = None
        self._tp_change_rounds = 0
        return False, "无需重建"
    
    def can_trade(self):
        """检查是否可以交易"""
        if time.time() < self._position_cooldown_until:
            remain = int(self._position_cooldown_until - time.time())
            return False, f"持仓冷却期中: 剩余{remain}s"

        if self._is_paused:
            return False, f"策略已暂停: {self._pause_reason}"
        
        today_trades = len([
            t for t in self.trades_today 
            if datetime.fromisoformat(t['time']).date() == datetime.now().date()
        ])
        
        if today_trades >= self.max_trades_per_day:
            return False, f"今日交易已达上限 ({today_trades})"
        
        if self.monthly_pnl >= self.monthly_target:
            return False, f"月度目标已达成 (${self.monthly_pnl:.2f})"
        
        return True, "可以交易"
    
    def record_trade(self, profit_usdt, fee_usdt=0.0):
        """
        记录交易
        ✅ 修复：PnL 扣除手续费，记录净利润

        Args:
            profit_usdt: 毛利润（GridExecutor FIFO 计算）
            fee_usdt: 手续费（可选，默认0）
        """
        net_profit = profit_usdt - fee_usdt
        trade_record = {
            'time': datetime.now().isoformat(),
            'profit': net_profit,
            'gross_profit': profit_usdt,
            'fee': fee_usdt,
        }
        with self._lock:
            self.trades_today.append(trade_record)
            self.daily_pnl += net_profit
            self.monthly_pnl += net_profit
            self._has_position = True
            self._save_trade_log()

        logger.info(
            f"[Strategy] 📈 交易记录 | 净利润: ${net_profit:.2f} "
            f"(毛利: ${profit_usdt:.2f} 费用: ${fee_usdt:.2f}) | "
            f"日盈亏: ${self.daily_pnl:.2f}"
        )
    
    def _save_trade_log(self):
        """保存交易日志"""
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trade_log.json'), 'w') as f:
                json.dump({
                    'trades_today': self.trades_today,
                    'daily_pnl': self.daily_pnl,
                    'monthly_pnl': self.monthly_pnl,
                    'last_updated': datetime.now().isoformat()
                }, f, indent=2)
        except Exception as e:
            logger.error(f"[Strategy] 保存日志失败: {e}")
    
    def activate_grid(self, grid_info, trader=None):
        """激活网格（含持仓上限/冷却期门禁）"""
        if time.time() < self._position_cooldown_until:
            remain = int(self._position_cooldown_until - time.time())
            logger.warning(f"[Strategy] 冷却期未结束，拒绝激活网格（剩余 {remain}s）")
            return False

        if trader is not None:
            try:
                pos = trader.get_position()
                bal = trader.get_balance()
                if pos and pos.get('amount') and bal and bal.get('total', 0) > 0:
                    price_ref = grid_info.get('P0') or pos.get('entry_price', 0) or 0
                    leverage = getattr(config, 'LEVERAGE', 1) or 1
                    pos_margin = abs(pos['amount']) * price_ref / leverage if price_ref > 0 else 0
                    margin_ratio = pos_margin / (bal['total'] + 1e-10)
                    max_ratio = getattr(config, 'MAX_REDEPLOY_MARGIN_RATIO', 0.50)
                    if margin_ratio >= max_ratio:
                        self._is_paused = True
                        self._pause_reason = "position_cap"
                        logger.warning(
                            f"[Strategy] 拒绝重新部署：保证金占比 {margin_ratio*100:.1f}% >= {max_ratio*100:.0f}%"
                        )
                        return False
            except Exception as e:
                logger.warning(f"[Strategy] 激活前持仓检查失败，保守拒绝激活: {e}")
                return False

        self.last_grid_info = grid_info  # ✅ 修复：部署后才更新 last_grid_info
        self._last_rebuild_time = time.time()  # ✅ 修复：记录重建时间用于冷却期
        # 将本次实际生效网格层数落到策略状态，避免"建议层数"反复打印
        applied_n = grid_info.get("N")
        if isinstance(applied_n, int) and applied_n > 0 and applied_n != self.N:
            logger.info(f"[Strategy] 生效层数更新: {self.N} -> {applied_n}")
            self.N = applied_n
            self.target_N = applied_n
            self._save_state()
        self._last_suggested_N = None
        self._n_change_candidate = None
        self._n_change_rounds = 0
        self._is_paused = False
        self._pause_reason = None
        logger.info("[Strategy] 网格已激活")
        return True
    
    def deactivate_grid(self, reason="market_state_changed"):
        """停用网格"""
        self._is_paused = True
        self._pause_reason = reason
        logger.info("[Strategy] 网格已停用")

    def set_position_cooldown(self, seconds, reason="over_exposure"):
        """设置持仓冷却期，冷却期内禁止重新部署网格"""
        sec = max(0, int(seconds))
        if sec <= 0:
            return
        self._position_cooldown_until = max(self._position_cooldown_until, time.time() + sec)
        self._is_paused = True
        self._pause_reason = reason
        logger.warning(f"[Strategy] 持仓冷却已设置: {sec}s | 原因: {reason}")
    
    @property
    def is_paused(self):
        return self._is_paused
    
    @property
    def market_state(self):
        return self._market_state
    
    def reset_daily(self, has_position=False):
        """重置每日计数器（每天零点调用）"""
        logger.info(f"[Strategy] 日度重置 | 今日盈亏: ${self.daily_pnl:.2f} | 交易数: {len(self.trades_today)}")
        with self._lock:
            prev_daily_pnl = self.daily_pnl
            self.daily_pnl = 0.0
            self.trades_today = []
            # 有持仓时不恢复层数，避免日切触发不必要的撤单重建
            if self.N != self._N_original:
                should_restore = (not has_position) and (
                    prev_daily_pnl >= 0 or not hasattr(self, '_has_position') or not self._has_position
                )
                if should_restore:
                    logger.info(f"[Strategy] 资金回升恢复 | 层数 {self.N} → {self._N_original}")
                    self.N = self._N_original
                    self.target_N = self._N_original
                    self._save_state()
                else:
                    logger.warning(
                        f"[Strategy] 跳过恢复 | has_position={has_position} "
                        f"亏损状态 {prev_daily_pnl}，保持层数 {self.N}"
                    )

    def _load_state(self):
        """✅ 修复1：从文件加载网格层数状态"""
        try:
            import os
            if os.path.exists(self._state_file):
                import json
                with open(self._state_file, 'r') as f:
                    state = json.load(f)
                if 'N' in state and state['N'] != self.N:
                    logger.info(f"[Strategy] 从文件恢复层数: {self.N} → {state['N']}")
                    self.N = state['N']
                    self.target_N = state['N']
                # ✅ 修复：恢复 _N_original，防止重启后与 N 不一致
                if 'N_original' in state:
                    self._N_original = state['N_original']
        except Exception as e:
            logger.warning(f"[Strategy] 加载状态失败: {e}")

    def _save_state(self):
        """✅ 修复1：保存网格层数到文件"""
        try:
            import json
            state = {'N': self.N, 'N_original': self._N_original}
            with open(self._state_file, 'w') as f:
                json.dump(state, f)
        except Exception as e:
            logger.warning(f"[Strategy] 保存状态失败: {e}")

    def reset_monthly(self):
        """
        ✅ 修复：重置月度计数器（每月1日零点调用）
        """
        logger.info(f"[Strategy] 月度重置 | 本月盈亏: ${self.monthly_pnl:.2f}")
        with self._lock:
            self.monthly_pnl = 0.0

