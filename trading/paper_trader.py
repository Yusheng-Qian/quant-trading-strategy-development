# paper_trader.py - 模拟交易（仿真实盘）
"""
模拟交易执行器 - 完全仿照实盘行为
✅ 限价单挂单，价格到达才成交（不再立即成交）
✅ 余额随交易动态变化（保证金冻结/释放）
✅ 正确的开仓/平仓/反向开仓逻辑
✅ 杠杆保证金计算
✅ 手续费模拟（Binance 合约 taker 0.04%, maker 0.02%）
✅ 市价单滑点模拟
✅ 完整的 PnL 追踪和历史记录
✅ 与 BinanceTrader 接口完全兼容
"""
import time
import json
import os
import math
from datetime import datetime
from collections import defaultdict
from utils.logger import get_logger
import config

logger = get_logger()

# Binance USDT 合约费率
MAKER_FEE = 0.0002   # 0.02%
TAKER_FEE = 0.0004   # 0.04%
SLIPPAGE_PCT = 0.0001 # 市价单滑点 0.01%


class PaperTrader:
    """
    仿实盘模拟交易器
    
    与 BinanceTrader 接口完全一致，可无缝切换。
    """
    
    def __init__(self, initial_capital=None, state_file=None,
                 load_state=True, clear_history_on_restart=None):
        self.initial_capital = initial_capital or config.PAPER_INITIAL_CAPITAL
        self.leverage = 1
        if clear_history_on_restart is None:
            clear_history_on_restart = bool(
                getattr(config, "PAPER_CLEAR_STATE_ON_STARTUP", True)
            )
        
        # ── 资金账户 ────────────────────────────────────────
        self._wallet_balance = self.initial_capital     # 钱包余额（已实现盈亏累计）
        self._frozen_margin = 0.0                       # 冻结保证金
        
        # ── 持仓 ────────────────────────────────────────────
        self._position = {
            'amount': 0.0,           # 正=多，负=空，0=无持仓
            'entry_price': 0.0,
            'unrealized_pnl': 0.0,
        }
        
        # ── 挂单簿 ──────────────────────────────────────────
        self._open_orders = {}       # {order_id: order_dict}
        self._order_id_counter = 1000
        
        # ── 当前市场价 ──────────────────────────────────────
        self._current_price = 0.0
        
        # ── 交易历史和 PnL 追踪 ─────────────────────────────
        self._trade_history = []     # 已成交记录
        self._income_history = []    # 收益流水（兼容 Binance income API）
        self._realized_pnl_total = 0.0
        self._commission_total = 0.0
        self._event_seq = 0
        self._event_log = []
        self._event_log_limit = int(getattr(config, "PAPER_EVENT_LOG_LIMIT", 20000))
        
        # ── 持久化 ──────────────────────────────────────────
        self._state_file = state_file or getattr(config, "PAPER_STATE_FILE", "paper_state.json")
        if load_state:
            self._load_state(clear_history_on_restart=clear_history_on_restart)
        elif clear_history_on_restart:
            self._load_state(clear_history_on_restart=True)
        
        logger.info(
            f"[PaperTrader] 初始化 | 资金: ${self.initial_capital} | "
            f"钱包余额: ${self._wallet_balance:.2f}"
        )
    
    # ══════════════════════════════════════════════════════════════
    # 与 BinanceTrader 兼容的公开接口
    # ══════════════════════════════════════════════════════════════
    
    def get_balance(self):
        """获取账户余额"""
        unrealized = self._calc_unrealized_pnl()
        return {
            'free': round(self._wallet_balance - self._frozen_margin, 2),
            'total': round(self._wallet_balance + unrealized, 2),
        }
    
    def get_position(self):
        """获取当前持仓"""
        if self._position['amount'] == 0:
            return None
        
        self._position['unrealized_pnl'] = self._calc_unrealized_pnl()
        return {
            'amount': self._position['amount'],
            'entry_price': self._position['entry_price'],
            'unrealized_pnl': round(self._position['unrealized_pnl'], 2),
        }
    
    def get_open_orders(self):
        """获取当前挂单"""
        return list(self._open_orders.values())

    def get_reduce_only_capacity(self, side):
        """估算 reduceOnly 可用平仓量（兼容 BinanceTrader 接口）。"""
        pos = self.get_position()
        if not pos:
            return 0.0, 0.0, 0.0, 0.0, 0.0
        pos_amt = float(pos.get("amount", 0.0) or 0.0)
        max_reducible = abs(pos_amt)
        if side == "sell" and pos_amt <= 0:
            return 0.0, 0.0, max_reducible, 0.0, 0.0
        if side == "buy" and pos_amt >= 0:
            return 0.0, 0.0, max_reducible, 0.0, 0.0

        reserved = 0.0
        for order in self._open_orders.values():
            if order.get("status") != "open" or not bool(order.get("reduce_only", False)):
                continue
            if str(order.get("side", "")).lower() != str(side).lower():
                continue
            try:
                reserved += abs(float(order.get("amount", 0.0) or 0.0))
            except Exception:
                continue
        available = max(0.0, max_reducible - reserved)
        step = float(getattr(config, "ORDER_STEP_SIZE", 0.001) or 0.001)
        if step > 0:
            available = math.floor(available / step) * step
        return round(available, 6), round(reserved, 6), round(max_reducible, 6), round(reserved, 6), 0.0
    
    def get_order_status(self, order_id):
        """获取订单状态"""
        order_id = str(order_id)
        # 先查挂单簿
        if order_id in self._open_orders:
            return self._open_orders[order_id]
        # 再查历史成交
        for trade in self._trade_history:
            if str(trade.get('id')) == order_id:
                return trade
        return None
    
    def cancel_all_orders(self):
        """撤销所有挂单"""
        count = len(self._open_orders)
        # 释放冻结的保证金
        for order in self._open_orders.values():
            self._release_margin(order)
            self._emit_event(
                "ORDER_CANCELED",
                order_id=str(order.get("id")),
                side=str(order.get("side", "")),
                order_type=str(order.get("type", "")),
                price=float(order.get("price", 0) or 0),
                amount=float(order.get("amount", 0) or 0),
                reason="cancel_all_orders",
            )
        self._open_orders.clear()
        logger.info(f"[PaperTrader] 已撤销 {count} 个挂单")
        self._save_state()
        return count

    def cancel_order(self, order_id):
        """撤销单个挂单"""
        oid = str(order_id)
        if oid not in self._open_orders:
            return False
        order = self._open_orders.pop(oid)
        self._release_margin(order)
        self._emit_event(
            "ORDER_CANCELED",
            order_id=str(order.get("id")),
            side=str(order.get("side", "")),
            order_type=str(order.get("type", "")),
            price=float(order.get("price", 0) or 0),
            amount=float(order.get("amount", 0) or 0),
            reason="cancel_order",
        )
        logger.info(f"[PaperTrader] 已撤销订单 {oid}")
        self._save_state()
        return True
    
    def place_limit_order(self, side, price, amount, client_order_id=None, reduce_only=False, skip_ro_check=False):
        """
        下限价单 - 挂单等待成交
        
        不会立即成交！只有当 set_current_price() 检测到价格触发时才成交。
        """
        if amount < 0.001:
            logger.warning(f"[PaperTrader] 数量 {amount} < 最小 0.001，跳过")
            self._emit_event(
                "ORDER_REJECTED",
                side=str(side),
                order_type="limit",
                price=float(price or 0),
                amount=float(amount or 0),
                reason="amount_below_min",
            )
            return None
        
        normalized_amount = self._normalize_reduce_only_amount(side, amount, reduce_only)
        if normalized_amount is None:
            self._emit_event(
                "ORDER_REJECTED",
                side=str(side),
                order_type="limit",
                price=float(price or 0),
                amount=float(amount or 0),
                reduce_only=bool(reduce_only),
                reason="reduce_only_invalid",
            )
            return None
        amount = normalized_amount

        # 检查保证金是否足够
        required_margin = 0.0 if reduce_only else (price * amount) / self.leverage
        fee_estimate = price * amount * MAKER_FEE
        required_total = fee_estimate if reduce_only else (required_margin + fee_estimate)
        if required_total > self._wallet_balance - self._frozen_margin:
            logger.warning(
                f"[PaperTrader] 保证金不足 | "
                f"需要: ${required_total:.2f} | "
                f"可用: ${self._wallet_balance - self._frozen_margin:.2f}"
            )
            self._emit_event(
                "ORDER_REJECTED",
                side=str(side),
                order_type="limit",
                price=float(price or 0),
                amount=float(amount or 0),
                reduce_only=bool(reduce_only),
                reason="insufficient_margin",
                required_margin=round(required_total, 6),
                available_margin=round(self._wallet_balance - self._frozen_margin, 6),
            )
            return None
        
        order_id = str(self._order_id_counter)
        self._order_id_counter += 1
        
        order = {
            'id': order_id,
            'side': side,
            'type': 'limit',
            'price': price,
            'amount': amount,
            'filled': 0,
            'average': 0,
            'status': 'open',
            'created_at': datetime.now().isoformat(),
            'margin_frozen': required_margin,
            'client_order_id': client_order_id,
            'reduce_only': bool(reduce_only),
        }
        
        self._open_orders[order_id] = order
        self._frozen_margin += required_margin
        self._emit_event(
            "ORDER_SUBMITTED",
            order_id=order_id,
            side=str(side),
            order_type="limit",
            price=float(price),
            amount=float(amount),
            reduce_only=bool(reduce_only),
            client_order_id=client_order_id,
        )
        self._emit_event(
            "ORDER_ACCEPTED",
            order_id=order_id,
            side=str(side),
            order_type="limit",
            price=float(price),
            amount=float(amount),
            reduce_only=bool(reduce_only),
        )
        
        logger.info(
            f"[PaperTrader] 📋 挂单: {side.upper()} {amount:.6f} BTC "
            f"@ ${price:.2f} | 冻结保证金: ${required_margin:.2f}"
        )
        
        # ✅ 修复：不在挂单时立即检查成交
        # 限价单必须等待下一次 set_current_price() 带来的价格变动才能触发
        # 买单需要价格跌到挂单价，卖单需要价格涨到挂单价
        
        self._save_state()
        return order
    
    def place_market_order(self, side, amount, note=None, reduce_only=False):
        """
        下市价单 - 以当前价（含滑点）立即成交
        """
        if self._current_price <= 0:
            logger.error("[PaperTrader] 无法下市价单：当前价格未知")
            self._emit_event(
                "ORDER_REJECTED",
                side=str(side),
                order_type="market",
                amount=float(amount or 0),
                reason="unknown_current_price",
            )
            return None
        
        if amount < 0.001:
            logger.warning(f"[PaperTrader] 数量 {amount} < 最小 0.001，跳过")
            self._emit_event(
                "ORDER_REJECTED",
                side=str(side),
                order_type="market",
                amount=float(amount or 0),
                reason="amount_below_min",
            )
            return None

        normalized_amount = self._normalize_reduce_only_amount(side, amount, reduce_only)
        if normalized_amount is None:
            self._emit_event(
                "ORDER_REJECTED",
                side=str(side),
                order_type="market",
                amount=float(amount or 0),
                reduce_only=bool(reduce_only),
                reason="reduce_only_invalid",
            )
            return None
        amount = normalized_amount

        # 模拟滑点
        if side == 'buy':
            fill_price = self._current_price * (1 + SLIPPAGE_PCT)
        else:
            fill_price = self._current_price * (1 - SLIPPAGE_PCT)
        
        fill_price = round(fill_price, 2)
        
        order_id = str(self._order_id_counter)
        self._order_id_counter += 1
        
        order = {
            'id': order_id,
            'side': side,
            'type': 'market',
            'price': fill_price,
            'amount': amount,
            'filled': amount,
            'average': fill_price,
            'status': 'filled',
            'created_at': datetime.now().isoformat(),
            'note': note or '',
            'reduce_only': bool(reduce_only),
        }
        self._emit_event(
            "ORDER_SUBMITTED",
            order_id=order_id,
            side=str(side),
            order_type="market",
            price=float(fill_price),
            amount=float(amount),
            reduce_only=bool(reduce_only),
            note=str(note or ""),
        )
        self._emit_event(
            "ORDER_ACCEPTED",
            order_id=order_id,
            side=str(side),
            order_type="market",
            price=float(fill_price),
            amount=float(amount),
            reduce_only=bool(reduce_only),
        )
        
        # 直接执行成交
        self._execute_fill(order, fill_price, amount, is_taker=True)
        
        self._save_state()
        return order
    
    def close_position(self, side, amount, note=None, reduce_only=True):
        """平仓"""
        return self.place_market_order(side, amount, note=note, reduce_only=reduce_only)
    
    def set_leverage(self, leverage):
        """设置杠杆"""
        self.leverage = leverage
        # 重新计算冻结保证金
        self._recalc_frozen_margin()
        logger.info(f"[PaperTrader] 杠杆已设置为 {leverage}x")
    
    def set_current_price(self, price):
        """
        更新当前市场价 - 核心！触发挂单成交检查
        
        每轮主循环都会调用，模拟行情推动挂单成交。
        """
        old_price = self._current_price
        self._current_price = price
        
        # 更新浮动盈亏
        self._position['unrealized_pnl'] = self._calc_unrealized_pnl()
        
        # 检查所有挂单是否触发
        if old_price > 0 and price > 0:
            self._check_pending_fills(old_price, price)
    
    # ══════════════════════════════════════════════════════════════
    # Binance PnL 接口兼容（供 AdaptiveStrategy 使用）
    # ══════════════════════════════════════════════════════════════
    
    def get_income_history(self, income_type=None, limit=100, start_time=None, end_time=None):
        """获取收益流水（兼容 Binance income API）"""
        records = self._income_history
        
        if income_type:
            records = [r for r in records if r['income_type'] == income_type]
        
        if start_time:
            records = [r for r in records if r['time'] >= start_time]
        if end_time:
            records = [r for r in records if r['time'] <= end_time]
        
        return records[-limit:]
    
    def get_pnl_summary(self, days=30):
        """获取盈亏汇总（兼容 BinanceTrader.get_pnl_summary）"""
        cutoff = int((time.time() - days * 86400) * 1000)
        
        realized = [r for r in self._income_history 
                     if r['income_type'] == 'REALIZED_PNL' and r['time'] >= cutoff]
        commissions = [r for r in self._income_history 
                        if r['income_type'] == 'COMMISSION' and r['time'] >= cutoff]
        
        total_realized = sum(r['income'] for r in realized)
        total_commission = sum(r['income'] for r in commissions)
        
        win_count = sum(1 for r in realized if r['income'] > 0)
        loss_count = sum(1 for r in realized if r['income'] < 0)
        trade_count = len(realized)
        
        daily_pnl = defaultdict(float)
        for r in realized:
            day_str = datetime.fromtimestamp(r['time'] / 1000).strftime('%Y-%m-%d')
            daily_pnl[day_str] += r['income']
        
        return {
            'total_realized_pnl': round(total_realized, 2),
            'total_funding_fee': 0.0,  # Paper 模式无资金费率
            'total_commission': round(total_commission, 2),
            'net_pnl': round(total_realized + total_commission, 2),
            'trade_count': trade_count,
            'win_count': win_count,
            'loss_count': loss_count,
            'win_rate': round(win_count / trade_count, 4) if trade_count > 0 else 0,
            'daily_pnl': dict(daily_pnl),
            'days': days,
        }
    
    def get_trade_history(self, limit=50):
        """获取成交记录"""
        return self._trade_history[-limit:]

    def get_event_log(self, limit=1000, event_type=None):
        """获取 PaperTrader 事件账本（借鉴事件驱动回测模型）。"""
        events = self._event_log
        if event_type:
            events = [e for e in events if str(e.get("type")) == str(event_type)]
        if limit is None:
            return list(events)
        try:
            n = int(limit)
        except Exception:
            n = 1000
        if n <= 0:
            return []
        return events[-n:]
    
    # ══════════════════════════════════════════════════════════════
    # 核心引擎：挂单触发检查
    # ══════════════════════════════════════════════════════════════
    
    def _check_pending_fills(self, old_price, new_price):
        """
        检查价格变动是否触发挂单成交
        
        ✅ 修复：严格检查价格穿越
        买单：市价从上方跌穿挂单价（new_price <= order_price）
        卖单：市价从下方涨穿挂单价（new_price >= order_price）
        """
        filled_ids = []
        
        for order_id, order in list(self._open_orders.items()):
            if order['status'] != 'open':
                continue
            
            order_price = order['price']
            triggered = False
            
            if order['side'] == 'buy':
                # 买单：当前价跌到挂单价或更低时成交
                if new_price <= order_price:
                    triggered = True
            elif order['side'] == 'sell':
                # 卖单：当前价涨到挂单价或更高时成交
                if new_price >= order_price:
                    triggered = True
            
            if triggered:
                # 以挂单价成交（限价单保证成交价不差于挂单价）
                self._try_fill_order(order_id, order_price)
                filled_ids.append(order_id)
        
        # 清理已成交订单
        for oid in filled_ids:
            if oid in self._open_orders and self._open_orders[oid]['status'] == 'filled':
                del self._open_orders[oid]
    
    def _try_fill_order(self, order_id, fill_price):
        """尝试成交一个挂单"""
        if order_id not in self._open_orders:
            return
        
        order = self._open_orders[order_id]
        if order['status'] != 'open':
            return
        
        amount = order['amount']
        self._emit_event(
            "ORDER_TRIGGERED",
            order_id=str(order_id),
            side=str(order.get("side", "")),
            order_type=str(order.get("type", "")),
            trigger_price=float(fill_price or 0),
            amount=float(amount or 0),
        )
        
        # 限价单用 maker 费率
        self._execute_fill(order, fill_price, amount, is_taker=False)
        
        # 释放冻结保证金
        self._release_margin(order)
    
    # ══════════════════════════════════════════════════════════════
    # 核心引擎：成交执行
    # ══════════════════════════════════════════════════════════════
    
    def _execute_fill(self, order, fill_price, fill_amount, is_taker=False):
        """
        执行成交 - 正确处理开仓/平仓/反向开仓
        
        合约逻辑：
        - 买入时有空仓 → 先平空，多余部分开多
        - 卖出时有多仓 → 先平多，多余部分开空
        - 同向 → 加仓
        """
        side = order['side']
        fee_rate = TAKER_FEE if is_taker else MAKER_FEE
        fee = round(fill_price * fill_amount * fee_rate, 4)
        
        # 扣手续费
        self._wallet_balance -= fee
        self._commission_total += fee
        
        # 记录手续费到收益流水
        self._record_income('COMMISSION', -fee, f"{'taker' if is_taker else 'maker'} fee")
        
        old_amount = self._position['amount']
        old_entry = self._position['entry_price']

        if bool(order.get("reduce_only", False)):
            if old_amount == 0:
                logger.warning("[PaperTrader] reduceOnly 成交时已无持仓，忽略反向开仓")
                order['status'] = 'canceled'
                return
            if old_amount > 0 and side != 'sell':
                logger.warning("[PaperTrader] reduceOnly 方向与多头持仓不匹配，忽略成交")
                order['status'] = 'canceled'
                return
            if old_amount < 0 and side != 'buy':
                logger.warning("[PaperTrader] reduceOnly 方向与空头持仓不匹配，忽略成交")
                order['status'] = 'canceled'
                return
            fill_amount = min(fill_amount, abs(old_amount))

        trade_amount = fill_amount if side == 'buy' else -fill_amount
        new_amount = old_amount + trade_amount

        # ── 计算已实现盈亏（平仓部分）──────────────────────
        realized_pnl = 0.0
        if old_amount != 0 and (old_amount * trade_amount < 0):
            # 有平仓发生（方向相反）
            close_amount = min(abs(old_amount), abs(trade_amount))
            if old_amount > 0:
                realized_pnl = (fill_price - old_entry) * close_amount
            else:
                realized_pnl = (old_entry - fill_price) * close_amount
            self._wallet_balance += realized_pnl
            self._realized_pnl_total += realized_pnl
            self._record_income('REALIZED_PNL', realized_pnl,
                                f"close {'long' if old_amount > 0 else 'short'}")
            logger.info(
                f"[PaperTrader] 💰 平仓盈亏: ${realized_pnl:+.2f} | "
                f"平仓数量: {close_amount:.6f} @ ${fill_price:.2f}"
            )

        # ── 更新持仓 ────────────────────────────────────────
        if abs(new_amount) < 1e-8:
            # 完全平仓
            self._position = {
                'amount': 0.0,
                'entry_price': 0.0,
                'unrealized_pnl': 0.0,
            }
        elif old_amount * trade_amount > 0:
            # 同向加仓
            total_cost = old_entry * abs(old_amount) + fill_price * abs(trade_amount)
            self._position['amount'] = new_amount
            self._position['entry_price'] = total_cost / abs(new_amount)
        elif old_amount * trade_amount < 0:
            self._position['amount'] = new_amount
            if old_amount * new_amount < 0:
                # 方向翻转（多→空 或 空→多），用新成交价作为入场价
                self._position['entry_price'] = fill_price
            # else: 部分平仓，保持原入场价
        else:
            # 从空仓开始建仓
            self._position['amount'] = new_amount
            self._position['entry_price'] = fill_price
        
        # 更新浮动盈亏
        self._position['unrealized_pnl'] = self._calc_unrealized_pnl()
        
        # ── 更新冻结保证金 ──────────────────────────────────
        self._recalc_frozen_margin()
        
        # ── 标记订单为已成交 ────────────────────────────────
        order['status'] = 'filled'
        order['filled'] = fill_amount
        order['average'] = fill_price
        
        # ── 记录交易历史 ────────────────────────────────────
        trade_record = {
            'id': order['id'],
            'status': 'filled',           # ✅ GridExecutor check_fills 需要
            'filled': fill_amount,         # ✅ GridExecutor check_fills 需要
            'average': fill_price,         # ✅ GridExecutor check_fills 需要
            'time': datetime.now().isoformat(),
            'datetime': datetime.now().isoformat(),
            'side': side,
            'price': fill_price,
            'amount': fill_amount,
            'cost': fill_price * fill_amount,
            'fee': fee,
            'realized_pnl': realized_pnl,
            'position_after': self._position['amount'],
            'note': order.get('note', ''),
        }
        self._trade_history.append(trade_record)
        self._emit_event(
            "ORDER_FILLED",
            order_id=str(order.get("id")),
            side=str(side),
            order_type=str(order.get("type", "")),
            filled=float(fill_amount or 0),
            average=float(fill_price or 0),
            fee=float(fee or 0),
            realized_pnl=float(realized_pnl or 0),
            taker=bool(is_taker),
            position_after=float(self._position.get("amount", 0) or 0),
        )
        
        logger.info(
            f"[PaperTrader] ✅ {side.upper()} 成交 | "
            f"价格: ${fill_price:.2f} x {fill_amount:.6f} BTC | "
            f"手续费: ${fee:.4f} | "
            f"盈亏: ${realized_pnl:+.2f} | "
            f"持仓: {self._position['amount']:.6f} BTC | "
            f"余额: ${self._wallet_balance:.2f}"
        )
    
    # ══════════════════════════════════════════════════════════════
    # 内部计算
    # ══════════════════════════════════════════════════════════════
    
    def _calc_unrealized_pnl(self):
        """计算浮动盈亏"""
        if self._position['amount'] == 0 or self._current_price <= 0:
            return 0.0
        
        amount = self._position['amount']
        entry = self._position['entry_price']
        
        if amount > 0:
            return round((self._current_price - entry) * amount, 2)
        else:
            return round((entry - self._current_price) * abs(amount), 2)
    
    def _recalc_frozen_margin(self):
        """重新计算冻结保证金"""
        # 持仓保证金
        pos_margin = 0.0
        if self._position['amount'] != 0 and self._position['entry_price'] > 0:
            pos_margin = (abs(self._position['amount']) * self._position['entry_price']) / self.leverage

        # 挂单保证金
        order_margin = 0.0
        for order in self._open_orders.values():
            if order['status'] == 'open':
                if bool(order.get("reduce_only", False)):
                    continue
                order_margin += (order['price'] * order['amount']) / self.leverage

        self._frozen_margin = round(pos_margin + order_margin, 2)

    def _normalize_reduce_only_amount(self, side, amount, reduce_only):
        """校验并收敛 reduceOnly 数量，避免 paper 模式误开反向仓。"""
        try:
            amount = abs(float(amount or 0.0))
        except Exception:
            return None
        if amount < 0.001:
            return None
        if not reduce_only:
            return amount

        pos = self.get_position()
        if not pos:
            return None
        pos_amt = float(pos.get("amount", 0.0) or 0.0)
        if pos_amt > 0 and side != "sell":
            return None
        if pos_amt < 0 and side != "buy":
            return None
        available = abs(pos_amt)
        for order in self._open_orders.values():
            if order.get("status") != "open" or not bool(order.get("reduce_only", False)):
                continue
            if str(order.get("side", "")).lower() != str(side).lower():
                continue
            try:
                available -= abs(float(order.get("amount", 0.0) or 0.0))
            except Exception:
                continue
        available = max(0.0, available)
        if available < 0.001:
            return None
        if amount > available:
            amount = available
        step = float(getattr(config, "ORDER_STEP_SIZE", 0.001) or 0.001)
        if step > 0:
            amount = math.floor(amount / step) * step
        return amount if amount >= 0.001 else None
    
    def _release_margin(self, order):
        """释放挂单冻结的保证金"""
        margin = order.get('margin_frozen', 0)
        self._frozen_margin = max(0, self._frozen_margin - margin)
    
    def _record_income(self, income_type, income, info=''):
        """记录收益流水"""
        self._income_history.append({
            'symbol': 'BTCUSDT',
            'income_type': income_type,
            'income': round(income, 4),
            'asset': 'USDT',
            'time': int(time.time() * 1000),
            'info': info,
            'trade_id': str(len(self._income_history)),
        })

    def _emit_event(self, event_type, **payload):
        """记录轻量事件（订单生命周期 + 账户关键变化）用于回测对账。"""
        try:
            self._event_seq += 1
            event = {
                "seq": self._event_seq,
                "type": str(event_type),
                "ts_ms": int(time.time() * 1000),
                "datetime": datetime.now().isoformat(),
                "wallet_balance": round(float(self._wallet_balance), 6),
                "frozen_margin": round(float(self._frozen_margin), 6),
                "position_amount": round(float(self._position.get("amount", 0.0) or 0.0), 8),
            }
            event.update(payload or {})
            self._event_log.append(event)
            if len(self._event_log) > self._event_log_limit:
                keep = max(1000, int(self._event_log_limit * 0.7))
                self._event_log = self._event_log[-keep:]
        except Exception:
            # 事件账本是回测增强能力，不影响交易主流程
            pass
    
    # ══════════════════════════════════════════════════════════════
    # 状态持久化
    # ══════════════════════════════════════════════════════════════
    
    def _save_state(self):
        """保存 Paper 交易状态到文件"""
        try:
            state = {
                'wallet_balance': self._wallet_balance,
                'frozen_margin': self._frozen_margin,
                'position': self._position,
                'leverage': self.leverage,
                'order_id_counter': self._order_id_counter,
                'open_orders': self._open_orders,
                'trade_history': self._trade_history[-500:],  # 保留最近500条
                'income_history': self._income_history[-1000:],
                'realized_pnl_total': self._realized_pnl_total,
                'commission_total': self._commission_total,
                'current_price': self._current_price,
                'saved_at': datetime.now().isoformat(),
            }
            with open(self._state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"[PaperTrader] 保存状态失败: {e}")
    
    def _load_state(self, clear_history_on_restart=False):
        """从文件恢复 Paper 交易状态

        Args:
            clear_history_on_restart: True = 模拟盘，每次重启完全重置为初始资金
                                     False = 保持上次状态
        """
        if clear_history_on_restart:
            # ═══ 模拟盘：完全重置为初始状态 ═══
            self._wallet_balance = self.initial_capital
            self._frozen_margin = 0.0
            self._position = {'amount': 0.0, 'entry_price': 0.0, 'unrealized_pnl': 0.0}
            self._open_orders = {}
            self._trade_history = []
            self._income_history = []
            self._realized_pnl_total = 0.0
            self._commission_total = 0.0
            self._current_price = 0.0
            self._event_seq = 0
            self._event_log = []
            try:
                if self._state_file and os.path.exists(self._state_file):
                    os.remove(self._state_file)
            except Exception as e:
                logger.debug(f"[PaperTrader] 清理状态文件失败: {e}")
            logger.info(
                f"[PaperTrader] 模拟盘重启 → 完全重置 | "
                f"初始资金: ${self.initial_capital}"
            )
            return

        # ═══ 非模拟盘：尝试从文件恢复 ═══
        try:
            with open(self._state_file, 'r') as f:
                state = json.load(f)

            self._wallet_balance = state.get('wallet_balance', self.initial_capital)
            self._frozen_margin = state.get('frozen_margin', 0)
            self._position = state.get('position', self._position)
            self.leverage = state.get('leverage', 1)
            self._order_id_counter = state.get('order_id_counter', 1000)
            self._open_orders = {}  # 不恢复旧挂单

            self._trade_history = state.get('trade_history', [])
            self._income_history = state.get('income_history', [])
            self._realized_pnl_total = state.get('realized_pnl_total', 0)
            self._commission_total = state.get('commission_total', 0)
            self._current_price = state.get('current_price', 0)

            logger.info(
                f"[PaperTrader] 从文件恢复状态 | "
                f"余额: ${self._wallet_balance:.2f} | "
                f"持仓: {self._position['amount']:.6f} BTC | "
                f"累计盈亏: ${self._realized_pnl_total:.2f}"
            )
        except FileNotFoundError:
            logger.info("[PaperTrader] 无历史状态，全新开始")
        except Exception as e:
            logger.warning(f"[PaperTrader] 恢复状态失败: {e}，全新开始")
    
    def get_filled_orders(self):
        """获取已成交订单"""
        return [t for t in self._trade_history]
    
    def get_account_summary(self):
        """获取账户完整摘要"""
        unrealized = self._calc_unrealized_pnl()
        return {
            'initial_capital': self.initial_capital,
            'wallet_balance': round(self._wallet_balance, 2),
            'unrealized_pnl': round(unrealized, 2),
            'total_equity': round(self._wallet_balance + unrealized, 2),
            'frozen_margin': round(self._frozen_margin, 2),
            'available': round(self._wallet_balance - self._frozen_margin, 2),
            'leverage': self.leverage,
            'realized_pnl_total': round(self._realized_pnl_total, 2),
            'commission_total': round(self._commission_total, 2),
            'net_pnl': round(self._realized_pnl_total - self._commission_total + unrealized, 2),
            'roi_pct': round(
                (self._wallet_balance + unrealized - self.initial_capital) 
                / self.initial_capital * 100, 2
            ),
            'total_trades': len(self._trade_history),
            'open_orders': len(self._open_orders),
            'position': self._position.copy(),
        }

