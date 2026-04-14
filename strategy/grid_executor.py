# grid_executor.py - 网格交易执行器
"""
网格订单执行模块
✅ 双向网格：多头(买低卖高) + 空头(卖高买低)
✅ FIFO 持仓队列持久化 + Binance 双向同步
✅ 移动止盈支持多空双向
✅ 止损支持多空双向
"""

import json
import math
import time
from collections import deque
from datetime import datetime, timezone
from utils.logger import get_logger
from utils.notifier import send_telegram, notify_stop_loss
import config
import os

logger = get_logger()

# ✅ 修复：手续费率常量，与 PaperTrader / Binance 一致
_MAKER_FEE = 0.0002   # 0.02% (限价单)
_TAKER_FEE = 0.0004   # 0.04% (市价单)

class GridExecutor:
    """负责在 Binance 上执行网格交易"""

    def __init__(self, trader, strategy=None, persistence_file=None, clear_on_start=False):
        self.trader = trader
        self.strategy = strategy
        self.persistence_file = persistence_file or getattr(config, "GRID_STATE_FILE", "grid_state.json")
        self._clear_on_start = clear_on_start
        self.database = None  # 由 main 注入 DatabaseManager，用于持久化成交
        self._trade_session_id = f"{int(time.time() * 1000)}-pid{os.getpid()}"

        self.active_orders = []
        self.last_grid_info = None
        self._grid_cycle_complete = False
        self._pending_tp_orders = {}  # "side_layer" -> 待挂止盈单信息

        # 持仓队列（FIFO）
        self._pending_closes = self._load_state()

        # ✅ 修复Bug2：重启后同步 _pending_closes 与实际持仓
        self._sync_pending_closes_with_position()

        # 移动止盈(Trailing Stop)状态
        self._trailing_stop_active = False
        self._highest_price_since_entry = 0.0
        self._lowest_price_since_entry = float('inf')  # ✅ 修复Bug3：空头追踪最低价
        self._trailing_stop_entry_price = 0.0
        self._deploy_timestamps = deque()
        self._fill_timestamps = deque(maxlen=500)
        self._last_fill_ts = time.time()
        self._no_fill_anchor_ts = self._last_fill_ts
        self._last_no_fill_rebuild_ts = 0.0
        self._no_fill_reset_by_rebuild = False

        # 单边成交计数（连续同方向成交保护）
        self._consecutive_side = ""    # "long" / "short"
        self._consecutive_count = 0
        self._consecutive_peak_count = 0  # 衰减用：记录峰值
        self._side_blocked = ""        # 被阻断的方向
        self._max_consecutive_same_side = int(getattr(config, "MAX_CONSECUTIVE_SAME_SIDE", 2))

        logger.info("[GridExecutor] 已初始化")

    def _sync_pending_closes_with_position(self):
        """
        ✅ 修复：确保 _pending_closes 与实际持仓双向同步

        - Binance 有仓位但 FIFO 不足 → 补建记录
        - Binance 无仓位但 FIFO 有记录 → 清空幽灵记录
        - Binance 仓位量 < FIFO 总量 → 截断 FIFO 使之匹配
        """
        try:
            position = self.trader.get_position()

            # ── 场景1：Binance 无持仓 → 清空 FIFO 幽灵记录 ──
            if not position or position.get('amount', 0) == 0:
                if self._pending_closes:
                    stale_count = len(self._pending_closes)
                    stale_amount = sum(p['amount'] for p in self._pending_closes)
                    self._pending_closes = []
                    self._save_state()
                    logger.warning(
                        f"[GridExecutor] ⚠️ Binance 无持仓，清空 {stale_count} 条幽灵 FIFO 记录 "
                        f"(总量: {stale_amount:.6f} BTC)"
                    )
                return

            actual_amount = abs(position['amount'])
            actual_side = 'long' if position['amount'] > 0 else 'short'
            entry_price = position.get('entry_price', 0)

            # 只保留同方向记录，清除反方向的幽灵记录
            same_side = [p for p in self._pending_closes if p.get('side', 'long') == actual_side]
            other_side = [p for p in self._pending_closes if p.get('side', 'long') != actual_side]
            if other_side:
                logger.warning(
                    f"[GridExecutor] ⚠️ 清除 {len(other_side)} 条反方向 FIFO 记录"
                )

            pending_amount = sum(p['amount'] for p in same_side)

            # ── 场景2：FIFO 总量 > 实际持仓 → 截断 FIFO ──
            if pending_amount > actual_amount + 0.0001:
                logger.warning(
                    f"[GridExecutor] ⚠️ FIFO ({pending_amount:.6f}) > 实际持仓 ({actual_amount:.6f})，"
                    f"用 Binance 入场价重建"
                )
                # 直接用 Binance 的真实入场价重建整个 FIFO
                self._pending_closes = [{
                    'entry_price': entry_price,
                    'buy_price': entry_price,
                    'amount': round(actual_amount, 8),
                    'side': actual_side,
                    'layer': 0,
                    'entry_time': datetime.now().isoformat(),
                    'entry_fee_rate': _MAKER_FEE,
                    '_synced': True,
                }]
                self._save_state()
                logger.info(
                    f"[GridExecutor] 已重建 FIFO | {actual_side} {actual_amount:.6f} BTC @ ${entry_price:.2f}"
                )
                return

            # ── 场景3：FIFO 不足 → 补建记录 ──
            self._pending_closes = same_side  # 丢弃反方向记录
            gap = actual_amount - pending_amount
            if gap > 0.0001 and entry_price > 0:
                self._pending_closes.append({
                    'entry_price': entry_price,
                    'buy_price': entry_price,
                    'amount': round(gap, 8),
                    'side': actual_side,
                    'layer': 0,
                    'entry_time': datetime.now().isoformat(),
                    'entry_fee_rate': _MAKER_FEE,
                    '_synced': True,
                })
                self._save_state()
                logger.info(
                    f"[GridExecutor] 同步持仓到 FIFO 队列 | "
                    f"{actual_side} {gap:.6f} BTC @ ${entry_price:.2f}"
                )
            elif other_side:
                # 只是清除了反方向记录，也要保存
                self._save_state()

        except Exception as e:
            logger.warning(f"[GridExecutor] 持仓同步检查失败: {e}")

    def _load_state(self):
        """从文件恢复持仓状态"""
        if getattr(self, "_clear_on_start", False):
            logger.info("[GridExecutor] clear_on_start=True，忽略旧持仓状态")
            return []
        try:
            with open(self.persistence_file, 'r') as f:
                state = json.load(f)
                logger.info(f"[GridExecutor] 从文件恢复 {len(state)} 笔待平仓持仓")
                return state
        except FileNotFoundError:
            logger.debug(f"[GridExecutor] 持仓文件不存在，初始化为空")
            return []
        except Exception as e:
            logger.error(f"[GridExecutor] 加载持仓失败: {e}")
            return []

    def _save_state(self):
        """持久化持仓状态"""
        try:
            with open(self.persistence_file, 'w') as f:
                json.dump(self._pending_closes, f, indent=2)
                logger.debug(f"[GridExecutor] 已保存 {len(self._pending_closes)} 笔待平仓持仓")
        except Exception as e:
            logger.error(f"[GridExecutor] 保存持仓失败: {e}")

    _trade_id_counter = 0  # 类级别自增 ID

    def _log_binance_trade(self, side, price, amount, realized_pnl=0.0, fee_rate=0.0):
        """
        按 Binance 风格记录成交

        Fields: id, time, symbol, side, price, qty, realizedPnl, commission, commissionAsset
        ✅ 修复：统一使用 UTC ISO 格式 + 自增 trade_id 用于对账匹配
        """
        try:
            GridExecutor._trade_id_counter += 1
            trade_id = f"local_{int(time.time()*1000)}_{GridExecutor._trade_id_counter}"
            trade_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            commission = price * amount * fee_rate
            runtime_mode = str(getattr(config, "TRADING_MODE", "paper") or "paper").lower()
            strategy_mode = str(getattr(config, "TRADING_MODE_ACTIVE", "normal") or "normal").lower()
            record = {
                "id": trade_id,
                "time": trade_time,
                "symbol": config.SYMBOL,
                "side": side.upper(),
                "price": round(price, 2),
                "qty": round(amount, 6),
                "realizedPnl": round(realized_pnl, 2),
                "commission": round(commission, 4),
                "commissionAsset": "USDT",
                "mode": strategy_mode,
                "live_or_paper": runtime_mode,
                "session_id": self._trade_session_id,
            }
            logger.info(
                f"[BinanceTrade] time={trade_time} "
                f"symbol={config.SYMBOL} side={side.upper()} "
                f"price={price:.2f} qty={amount:.6f} "
                f"realizedPnl={realized_pnl:.2f} "
                f"commission={commission:.4f} commissionAsset=USDT "
                f"mode={strategy_mode} live_or_paper={runtime_mode} "
                f"session_id={self._trade_session_id}"
            )
            # 统一落库：为训练/复盘提供真实成交与手续费数据
            db = getattr(self, "database", None)
            if db is not None and hasattr(db, "record_trade"):
                try:
                    db.record_trade(
                        side=str(side).lower(),
                        price=float(price or 0.0),
                        amount=float(amount or 0.0),
                        fee=float(commission or 0.0),
                        realized_pnl=float(realized_pnl or 0.0),
                        strategy_name="grid",
                        timestamp=datetime.now().isoformat(),
                        runtime_mode=runtime_mode,
                        strategy_mode=strategy_mode,
                        session_id=self._trade_session_id,
                    )
                except Exception as db_e:
                    logger.debug(f"[GridExecutor] trades落库失败: {db_e}")
            self._append_trade_log(record)
        except Exception as e:
            logger.debug(f"[GridExecutor] Binance风格成交记录失败: {e}")

    def _append_trade_log(self, record):
        """追加写入 Binance 风格 JSONL 交易日志"""
        try:
            log_dir = os.path.join(getattr(config, "LOG_DIR", "data/logs"), "events")
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, "binance_trades.jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"[GridExecutor] 写入 binance_trades.jsonl 失败: {e}")

    def _log_single_pnl(self, net_profit):
        """单笔盈亏记录（正=盈利，负=亏损）"""
        tag = "PNL+" if net_profit >= 0 else "PNL-"
        logger.info(f"[{tag}] time={datetime.now().isoformat()} net={net_profit:.2f} USDT")

    def _close_position_safely(self, side, amount, current_price, reason="exit"):
        """根据仓位规模选择市价或 TWAP 平仓"""
        notional = amount * current_price
        threshold = getattr(config, "EXIT_LARGE_POSITION_USDT", 5000)
        if notional >= threshold:
            return self._close_position_twap(side, amount, current_price, reason)
        return self._close_position_immediate(side, amount, current_price, reason)

    def _close_position_immediate(self, side, amount, current_price, reason="exit"):
        order = self.trader.place_market_order(side, amount, reduce_only=True)
        if not order:
            logger.error(f"[GridExecutor] ❌ 平仓失败({reason})")
            return None
        return order

    def _extract_filled_amount(self, order, fallback_amount):
        """提取实际成交量，避免用计划下单量误记账。"""
        if not isinstance(order, dict):
            return float(fallback_amount)
        raw = order.get("filled", None)
        if raw is None:
            raw = order.get("amount", fallback_amount)
        try:
            filled = float(raw)
        except Exception:
            filled = float(fallback_amount)
        return max(0.0, filled)

    def _close_position_twap(self, side, amount, current_price, reason="exit"):
        slices = max(1, int(getattr(config, "TWAP_SLICES", 5)))
        interval = max(1, int(getattr(config, "TWAP_INTERVAL_SEC", 5)))
        slice_amt = round(amount / slices, 6)
        remaining = amount
        total_filled = 0.0
        total_cost = 0.0
        logger.warning(
            f"[GridExecutor] 🧩 TWAP 平仓({reason}) | {slices}片, 每片 {slice_amt} BTC"
        )
        for i in range(slices):
            qty = slice_amt if i < slices - 1 else round(remaining, 6)
            if qty <= 0:
                break
            order = self.trader.place_market_order(side, qty, reduce_only=True)
            if order:
                filled = float(order.get("filled", qty))
                avg = float(order.get("average", order.get("price", current_price)))
                total_filled += filled
                total_cost += filled * avg
            remaining = round(remaining - qty, 6)
            time.sleep(interval)
        if total_filled <= 0:
            return None
        avg_price = total_cost / total_filled if total_filled > 0 else current_price
        return {"average": avg_price, "filled": total_filled, "price": avg_price}

    def _record_cb_trade(self, net_profit):
        """将交易盈亏同步给断路器（用于亏损笔数统计）"""
        cb = getattr(self, "circuit_breaker", None)
        if not cb:
            return
        try:
            cb.record_trade(net_profit)
        except Exception:
            pass

    def _fifo_match_close(self, close_price, close_amount, is_taker=False, close_side=None):
        """
        FIFO 匹配平仓，正确计算多/空方向 PnL
        ✅ 修复：计算手续费，返回 (毛利润, 手续费)
        ✅ 修复：添加 close_side 参数，只匹配对应方向的 FIFO 记录

        Args:
            close_price: 平仓成交价
            close_amount: 平仓数量 (BTC)
            is_taker: 是否为 taker（市价单）
            close_side: 平仓方向 'long'（卖出平多）或 'short'（买回平空）

        Returns:
            tuple: (gross_profit, total_fee) 毛利润和总手续费 (USDT)
        """
        if not self._pending_closes:
            logger.warning(
                f"[GridExecutor] ⚠️ FIFO 队列为空，无法匹配平仓 "
                f"{close_amount:.6f} BTC @ ${close_price:.2f}"
            )
            return 0.0, 0.0

        total_profit = 0.0
        total_fee = 0.0
        remaining = close_amount

        # ✅ 修复：按方向过滤，只匹配对应 side 的 FIFO 记录
        i = 0
        while remaining > 0 and i < len(self._pending_closes):
            pending = self._pending_closes[i]
            side = pending.get('side', 'long')

            # 跳过方向不匹配的记录
            if close_side and side != close_side:
                i += 1
                continue

            match_qty = min(remaining, pending['amount'])
            entry_price = pending.get('entry_price', pending.get('buy_price', 0))

            if side == 'short':
                total_profit += (entry_price - close_price) * match_qty
            else:
                total_profit += (close_price - entry_price) * match_qty

            # 手续费：开仓费率(按入场来源记录) + 平仓 maker/taker
            close_fee_rate = _TAKER_FEE if is_taker else _MAKER_FEE
            entry_fee_rate = float(pending.get('entry_fee_rate', _MAKER_FEE) or _MAKER_FEE)
            fee_open = entry_price * match_qty * entry_fee_rate
            fee_close = close_price * match_qty * close_fee_rate
            total_fee += fee_open + fee_close

            remaining -= match_qty
            if match_qty >= pending['amount']:
                self._pending_closes.pop(i)
            else:
                pending['amount'] -= match_qty
                i += 1

        if remaining > 0.0001:
            logger.warning(
                f"[GridExecutor] ⚠️ FIFO 匹配不足 | side={close_side or '-'} "
                f"需平={close_amount:.6f} 已匹配={close_amount - remaining:.6f} 剩余={remaining:.6f}"
            )
        return round(total_profit, 2), round(total_fee, 4)

    def _estimate_close_pnl_from_entry(self, entry_price, close_price, close_amount, close_side, is_taker=False):
        """当 FIFO 丢失时，按入场价估算平仓毛利与手续费。"""
        try:
            ep = float(entry_price or 0.0)
            cp = float(close_price or 0.0)
            qty = float(close_amount or 0.0)
            if ep <= 0 or cp <= 0 or qty <= 0:
                return 0.0, 0.0
            side = str(close_side or "").lower()
            if side == "short":
                gross = (ep - cp) * qty
            else:
                gross = (cp - ep) * qty
            close_fee_rate = _TAKER_FEE if is_taker else _MAKER_FEE
            fee_open = ep * qty * _MAKER_FEE
            fee_close = cp * qty * close_fee_rate
            return round(gross, 2), round(fee_open + fee_close, 4)
        except Exception:
            return 0.0, 0.0

    def _reset_trailing_stop(self):
        """重置移动止盈状态"""
        self._trailing_stop_active = False
        self._highest_price_since_entry = 0.0
        self._lowest_price_since_entry = float('inf')
        self._trailing_stop_entry_price = 0.0

    @staticmethod
    def _as_bool(v):
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "y", "on")
        return bool(v)

    @classmethod
    def _is_reduce_only_order(cls, order):
        if not isinstance(order, dict):
            return False
        if "reduceOnly" in order:
            return cls._as_bool(order.get("reduceOnly"))
        if "reduce_only" in order:
            return cls._as_bool(order.get("reduce_only"))
        info = order.get("info", {}) if isinstance(order.get("info"), dict) else {}
        if "reduceOnly" in info:
            return cls._as_bool(info.get("reduceOnly"))
        if "reduce_only" in info:
            return cls._as_bool(info.get("reduce_only"))
        return False

    @staticmethod
    def _extract_order_id(order):
        if not isinstance(order, dict):
            return ""
        oid = order.get("order_id")
        if oid is None:
            oid = order.get("id")
        return str(oid or "")

    def _get_open_orders_snapshot(self):
        """获取交易所挂单快照；None 代表当前挂单状态未知。"""
        try:
            orders = self.trader.get_open_orders()
        except Exception:
            return None
        if orders is None:
            return None
        return list(orders)

    def _release_tp_tracking(self, order, reason="tp_release"):
        grid_side = str(order.get('grid_side', '') or '')
        if grid_side not in ('long_tp', 'short_tp'):
            return
        if not hasattr(self.trader, 'adjust_local_ro_reserved'):
            return
        tp_side = 'sell' if grid_side == 'long_tp' else 'buy'
        try:
            release_qty = abs(float(order.get('amount', 0.0) or 0.0))
        except Exception:
            release_qty = 0.0
        if release_qty > 0:
            self.trader.adjust_local_ro_reserved(tp_side, -release_qty, reason=reason)

    def _get_reduce_only_available(self, side: str):
        """
        估算指定方向 reduceOnly 可用减仓量。
        side: 'sell' (平多) / 'buy' (平空)
        """
        try:
            if hasattr(self.trader, "get_reduce_only_capacity"):
                cap = self.trader.get_reduce_only_capacity(side)
                if isinstance(cap, (list, tuple)) and len(cap) >= 3:
                    available = float(cap[0] or 0.0)
                    reserved = float(cap[1] or 0.0)
                    max_reducible = float(cap[2] or 0.0)
                    return round(max(0.0, available), 6), round(max(0.0, reserved), 6), round(max(0.0, max_reducible), 6)
            pos = self.trader.get_position()
            if not pos:
                return 0.0, 0.0, 0.0
            pos_amt = float(pos.get('amount', 0.0) or 0.0)
            if abs(pos_amt) < 1e-9:
                return 0.0, 0.0, 0.0
            if side == 'sell' and pos_amt <= 0:
                return 0.0, 0.0, abs(pos_amt)
            if side == 'buy' and pos_amt >= 0:
                return 0.0, 0.0, abs(pos_amt)

            max_reducible = abs(pos_amt)
            reserved = 0.0

            try:
                open_orders = self.trader.get_open_orders() or []
            except Exception:
                open_orders = []
            for o in open_orders:
                if not self._is_reduce_only_order(o):
                    continue
                if str(o.get('side', '')).lower() != str(side).lower():
                    continue
                rem = o.get('remaining', None)
                if rem is None:
                    rem = o.get('amount', 0)
                try:
                    reserved += abs(float(rem or 0.0))
                except Exception:
                    continue

            # 本地订单兜底（防 API 延迟）
            local_reserved = 0.0
            for o in (self.active_orders or []):
                if str(o.get('side', '')).lower() != str(side).lower():
                    continue
                gs = str(o.get('grid_side', '') or '')
                if gs not in ('long_tp', 'short_tp'):
                    continue
                try:
                    local_reserved += abs(float(o.get('amount', 0.0) or 0.0))
                except Exception:
                    continue

            step = float(getattr(config, 'ORDER_STEP_SIZE', 0.001) or 0.001)
            reserved = max(reserved, local_reserved)
            available = max(0.0, max_reducible - reserved)
            if step > 0:
                available = math.floor(available / step) * step
            return round(max(0.0, available), 6), round(reserved, 6), round(max_reducible, 6)
        except Exception:
            return 0.0, 0.0, 0.0

    def _cancel_stale_tp_orders(self, side: str, binance_open_orders: list):
        """撤销 Binance 上指定方向的旧 reduceOnly TP 单，为重挂腾出额度"""
        cancelled = 0
        for o in binance_open_orders:
            if str(o.get('side', '')).lower() != side.lower():
                continue
            if not self._is_reduce_only_order(o):
                continue
            oid = o.get('id') or o.get('order_id')
            if oid:
                try:
                    if self.trader.cancel_order(oid) is True:
                        cancelled += 1
                except Exception:
                    pass
        if cancelled:
            logger.info(f"[GridExecutor] 撤销 {cancelled} 个旧TP单，准备按当前仓位重挂")
            # 清理本地 reduceOnly 记录
            if hasattr(self.trader, 'adjust_local_ro_reserved'):
                current = float(getattr(self.trader, "_local_ro_reserved", {}).get(side, 0.0) or 0.0)
                if current > 0:
                    self.trader.adjust_local_ro_reserved(side, -current, reason="撤旧TP后释放")
            # 等待 Binance 撤单生效
            time.sleep(0.5)
            self.active_orders = [
                o for o in self.active_orders
                if not (
                    str(o.get("side", "")).lower() == side.lower()
                    and str(o.get("grid_side", "") or "") in ("long_tp", "short_tp")
                )
            ]

    def _free_reduce_only_capacity(self, side: str, required_qty: float, keep_layer=None):
        """
        释放同侧 reduceOnly 额度：优先撤销 orphan_tp，再撤销其他层 TP。
        返回已释放估算数量。
        """
        released = 0.0
        # 候选：仅同侧 TP 订单
        candidates = []
        for o in (self.active_orders or []):
            if str(o.get('side', '')).lower() != str(side).lower():
                continue
            gs = str(o.get('grid_side', '') or '')
            if gs not in ('long_tp', 'short_tp'):
                continue
            layer = int(o.get('layer', -1) or -1)
            if keep_layer is not None and layer == int(keep_layer):
                continue
            orphan = bool(o.get('_orphan_tp', False))
            qty = abs(float(o.get('amount', 0.0) or 0.0))
            oid = o.get('order_id')
            if not oid:
                continue
            # orphan 优先撤，且小单优先释放，尽量减少影响
            candidates.append((0 if orphan else 1, qty, oid, qty))

        candidates.sort(key=lambda x: (x[0], x[1]))
        for _, _, oid, qty in candidates:
            if released >= required_qty:
                break
            try:
                ok = self.trader.cancel_order(oid)
                if ok is True:
                    released += qty
                    if hasattr(self.trader, 'adjust_local_ro_reserved'):
                        self.trader.adjust_local_ro_reserved(side, -qty, reason="腾挪容量撤TP")
            except Exception:
                continue
        if released > 0:
            logger.warning(f"[GridExecutor] 为腾挪 reduceOnly 容量，撤销同侧TP约 {released:.6f} BTC")
        return round(released, 6)

    def _place_reduce_only_with_rebalance(self, side, price, qty, *, keep_layer=None, context="", min_qty=None, quantize=None):
        """
        reduceOnly 下单助手：
        1) 先正常下单
        2) 若失败，按可用容量判断并尝试腾挪同侧 TP
        3) 容量恢复后按可用量收敛重试
        返回: (order_or_none, placed_qty)
        """
        qty = float(qty or 0.0)
        if quantize:
            qty = float(quantize(qty))
        step = float(min_qty if min_qty is not None else getattr(config, 'ORDER_STEP_SIZE', 0.001) or 0.001)
        if qty < step:
            return None, 0.0

        order = self.trader.place_limit_order(side, price, qty, reduce_only=True)
        if order:
            placed = float(order.get('amount', qty) or qty) if isinstance(order, dict) else qty
            return order, round(placed, 6)

        available, reserved, max_reducible = self._get_reduce_only_available(side)
        logger.warning(
            f"[GridExecutor] reduceOnly补挂失败，尝试腾挪容量 | side={side} req={qty:.6f} "
            f"可减={max_reducible:.6f} 已占用={reserved:.6f} 可用={available:.6f}"
            + (f" | {context}" if context else "")
        )
        need_qty = max(step, qty - max(0.0, available))
        self._free_reduce_only_capacity(side, need_qty, keep_layer=keep_layer)

        retry_avail, retry_reserved, retry_max = self._get_reduce_only_available(side)
        retry_qty = min(qty, retry_avail)
        if quantize:
            retry_qty = float(quantize(retry_qty))
        retry_qty = round(retry_qty, 6)
        if retry_qty < step:
            logger.warning(
                f"[GridExecutor] reduceOnly重试跳过：可用量仍不足 | side={side} req={qty:.6f} "
                f"可减={retry_max:.6f} 已占用={retry_reserved:.6f} 可用={retry_avail:.6f}"
                + (f" | {context}" if context else "")
            )
            return None, 0.0

        retry = self.trader.place_limit_order(side, price, retry_qty, reduce_only=True)
        if retry:
            placed = float(retry.get('amount', retry_qty) or retry_qty) if isinstance(retry, dict) else retry_qty
            logger.warning(
                f"[GridExecutor] reduceOnly重试成功 | side={side} qty={placed:.6f}"
                + (f" | {context}" if context else "")
            )
            return retry, round(placed, 6)

        return None, 0.0

    def _place_tp_order(self, tp_info, filled_amount, filled_price, layer_idx):
        """
        入场单成交后挂出对应的止盈单（多空通用）

        多头入场(买)成交 → 挂止盈卖单
        空头入场(卖)成交 → 挂止盈买单

        Args:
            tp_info: 止盈信息 dict (来自 _pending_tp_orders)
            filled_amount: 实际成交数量
            filled_price: 实际成交价格
            layer_idx: 网格层索引
        """
        entry_side = tp_info.get('entry_side', 'long')
        # 多头止盈 = 卖出, 空头止盈 = 买回
        tp_order_side = 'sell' if entry_side == 'long' else 'buy'
        # grid_side 标记：用于 check_fills 区分"入场"和"止盈"
        tp_grid_side = 'long_tp' if entry_side == 'long' else 'short_tp'

        min_notional = getattr(config, 'MIN_ORDER_NOTIONAL', 130)
        min_btc = getattr(config, 'ORDER_STEP_SIZE', 0.001)

        def _quantize(qty):
            # 向下贴合交易所步长，避免下单时被隐式截断导致名义价值不达标
            if min_btc <= 0:
                return round(qty, 6)
            return round(math.floor(qty / min_btc) * min_btc, 6)

        if tp_info.get('tiered'):
            # 先检查分层 TP 是否每层都满足最小名义价值
            tp_levels = tp_info.get('tp_levels', [])
            ratio = filled_amount / (tp_info.get('original_size', filled_amount) or filled_amount)
            can_tier = True
            for tp_level in tp_levels:
                tp_size = tp_level['size_btc']
                adjusted = _quantize(tp_size * ratio) if ratio != 1.0 else _quantize(tp_size)
                tp_price = tp_level.get('tp_price', tp_level.get('sell_price', 0))
                if adjusted < min_btc or adjusted * tp_price < min_notional:
                    can_tier = False
                    break

            if can_tier:
                # 分层止盈：每层数量足够
                for tp_level in tp_levels:
                    tp_price = tp_level.get('tp_price', tp_level.get('sell_price', 0))
                    tp_size = tp_level['size_btc']
                    adjusted_size = _quantize(tp_size * ratio) if ratio != 1.0 else _quantize(tp_size)

                    valid = (tp_price > filled_price) if entry_side == 'long' else (tp_price < filled_price)
                    if adjusted_size > 0.0001 and valid:
                        order, placed_qty = self._place_reduce_only_with_rebalance(
                            tp_order_side, tp_price, adjusted_size,
                            keep_layer=layer_idx,
                            context=f"L{layer_idx}-TP{tp_level.get('tp_level', 0)}",
                            min_qty=min_btc,
                            quantize=_quantize
                        )
                        if order:
                            self.active_orders.append({
                                'order_id': order['id'],
                                'side': tp_order_side,
                                'grid_side': tp_grid_side,
                                'price': tp_price,
                                'amount': placed_qty,
                                'entry_price': filled_price,
                                'layer': layer_idx,
                                'tp_level': tp_level.get('tp_level', 0),
                                'est_profit': tp_level.get('est_profit', 0),
                            })
                            side_label = "卖" if entry_side == 'long' else "买"
                            logger.info(
                                f"[GridExecutor] 📤 止盈{side_label}单已挂出 | L{layer_idx}-TP{tp_level.get('tp_level', 0)} | "
                                f"价格: ${tp_price:.2f} | 数量: {adjusted_size:.6f} BTC"
                            )
            else:
                # 回退：仓位太小无法分层，用第一层 TP 价格挂整仓止盈
                fallback_tp = tp_levels[0] if tp_levels else None
                if fallback_tp and filled_amount >= min_btc:
                    tp_price = fallback_tp.get('tp_price', fallback_tp.get('sell_price', 0))
                    valid = (tp_price > filled_price) if entry_side == 'long' else (tp_price < filled_price)
                    if valid:
                        q = _quantize(filled_amount)
                        order, placed_qty = self._place_reduce_only_with_rebalance(
                            tp_order_side, tp_price, q,
                            keep_layer=layer_idx,
                            context=f"L{layer_idx}-fallback",
                            min_qty=min_btc,
                            quantize=_quantize
                        )
                        if order:
                            self.active_orders.append({
                                'order_id': order['id'],
                                'side': tp_order_side,
                                'grid_side': tp_grid_side,
                                'price': tp_price,
                                'amount': placed_qty,
                                'entry_price': filled_price,
                                'layer': layer_idx,
                                'tp_level': 1,
                                'est_profit': fallback_tp.get('est_profit', 0),
                            })
                            side_label = "卖" if entry_side == 'long' else "买"
                            logger.info(
                                f"[GridExecutor] 📤 止盈{side_label}单(整仓回退) | L{layer_idx} | "
                                f"价格: ${tp_price:.2f} | 数量: {placed_qty:.6f} BTC"
                            )
                        else:
                            available, reserved, max_reducible = self._get_reduce_only_available(tp_order_side)
                            covered = (max_reducible > 0 and (max_reducible - available) >= max(0.0, max_reducible - 0.001))
                            if covered:
                                logger.info(
                                    f"[GridExecutor] 整仓回退止盈跳过：同侧 reduceOnly 已覆盖 | "
                                    f"可减={max_reducible:.6f} 已占用={reserved:.6f} 可用={available:.6f} | L{layer_idx}"
                                )
                            else:
                                logger.error(f"[GridExecutor] ❌ 整仓回退止盈单失败 | L{layer_idx}")
                else:
                    logger.warning(
                        f"[GridExecutor] ⚠️ 仓位 {filled_amount} BTC 太小，无法挂止盈单 | L{layer_idx}"
                    )
        else:
            tp_price = tp_info.get('tp_price', tp_info.get('sell_price', 0))
            valid = (tp_price > filled_price) if entry_side == 'long' else (tp_price < filled_price)
            q = _quantize(filled_amount)
            if not valid:
                logger.warning(
                    f"[GridExecutor] 跳过止盈挂单：目标价方向无效 | L{layer_idx} | tp={tp_price:.2f} fill={filled_price:.2f}"
                )
                return
            if q < min_btc:
                logger.warning(
                    f"[GridExecutor] 跳过止盈挂单：数量不足最小步长 | L{layer_idx} | q={q:.6f}<{min_btc:.6f}"
                )
                return
            if q * tp_price < min_notional:
                logger.warning(
                    f"[GridExecutor] 跳过止盈挂单：名义价值不足 | L{layer_idx} | "
                    f"notional=${q * tp_price:.2f} < ${min_notional:.2f}"
                )
                return
            if valid and q > 0.0001:
                order, placed_qty = self._place_reduce_only_with_rebalance(
                    tp_order_side, tp_price, q,
                    keep_layer=layer_idx,
                    context=f"L{layer_idx}",
                    min_qty=min_btc,
                    quantize=_quantize
                )
                if order:
                    self.active_orders.append({
                        'order_id': order['id'],
                        'side': tp_order_side,
                        'grid_side': tp_grid_side,
                        'price': tp_price,
                        'amount': placed_qty,
                        'entry_price': filled_price,
                        'layer': layer_idx,
                        'est_profit': tp_info.get('est_profit', 0),
                    })
                    side_label = "卖" if entry_side == 'long' else "买"
                    logger.info(
                        f"[GridExecutor] 📤 止盈{side_label}单已挂出 | L{layer_idx} | "
                        f"价格: ${tp_price:.2f} | 数量: {placed_qty:.6f} BTC"
                    )
                else:
                    logger.error(
                        f"[GridExecutor] ❌ 止盈单失败 | L{layer_idx} | tp=${tp_price:.2f} qty={q:.6f}"
                    )

    def _place_tp_for_orphaned_positions(self, grid_info, tp_pct_override=None, ignore_binance_existing=False):
        """
        为没有止盈单的旧持仓补挂止盈单（多空通用）
        以 Binance 实际持仓为准，防止基于幽灵记录开仓
        ✅ 修复：用 FIFO 记录的入场价计算 TP，而非当前价
        ✅ 修复：查询 Binance 实际挂单而非仅检查 local active_orders
        """
        if not self._pending_closes:
            return

        position = self.trader.get_position()
        if not position or position.get('amount', 0) == 0:
            logger.info("[GridExecutor] 无实际持仓，跳过补挂止盈")
            return

        actual_amount = abs(position['amount'])
        actual_side = 'long' if position['amount'] > 0 else 'short'

        current_price = grid_info['P0']
        tp_pct = tp_pct_override if tp_pct_override is not None else grid_info.get('Tp', getattr(config, 'TP_PER_GRID', 0.01))

        # ✅ 修复：从 FIFO 记录中获取加权平均入场价
        same_side_entries = [p for p in self._pending_closes if p.get('side', 'long') == actual_side]
        if same_side_entries:
            total_qty = sum(p['amount'] for p in same_side_entries)
            if total_qty > 0:
                entry_price = sum(p['entry_price'] * p['amount'] for p in same_side_entries) / total_qty
            else:
                entry_price = current_price
        else:
            entry_price = position.get('entry_price', current_price)

        # 🕐 时间衰减：持仓越久 → TP 越近 → 快走
        try:
            entries_with_time = [
                p for p in same_side_entries
                if 'entry_time' in p and p['entry_time']
            ]
            if entries_with_time:
                oldest = min(
                    datetime.fromisoformat(p['entry_time']) for p in entries_with_time
                )
                holding_min = (datetime.now() - oldest).total_seconds() / 60.0
                decay_60 = float(getattr(config, "ORPHAN_TP_DECAY_60MIN", 0.60))
                decay_30 = float(getattr(config, "ORPHAN_TP_DECAY_30MIN", 0.80))
                tp_floor = float(getattr(config, "ORPHAN_TP_FLOOR", 0.002))
                if holding_min > 60:
                    tp_pct = max(tp_floor, tp_pct * decay_60)
                    logger.info(f"[GridExecutor] TP时间衰减 | 持仓{holding_min:.0f}min → TP={tp_pct*100:.2f}%")
                elif holding_min > 30:
                    tp_pct = max(tp_floor, tp_pct * decay_30)
                    logger.info(f"[GridExecutor] TP时间衰减 | 持仓{holding_min:.0f}min → TP={tp_pct*100:.2f}%")
        except Exception as e:
            logger.debug(f"[GridExecutor] TP时间衰减计算跳过: {e}")

        # ✅ 返回 TP 价格供 deploy_grid 使用
        self._orphan_tp_price = None

        # ✅ 修复：查询 Binance 实际 reduceOnly 挂单，而非仅依赖 local active_orders
        # 重启后 active_orders 为空，但 Binance 可能仍有未撤销的 reduceOnly 订单
        binance_open_orders = self.trader.get_open_orders() or []
        binance_sell_tp_amount = sum(
            abs(o.get('amount', o.get('remaining', 0)))
            for o in binance_open_orders
            if o.get('side', '').lower() == 'sell' and self._is_reduce_only_order(o)
        )
        binance_buy_tp_amount = sum(
            abs(o.get('amount', o.get('remaining', 0)))
            for o in binance_open_orders
            if o.get('side', '').lower() == 'buy' and self._is_reduce_only_order(o)
        )

        if actual_side == 'long':
            # 多头持仓 → 补挂止盈卖单
            # 取 local 和 Binance 中较大的已覆盖量
            local_tp_amount = sum(
                o['amount'] for o in self.active_orders
                if o['side'] == 'sell' and o.get('grid_side') in ('long_tp', None)
            )
            existing_tp_amount = local_tp_amount if ignore_binance_existing else max(local_tp_amount, binance_sell_tp_amount)
            orphan_amount = actual_amount - existing_tp_amount
            # 差额太小无法单独下单 → 检查是否需要撤旧重挂
            min_notional = float(getattr(config, 'MIN_ORDER_NOTIONAL_EXCHANGE_FLOOR', 100))
            if orphan_amount > 0 and orphan_amount * current_price < min_notional:
                if existing_tp_amount > 0:
                    # 旧 TP 覆盖了大部分仓位，差额低于最小名义，撤旧按全仓重挂
                    self._cancel_stale_tp_orders('sell', binance_open_orders)
                    orphan_amount = actual_amount
                else:
                    return  # 无旧 TP 且量太小，跳过
            if orphan_amount < 0.001:
                return
            # ✅ 修复：基于入场价计算 TP，确保达到真正的 0.8% 盈利
            tp_price = round(entry_price * (1 + tp_pct), 2)
            if tp_price <= current_price:
                tp_price = round(current_price * (1 + tp_pct / 2), 2)
            self._orphan_tp_price = tp_price
            req_qty = round(orphan_amount, 6)
            order, placed_qty = self._place_reduce_only_with_rebalance(
                'sell', tp_price, req_qty, keep_layer=0, context="orphan-long", min_qty=0.001
            )
            if order:
                self.active_orders.append({
                    'order_id': order['id'],
                    'side': 'sell',
                    'grid_side': 'long_tp',
                    'price': tp_price,
                    'amount': placed_qty,
                    'entry_price': entry_price,
                    'layer': 0,
                    'est_profit': round(placed_qty * entry_price * tp_pct, 2),
                    '_orphan_tp': True,
                })
                logger.info(
                    f"[GridExecutor] 📤 补挂止盈卖单（多头旧持仓） | "
                    f"数量: {placed_qty:.6f} BTC | 入场: ${entry_price:.2f} | 卖价: ${tp_price:.2f}"
                )
        else:
            # 空头持仓 → 补挂止盈买单
            local_tp_amount = sum(
                o['amount'] for o in self.active_orders
                if o['side'] == 'buy' and o.get('grid_side') == 'short_tp'
            )
            existing_tp_amount = local_tp_amount if ignore_binance_existing else max(local_tp_amount, binance_buy_tp_amount)
            orphan_amount = actual_amount - existing_tp_amount
            min_notional = float(getattr(config, 'MIN_ORDER_NOTIONAL_EXCHANGE_FLOOR', 100))
            if orphan_amount > 0 and orphan_amount * current_price < min_notional:
                if existing_tp_amount > 0:
                    self._cancel_stale_tp_orders('buy', binance_open_orders)
                    orphan_amount = actual_amount
                else:
                    return
            if orphan_amount < 0.001:
                return
            # ✅ 修复：基于入场价计算 TP
            tp_price = round(entry_price * (1 - tp_pct), 2)
            if tp_price >= current_price:
                tp_price = round(current_price * (1 - tp_pct / 2), 2)
            self._orphan_tp_price = tp_price
            req_qty = round(orphan_amount, 6)
            order, placed_qty = self._place_reduce_only_with_rebalance(
                'buy', tp_price, req_qty, keep_layer=0, context="orphan-short", min_qty=0.001
            )
            if order:
                self.active_orders.append({
                    'order_id': order['id'],
                    'side': 'buy',
                    'grid_side': 'short_tp',
                    'price': tp_price,
                    'amount': placed_qty,
                    'entry_price': entry_price,
                    'layer': 0,
                    'est_profit': round(placed_qty * entry_price * tp_pct, 2),
                    '_orphan_tp': True,
                })
                logger.info(
                    f"[GridExecutor] 📤 补挂止盈买单（空头旧持仓） | "
                    f"数量: {placed_qty:.6f} BTC | 入场: ${entry_price:.2f} | 买价: ${tp_price:.2f}"
                )

    def deploy_grid(self, grid_info):
        """
        部署网格订单（支持双向）

        多头层: 在当前价以下挂买单 → 成交后挂止盈卖单
        空头层: 在当前价以上挂卖单 → 成交后挂止盈买单

        ✅ 修复：有旧持仓时，跳过与 TP 冲突的反向网格层
        """
        # 模式参数（由 AI/规则切换 TRADING_MODE_ACTIVE 后自动生效）
        mode_params = config.get_mode_params() if hasattr(config, "get_mode_params") else {}

        def _mp_get(name, default):
            try:
                v = mode_params.get(name, None)
                return default if v is None else v
            except Exception:
                return default

        # 执行器级硬闸门：高持仓时拒绝重建，防止“撤单-重建”循环
        position = None
        existing_side = None
        try:
            pos = self.trader.get_position()
            position = pos
            bal = self.trader.get_balance()
            if pos and pos.get('amount') and bal and bal.get('total', 0) > 0:
                existing_side = 'long' if pos['amount'] > 0 else 'short'
                price_ref = grid_info.get('P0') or pos.get('entry_price', 0) or 0
                leverage = getattr(config, 'LEVERAGE', 1) or 1
                pos_margin = abs(pos['amount']) * price_ref / leverage if price_ref > 0 else 0
                margin_ratio = pos_margin / (bal['total'] + 1e-10)
                max_ratio = float(_mp_get("max_redeploy_margin_ratio", getattr(config, 'MAX_REDEPLOY_MARGIN_RATIO', 0.50)))
                if margin_ratio >= max_ratio:
                    logger.warning(
                        f"[GridExecutor] ❌ 拒绝部署：保证金占比 {margin_ratio*100:.1f}% >= {max_ratio*100:.0f}%"
                    )
                    return False
        except Exception as e:
            logger.warning(f"[GridExecutor] 部署前风控检查失败，拒绝部署: {e}")
            return False

        # 有仓状态下限制重建频率，避免“撤单-重建”循环
        now = time.time()
        if existing_side:
            while self._deploy_timestamps and self._deploy_timestamps[0] < now - 3600:
                self._deploy_timestamps.popleft()
            max_rebuilds = max(1, int(_mp_get("position_rebuild_max_per_hour", getattr(config, "POSITION_REBUILD_MAX_PER_HOUR", 2))))
            # 波动越高，允许更高重建上限，降低“想重建但总被限频”摩擦
            atr_val = float(grid_info.get("atr", 0.0) or 0.0)
            p0_val = float(grid_info.get("P0", 0.0) or 0.0)
            atr_ratio = (atr_val / p0_val) if p0_val > 0 else 0.0
            atr_mid = float(getattr(config, "POSITION_REBUILD_ATR_RATIO_MID", 0.006))
            atr_high = float(getattr(config, "POSITION_REBUILD_ATR_RATIO_HIGH", 0.008))
            if atr_ratio >= atr_high:
                max_rebuilds = max(max_rebuilds, int(_mp_get("position_rebuild_max_per_hour_high", getattr(config, "POSITION_REBUILD_MAX_PER_HOUR_HIGH", 6))))
            elif atr_ratio >= atr_mid:
                max_rebuilds = max(max_rebuilds, int(_mp_get("position_rebuild_max_per_hour_mid", getattr(config, "POSITION_REBUILD_MAX_PER_HOUR_MID", 4))))
            if len(self._deploy_timestamps) >= max_rebuilds:
                logger.warning(
                    f"[GridExecutor] ⏸ 有仓重建限频触发：最近1小时 {len(self._deploy_timestamps)} 次 >= {max_rebuilds} 次，"
                    f"ATR比={atr_ratio:.3%}，跳过本轮重建"
                )
                return False

        # 有仓且已有有效入场挂单时，优先保留挂单，避免全撤全挂
        if existing_side and getattr(config, "PRESERVE_ORDERS_WHEN_IN_POSITION", True):
            entry_sides = {"long_entry", "short_entry"}
            has_entry_orders = any(o.get("grid_side") in entry_sides for o in self.active_orders)
            if has_entry_orders:
                self.last_grid_info = grid_info
                # 先补齐孤儿持仓 TP，再保留挂单返回，避免仓位裸露
                self._orphan_tp_price = None
                self._place_tp_for_orphaned_positions(grid_info)
                tp_orders = sum(1 for o in self.active_orders if o.get('_orphan_tp'))
                logger.info(
                    f"[GridExecutor] 有仓保留模式：保留现有入场挂单，跳过全量重建 | 补挂TP: {tp_orders}"
                )
                return True

        # 有持仓时保留 Binance 上的 reduceOnly TP/SL 单，只撤入场单
        has_position = existing_side is not None
        self.cancel_all(preserve_tp=has_position)
        self._grid_cycle_complete = False
        self._pending_tp_orders = {}  # key -> 待挂止盈单信息
        self._orphan_tp_price = None

        current_price = grid_info['P0']
        long_count = sum(1 for g in grid_info['grid'] if g.get('side', 'long') == 'long')
        short_count = sum(1 for g in grid_info['grid'] if g.get('side') == 'short')

        logger.info(
            f"[GridExecutor] 开始部署网格 | 多头: {long_count}层 空头: {short_count}层"
            + (f" | 保留旧TP" if has_position else "")
        )

        balance = self.trader.get_balance()
        available_capital = balance['free'] if balance else 0
        leverage = getattr(config, "LEVERAGE", 1) or 1
        fee_rate = 0.0004

        min_notional = float(getattr(config, 'MIN_ORDER_NOTIONAL', 130))
        if getattr(config, "TRADING_MODE", "paper") == "paper":
            min_notional = float(getattr(config, "MIN_ORDER_NOTIONAL_PAPER", 0))
        step_size = getattr(config, 'ORDER_STEP_SIZE', 0.001)

        # 为旧持仓补挂止盈单（如果 Binance 上已有则不重复挂）
        self._place_tp_for_orphaned_positions(grid_info)
        orphan_tp_price = self._orphan_tp_price

        # ✅ 修复：确定旧持仓方向，用于跳过冲突层
        if (not existing_side) and position and position.get('amount', 0) != 0:
            existing_side = 'long' if position['amount'] > 0 else 'short'

        # 持仓保护模式：已有仓位时仅管理止盈/止损，不再新增网格入场
        if existing_side and getattr(config, "EXISTING_POSITION_TP_ONLY", True):
            self.last_grid_info = grid_info
            tp_orders = sum(1 for o in self.active_orders if o.get('_orphan_tp'))
            logger.warning(
                f"[GridExecutor] 持仓保护模式已启用 | 现有{existing_side}仓，仅保留退出单管理 | 补挂TP: {tp_orders}"
            )
            send_telegram(
                f"🛡️ *持仓保护模式*\n\n"
                f"检测到已有{('多头' if existing_side == 'long' else '空头')}仓位，\n"
                f"本轮仅管理止盈/止损，不新增网格入场单。"
            )
            return True

        # 对冲层保留：有仓时允许保留最远的反向层，降低单边暴露
        # 当无成交时间过长时，自动放宽可保留的反向层数量（最多到上限）。
        hedge_keep = set()
        if existing_side:
            base_keep_n = max(0, int(_mp_get("position_hedge_opposite_layers", getattr(config, "POSITION_HEDGE_OPPOSITE_LAYERS", 0))))
            max_keep_n = max(base_keep_n, int(_mp_get("position_hedge_opposite_max_layers", getattr(config, "POSITION_HEDGE_OPPOSITE_MAX_LAYERS", 2))))
            relax_after_sec = max(0, int(_mp_get("position_hedge_no_fill_relax_after_sec", getattr(config, "POSITION_HEDGE_NO_FILL_RELAX_AFTER_SEC", 3600))))
            extra_keep_n = max(0, int(_mp_get("position_hedge_no_fill_extra_layers", getattr(config, "POSITION_HEDGE_NO_FILL_EXTRA_LAYERS", 1))))
            no_fill_sec = int(self.get_no_fill_seconds() if hasattr(self, "get_no_fill_seconds") else 0)

            keep_n = base_keep_n
            if relax_after_sec > 0 and no_fill_sec >= relax_after_sec:
                keep_n = min(max_keep_n, base_keep_n + extra_keep_n)
                if keep_n > base_keep_n:
                    logger.info(
                        f"[GridExecutor] 无成交放宽反向层: {base_keep_n} -> {keep_n} "
                        f"(no_fill={no_fill_sec}s >= {relax_after_sec}s)"
                    )

            if keep_n > 0:
                opposite_layers = []
                for layer in grid_info['grid']:
                    side = layer.get('side', 'long')
                    if side == existing_side:
                        continue
                    entry_price = layer.get('entry_price', layer.get('buy_price', 0)) or 0
                    # 优先保留“离旧持仓TP更远”的反向层，降低对旧TP的干扰
                    ref_price = float(orphan_tp_price or current_price or 0.0)
                    dist = abs(entry_price - ref_price)
                    opposite_layers.append((dist, layer.get('layer')))
                opposite_layers.sort(reverse=True)  # 最远层优先
                hedge_keep = {lid for _, lid in opposite_layers[:keep_n]}

        skipped_conflict = 0
        skipped_notional = 0
        skipped_direction = 0
        candidates = []

        # 预算前置筛层：先确定”可下哪些层”，再统一下单，避免部署到一半才因保证金不足跳层
        for layer in grid_info['grid']:
            layer_side = layer.get('side', 'long')
            entry_price = layer.get('entry_price', layer.get('buy_price', 0))
            size = layer['size_btc']
            est_profit = layer.get('est_profit', 0)
            layer_idx = layer['layer']
            tp_price = layer.get('tp_price', layer.get('sell_price', 0))

            # 单边成交保护：连续同方向入场达到上限 → 阻断该方向新开仓
            if self._side_blocked and layer_side == self._side_blocked:
                skipped_direction += 1
                if skipped_direction <= 1:
                    logger.warning(
                        "[GridExecutor] 单边保护生效: 阻断 %s 方向入场 (连续%d次同方向成交)"
                        % (layer_side, self._consecutive_count)
                    )
                continue

            # 有旧持仓时：默认跳过反向层；但保留少量远端反向层用于对冲缓冲
            if existing_side and layer_side != existing_side:
                if layer_idx not in hedge_keep:
                    skipped_conflict += 1
                    logger.info(
                        f"[GridExecutor] ⏭ 跳过反向层 L{layer_idx} ({layer_side})，已有持仓方向={existing_side}"
                    )
                    continue

            # 近TP保护：保护带内的反向层继续跳过，避免先于旧TP成交造成冲突
            if orphan_tp_price and existing_side:
                tp_buffer_ratio = max(0.0, float(_mp_get("position_hedge_tp_buffer_ratio", getattr(config, "POSITION_HEDGE_TP_BUFFER_RATIO", 0.0015))))
                if existing_side == 'long' and layer_side == 'short':
                    # 多头持仓时，空头卖单不能低于(或过近于)TP卖价（否则先于/贴近TP成交）
                    conflict_top = orphan_tp_price * (1.0 + tp_buffer_ratio)
                    if entry_price <= conflict_top:
                        skipped_conflict += 1
                        logger.info(
                            f"[GridExecutor] ⏭ 跳过空头L{layer_idx} @ ${entry_price:.2f}"
                            f" (近多头TP保护带 <= ${conflict_top:.2f}, TP=${orphan_tp_price:.2f})"
                        )
                        continue
                elif existing_side == 'short' and layer_side == 'long':
                    # 空头持仓时，多头买单不能高于(或过近于)TP买价（否则先于/贴近TP成交）
                    conflict_bottom = orphan_tp_price * (1.0 - tp_buffer_ratio)
                    if entry_price >= conflict_bottom:
                        skipped_conflict += 1
                        logger.info(
                            f"[GridExecutor] ⏭ 跳过多头L{layer_idx} @ ${entry_price:.2f}"
                            f" (近空头TP保护带 >= ${conflict_bottom:.2f}, TP=${orphan_tp_price:.2f})"
                        )
                        continue

            # 取整到 Binance 步长并校验名义价值
            size = math.ceil(size / step_size) * step_size
            if existing_side and layer_side != existing_side:
                hedge_size_mult = float(getattr(config, "POSITION_HEDGE_OPPOSITE_SIZE_MULT", 0.5))
                size = max(step_size, round(size * max(0.1, hedge_size_mult), 6))
            size = round(size, 6)
            required_capital = entry_price * size
            margin = required_capital / leverage

            if required_capital < min_notional:
                skipped_notional += 1
                continue

            if layer_side == 'long' and not (entry_price < current_price):
                skipped_direction += 1
                continue
            if layer_side == 'short' and not (entry_price > current_price):
                skipped_direction += 1
                continue

            reserve_cost = margin + required_capital * fee_rate
            candidates.append({
                "layer": layer,
                "layer_side": layer_side,
                "layer_idx": layer_idx,
                "entry_price": entry_price,
                "size": size,
                "est_profit": est_profit,
                "tp_price": tp_price,
                "required_capital": required_capital,
                "margin": margin,
                "reserve_cost": reserve_cost,
            })

        # 按“离现价最近优先”做保证金预算裁剪
        budget_capital = max(0.0, available_capital * 0.9)
        used_budget = 0.0
        selected_keys = set()
        dropped_budget = []
        for c in sorted(candidates, key=lambda x: (abs(int(x["layer_idx"])), 0 if x["layer_side"] == "long" else 1)):
            next_used = used_budget + c["reserve_cost"]
            if next_used <= budget_capital:
                selected_keys.add((c["layer_side"], int(c["layer_idx"])))
                used_budget = next_used
            else:
                dropped_budget.append(c)

        if dropped_budget:
            dropped_labels = ", ".join(
                [f"{'多' if c['layer_side']=='long' else '空'}L{c['layer_idx']}" for c in dropped_budget[:8]]
            )
            more = "" if len(dropped_budget) <= 8 else f" ... +{len(dropped_budget)-8}"
            logger.warning(
                f"[GridExecutor] 预算裁剪: 可用预算 ${budget_capital:.2f}, 预估占用 ${used_budget:.2f} | "
                f"裁掉 {len(dropped_budget)} 层: {dropped_labels}{more}"
            )

        # ── margin 预检：计算可用加仓余量，避免成交后触发 REDUCE ──
        reduce_margin_limit = float(_mp_get("max_redeploy_margin_ratio", getattr(config, 'MAX_REDEPLOY_MARGIN_RATIO', 0.50)))
        _margin_safety = 0.95          # 留 5% 安全余量
        _margin_cap = reduce_margin_limit * _margin_safety   # 实际上限 ~0.475
        _total_equity = bal.get('total', 0) if bal else 0
        _cur_pos_amt = abs(position.get('amount', 0)) if position else 0
        _cur_pos_side = existing_side    # 'long' / 'short' / None
        skipped_margin_precheck = 0

        for c in candidates:
            layer = c["layer"]
            layer_side = c["layer_side"]
            layer_idx = c["layer_idx"]
            entry_price = c["entry_price"]
            size = c["size"]
            est_profit = c["est_profit"]
            tp_price = c["tp_price"]
            required_capital = c["required_capital"]
            reserve_cost = c["reserve_cost"]

            if (layer_side, int(layer_idx)) not in selected_keys:
                continue

            if reserve_cost > available_capital:
                side_label = "多" if layer_side == 'long' else "空"
                logger.warning(
                    f"[GridExecutor] 实时保证金不足，跳过{side_label}头L{layer_idx}: "
                    f"需占用 ${reserve_cost:.2f}, 可用 ${available_capital:.2f}"
                )
                continue

            # ── margin 预检：若该单成交会导致 margin 超限，缩量或跳过 ──
            _same_dir = (
                (_cur_pos_side == 'long' and layer_side == 'long') or
                (_cur_pos_side == 'short' and layer_side == 'short')
            )
            if _same_dir and _total_equity > 0 and leverage > 0:
                projected_amt = _cur_pos_amt + size
                projected_margin = projected_amt * current_price / leverage / _total_equity
                if projected_margin >= _margin_cap:
                    # 计算不超标的最大可加仓量
                    max_amt = _margin_cap * _total_equity * leverage / current_price - _cur_pos_amt
                    max_amt = max(0.0, max_amt)
                    max_amt = math.floor(max_amt / step_size) * step_size
                    max_amt = round(max_amt, 6)
                    if max_amt < step_size or max_amt * current_price < min_notional:
                        side_label = "多" if layer_side == 'long' else "空"
                        logger.warning(
                            f"[GridExecutor] margin预检跳过{side_label}头L{layer_idx}: "
                            f"成交后margin={projected_margin*100:.1f}% >= {_margin_cap*100:.0f}% | "
                            f"当前仓位={_cur_pos_amt} 订单={size}"
                        )
                        skipped_margin_precheck += 1
                        continue
                    else:
                        old_size = size
                        size = max_amt
                        required_capital = entry_price * size
                        reserve_cost = required_capital / leverage + required_capital * fee_rate
                        side_label = "多" if layer_side == 'long' else "空"
                        logger.info(
                            f"[GridExecutor] margin预检缩量{side_label}头L{layer_idx}: "
                            f"{old_size}->{size} | 成交后margin≈"
                            f"{(_cur_pos_amt + size) * current_price / leverage / _total_equity * 100:.1f}%"
                        )

            if layer_side == 'long':
                # ═══ 多头：当前价以下挂买单 ═══
                order = self.trader.place_limit_order('buy', entry_price, size)
                if order:
                    available_capital -= reserve_cost
                    if _cur_pos_side == 'long':
                        _cur_pos_amt += size   # 累计同向挂单量，后续层继续预检
                    self.active_orders.append({
                        'order_id': order['id'],
                        'side': 'buy',
                        'grid_side': 'long_entry',
                        'price': entry_price,
                        'amount': size,
                        'layer': layer_idx,
                        'est_profit': est_profit,
                    })
            else:
                # ═══ 空头：当前价以上挂卖单 ═══
                order = self.trader.place_limit_order('sell', entry_price, size)
                if order:
                    available_capital -= reserve_cost
                    if _cur_pos_side == 'short':
                        _cur_pos_amt += size   # 累计同向挂单量，后续层继续预检
                    self.active_orders.append({
                        'order_id': order['id'],
                        'side': 'sell',
                        'grid_side': 'short_entry',
                        'price': entry_price,
                        'amount': size,
                        'layer': layer_idx,
                        'est_profit': est_profit,
                    })

            # 保存止盈单信息，等入场单成交后再挂出
            tp_key = f"{layer_side}_{layer_idx}"
            if layer.get('tiered_tp') and layer.get('tp_levels'):
                self._pending_tp_orders[tp_key] = {
                    'tiered': True,
                    'entry_side': layer_side,
                    'tp_levels': layer['tp_levels'],
                    'original_size': size,
                }
            else:
                self._pending_tp_orders[tp_key] = {
                    'tiered': False,
                    'entry_side': layer_side,
                    'tp_price': tp_price,
                    'size': size,
                    'est_profit': est_profit,
                }

        if skipped_margin_precheck:
            logger.warning(
                f"[GridExecutor] margin预检: 跳过 {skipped_margin_precheck} 个同向入场层 "
                f"(当前仓位={_cur_pos_amt} margin上限={_margin_cap*100:.0f}%)"
            )
        if skipped_conflict:
            logger.info(f"[GridExecutor] 已跳过 {skipped_conflict} 个与旧持仓TP冲突的反向网格层")
        if skipped_notional:
            logger.info(f"[GridExecutor] 已跳过 {skipped_notional} 个低于最小名义价值的网格层")
        if skipped_direction:
            logger.debug(f"[GridExecutor] 已跳过 {skipped_direction} 个方向不匹配的网格层")

        self.last_grid_info = grid_info

        buy_orders = sum(1 for o in self.active_orders if o.get('grid_side') == 'long_entry')
        sell_orders = sum(1 for o in self.active_orders if o.get('grid_side') == 'short_entry')
        tp_orders = sum(1 for o in self.active_orders if o.get('_orphan_tp'))
        logger.info(
            f"[GridExecutor] ✅ 网格已部署 | 买入: {buy_orders} 卖出: {sell_orders} 补挂TP: {tp_orders}"
        )

        stop_info = f"多头止损: ${grid_info.get('StopOutPrice', 0):.2f}"
        if grid_info.get('StopOutPriceUpper'):
            stop_info += f" | 空头止损: ${grid_info['StopOutPriceUpper']:.2f}"

        send_telegram(
            f"✅ *网格已部署*\n\n"
            f"多头: {long_count}层 | 空头: {short_count}层\n"
            f"范围: ${grid_info['P_low']:.2f} - ${grid_info['P_high']:.2f}\n"
            f"{stop_info}"
        )
        self._deploy_timestamps.append(time.time())
        return True

    def check_fills(self):
        """
        检查订单成交情况（双向版）

        通过 grid_side 字段区分订单角色：
        - long_entry:  多头入场买单 → 记录 FIFO + 挂止盈卖单
        - short_entry: 空头入场卖单 → 记录 FIFO + 挂止盈买单
        - long_tp:     多头止盈卖单 → FIFO 匹配平仓
        - short_tp:    空头止盈买单 → FIFO 匹配平仓

        Returns:
            list: 成交列表
        """
        filled = []
        still_active = []
        open_ids = None
        sync_every = max(1, int(getattr(config, "ORDER_STATUS_MISS_SYNC_EVERY", 3)))

        for order in self.active_orders:
            order_status = self.trader.get_order_status(order['order_id'])

            if order_status is None:
                if open_ids is None:
                    open_orders = self._get_open_orders_snapshot()
                    if open_orders is None:
                        open_ids = None
                    else:
                        open_ids = {
                            str(o.get("id"))
                            for o in open_orders
                            if o.get("id") is not None
                        }

                miss = int(order.get("_status_miss", 0)) + 1
                order["_status_miss"] = miss
                oid = str(order.get("order_id"))
                if open_ids is None:
                    still_active.append(order)
                    if miss == 1 or miss % 5 == 0:
                        logger.warning(
                            f"[GridExecutor] ⚠️ 订单 {order['order_id']} 状态查询失败({miss})，"
                            "当前交易所挂单状态未知，保留并重试"
                        )
                elif oid in open_ids:
                    still_active.append(order)
                    if miss == 1 or miss % 5 == 0:
                        logger.warning(
                            f"[GridExecutor] ⚠️ 订单 {order['order_id']} 状态查询失败({miss})，"
                            "但交易所仍显示 open，保留并重试"
                        )
                elif miss >= sync_every:
                    logger.warning(
                        f"[GridExecutor] ⚠️ 订单 {order['order_id']} 状态查询失败({miss})，"
                        "且交易所挂单簿中已不存在，按终态移除本地跟踪"
                    )
                    self._release_tp_tracking(order, reason=f"状态缺失移除 miss={miss}")
                else:
                    still_active.append(order)
                continue

            status = order_status.get('status', '')
            if "_status_miss" in order:
                order.pop("_status_miss", None)

            if status in ['closed', 'filled']:
                filled.append(order)

                actual_price = float(order_status.get('average', order['price']))
                actual_amount = float(order_status.get('filled', order['amount']))
                layer_idx = order.get('layer', 0)
                grid_side = order.get('grid_side', '')

                # ── 推断 grid_side（兼容旧版没有此字段的情况）──
                if not grid_side:
                    side_lower = str(order.get('side', '')).lower()
                    is_ro = self._is_reduce_only_order(order_status) or self._is_reduce_only_order(order)
                    if side_lower == 'buy':
                        grid_side = 'short_tp' if is_ro else 'long_entry'
                    else:
                        grid_side = 'long_tp' if is_ro else 'short_entry'

                if grid_side == 'long_entry':
                    # ═══ 多头入场买单成交 ═══
                    self._log_binance_trade('buy', actual_price, actual_amount, realized_pnl=0.0, fee_rate=_MAKER_FEE)
                    # 防重：sync_binance_state 可能已在同轮把该持仓同步进 FIFO
                    pos_now = self.trader.get_position()
                    actual_abs = abs(pos_now.get('amount', 0)) if pos_now else 0
                    fifo_same = sum(p['amount'] for p in self._pending_closes if p.get('side', 'long') == 'long')
                    if actual_abs > 0 and fifo_same + actual_amount > actual_abs + 0.0002:
                        logger.info(
                            f"[GridExecutor] 入场去重(多头) | FIFO {fifo_same:.6f} + 成交 {actual_amount:.6f} "
                            f"> 实际 {actual_abs:.6f}，跳过重复记账"
                        )
                    else:
                        self._pending_closes.append({
                            'entry_price': actual_price,
                            'buy_price': actual_price,
                            'amount': actual_amount,
                            'side': 'long',
                            'layer': layer_idx,
                            'entry_time': datetime.now().isoformat(),
                            'entry_fee_rate': _MAKER_FEE,
                        })
                    logger.info(
                        f"[GridExecutor] 📥 多头买入成交 | 价格: ${actual_price:.2f} | "
                        f"数量: {actual_amount:.6f} BTC | FIFO: {len(self._pending_closes)}"
                    )
                    # 挂止盈卖单
                    tp_key = f"long_{layer_idx}"
                    tp_info = self._pending_tp_orders.get(tp_key)
                    if tp_info:
                        self._place_tp_order(tp_info, actual_amount, actual_price, layer_idx)
                    self._save_state()

                elif grid_side == 'short_entry':
                    # ═══ 空头入场卖单成交 ═══
                    self._log_binance_trade('sell', actual_price, actual_amount, realized_pnl=0.0, fee_rate=_MAKER_FEE)
                    # 防重：sync_binance_state 可能已在同轮把该持仓同步进 FIFO
                    pos_now = self.trader.get_position()
                    actual_abs = abs(pos_now.get('amount', 0)) if pos_now else 0
                    fifo_same = sum(p['amount'] for p in self._pending_closes if p.get('side') == 'short')
                    if actual_abs > 0 and fifo_same + actual_amount > actual_abs + 0.0002:
                        logger.info(
                            f"[GridExecutor] 入场去重(空头) | FIFO {fifo_same:.6f} + 成交 {actual_amount:.6f} "
                            f"> 实际 {actual_abs:.6f}，跳过重复记账"
                        )
                    else:
                        self._pending_closes.append({
                            'entry_price': actual_price,
                            'buy_price': actual_price,
                            'amount': actual_amount,
                            'side': 'short',
                            'layer': layer_idx,
                            'entry_time': datetime.now().isoformat(),
                            'entry_fee_rate': _MAKER_FEE,
                        })
                    logger.info(
                        f"[GridExecutor] 📥 空头卖出成交 | 价格: ${actual_price:.2f} | "
                        f"数量: {actual_amount:.6f} BTC | FIFO: {len(self._pending_closes)}"
                    )
                    # 挂止盈买单
                    tp_key = f"short_{layer_idx}"
                    tp_info = getattr(self, '_pending_tp_orders', {}).get(tp_key)
                    if tp_info:
                        self._place_tp_order(tp_info, actual_amount, actual_price, layer_idx)
                    self._save_state()

                elif grid_side in ('long_tp', 'short_tp'):
                    # ═══ 止盈单成交 → FIFO 匹配平仓 ═══
                    fifo_side = 'long' if grid_side == 'long_tp' else 'short'
                    fifo_side_qty_before = sum(
                        p.get('amount', 0.0) for p in self._pending_closes
                        if p.get('side', 'long') == fifo_side
                    )
                    profit_usdt, fee_usdt = self._fifo_match_close(
                        actual_price, actual_amount, is_taker=False, close_side=fifo_side
                    )
                    # FIFO 丢失兜底：用挂单时记录的入场价估算本笔盈亏，避免错误记为 0
                    if fifo_side_qty_before <= 0.000001 and actual_amount > 0:
                        fallback_entry = float(order.get('entry_price', 0.0) or 0.0)
                        if fallback_entry > 0:
                            profit_usdt, fee_usdt = self._estimate_close_pnl_from_entry(
                                entry_price=fallback_entry,
                                close_price=actual_price,
                                close_amount=actual_amount,
                                close_side=fifo_side,
                                is_taker=False,
                            )
                            logger.warning(
                                f"[GridExecutor] ⚠️ FIFO 缺失，使用回退估算 | "
                                f"side={fifo_side} entry=${fallback_entry:.2f} close=${actual_price:.2f} "
                                f"qty={actual_amount:.6f}"
                            )
                    side_label = "多头卖出" if grid_side == 'long_tp' else "空头买回"
                    tp_side = 'sell' if grid_side == 'long_tp' else 'buy'
                    self._log_binance_trade(tp_side, actual_price, actual_amount, realized_pnl=profit_usdt, fee_rate=_MAKER_FEE)
                    if profit_usdt != 0 or actual_amount > 0:
                        logger.info(
                            f"[GridExecutor] 📤 {side_label}止盈成交 | 价格: ${actual_price:.2f} | "
                            f"数量: {actual_amount:.6f} BTC | "
                            f"净利润: ${profit_usdt - fee_usdt:.2f} "
                            f"(毛利: ${profit_usdt:.2f} 费用: ${fee_usdt:.4f})"
                        )
                        if self.strategy:
                            self.strategy.record_trade(profit_usdt=profit_usdt, fee_usdt=fee_usdt)
                        self._record_cb_trade(profit_usdt - fee_usdt)
                        self._log_single_pnl(profit_usdt - fee_usdt)
                    if hasattr(self.trader, 'adjust_local_ro_reserved'):
                        self.trader.adjust_local_ro_reserved(tp_side, -actual_amount, reason=f"TP成交 L{layer_idx}")
                    self._save_state()

                else:
                    # 未知 grid_side，按旧逻辑处理
                    logger.warning(f"[GridExecutor] 未知 grid_side={grid_side}，订单 {order['order_id']}")

            elif status in ['open', 'pending']:
                still_active.append(order)
            else:
                # 被撤销/过期等非成交结束：回收本地 reduceOnly 预留
                self._release_tp_tracking(order, reason=f"TP非成交结束 status={status}")
                logger.info(f"[GridExecutor] ℹ️ 订单 {order['order_id']} 状态: {status}，跳过")

        self.active_orders = still_active

        if filled and not self.active_orders:
            self._grid_cycle_complete = True
            logger.info(f"[GridExecutor] 🔄 网格一轮完成，需要补新网格")
        if filled:
            now_ts = time.time()
            self._last_fill_ts = now_ts
            self._no_fill_anchor_ts = self._last_fill_ts
            self._last_no_fill_rebuild_ts = 0.0
            self._no_fill_reset_by_rebuild = False
            for _ in range(max(1, len(filled))):
                self._fill_timestamps.append(now_ts)

            # 单边成交计数 + 事件日志
            _evt = getattr(self, "_event_logger", None)
            no_fill = self.get_no_fill_seconds() if hasattr(self, "get_no_fill_seconds") else 0
            max_consecutive = int(getattr(self, "_max_consecutive_same_side", getattr(config, "MAX_CONSECUTIVE_SAME_SIDE", 3)))
            for fo in filled:
                gs = fo.get("grid_side", "")
                # 入场单：累加同方向计数
                if gs == "long_entry":
                    if self._consecutive_side == "long":
                        self._consecutive_count += 1
                    else:
                        self._consecutive_side = "long"
                        self._consecutive_count = 1
                elif gs == "short_entry":
                    if self._consecutive_side == "short":
                        self._consecutive_count += 1
                    else:
                        self._consecutive_side = "short"
                        self._consecutive_count = 1
                # 止盈单：反向平仓 → 重置计数
                elif gs in ("long_tp", "short_tp"):
                    self._consecutive_count = 0
                    self._consecutive_side = ""
                    self._side_blocked = ""

                # 连续同方向成交达到上限 → 阻断该方向
                if self._consecutive_count >= max_consecutive and self._side_blocked != self._consecutive_side:
                    self._side_blocked = self._consecutive_side
                    logger.warning(
                        f"[GridExecutor] ⚠️ 单边保护: 连续{self._consecutive_count}次 "
                        f"{self._consecutive_side} 入场 | 阻断该方向新开仓"
                    )

                if _evt:
                    if gs in ("long_entry", "short_entry"):
                        side = "buy" if gs == "long_entry" else "sell"
                    elif gs in ("long_tp", "short_tp"):
                        side = "sell" if gs == "long_tp" else "buy"
                    else:
                        side = fo.get("side", "unknown")
                    _evt.fill(side=side, price=float(fo.get("price", 0)),
                              qty=float(fo.get("amount", 0)), no_fill_sec=no_fill,
                              grid_side=gs)

        # ✅ 修复：每次有成交后，对账 FIFO 与 Binance 实际持仓
        if filled:
            self._reconcile_fifo()

        return filled

    def _reconcile_fifo(self):
        """✅ 修复：FIFO 队列与 Binance 实际持仓对账"""
        try:
            position = self.trader.get_position()
            actual_amount = position['amount'] if position else 0

            if actual_amount == 0 and self._pending_closes:
                stale = len(self._pending_closes)
                total_qty = sum(p['amount'] for p in self._pending_closes)
                logger.warning(
                    f"[GridExecutor] ⚠️ Binance 无持仓，清空 {stale} 条幽灵 FIFO "
                    f"(总量: {total_qty:.6f} BTC)"
                )
                self._pending_closes = []
                self._save_state()
                return

            if actual_amount == 0:
                return

            actual_side = 'long' if actual_amount > 0 else 'short'
            abs_actual = abs(actual_amount)

            # 清除反方向记录
            same = [p for p in self._pending_closes if p.get('side', 'long') == actual_side]
            other = [p for p in self._pending_closes if p.get('side', 'long') != actual_side]
            if other:
                logger.warning(f"[GridExecutor] ⚠️ 对账清除 {len(other)} 条反方向 FIFO 记录")
                self._pending_closes = same

            # 检查总量是否匹配
            fifo_total = sum(p['amount'] for p in self._pending_closes)
            diff = abs(fifo_total - abs_actual)

            if diff > 0.0005:  # 超过容差
                if fifo_total > abs_actual:
                    # FIFO 多了，从前面裁减
                    excess = fifo_total - abs_actual
                    while excess > 0.0001 and self._pending_closes:
                        p = self._pending_closes[0]
                        if p['amount'] <= excess + 0.0001:
                            excess -= p['amount']
                            self._pending_closes.pop(0)
                        else:
                            p['amount'] -= excess
                            excess = 0
                    logger.warning(
                        f"[GridExecutor] ⚠️ 对账裁减 FIFO: {fifo_total:.6f} → {abs_actual:.6f} BTC"
                    )
                else:
                    # FIFO 少了，用 Binance 入场价补充
                    gap = abs_actual - fifo_total
                    entry_price = position.get('entry_price', 0)
                    if entry_price > 0:
                        self._pending_closes.append({
                            'entry_price': entry_price,
                            'amount': round(gap, 6),
                            'side': actual_side,
                            'layer': -1,
                            'entry_time': datetime.now().isoformat(),
                            'entry_fee_rate': _MAKER_FEE,
                        })
                        logger.info(
                            f"[GridExecutor] 对账补充 FIFO: +{gap:.6f} BTC @ ${entry_price:.2f}"
                        )

                self._save_state()
        except Exception as e:
            logger.warning(f"[GridExecutor] FIFO 对账异常: {e}")

    def sync_binance_state(self):
        """
        同步 Binance 实际状态到本地执行器
        - 对齐 FIFO 与实际持仓
        - 对账修正异常数量
        - 在已有网格信息时补挂孤儿持仓止盈
        """
        try:
            self._sync_pending_closes_with_position()
            self._reconcile_fifo()
            if self.last_grid_info:
                self._place_tp_for_orphaned_positions(self.last_grid_info)
            return True
        except Exception as e:
            logger.warning(f"[GridExecutor] 状态同步异常: {e}")
            return False

    def cancel_all(self, preserve_tp=False):
        """撤销所有网格订单

        Args:
            preserve_tp: 若为 True，保留 Binance 上的 reduceOnly 订单（TP/SL），
                         仅撤销入场单。用于有持仓时的网格重建，防止 TP 丢失。
        """
        if not preserve_tp:
            # 无持仓场景：全撤
            if self.active_orders:
                logger.info(f"[GridExecutor] 撤销 {len(self.active_orders)} 个订单")
                result = self.trader.cancel_all_orders()
                if result is None:
                    logger.warning("[GridExecutor] 全撤结果未确认，保留本地订单跟踪")
                    return False
            open_orders = self._get_open_orders_snapshot()
            if open_orders is None:
                for order in list(self.active_orders):
                    self._release_tp_tracking(order, reason="全撤后释放")
                self.active_orders = []
            else:
                open_ids = {self._extract_order_id(o) for o in open_orders}
                remain = []
                for order in self.active_orders:
                    oid = self._extract_order_id(order)
                    if oid and oid in open_ids:
                        remain.append(order)
                    else:
                        self._release_tp_tracking(order, reason="全撤后释放")
                self.active_orders = remain
            return True
        else:
            # 有持仓场景：只撤入场单，保留 Binance 上的 reduceOnly TP/SL
            tp_sides = {"long_tp", "short_tp"}
            # 1) 撤 local active_orders 中的入场单
            remain = []
            cancelled = 0
            for o in self.active_orders:
                if o.get("grid_side") in tp_sides:
                    remain.append(o)  # 保留 TP
                    continue
                oid = o.get("order_id")
                if oid and hasattr(self.trader, "cancel_order"):
                    try:
                        ok = self.trader.cancel_order(oid)
                        if ok is True:
                            cancelled += 1
                        else:
                            remain.append(o)
                    except Exception:
                        remain.append(o)
                else:
                    remain.append(o)
            self.active_orders = remain
            # 2) 兜底：扫 Binance 实际挂单，撤掉非 reduceOnly 的
            try:
                open_orders = self._get_open_orders_snapshot()
                if open_orders is not None:
                    for o in open_orders:
                        if self._is_reduce_only_order(o):
                            continue  # 保留 reduceOnly 订单
                        oid = o.get('id') or o.get('order_id')
                        if oid and hasattr(self.trader, 'cancel_order'):
                            try:
                                if self.trader.cancel_order(oid) is True:
                                    cancelled += 1
                            except Exception:
                                pass
            except Exception as e:
                logger.warning(f"[GridExecutor] 兜底撤单扫描失败: {e}")
            if cancelled > 0:
                logger.info(f"[GridExecutor] 已撤销 {cancelled} 个入场单，保留 TP/SL")
        # 注意：不清空 _pending_closes，因为持仓可能还在
        return True

    def cancel_entry_orders(self):
        """仅撤销入场单，保留退出单（TP/SL）"""
        if not self.active_orders:
            return 0
        entry_sides = {"long_entry", "short_entry"}
        entry_orders = [o for o in self.active_orders if o.get("grid_side") in entry_sides]
        if not entry_orders:
            return 0

        cancelled = 0
        remain = []
        for o in self.active_orders:
            if o.get("grid_side") not in entry_sides:
                remain.append(o)
                continue
            oid = o.get("order_id")
            ok = False
            if oid is not None and hasattr(self.trader, "cancel_order"):
                ok = self.trader.cancel_order(oid) is True
            elif hasattr(self.trader, "cancel_all_orders"):
                # 兜底：若不支持单撤，只能全撤
                result = self.trader.cancel_all_orders()
                if result is not None:
                    cancelled = len(entry_orders)
                    remain = [x for x in self.active_orders if x.get("grid_side") not in entry_sides]
                    self.active_orders = remain
                    logger.warning("[GridExecutor] 交易器不支持单撤，已回退为全撤")
                    return cancelled
            if ok:
                cancelled += 1
            else:
                remain.append(o)

        self.active_orders = remain
        if cancelled > 0:
            logger.info(f"[GridExecutor] 已撤销 {cancelled} 个入场单，保留退出单管理")
        return cancelled

    def cancel_opposite_entry_orders(self, keep_side):
        """
        按方向撤销反向入场单：
        keep_side='long' -> 保留 long_entry，撤销 short_entry
        keep_side='short' -> 保留 short_entry，撤销 long_entry
        """
        if keep_side not in ("long", "short"):
            return self.cancel_entry_orders()
        if not self.active_orders:
            return 0

        cancel_grid_side = "short_entry" if keep_side == "long" else "long_entry"
        target_orders = [o for o in self.active_orders if o.get("grid_side") == cancel_grid_side]
        if not target_orders:
            return 0

        cancelled = 0
        remain = []
        for o in self.active_orders:
            if o.get("grid_side") != cancel_grid_side:
                remain.append(o)
                continue
            oid = o.get("order_id")
            ok = False
            if oid is not None and hasattr(self.trader, "cancel_order"):
                ok = self.trader.cancel_order(oid) is True
            elif hasattr(self.trader, "cancel_all_orders"):
                result = self.trader.cancel_all_orders()
                if result is not None:
                    cancelled = len(target_orders)
                    remain = [x for x in self.active_orders if x.get("grid_side") != cancel_grid_side]
                    self.active_orders = remain
                    logger.warning("[GridExecutor] 交易器不支持单撤，已回退为全撤")
                    return cancelled
            if ok:
                cancelled += 1
            else:
                remain.append(o)

        self.active_orders = remain
        if cancelled > 0:
            logger.info(f"[GridExecutor] 方向化保留{keep_side}，已撤销 {cancelled} 个反向入场单")
        return cancelled

    def check_stop_loss(self, current_price):
        """
        检查是否触发止损
        ✅ 改进：使用实际成交价计算 PnL

        Args:
            current_price: 当前价格

        Returns:
            bool: 是否触发止损
        """
        position = self.trader.get_position()
        if not position or position.get("amount", 0) == 0:
            return False

        is_long = position["amount"] > 0
        stop_price = None

        # 优先使用网格止损边界；缺失时回退到“入场价 ± STOP_LOSS_PCT”
        if self.last_grid_info:
            stop_price = (
                self.last_grid_info.get("StopOutPrice")
                if is_long else
                self.last_grid_info.get("StopOutPriceUpper")
            )

        if not stop_price:
            entry = position.get("entry_price", current_price)
            stop_pct = getattr(config, "STOP_LOSS_PCT", 0.05)
            stop_price = entry * (1 - stop_pct) if is_long else entry * (1 + stop_pct)

        triggered = False
        if is_long and stop_price and current_price <= stop_price:
            logger.warning(
                f"[GridExecutor] 🛑 多头止损触发! 价格 ${current_price:.2f} <= 止损价 ${stop_price:.2f}"
            )
            triggered = True
        elif (not is_long) and stop_price and current_price >= stop_price:
            logger.warning(
                f"[GridExecutor] 🛑 空头止损触发! 价格 ${current_price:.2f} >= 止损价 ${stop_price:.2f}"
            )
            triggered = True

        if triggered:

            self.cancel_all()

            # 平仓
            position = self.trader.get_position()
            if position and position['amount'] != 0:
                is_long_close = position['amount'] > 0
                close_side = 'sell' if is_long_close else 'buy'
                close_amount = abs(position['amount'])

                # 执行平仓（小仓市价 / 大仓TWAP）
                close_order = self._close_position_safely(close_side, close_amount, current_price, reason="stop_loss")

                if not close_order:
                    logger.error("[GridExecutor] ❌ 止损平仓订单下单失败")
                    return False

                # === 使用实际成交价计算 PnL（支持多空方向）===
                actual_close_price = float(close_order.get('average', current_price))
                actual_close_amount = self._extract_filled_amount(close_order, close_amount)
                if actual_close_amount <= 0:
                    logger.error("[GridExecutor] ❌ 止损平仓返回成交量为0")
                    return False

                fifo_side = 'long' if is_long_close else 'short'
                profit_usdt, fee_usdt = self._fifo_match_close(
                    actual_close_price, actual_close_amount, is_taker=True, close_side=fifo_side
                )

                logger.info(
                    f"[GridExecutor] 🛑 止损平仓 | 平仓价: ${actual_close_price:.2f} | "
                    f"数量: {actual_close_amount:.6f} BTC | "
                    f"净盈亏: ${profit_usdt - fee_usdt:.2f} (费用: ${fee_usdt:.4f})"
                )
                self._log_binance_trade(close_side, actual_close_price, actual_close_amount, realized_pnl=profit_usdt, fee_rate=_TAKER_FEE)

                if self.strategy:
                    self.strategy.record_trade(profit_usdt=profit_usdt, fee_usdt=fee_usdt)
                self._record_cb_trade(profit_usdt - fee_usdt)
                self._log_single_pnl(profit_usdt - fee_usdt)

                # 清空待平仓队列
                self._pending_closes = []
                self._save_state()

                # 重置移动止盈状态
                self._reset_trailing_stop()

                # 通知
                notify_stop_loss()
                return True

        return False

    def check_layer_stop_loss(self, current_price):
        """
        分层止损：按每笔入场记录独立触发，逐层减仓。
        整仓止损仍由 check_stop_loss() 兜底。

        Returns:
            int: 本轮触发并执行的分层止损笔数
        """
        if not getattr(config, "LAYER_STOP_LOSS_ENABLED", False):
            return 0

        position = self.trader.get_position()
        if not position or position.get("amount", 0) == 0:
            return 0

        if not self._pending_closes:
            self._sync_pending_closes_with_position()
        if not self._pending_closes:
            return 0

        is_long = position["amount"] > 0
        side_key = "long" if is_long else "short"
        stop_pct = max(0.0, float(getattr(config, "LAYER_STOP_LOSS_PCT", 0.025)))
        max_per_cycle = max(1, int(getattr(config, "LAYER_STOP_LOSS_MAX_PER_CYCLE", 1)))

        triggered = []
        for p in self._pending_closes:
            if p.get("side", "long") != side_key:
                continue
            entry = p.get("entry_price", p.get("buy_price", 0))
            amount = float(p.get("amount", 0))
            if entry <= 0 or amount <= 0:
                continue

            stop_price = entry * (1 - stop_pct) if is_long else entry * (1 + stop_pct)
            hit = (current_price <= stop_price) if is_long else (current_price >= stop_price)
            if hit:
                triggered.append({
                    "entry_price": entry,
                    "amount": amount,
                    "layer": p.get("layer", 0),
                    "stop_price": stop_price,
                })

        if not triggered:
            return 0

        # 越接近风险侧的层优先减仓：多头先减高成本，空头先减低成本
        triggered.sort(key=lambda x: x["entry_price"], reverse=is_long)

        executed = 0
        for item in triggered[:max_per_cycle]:
            pos_now = self.trader.get_position()
            if not pos_now or pos_now.get("amount", 0) == 0:
                break
            if (pos_now["amount"] > 0) != is_long:
                break

            close_side = "sell" if is_long else "buy"
            close_amount = min(item["amount"], abs(pos_now["amount"]))
            if close_amount <= 0:
                continue

            close_order = self._close_position_safely(
                close_side, close_amount, current_price, reason="layer_stop_loss"
            )
            if not close_order:
                logger.error(
                    f"[GridExecutor] ❌ 分层止损下单失败 | L{item['layer']} | 数量: {close_amount:.6f} BTC"
                )
                continue

            actual_close_price = float(close_order.get("average", current_price))
            actual_close_amount = self._extract_filled_amount(close_order, close_amount)
            if actual_close_amount <= 0:
                logger.error(
                    f"[GridExecutor] ❌ 分层止损成交量为0 | L{item['layer']} | 数量: {close_amount:.6f} BTC"
                )
                continue
            profit_usdt, fee_usdt = self._fifo_match_close(
                actual_close_price, actual_close_amount, is_taker=True, close_side=side_key
            )
            self._log_binance_trade(
                close_side, actual_close_price, actual_close_amount, realized_pnl=profit_usdt, fee_rate=_TAKER_FEE
            )
            if self.strategy:
                self.strategy.record_trade(profit_usdt=profit_usdt, fee_usdt=fee_usdt)
            self._record_cb_trade(profit_usdt - fee_usdt)
            self._log_single_pnl(profit_usdt - fee_usdt)

            logger.warning(
                f"[GridExecutor] ⚠️ 分层止损触发 | L{item['layer']} | "
                f"入场: ${item['entry_price']:.2f} 止损线: ${item['stop_price']:.2f} "
                f"当前: ${current_price:.2f} | 减仓: {actual_close_amount:.6f} BTC | "
                f"净盈亏: ${profit_usdt - fee_usdt:.2f}"
            )
            executed += 1

        if executed > 0:
            self._rebuild_exits_after_position_change(current_price, reason="layer_stop_loss")
            self._save_state()
            self._reconcile_fifo()

        return executed

    def check_trailing_stop(self, current_price):
        """
        检查移动止盈（Trailing Stop）
        ✅ 修复Bug3：支持多头和空头

        多头：跟踪最高价，从最高价回撤触发平仓
        空头：跟踪最低价，从最低价反弹触发平仓

        Args:
            current_price: 当前市场价格

        Returns:
            bool: 是否触发了移动止盈平仓
        """
        if not getattr(config, 'TRAILING_STOP_ENABLED', False):
            return False

        # 需要有持仓才能触发；以 Binance 实际持仓为准防止 FIFO 漂移误触发
        position = self.trader.get_position()
        if not position or position.get("amount", 0) == 0:
            self._reset_trailing_stop()
            return False
        if not self._pending_closes:
            self._sync_pending_closes_with_position()
        if not self._pending_closes:
            self._reset_trailing_stop()
            return False

        # 判断持仓方向（取多数方向）
        long_amount = sum(p['amount'] for p in self._pending_closes if p.get('side', 'long') == 'long')
        short_amount = sum(p['amount'] for p in self._pending_closes if p.get('side') == 'short')
        is_short = short_amount > long_amount

        # 计算持仓均价
        total_amount = sum(p['amount'] for p in self._pending_closes)
        if total_amount <= 0:
            return False
        avg_entry = sum(
            p.get('entry_price', p.get('buy_price', 0)) * p['amount']
            for p in self._pending_closes
        ) / total_amount

        activation_pct = getattr(config, 'TRAILING_STOP_ACTIVATION_PCT', 0.02)
        callback_pct = getattr(config, 'TRAILING_STOP_CALLBACK_PCT', 0.008)

        if is_short:
            # ═══ 空头逻辑 ═══
            # 更新最低价
            if current_price < self._lowest_price_since_entry:
                self._lowest_price_since_entry = current_price

            # 浮盈百分比（空头：入场价高于当前价 = 盈利）
            floating_pct = (avg_entry - current_price) / avg_entry if avg_entry > 0 else 0

            # 激活
            if not self._trailing_stop_active and floating_pct >= activation_pct:
                self._trailing_stop_active = True
                self._trailing_stop_entry_price = avg_entry
                logger.info(
                    f"[GridExecutor] 移动止盈已激活(空头) | 浮盈: {floating_pct*100:.2f}% | "
                    f"最低价: ${self._lowest_price_since_entry:.2f} | 均价: ${avg_entry:.2f}"
                )
                send_telegram(
                    f"📉 *移动止盈已激活(空头)*\n\n"
                    f"浮盈: {floating_pct*100:.2f}%\n"
                    f"均价: ${avg_entry:.2f}\n"
                    f"当前: ${current_price:.2f}"
                )

            # 检查反弹触发（价格从最低点上涨）
            if self._trailing_stop_active and self._lowest_price_since_entry < float('inf'):
                bounce_from_low = (current_price - self._lowest_price_since_entry) / self._lowest_price_since_entry
                if bounce_from_low >= callback_pct:
                    logger.warning(
                        f"[GridExecutor] 移动止盈触发(空头)! 最低: ${self._lowest_price_since_entry:.2f} | "
                        f"当前: ${current_price:.2f} | 反弹: {bounce_from_low*100:.2f}%"
                    )
                    return self._execute_trailing_stop_close(current_price)

        else:
            # ═══ 多头逻辑 ═══
            # 更新最高价
            if current_price > self._highest_price_since_entry:
                self._highest_price_since_entry = current_price

            # 浮盈百分比（多头：当前价高于入场价 = 盈利）
            floating_pct = (current_price - avg_entry) / avg_entry if avg_entry > 0 else 0

            # 激活
            if not self._trailing_stop_active and floating_pct >= activation_pct:
                self._trailing_stop_active = True
                self._trailing_stop_entry_price = avg_entry
                logger.info(
                    f"[GridExecutor] 移动止盈已激活(多头) | 浮盈: {floating_pct*100:.2f}% | "
                    f"最高价: ${self._highest_price_since_entry:.2f} | 均价: ${avg_entry:.2f}"
                )
                send_telegram(
                    f"📈 *移动止盈已激活(多头)*\n\n"
                    f"浮盈: {floating_pct*100:.2f}%\n"
                    f"均价: ${avg_entry:.2f}\n"
                    f"当前: ${current_price:.2f}"
                )

            # 检查回撤触发（价格从最高点下跌）
            if self._trailing_stop_active and self._highest_price_since_entry > 0:
                drawdown_from_high = (self._highest_price_since_entry - current_price) / self._highest_price_since_entry
                if drawdown_from_high >= callback_pct:
                    logger.warning(
                        f"[GridExecutor] 移动止盈触发(多头)! 最高: ${self._highest_price_since_entry:.2f} | "
                        f"当前: ${current_price:.2f} | 回撤: {drawdown_from_high*100:.2f}%"
                    )
                    return self._execute_trailing_stop_close(current_price)

        return False

    def _execute_trailing_stop_close(self, current_price):
        """执行移动止盈平仓（多空通用）"""
        # 撤销所有挂单
        self.cancel_all()

        # 市价平仓
        position = self.trader.get_position()
        closed = False
        if position and position['amount'] != 0:
            close_side = 'sell' if position['amount'] > 0 else 'buy'
            close_amount = abs(position['amount'])

            close_order = self._close_position_safely(close_side, close_amount, current_price, reason="trailing_stop")
            if close_order:
                actual_close_price = float(close_order.get('average', current_price))
                actual_close_amount = self._extract_filled_amount(close_order, close_amount)
                if actual_close_amount <= 0:
                    logger.error("[GridExecutor] 移动止盈平仓成交量为0")
                    return False
                profit_usdt, fee_usdt = self._fifo_match_close(actual_close_price, actual_close_amount, is_taker=True, close_side='long' if position['amount'] > 0 else 'short')
                net_profit = profit_usdt - fee_usdt

                logger.info(
                    f"[GridExecutor] 移动止盈平仓 | 平仓价: ${actual_close_price:.2f} | "
                    f"数量: {actual_close_amount:.6f} BTC | 净利润: ${net_profit:.2f}"
                )
                self._log_binance_trade(close_side, actual_close_price, actual_close_amount, realized_pnl=profit_usdt, fee_rate=_TAKER_FEE)
                if self.strategy:
                    self.strategy.record_trade(profit_usdt=profit_usdt, fee_usdt=fee_usdt)
                self._record_cb_trade(net_profit)
                self._log_single_pnl(net_profit)

                send_telegram(
                    f"📈 *移动止盈平仓*\n\n"
                    f"平仓价: ${actual_close_price:.2f}\n"
                    f"数量: {actual_close_amount:.6f} BTC\n"
                    f"净利润: ${net_profit:.2f} USDT"
                )

                self._pending_closes = []
                self._save_state()
                closed = True
            else:
                logger.error("[GridExecutor] 移动止盈平仓下单失败")

        # 没有实际持仓时不应返回触发成功
        if not position or position.get("amount", 0) == 0:
            self._reset_trailing_stop()
            return False

        # 仅在平仓成功时重置状态并返回成功，避免误判
        if closed:
            self._reset_trailing_stop()
            return True
        return False

    # ════════════════════════════════════════════════════════════════
    # 时间止损 + 节奏止损
    # ════════════════════════════════════════════════════════════════

    _last_time_exit_ts = 0.0  # 类级别冷却时间戳
    _last_time_tp_refresh_ts = 0.0  # 类级别“时间止盈重挂”冷却时间戳

    def check_time_based_take_profit(self, current_price):
        """
        时间止盈：持仓长时间未止盈时，逐步收窄 TP 并重挂退出单。
        与时间止损互补：先争取“合理止盈离场”，再由时间止损做亏损端兜底。
        """
        if not bool(getattr(config, "TIME_TP_ENABLED", True)):
            return False

        cooldown = float(getattr(config, "TIME_TP_REFRESH_COOLDOWN_SEC", 300))
        now = time.time()
        if now - GridExecutor._last_time_tp_refresh_ts < cooldown:
            return False

        position = self.trader.get_position()
        if not position or position.get("amount", 0) == 0:
            return False

        side_key = "long" if position["amount"] > 0 else "short"
        actual_amount = abs(position["amount"])
        side_order = "sell" if side_key == "long" else "buy"
        tp_grid_side = "long_tp" if side_key == "long" else "short_tp"

        same_side_entries = [p for p in self._pending_closes if p.get("side", "long") == side_key]
        if not same_side_entries:
            return False

        entries_with_time = [p for p in same_side_entries if p.get("entry_time")]
        if not entries_with_time:
            return False
        try:
            oldest = min(datetime.fromisoformat(p["entry_time"]) for p in entries_with_time)
        except Exception:
            return False

        holding_min = (datetime.now() - oldest).total_seconds() / 60.0
        threshold_min = float(getattr(config, "TIME_TP_HOLDING_MIN", 45))
        if holding_min < threshold_min:
            return False

        base_tp = float((self.last_grid_info or {}).get("Tp", getattr(config, "TP_PER_GRID", 0.0065)))
        tp_floor = float(getattr(config, "TIME_TP_FLOOR", getattr(config, "ORPHAN_TP_FLOOR", 0.002)))
        decay_60 = float(getattr(config, "TIME_TP_DECAY_60MIN", getattr(config, "ORPHAN_TP_DECAY_60MIN", 0.60)))
        decay_120 = float(getattr(config, "TIME_TP_DECAY_120MIN", 0.45))
        decay_mid = float(getattr(config, "TIME_TP_DECAY_MID", 0.85))

        if holding_min >= 120:
            target_tp_pct = max(tp_floor, base_tp * decay_120)
        elif holding_min >= 60:
            target_tp_pct = max(tp_floor, base_tp * decay_60)
        else:
            target_tp_pct = max(tp_floor, base_tp * decay_mid)

        entry_price = float(position.get("entry_price", current_price) or current_price)
        if entry_price <= 0:
            return False

        pnl_pct = ((current_price - entry_price) / entry_price) if side_key == "long" else ((entry_price - current_price) / entry_price)
        loss_gate = abs(float(getattr(config, "TIME_TP_AGGRESSIVE_LOSS_GATE", 0.006)))
        if pnl_pct < -loss_gate:
            # 若已进入明显浮亏，则加速收窄到 floor，尽快在反弹中退出
            target_tp_pct = tp_floor

        desired_price = round(entry_price * (1 + target_tp_pct), 2) if side_key == "long" else round(entry_price * (1 - target_tp_pct), 2)
        if side_key == "long" and desired_price <= current_price:
            desired_price = round(current_price * (1 + target_tp_pct / 2), 2)
        elif side_key == "short" and desired_price >= current_price:
            desired_price = round(current_price * (1 - target_tp_pct / 2), 2)

        # 已有同侧 TP 足够接近目标，则跳过重挂
        active_same_tp = [
            o for o in self.active_orders
            if str(o.get("grid_side", "")) == tp_grid_side and str(o.get("side", "")).lower() == side_order
        ]
        existing_prices = [float(o.get("price", 0) or 0) for o in active_same_tp if float(o.get("price", 0) or 0) > 0]
        nearest_existing = min(existing_prices) if side_key == "long" and existing_prices else (max(existing_prices) if existing_prices else 0.0)
        reprice_delta = float(getattr(config, "TIME_TP_REPRICE_DELTA", 0.0006))
        if nearest_existing > 0 and current_price > 0:
            diff_pct = abs(nearest_existing - desired_price) / current_price
            if diff_pct < reprice_delta:
                return False

        # 取消同侧 TP（本地跟踪 + Binance）
        cancelled_local = 0
        cancelled_binance_ids = set()
        remain = []
        for o in self.active_orders:
            if str(o.get("grid_side", "")) == tp_grid_side and str(o.get("side", "")).lower() == side_order:
                oid = o.get("order_id")
                if oid:
                    try:
                        if self.trader.cancel_order(oid):
                            cancelled_local += 1
                            cancelled_binance_ids.add(str(oid))
                    except Exception:
                        pass
                continue
            remain.append(o)
        self.active_orders = remain

        # 也撤 Binance 上本地未跟踪的同侧 reduceOnly 单（重启后 active_orders 为空的情况）
        try:
            binance_orders = self.trader.get_open_orders() or []
            for o in binance_orders:
                if str(o.get('side', '')).lower() != side_order:
                    continue
                if not self._is_reduce_only_order(o):
                    continue
                oid = o.get('id') or o.get('order_id')
                if oid and str(oid) not in cancelled_binance_ids:
                    try:
                        if self.trader.cancel_order(oid):
                            cancelled_local += 1
                    except Exception:
                        pass
        except Exception:
            pass

        # 清理本地 reduceOnly 记录
        if hasattr(self.trader, '_local_ro_reserved'):
            self.trader._local_ro_reserved[side_order] = 0.0

        # 等待 Binance 撤单生效后直接下单，不再走 _place_tp_for_orphaned_positions 避免竞态
        if cancelled_local > 0:
            time.sleep(1.5)

        # 直接下 TP 单，不查 Binance 额度（刚撤完，额度一定够）
        tp_amount = round(actual_amount, 6)
        tp_order = self.trader.place_limit_order(side_order, desired_price, tp_amount, reduce_only=True, skip_ro_check=True)
        placed = False
        if tp_order:
            self.active_orders.append({
                'order_id': tp_order['id'],
                'side': side_order,
                'grid_side': tp_grid_side,
                'price': desired_price,
                'amount': tp_amount,
                'entry_price': entry_price,
                'layer': 0,
                '_orphan_tp': True,
            })
            self._orphan_tp_price = desired_price
            placed = True

        if not placed:
            # 下单失败时不进入刷新冷却，允许下一轮继续重试，避免“撤了旧TP却长时间不补挂”
            logger.error(
                f"[GridExecutor] ❌ 时间止盈重挂失败 | side={side_order} qty={tp_amount:.6f} "
                f"target=${desired_price:.2f} holding={holding_min:.0f}min"
            )
            # 兜底：按当前目标TP尝试孤儿仓补挂（走 reduceOnly 重平衡逻辑）
            try:
                fallback_gi = self.last_grid_info or {"P0": current_price, "Tp": target_tp_pct}
                self._place_tp_for_orphaned_positions(
                    fallback_gi, tp_pct_override=target_tp_pct, ignore_binance_existing=True
                )
                placed = any(
                    str(o.get("grid_side", "")) == tp_grid_side and str(o.get("side", "")).lower() == side_order
                    for o in self.active_orders
                )
            except Exception as e:
                logger.warning(f"[GridExecutor] 时间止盈重挂兜底失败: {e}")
            if not placed:
                return False

        GridExecutor._last_time_tp_refresh_ts = now
        logger.warning(
            f"[GridExecutor] ⏳ 时间止盈重挂 | 持仓{holding_min:.0f}min | TP={target_tp_pct*100:.2f}% | "
            f"撤TP={cancelled_local} | 目标价≈${desired_price:.2f}"
        )
        return True

    def check_time_based_exit(self, current_price):
        """
        时间止损 + 节奏止损：持仓不动 / 市场节奏变慢 → 部分减仓

        1. 时间止损：持仓 > N 分钟且亏损 → 减仓 30%
        2. 节奏止损：近期成交间隔 / 基线间隔 ≥ 2x 且亏损 → 减仓 20%

        设计原则：
        - 只减仓，不全平（留给 check_stop_loss 兜底）
        - 只在亏损时触发（盈利时不干预）
        - 有冷却期（600s 内不重复触发）
        """
        # 总开关
        time_enabled = getattr(config, "TIME_SL_ENABLED", True)
        rhythm_enabled = getattr(config, "RHYTHM_SL_ENABLED", True)
        if not time_enabled and not rhythm_enabled:
            return False

        # 冷却期
        cooldown = float(getattr(config, "TIME_EXIT_COOLDOWN_SEC", 600))
        now = time.time()
        if now - GridExecutor._last_time_exit_ts < cooldown:
            return False

        position = self.trader.get_position()
        if not position or position.get('amount', 0) == 0:
            return False

        is_long = position['amount'] > 0
        entry_price = position.get('entry_price', current_price)
        if entry_price <= 0:
            return False

        # 亏损判断
        if is_long:
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price
        if pnl_pct >= 0:
            return False  # 盈利中，不干预

        # FIFO 同向记录
        side_key = 'long' if is_long else 'short'
        same_side = [p for p in self._pending_closes if p.get('side', 'long') == side_key]
        if not same_side:
            return False

        reduce_pct = 0.0
        reason = ""

        # ── 时间止损 ──
        if time_enabled:
            entries_with_time = [
                p for p in same_side if 'entry_time' in p and p['entry_time']
            ]
            if entries_with_time:
                try:
                    oldest = min(
                        datetime.fromisoformat(p['entry_time'])
                        for p in entries_with_time
                    )
                    holding_min = (datetime.now() - oldest).total_seconds() / 60.0
                    threshold_min = float(getattr(config, "TIME_SL_HOLDING_MIN", 45))
                    if holding_min > threshold_min:
                        loss_gate = abs(float(getattr(config, "TIME_SL_MIN_LOSS_PCT", 0.0035)))
                        force_exit_min = float(getattr(config, "TIME_SL_FORCE_EXIT_HOLDING_MIN", 180))
                        force_exit_loss_gate = abs(float(getattr(config, "TIME_SL_FORCE_EXIT_MIN_LOSS_PCT", 0.0040)))
                        force_exit = (
                            force_exit_min > threshold_min
                            and holding_min >= force_exit_min
                            and abs(pnl_pct) >= force_exit_loss_gate
                        )
                        if abs(pnl_pct) >= loss_gate or force_exit:
                            reduce_pct = float(getattr(config, "TIME_SL_REDUCE_PCT", 0.30))
                            trigger = (
                                f"亏损阈值触发 |loss|={abs(pnl_pct)*100:.2f}%≥{loss_gate*100:.2f}%"
                                if not force_exit else
                                f"超时强制触发 持仓{holding_min:.0f}min≥{force_exit_min:.0f}min 且 "
                                f"|loss|={abs(pnl_pct)*100:.2f}%≥{force_exit_loss_gate*100:.2f}%"
                            )
                            reason = (
                                f"时间止损: 持仓{holding_min:.0f}min>{threshold_min:.0f}min, "
                                f"PnL={pnl_pct*100:.2f}% | {trigger}"
                            )
                except Exception:
                    pass

        # ── 节奏止损 ──
        if rhythm_enabled:
            min_samples = int(getattr(config, "RHYTHM_SL_MIN_SAMPLES", 10))
            if len(list(self._fill_timestamps)) >= min_samples:
                interval_short = self.get_avg_fill_interval(last_n=5)
                interval_baseline = self.get_avg_fill_interval(last_n=20)
                ratio_threshold = float(getattr(config, "RHYTHM_SL_RATIO", 2.0))
                if interval_baseline > 30 and interval_short > 0:
                    ratio = interval_short / interval_baseline
                    if ratio >= ratio_threshold:
                        rhythm_loss_gate = abs(float(getattr(config, "RHYTHM_SL_MIN_LOSS_PCT", 0.0040)))
                        if abs(pnl_pct) >= rhythm_loss_gate:
                            rhythm_pct = float(getattr(config, "RHYTHM_SL_REDUCE_PCT", 0.20))
                            if rhythm_pct > reduce_pct:
                                reduce_pct = rhythm_pct
                                reason = (
                                    f"节奏止损: 间隔比={ratio:.1f}x≥{ratio_threshold}x "
                                    f"(近5: {interval_short:.0f}s / 基线: {interval_baseline:.0f}s), "
                                    f"PnL={pnl_pct*100:.2f}%"
                                )

        if reduce_pct <= 0:
            return False

        # 执行部分减仓
        close_side = 'sell' if is_long else 'buy'
        step = float(getattr(config, 'ORDER_STEP_SIZE', 0.001))
        raw_amount = abs(position['amount']) * reduce_pct
        close_amount = math.floor(raw_amount / step) * step
        if close_amount < step:
            return False

        min_notional = float(getattr(config, 'MIN_ORDER_NOTIONAL', 130))
        if getattr(config, "TRADING_MODE", "paper") == "paper":
            min_notional = float(getattr(config, "MIN_ORDER_NOTIONAL_PAPER", 0))
        if close_amount * current_price < min_notional:
            return False

        logger.warning(f"[GridExecutor] ⏰ {reason} → 减仓 {close_amount:.4f} BTC ({reduce_pct*100:.0f}%)")

        close_order = self._close_position_safely(
            close_side, close_amount, current_price, reason="time_rhythm_sl"
        )
        if not close_order:
            logger.error("[GridExecutor] ❌ 时间/节奏止损下单失败")
            return False

        actual_price = float(close_order.get('average', current_price))
        actual_amount = self._extract_filled_amount(close_order, close_amount)
        if actual_amount <= 0:
            return False

        profit_usdt, fee_usdt = self._fifo_match_close(
            actual_price, actual_amount, is_taker=True, close_side=side_key
        )
        net_profit = profit_usdt - fee_usdt
        self._log_binance_trade(
            close_side, actual_price, actual_amount,
            realized_pnl=profit_usdt, fee_rate=_TAKER_FEE
        )
        if self.strategy:
            self.strategy.record_trade(profit_usdt=profit_usdt, fee_usdt=fee_usdt)
        self._record_cb_trade(net_profit)
        self._log_single_pnl(net_profit)
        self._rebuild_exits_after_position_change(current_price, reason="time_rhythm_sl")
        self._save_state()

        GridExecutor._last_time_exit_ts = now

        send_telegram(
            f"⏰ *{'时间' if '时间' in reason else '节奏'}止损*\n\n"
            f"{reason}\n"
            f"减仓: {actual_amount:.4f} BTC @ ${actual_price:.2f}\n"
            f"净盈亏: ${net_profit:.2f}"
        )
        return True

    def rebuild_grid(self, new_grid_info):
        """重建网格"""
        logger.info("[GridExecutor] 重建网格")
        self.deploy_grid(new_grid_info)

    def _rebuild_exits_after_position_change(self, current_price, reason="position_change"):
        """
        部分减仓后，保守地撤掉旧网格单并按剩余仓位重挂退出单，
        避免 TP 总量大于真实仓位。
        """
        logger.info(f"[GridExecutor] 持仓变化后重建退出单 | reason={reason}")
        if self.cancel_all(preserve_tp=False) is False:
            logger.warning("[GridExecutor] 持仓变化后退出单重建跳过：撤单结果未确认")
            return
        if self.active_orders:
            logger.warning("[GridExecutor] 持仓变化后仍存在未清理挂单，跳过退出单重建")
            return
        pos = self.trader.get_position()
        if not pos or abs(float(pos.get("amount", 0.0) or 0.0)) < 1e-9:
            return
        fallback_gi = dict(self.last_grid_info or {})
        fallback_gi.setdefault("P0", float(current_price or 0.0))
        fallback_gi.setdefault(
            "Tp",
            float((self.last_grid_info or {}).get("Tp", getattr(config, "TP_PER_GRID", 0.0065))),
        )
        if float(fallback_gi.get("P0", 0.0) or 0.0) <= 0:
            return
        self._place_tp_for_orphaned_positions(fallback_gi, ignore_binance_existing=True)

    def refresh_after_external_position_change(self, current_price, reason="external_position_change"):
        """供执行层在外部直接市价减仓/平仓后刷新本地订单状态。"""
        self._sync_pending_closes_with_position()
        self._reconcile_fifo()
        self._rebuild_exits_after_position_change(current_price, reason=reason)

    def get_position_summary(self):
        """获取持仓摘要"""
        if not self._pending_closes:
            return {
                'total_amount': 0.0,
                'avg_buy_price': 0.0,
                'pending_closes_count': 0
            }

        total_amount = sum(p['amount'] for p in self._pending_closes)
        avg_buy_price = sum(
            p.get('entry_price', p.get('buy_price', 0)) * p['amount']
            for p in self._pending_closes
        ) / total_amount if total_amount > 0 else 0

        return {
            'total_amount': total_amount,
            'avg_buy_price': avg_buy_price,
            'pending_closes_count': len(self._pending_closes)
        }

    def get_no_fill_seconds(self):
        """获取距离上次成交的秒数。"""
        anchor = float(self._no_fill_anchor_ts or self._last_fill_ts or time.time())
        return max(0, int(time.time() - anchor))

    def get_consecutive_with_decay(self):
        """
        获取带时间衰减的连续同向成交计数。
        无成交时间越长 → 计数逐步回落，让 spacing 恢复"呼吸能力"。
        衰减规则：每过 CONSECUTIVE_DECAY_SEC 秒无成交，计数 -1（最低 0）。
        """
        if self._consecutive_count <= 0:
            return 0
        no_fill_sec = self.get_no_fill_seconds()
        decay_interval = float(getattr(config, "CONSECUTIVE_DECAY_SEC", 300))  # 默认5分钟衰减1级
        if decay_interval <= 0:
            return self._consecutive_count
        decay_steps = int(no_fill_sec / decay_interval)
        decayed = max(0, self._consecutive_count - decay_steps)
        if decayed < self._consecutive_count:
            logger.debug(
                f"[GridExecutor] 连续成交衰减: {self._consecutive_count}→{decayed} "
                f"(无成交{no_fill_sec:.0f}s, 每{decay_interval:.0f}s衰减1)"
            )
        return decayed

    def get_recent_fill_count(self, window_sec=1800):
        """获取最近窗口内的成交次数。"""
        now = time.time()
        window = max(1, int(window_sec or 1800))
        while self._fill_timestamps and (now - self._fill_timestamps[0]) > window:
            self._fill_timestamps.popleft()
        return len(self._fill_timestamps)

    def get_avg_fill_interval(self, last_n=10):
        """最近 N 次成交的平均间隔（秒）。无数据返回 0。"""
        ts_list = list(self._fill_timestamps)
        if len(ts_list) < 2:
            return 0.0
        recent = ts_list[-min(last_n + 1, len(ts_list)):]
        intervals = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
        return sum(intervals) / len(intervals) if intervals else 0.0

    def reset_no_fill_timer(self, reason="rebuild"):
        """重建后重置无成交计时，给新网格一个观察期。"""
        now = time.time()
        self._no_fill_anchor_ts = now
        self._no_fill_reset_by_rebuild = (str(reason) == "rebuild")
        logger.info(f"[GridExecutor] 无成交计时已重置 | reason={reason}")

    def consume_no_fill_rebuild_flag(self):
        """一次性读取并清空“因重建导致 no_fill 归零”标记。"""
        flag = bool(self._no_fill_reset_by_rebuild)
        self._no_fill_reset_by_rebuild = False
        return flag

    def should_force_rebuild_no_fill(self, no_fill_sec, has_position=False):
        """无成交超时触发强制重建（带冷却）。"""
        if has_position:
            return False
        threshold = int(
            getattr(
                config,
                "FLOW_FORCE_REBUILD_AFTER_SEC",
                getattr(config, "NO_FILL_FORCE_REBUILD_SEC", 3600)
            )
        )
        if int(no_fill_sec or 0) < threshold:
            return False
        cooldown = int(
            getattr(
                config,
                "FLOW_FORCE_REBUILD_COOLDOWN_SEC",
                getattr(config, "NO_FILL_FORCE_REBUILD_COOLDOWN_SEC", 1800)
            )
        )
        now = time.time()
        if self._last_no_fill_rebuild_ts > 0 and (now - self._last_no_fill_rebuild_ts) < cooldown:
            return False
        self._last_no_fill_rebuild_ts = now
        return True

    def handle_signal(self, signal):
        """
        处理策略信号
        ✅ 修复：实际执行交易而不只是 log
        ✅ 修复Bug4：出场后重置移动止盈状态

        Args:
            signal: 信号 dict {'type': 'entry'/'exit', 'side': 'long'/'short', ...}
        """
        signal_type = signal.get('type', '')

        if signal_type == 'entry':
            logger.info(f"[GridExecutor] 收到入场信号: {signal}")

            # 实际执行入场交易
            signal_side = signal.get('side', 'long')  # 'long' or 'short'
            order_side = 'buy' if signal_side == 'long' else 'sell'
            price = signal.get('price', 0)

            # 使用网格资金的一部分
            if self.strategy and price > 0:
                ratio = getattr(config, "TREND_CAPITAL_RATIO", 0.25)
                trade_capital = self.strategy.grid_capital * ratio
                amount = trade_capital / price

                order = self.trader.place_market_order(order_side, round(amount, 6))
                if order:
                    logger.info(f"[GridExecutor] ✅ 入场执行: {order_side} {amount:.6f} BTC @ ~${price:.2f}")
                    fill_price = float(order.get('average', order.get('price', price)))
                    fill_amount = float(order.get('filled', amount))
                    self._log_binance_trade(order_side, fill_price, fill_amount, realized_pnl=0.0, fee_rate=_TAKER_FEE)
                    # 记录持仓方向，用于正确计算平仓 PnL
                    self._pending_closes.append({
                        'entry_price': fill_price,
                        'buy_price': fill_price,  # 兼容网格买单的 FIFO 匹配
                        'amount': fill_amount,
                        'side': signal_side,       # 'long' or 'short'
                        'layer': 0,
                        'entry_time': datetime.now().isoformat(),
                        'entry_fee_rate': _TAKER_FEE,
                    })
                    self._save_state()
                    # 回调通知策略
                    self._notify_fill(signal)
                else:
                    logger.error(f"[GridExecutor] ❌ 入场执行失败")
            else:
                logger.warning(f"[GridExecutor] ⚠️ 无法执行入场: strategy={self.strategy is not None}, price={price}")

        elif signal_type == 'exit':
            logger.info(f"[GridExecutor] 收到出场信号: {signal}")
            self.cancel_all()

            # 实际执行平仓
            position = self.trader.get_position()
            if position and position['amount'] != 0:
                close_side = 'sell' if position['amount'] > 0 else 'buy'
                close_amount = abs(position['amount'])
                current_price = float(signal.get('price', position.get('entry_price', 0)) or 0)
                if current_price <= 0:
                    # 兜底：避免未定义或无效价格导致出场失败
                    current_price = float(position.get('entry_price', 0) or 0)
                order = self._close_position_safely(close_side, close_amount, current_price, reason="signal_exit")
                if order:
                    fill_price = float(order.get('average', order.get('price', signal.get('price', 0))))
                    fill_amount = float(order.get('filled', close_amount))

                    # FIFO 匹配 pending_closes（支持多空方向）
                    fifo_side = 'long' if position['amount'] > 0 else 'short'
                    profit_usdt, fee_usdt = self._fifo_match_close(fill_price, fill_amount, is_taker=True, close_side=fifo_side)
                    if self.strategy and (profit_usdt != 0 or fee_usdt != 0):
                        self.strategy.record_trade(profit_usdt=profit_usdt, fee_usdt=fee_usdt)
                    self._record_cb_trade(profit_usdt - fee_usdt)
                    self._log_binance_trade(close_side, fill_price, fill_amount, realized_pnl=profit_usdt, fee_rate=_TAKER_FEE)
                    self._log_single_pnl(profit_usdt - fee_usdt)
                    logger.info(
                        f"[GridExecutor] ✅ 出场执行: {close_side} {fill_amount:.6f} BTC | "
                        f"净盈亏: ${profit_usdt - fee_usdt:.2f}"
                    )
                    self._save_state()
                    self._notify_fill(signal)
                else:
                    logger.error(f"[GridExecutor] ❌ 出场执行失败")

            # ✅ 修复Bug4：出场后重置移动止盈状态
            self._reset_trailing_stop()

        else:
            logger.debug(f"[GridExecutor] 未知信号类型: {signal_type}")

    def _notify_fill(self, signal):
        """通知产生信号的策略成交结果"""
        # 通知 strategy_manager 中的所有策略
        if self.strategy and hasattr(self.strategy, '_strategy_manager'):
            manager = self.strategy._strategy_manager
            if manager:
                for s in manager.strategies:
                    if hasattr(s, 'on_fill'):
                        try:
                            s.on_fill(signal)
                        except Exception as e:
                            logger.error(f"[GridExecutor] on_fill 回调失败: {e}")

