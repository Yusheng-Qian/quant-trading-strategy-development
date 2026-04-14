# main.py - 交易机器人 9.0 主程序
"""
9.0 核心改造:
  所有模块只产"事实" → FeatureCollector 统一采集
  DecisionEngine 唯一决策中心 → 早停优先级链 + 三因子模型
  main.py 只做: collect → decide → execute

  从 8.0 的 1371 行 if-else → ~450 行清晰流程
"""
import argparse
import time
import os
import sys
import signal
import json
import math
from datetime import datetime
from threading import Event, Lock, Thread, current_thread, main_thread

import config
from utils.logger import get_logger, set_trading_mode

set_trading_mode(config.TRADING_MODE)
from data.market_data import (
    get_advanced_indicators,
    get_coinglass_bundle,
    fetch_binance_sentiment_bundle,
    get_aux_trend_indicators,
)
from core.strategy import GridStrategy
from engine.decision_engine import DecisionEngine, Action, MarketRegime, Decision
from engine.meta_decision import MetaDecisionLayer
from engine.feature_collector import FeatureCollector
from core.binance_trader import BinanceTrader
from core.grid_executor import GridExecutor
from risk.risk_manager import RiskManager
from engine.circuit_breaker import CircuitBreaker
from utils.notifier import init_notifier, send_telegram
from risk.position_bias_engine import PositionBiasEngine
from risk.market_sentry import MarketSentry

logger = get_logger()

# ════════════════════════════════════════════════════════════════
# PID 文件锁
# ════════════════════════════════════════════════════════════════

PID_FILE = getattr(config, "TRADING_BOT_PID_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "trading_bot.pid"))
_dashboard_server = None
_dashboard_server_lock = Lock()


def set_shared_state(**kwargs):
    """测试版不启动 Web 面板，保留空实现兼容原主流程辅助函数。"""
    return None

def _run_dashboard_server(stop_event=None):
    """测试版禁用 Dashboard，保留兼容空函数。"""
    logger.info("[Dashboard] Test 版已剥离")
    return


def _stop_dashboard_server():
    with _dashboard_server_lock:
        server = _dashboard_server
    if server is not None:
        try:
            server.shutdown()
        except Exception:
            pass


def acquire_pid_lock(force=False):
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            if force:
                logger.warning(f"[PID锁] --force 模式，终止旧进程 PID={old_pid}")
                os.kill(old_pid, signal.SIGTERM)
                time.sleep(2)
                try:
                    os.kill(old_pid, 0)
                    os.kill(old_pid, signal.SIGKILL)
                    time.sleep(1)
                except OSError:
                    pass
            else:
                logger.warning(f"[PID锁] 旧进程 PID={old_pid} 仍在运行，当前进程退出")
                sys.exit(0)
        except (OSError, ValueError):
            pass
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    logger.info(f"[PID锁] PID={os.getpid()} 已注册")


def release_pid_lock():
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, 'r') as f:
                pid = int(f.read().strip())
            if pid == os.getpid():
                os.remove(PID_FILE)
    except Exception:
        pass


def _is_reduce_only_order(order):
    if not isinstance(order, dict):
        return False
    if bool(order.get("reduce_only")) or bool(order.get("reduceOnly")):
        return True
    info = order.get("info") if isinstance(order.get("info"), dict) else {}
    val = info.get("reduceOnly")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return False


def _cancel_entry_orders_via_trader(trader):
    try:
        orders = trader.get_open_orders() or []
    except Exception:
        return 0
    cancelled = 0
    for order in orders:
        if _is_reduce_only_order(order):
            continue
        oid = order.get("id") or order.get("order_id")
        if oid and hasattr(trader, "cancel_order"):
            try:
                if trader.cancel_order(oid):
                    cancelled += 1
            except Exception:
                continue
    return cancelled


def _is_risk_wait(decision: Decision) -> bool:
    """仅对高风险 WAIT 触发额外防护（撤入场单）。"""
    if not decision or decision.action != Action.WAIT:
        return False
    params = decision.params if isinstance(getattr(decision, "params", None), dict) else {}
    reason = str(getattr(decision, "reason", "") or "")
    if bool(params.get("_drawdown_override", False)):
        return True
    risk_tags = ("d2d=", "daily_loss=", "sentry_emergency_active_violent")
    return (decision.regime == MarketRegime.EXTREME) or any(tag in reason for tag in risk_tags)


def _sync_position_framework_state(strategy: GridStrategy, cooldown_state: dict) -> dict:
    """
    维护日内仓位框架状态：
    - 连续盈亏笔数
    - 当日净利润
    - 已消费的 trades_today 游标
    """
    state = cooldown_state.setdefault("position_framework", {})
    today = datetime.now().date().isoformat()
    if state.get("date") != today:
        state.clear()
        state.update({
            "date": today,
            "trade_cursor": 0,
            "loss_streak": 0,
            "win_streak": 0,
            "day_net": 0.0,
            "last_trade_profit": 0.0,
            "last_trade_time": "",
            "last_log_sig": "",
            "last_log_ts": 0.0,
        })

    if strategy is None:
        return state

    trades_today = list(getattr(strategy, "trades_today", []) or [])
    cursor = int(state.get("trade_cursor", 0) or 0)
    if cursor < 0 or cursor > len(trades_today):
        cursor = 0

    for trade in trades_today[cursor:]:
        profit = float(trade.get("profit", 0.0) or 0.0)
        state["last_trade_profit"] = profit
        state["last_trade_time"] = str(trade.get("time", "") or "")
        if profit > 1e-9:
            state["win_streak"] = int(state.get("win_streak", 0) or 0) + 1
            state["loss_streak"] = 0
        elif profit < -1e-9:
            state["loss_streak"] = int(state.get("loss_streak", 0) or 0) + 1
            state["win_streak"] = 0
        else:
            state["win_streak"] = 0
            state["loss_streak"] = 0

    state["trade_cursor"] = len(trades_today)
    state["day_net"] = float(getattr(strategy, "daily_pnl", 0.0) or 0.0)
    return state


def _resolve_position_framework(features: dict, cooldown_state: dict) -> dict:
    """
    仓位乘数框架：
    - 低/中/高/极端波动分层
    - 连续亏损后自动降速
    - 日内达到收益阈值后软锁盈 / 硬锁盈

    说明：当前实时数据管线以 30m + ATR 为主，因此使用
    max(atr_ratio, sentry_change_pct) 作为短线波动代理。
    """
    state = cooldown_state.setdefault("position_framework", {})
    result = {
        "enabled": bool(getattr(config, "POSITION_FRAMEWORK_ENABLED", False)),
        "multiplier": 1.0,
        "allow_new_entries": True,
        "reduce_only": False,
        "bucket": "off",
        "vol_proxy": 0.0,
        "atr_ratio": float(features.get("atr_ratio", 0.0) or 0.0),
        "sentry_change_pct": abs(float(features.get("sentry_change_pct", 0.0) or 0.0)),
        "loss_streak": int(state.get("loss_streak", 0) or 0),
        "win_streak": int(state.get("win_streak", 0) or 0),
        "day_net": float(state.get("day_net", features.get("daily_pnl", 0.0)) or 0.0),
        "day_pct": 0.0,
        "reason": "disabled",
    }
    if not result["enabled"]:
        return result

    low_max = float(getattr(config, "POSITION_FRAMEWORK_VOL_LOW_MAX", 0.0025))
    sweet_max = float(getattr(config, "POSITION_FRAMEWORK_VOL_SWEET_MAX", 0.0080))
    high_max = float(getattr(config, "POSITION_FRAMEWORK_VOL_HIGH_MAX", 0.0120))

    vol_proxy = max(result["atr_ratio"], result["sentry_change_pct"])
    if bool(features.get("sentry_current_violent", False)):
        vol_proxy = max(vol_proxy, float(getattr(config, "SENTRY_VIOLENT_PCT", 0.008) or 0.008))
    result["vol_proxy"] = vol_proxy

    if vol_proxy < low_max:
        bucket = "low"
        mult = float(getattr(config, "POSITION_FRAMEWORK_MULT_LOW", 0.90))
    elif vol_proxy < sweet_max:
        bucket = "sweet"
        mult = float(getattr(config, "POSITION_FRAMEWORK_MULT_SWEET", 1.35))
    elif vol_proxy < high_max:
        bucket = "high"
        mult = float(getattr(config, "POSITION_FRAMEWORK_MULT_HIGH", 0.75))
    else:
        bucket = "extreme"
        mult = float(getattr(config, "POSITION_FRAMEWORK_MULT_EXTREME", 0.25))

    result["bucket"] = bucket
    reasons = [f"bucket={bucket}", f"vol_proxy={vol_proxy:.3%}"]

    loss_streak_threshold = int(getattr(config, "POSITION_FRAMEWORK_LOSS_STREAK_THRESHOLD", 2))
    if result["loss_streak"] >= loss_streak_threshold:
        loss_cap = float(getattr(config, "POSITION_FRAMEWORK_LOSS_STREAK_MAX_MULT", 0.70))
        mult = min(mult, loss_cap)
        reasons.append(f"loss_streak={result['loss_streak']}")

    total_equity = float(features.get("total_equity", 0.0) or 0.0)
    if total_equity > 0:
        result["day_pct"] = result["day_net"] / total_equity

    hard_lock_pct = float(getattr(config, "POSITION_FRAMEWORK_DAILY_HARD_LOCK_PCT", 0.03))
    soft_lock_pct = float(getattr(config, "POSITION_FRAMEWORK_DAILY_SOFT_LOCK_PCT", 0.02))
    if result["day_pct"] >= hard_lock_pct:
        result["multiplier"] = 0.0
        result["allow_new_entries"] = False
        result["reduce_only"] = True
        reasons.append(f"daily_lock={result['day_pct']:.2%}")
    else:
        if result["day_pct"] >= soft_lock_pct:
            soft_lock_mult = float(getattr(config, "POSITION_FRAMEWORK_DAILY_SOFT_LOCK_MULT", 0.60))
            mult = min(mult, soft_lock_mult)
            reasons.append(f"daily_soft_lock={result['day_pct']:.2%}")
        result["multiplier"] = max(0.0, mult)

    if result["multiplier"] <= 0:
        result["allow_new_entries"] = False
    result["reason"] = " | ".join(reasons)
    return result


def _log_position_framework(framework: dict, cooldown_state: dict):
    """低频记录仓位框架状态，避免每轮刷屏。"""
    if not framework:
        return
    state = cooldown_state.setdefault("position_framework", {})
    now_ts = time.time()
    cooldown_sec = int(getattr(config, "POSITION_FRAMEWORK_LOG_COOLDOWN_SEC", 180))
    sig = (
        f"{framework.get('bucket')}|{framework.get('multiplier', 1.0):.2f}|"
        f"{int(bool(framework.get('reduce_only', False)))}|"
        f"{framework.get('loss_streak', 0)}|{framework.get('day_pct', 0.0):.4f}"
    )
    if sig == state.get("last_log_sig") and (now_ts - float(state.get("last_log_ts", 0.0) or 0.0)) < cooldown_sec:
        return
    logger.info(
        f"[仓位框架] bucket={framework.get('bucket')} mult={framework.get('multiplier', 1.0):.2f} "
        f"allow_new={framework.get('allow_new_entries', True)} reduce_only={framework.get('reduce_only', False)} "
        f"loss_streak={framework.get('loss_streak', 0)} day={framework.get('day_pct', 0.0):.2%} "
        f"atr={framework.get('atr_ratio', 0.0):.3%} sentry={framework.get('sentry_change_pct', 0.0):.3%}"
    )
    state["last_log_sig"] = sig
    state["last_log_ts"] = now_ts


# ════════════════════════════════════════════════════════════════
# 执行器：将 Decision 转化为交易动作
# ════════════════════════════════════════════════════════════════

def execute_decision(decision: Decision, executor: GridExecutor, strategy: GridStrategy,
                     trader, features: dict, cooldown_state: dict, evt: "EventLogger | None" = None):
    """
    执行层：只读 Decision 对象，禁止再读 features 中的指标做判断。
    features 仅用于传递技术参数给 strategy.compute_grid_dynamic。
    """
    action = decision.action
    params = decision.params if isinstance(decision.params, dict) else {}
    position_framework = features.get("_position_framework", {}) if isinstance(features.get("_position_framework"), dict) else {}
    framework_mult = max(0.0, float(position_framework.get("multiplier", 1.0) or 0.0))

    # 同步模式化“连续同向成交阈值”到执行器，避免模式参数与执行器阈值不一致。
    try:
        _mode_max_consec = int(params.get("mode_max_consecutive_same_side", 0) or 0)
    except Exception:
        _mode_max_consec = 0
    if _mode_max_consec > 0:
        setattr(executor, "_max_consecutive_same_side", _mode_max_consec)

    # ── HALT: 全停 ──
    if action == Action.HALT:
        logger.warning(f"[执行] HALT | {decision.reason}")
        executor.cancel_all()

        if "circuit_breaker=emergency_close" in decision.reason:
            _force_close_position(trader, features.get("price", 0))
            cooldown_state["until"] = time.time() + int(getattr(config, "EMERGENCY_COOLDOWN_SEC", 1800))
            send_telegram(f"🔴 *断路器熔断，已强制平仓*\n\n原因: {decision.reason}")
        elif "circuit_breaker=pause" in decision.reason:
            # PAUSE 仅暂停入场，不做强平；保留仓位与退出单，避免小亏噪音触发连锁割肉
            _manage_exits(executor, trader, features)
            cooldown_state["until"] = time.time() + int(getattr(config, "EMERGENCY_COOLDOWN_SEC", 1800))
            send_telegram(f"🟡 *断路器暂停（未强平）*\n\n原因: {decision.reason}")
        elif "circuit_breaker" in decision.reason:
            _force_close_position(trader, features.get("price", 0))
            cooldown_state["until"] = time.time() + int(getattr(config, "EMERGENCY_COOLDOWN_SEC", 1800))
            send_telegram(f"🔴 *断路器触发，已强制平仓*\n\n原因: {decision.reason}")
        elif "emergency_stop" in decision.reason:
            _force_close_position(trader, features.get("price", 0))
            cooldown_state["until"] = time.time() + int(getattr(config, "EMERGENCY_COOLDOWN_SEC", 1800))
            send_telegram("🛑 *紧急止损触发*")
        elif "d2d" in decision.reason:
            send_telegram(f"🟠 *接近强平边界，已暂停*\n\n{decision.reason}")
        elif "flash_crash" in decision.reason:
            send_telegram("⚠️ *插针检测触发*")
        return

    # ── REDUCE: 主动减仓 ──
    if action == Action.REDUCE:
        logger.warning(f"[执行] REDUCE | {decision.reason}")
        # 只撤入场单，保留 TP/SL 退出单
        cancelled = int(executor.cancel_entry_orders() or 0)
        logger.info(f"[执行] REDUCE 撤入场单 {cancelled} 个，保留 TP/SL")
        target_margin = params.get("reduce_target_margin", 0.40)
        reduced = _reduce_position(trader, features, target_margin)
        if reduced and hasattr(executor, "refresh_after_external_position_change"):
            executor.refresh_after_external_position_change(features.get("price", 0), reason="decision_reduce")
        # 减仓后补挂 TP（持仓量变化，旧 TP 可能量不对）
        _manage_exits(executor, trader, features)
        _mode_cd = int(params.get("mode_cooldown_sec", 0) or 0) or int(getattr(config, "POSITION_COOLDOWN_SEC", 900))
        cooldown_state["until"] = time.time() + _mode_cd
        return

    # ── Spike Guard: 针刺防守兜底（执行层） ──
    # 即使决策层给出 WAIT/GRID，也在剧烈波动时先保护仓位与挂单。
    if bool(getattr(config, "SPIKE_GUARD_ENABLED", True)):
        sentry_violent = bool(features.get("sentry_current_violent", False))
        spike_change = abs(float(features.get("sentry_change_pct", 0.0) or 0.0))
        flash_crash = bool(features.get("flash_crash_detected", False))
        spike_trigger = float(getattr(config, "SPIKE_GUARD_TRIGGER_PCT", 0.008))
        reduce_trigger = float(getattr(config, "SPIKE_GUARD_REDUCE_PCT", 0.012))
        emergency_trigger = float(getattr(config, "SPIKE_GUARD_EMERGENCY_PCT", 0.018))
        if flash_crash or sentry_violent or spike_change >= spike_trigger:
            # 先管理退出，再撤入场（保留 TP/SL）
            _manage_exits(executor, trader, features)
            cancelled = 0
            if hasattr(executor, "cancel_entry_orders"):
                cancelled = int(executor.cancel_entry_orders() or 0)
            else:
                cancelled = int(_cancel_entry_orders_via_trader(trader) or 0)

            now_ts = time.time()
            notice_cd = int(getattr(config, "SPIKE_GUARD_NOTICE_COOLDOWN_SEC", 300))
            last_notice = float(cooldown_state.get("spike_notice_ts", 0.0) or 0.0)
            should_notice = (now_ts - last_notice) >= notice_cd

            position_amount = float(features.get("position_amount", 0.0) or 0.0)
            if flash_crash or spike_change >= emergency_trigger:
                logger.warning(
                    f"[执行] SpikeGuard 强平 | change={spike_change:.3%} flash={flash_crash} cancelled={cancelled}"
                )
                _force_close_position(trader, features.get("price", 0))
                cooldown_state["until"] = now_ts + int(getattr(config, "SPIKE_GUARD_EMERGENCY_COOLDOWN_SEC", 1800))
                if should_notice:
                    send_telegram(
                        f"🛑 *SpikeGuard 强平*\n\n"
                        f"change={spike_change:.2%} | 触发阈值={emergency_trigger:.2%}\n"
                        f"已撤入场单: {cancelled}"
                    )
                    cooldown_state["spike_notice_ts"] = now_ts
                return

            if abs(position_amount) > 0 and spike_change >= reduce_trigger:
                target_margin = float(getattr(config, "SPIKE_GUARD_REDUCE_TARGET_MARGIN", 0.35))
                logger.warning(
                    f"[执行] SpikeGuard 减仓 | change={spike_change:.3%} target_margin={target_margin:.2f} cancelled={cancelled}"
                )
                reduced = _reduce_position(trader, features, target_margin)
                if reduced and hasattr(executor, "refresh_after_external_position_change"):
                    executor.refresh_after_external_position_change(features.get("price", 0), reason="spike_guard_reduce")
                cooldown_state["until"] = now_ts + int(getattr(config, "SPIKE_GUARD_PAUSE_SEC", 900))
                if should_notice:
                    send_telegram(
                        f"⚠️ *SpikeGuard 减仓*\n\n"
                        f"change={spike_change:.2%} | 触发阈值={reduce_trigger:.2%}\n"
                        f"目标保证金占比: {target_margin:.2f}"
                    )
                    cooldown_state["spike_notice_ts"] = now_ts
                return

            # 轻度针刺：暂停新入场，观察冷却
            pause_sec = int(getattr(config, "SPIKE_GUARD_PAUSE_SEC", 900))
            cooldown_state["until"] = max(float(cooldown_state.get("until", 0) or 0), now_ts + pause_sec)
            logger.warning(
                f"[执行] SpikeGuard 暂停入场 | change={spike_change:.3%} cancelled={cancelled} pause={pause_sec}s"
            )
            if should_notice:
                send_telegram(
                    f"⚠️ *SpikeGuard 暂停入场*\n\n"
                    f"change={spike_change:.2%} | 触发阈值={spike_trigger:.2%}\n"
                    f"暂停 {pause_sec}s，已撤入场单: {cancelled}"
                )
                cooldown_state["spike_notice_ts"] = now_ts
            return

    # ── WAIT: 等待 ──
    if action == Action.WAIT:
        logger.info(f"[执行] WAIT | {decision.reason}")
        if _is_risk_wait(decision):
            cancelled = int(executor.cancel_entry_orders() or 0) if hasattr(executor, "cancel_entry_orders") else 0
            if cancelled <= 0:
                cancelled = int(_cancel_entry_orders_via_trader(trader) or 0)
            if cancelled > 0:
                logger.warning(f"[执行] 风险WAIT已撤入场单 {cancelled} 个，保留 TP/SL")
        _manage_exits(executor, trader, features)
        return

    # ── GRID / TREND_LONG / TREND_SHORT: 部署网格 ──
    if action in (Action.GRID, Action.TREND_LONG, Action.TREND_SHORT):
        if bool(position_framework.get("reduce_only", False)) or not bool(position_framework.get("allow_new_entries", True)):
            logger.info(
                f"[执行] 仓位框架锁新单 | action={action.value} | {position_framework.get('reason', '')}"
            )
            cancelled = int(executor.cancel_entry_orders() or 0) if hasattr(executor, "cancel_entry_orders") else 0
            if cancelled <= 0:
                cancelled = int(_cancel_entry_orders_via_trader(trader) or 0)
            if cancelled > 0:
                logger.info(f"[执行] 仓位框架已撤入场单 {cancelled} 个，保留 TP/SL")
            _manage_exits(executor, trader, features)
            return

        can_trade = True
        block_reason = ""
        if strategy is not None and hasattr(strategy, "can_trade"):
            try:
                can_trade, block_reason = strategy.can_trade()
            except Exception as e:
                can_trade, block_reason = True, f"can_trade_check_failed:{e}"
        if not can_trade:
            logger.warning(f"[执行] 交易门禁阻止新单 | action={action.value} | {block_reason}")
            cancelled = int(executor.cancel_entry_orders() or 0) if hasattr(executor, "cancel_entry_orders") else 0
            if cancelled <= 0:
                cancelled = int(_cancel_entry_orders_via_trader(trader) or 0)
            if cancelled > 0:
                logger.info(f"[执行] 门禁已撤入场单 {cancelled} 个，保留 TP/SL")
            _manage_exits(executor, trader, features)
            return

        had_fill_this_round = _manage_exits(executor, trader, features)

        price = features.get("price", 0)
        atr = features.get("atr", 0)
        atr_smoothed = features.get("atr_smoothed", atr)
        available_capital = features.get("free_capital", 0)
        total_equity = float(features.get("total_equity", 0.0) or 0.0)
        if framework_mult != 1.0:
            planning_capital = available_capital * framework_mult
            if total_equity > 0:
                planning_capital = min(total_equity, planning_capital)
            available_capital = max(0.0, planning_capital)
        lead_capital_scale = float(params.get("lead_capital_scale", 1.0) or 1.0)
        if lead_capital_scale > 0 and lead_capital_scale != 1.0:
            available_capital = max(0.0, available_capital * lead_capital_scale)

        side_allocation = {
            "long_ratio": params.get("long_ratio", 0.5),
            "short_ratio": params.get("short_ratio", 0.5),
            "score": features.get("bias_score", 0),
        }

        sentry_override = _build_sentry_override(features, params)
        dynamic_min_order_usdt = float(getattr(config, "SENTRY_MIN_ORDER_USDT", 130))
        _mp = config.get_mode_params()

        grid_bias = params.get("funding_bias")
        # 检测当前持仓方向
        _pos_for_bias = features.get("position_amount", 0)
        _existing_side_for_bias = None
        if _pos_for_bias and float(_pos_for_bias) != 0:
            _existing_side_for_bias = "long" if float(_pos_for_bias) > 0 else "short"

        if action == Action.TREND_LONG:
            grid_bias = "long_only"
        elif action == Action.TREND_SHORT:
            grid_bias = "short_only"
        elif action == Action.GRID and bool(getattr(config, "META_TREND_BIAS_ENABLED", True)):
            regime_probs = params.get("regime_probs") if isinstance(params, dict) else None
            if isinstance(regime_probs, dict):
                trend_up_prob = float(regime_probs.get("trend_up", 0.0) or 0.0)
                trend_down_prob = float(regime_probs.get("trend_down", 0.0) or 0.0)
                _mode_thr = float(params.get("_mode_trend_bias_thr", 0) or 0)
                trend_thr = _mode_thr if _mode_thr > 0 else float(getattr(config, "META_TREND_BIAS_PROB_THRESHOLD", 0.55))
                trend_gap = float(getattr(config, "META_TREND_BIAS_MIN_GAP", 0.05))
                if trend_down_prob >= trend_thr and (trend_down_prob - trend_up_prob) >= trend_gap:
                    if _existing_side_for_bias == "long":
                        # 持有多头但趋势偏空：按趋势强度压缩多头比例，保留退出管理能力
                        _minority = max(0.10, 0.30 - (trend_down_prob - 0.48) * 0.5)
                        side_allocation["long_ratio"] = round(_minority, 2)
                        side_allocation["short_ratio"] = round(1.0 - _minority, 2)
                        logger.warning(
                            f"[执行] Meta趋势偏置(有多头持仓): trend_down={trend_down_prob:.0%} "
                            f"→ 多头{side_allocation['long_ratio']:.0%}/空头{side_allocation['short_ratio']:.0%}"
                        )
                    else:
                        grid_bias = "short_only"
                        logger.warning(
                            f"[执行] Meta趋势偏置触发: trend_down={trend_down_prob:.0%} "
                            f"trend_up={trend_up_prob:.0%} → 强制 short_only"
                        )
                elif trend_up_prob >= trend_thr and (trend_up_prob - trend_down_prob) >= trend_gap:
                    if _existing_side_for_bias == "short":
                        # 持有空头但趋势偏多：按趋势强度压缩空头比例
                        _minority = max(0.10, 0.30 - (trend_up_prob - 0.48) * 0.5)
                        side_allocation["short_ratio"] = round(_minority, 2)
                        side_allocation["long_ratio"] = round(1.0 - _minority, 2)
                        logger.warning(
                            f"[执行] Meta趋势偏置(有空头持仓): trend_up={trend_up_prob:.0%} "
                            f"→ 多头{side_allocation['long_ratio']:.0%}/空头{side_allocation['short_ratio']:.0%}"
                        )
                    else:
                        grid_bias = "long_only"
                        logger.warning(
                            f"[执行] Meta趋势偏置触发: trend_up={trend_up_prob:.0%} "
                            f"trend_down={trend_down_prob:.0%} → 强制 long_only"
                        )

        # ── 模式切换：应用模式的杠杆 ──
        _mode_leverage = int(params.get("mode_leverage", 0) or 0)
        if _mode_leverage > 0 and _mode_leverage != int(getattr(config, "LEVERAGE", 3)):
            try:
                trader.set_leverage(_mode_leverage)
                config.LEVERAGE = _mode_leverage
                strategy.L = _mode_leverage  # 同步杠杆到 GridStrategy
                logger.info(f"[执行] 模式杠杆切换 → {_mode_leverage}x")
            except Exception as e:
                logger.warning(f"[执行] 杠杆切换失败: {e}")
        _active_mode = params.get("trading_mode", "normal")
        if _active_mode:
            features["_trading_mode"] = _active_mode

        # 应用三因子计算的参数（变化时记录事件）
        _new_params = {
            "grid_N": params.get("grid_N", config.GRID_NUM),
            "factor": params.get("factor", 1.0),
        }
        if evt:
            _old_params = {
                "grid_N": getattr(strategy, "target_N", None),
                "factor": getattr(strategy, "_last_factor", None),
            }
            for k, nv in _new_params.items():
                ov = _old_params.get(k)
                if ov is not None and ov != nv:
                    evt.param_change(field=k, old_val=ov, new_val=nv, source="decision")
        strategy._last_factor = _new_params["factor"]
        strategy.target_N = _new_params["grid_N"]
        strategy.Tp = params.get("tp_pct", config.TP_PER_GRID)
        strategy._tp_set_by_pipeline = True  # 标记：TP 已由决策管线设置
        strategy.stop_loss_pct = params.get("stop_loss_pct", config.STOP_LOSS_PCT)
        strategy.upper_pct = params.get("upper_pct", config.UPPER_PCT)
        strategy.lower_pct = params.get("lower_pct", config.LOWER_PCT)
        params["no_fill_mode"] = "inactive"
        params["no_fill_capital_score"] = None
        # 趋势模式下可选：先做一笔小仓主动首单（市价），再让网格接管后续成交
        _active_entry_done = False
        if str(params.get("lead_stage", "") or "") == "probe":
            _active_entry_done = _handle_lead_probe_entries(
                action=action,
                trader=trader,
                executor=executor,
                features=features,
                params=params,
                cooldown_state=cooldown_state,
                mode_params=_mp,
                position_multiplier=framework_mult,
                allow_entry=bool(position_framework.get("allow_new_entries", True)),
                framework_reason=str(position_framework.get("reason", "") or ""),
            )
        else:
            _cancel_lead_probe_orders(executor, trader)
            _active_entry_done = _try_mode_active_entry(
                action=action,
                trader=trader,
                executor=executor,
                strategy=strategy,
                features=features,
                params=params,
                cooldown_state=cooldown_state,
                mode_params=_mp,
                position_multiplier=framework_mult,
                allow_entry=bool(position_framework.get("allow_new_entries", True)),
                framework_reason=str(position_framework.get("reason", "") or ""),
            )
        if _active_entry_done:
            _pos_now = trader.get_position() or {}
            _amt_now = float(_pos_now.get("amount", 0.0) or 0.0)
            features["position_amount"] = _amt_now
            features["position_amount_abs"] = abs(_amt_now)
            features["position_side"] = "long" if _amt_now > 0 else ("short" if _amt_now < 0 else "none")
            _bal_now = trader.get_balance() or {}
            if _bal_now:
                available_capital = float(_bal_now.get("free", available_capital) or available_capital)
                features["free_capital"] = available_capital

        if bool(params.get("skip_full_grid_deploy", False)):
            logger.info(
                f"[执行] 三态探针模式，仅保留 probe 订单 | side={params.get('lead_side','-')} "
                f"stage={params.get('lead_stage','-')} mode={params.get('lead_order_mode','-')}"
            )
            _manage_exits(executor, trader, features)
            return

        # 成交驱动网格（flow-driven）：market_logic × flow_logic
        no_fill_sec = None
        no_fill_bucket = None
        fill_widen_triggered = False
        instant_widen_mult = 1.0
        flow_state = "inactive"
        flow_recent_fills = 0
        flow_level = 0
        if action == Action.GRID and hasattr(executor, "get_no_fill_seconds"):
            no_fill_sec = int(executor.get_no_fill_seconds())
            fill_window_sec = int(getattr(config, "FLOW_FILL_WINDOW_SEC", 1800))
            if hasattr(executor, "get_recent_fill_count"):
                flow_recent_fills = int(executor.get_recent_fill_count(fill_window_sec) or 0)

            tighten_after = int(_mp.get("flow_tighten_after_sec", getattr(config, "FLOW_TIGHTEN_AFTER_SEC", 900)))
            aggressive_after = int(_mp.get("flow_aggressive_after_sec", getattr(config, "FLOW_AGGRESSIVE_AFTER_SEC", 2700)))
            force_after = int(getattr(config, "FLOW_FORCE_REBUILD_AFTER_SEC", 5400))
            fills_to_normal = int(getattr(config, "FLOW_FILLS_TO_NORMAL", 2))
            fills_to_wide = int(getattr(config, "FLOW_FILLS_TO_WIDE", 4))
            prev_state = str(cooldown_state.get("flow_state", "normal") or "normal")

            # AI flow_bias 偏移：推动 tight/aggressive 阈值提前或延后
            ai_flow_bias = 0.0
            if bool(getattr(config, "FLOW_BIAS_ENABLED", True)):
                ai_adjust = params.get("ai_adjust")
                ai_weight_now = float(params.get("ai_weight", 0.0) or 0.0)
                min_ai_weight = float(getattr(config, "FLOW_BIAS_MIN_AI_WEIGHT", 0.08))
                adx_now = float(features.get("adx", 0.0) or 0.0)
                adx_low = float(getattr(config, "FLOW_BIAS_FUZZY_ADX_LOW", 20.0))
                adx_high = float(getattr(config, "FLOW_BIAS_FUZZY_ADX_HIGH", 30.0))
                bonus_w = float(getattr(config, "FLOW_BIAS_FUZZY_BONUS_WEIGHT", 0.05))
                if no_fill_sec is not None and no_fill_sec >= tighten_after and adx_low <= adx_now <= adx_high:
                    ai_weight_now = max(ai_weight_now, min_ai_weight + max(0.0, bonus_w))
                if ai_adjust and isinstance(ai_adjust, dict) and ai_weight_now >= min_ai_weight:
                    raw_bias = float(ai_adjust.get("flow_bias", 0.0) or 0.0)
                    hysteresis = float(getattr(config, "FLOW_BIAS_HYSTERESIS", 0.15))
                    filtered_bias = raw_bias if abs(raw_bias) >= hysteresis else 0.0

                    # EMA 惯性平滑：减少 flow_bias 抖动，稳定网格行为
                    prev_bias = float(cooldown_state.get("_prev_flow_bias", 0.0) or 0.0)
                    inertia = float(getattr(config, "FLOW_BIAS_INERTIA", 0.7))
                    ai_flow_bias = inertia * prev_bias + (1.0 - inertia) * filtered_bias
                    cooldown_state["_prev_flow_bias"] = ai_flow_bias

                    max_bias_sec = float(getattr(config, "FLOW_BIAS_MAX_SEC", 600))
                    bias_sec = ai_flow_bias * max_bias_sec

                    tight_lo, tight_hi = getattr(config, "FLOW_BIAS_TIGHTEN_CLAMP", (300, 3600))
                    aggr_lo, aggr_hi = getattr(config, "FLOW_BIAS_AGGRESSIVE_CLAMP", (600, 7200))
                    tighten_after = int(max(tight_lo, min(tight_hi, tighten_after - bias_sec)))
                    aggressive_after = int(max(aggr_lo, min(aggr_hi, aggressive_after - bias_sec)))
                else:
                    # AI 静默时清零偏移，避免“表面静默、实则还在改阈值”
                    cooldown_state["_prev_flow_bias"] = 0.0

            # 基础状态：先看 no-fill，再看 fill 反馈
            if no_fill_sec >= aggressive_after:
                flow_state = "aggressive"
                flow_level = 2
            elif no_fill_sec >= tighten_after:
                flow_state = "tight"
                flow_level = 1
            else:
                flow_state = "normal"
                flow_level = 0

            if flow_recent_fills >= fills_to_wide:
                flow_state = "wide"
                flow_level = -1
            elif flow_recent_fills >= fills_to_normal:
                if prev_state == "aggressive":
                    flow_state = "tight"
                    flow_level = 1
                elif prev_state == "tight":
                    flow_state = "normal"
                    flow_level = 0

            # 成交后立即放宽（不等待冷却）
            if bool(had_fill_this_round) and bool(getattr(config, "FILL_INSTANT_WIDEN_ENABLED", True)):
                flow_state = "wide"
                flow_level = -1
                fill_widen_triggered = True
                instant_widen_mult = max(1.0, float(getattr(config, "FILL_INSTANT_WIDEN_MULT", 1.08)))

            # 有仓位仍允许 flow 生效，但仓位过高时禁用 aggressive
            position_ratio = float(features.get("position_ratio", 0.0) or 0.0)
            aggressive_block = float(getattr(config, "FLOW_AGGRESSIVE_MAX_POSITION_RATIO", 0.50))
            if position_ratio > aggressive_block and flow_state == "aggressive":
                flow_state = "tight"
                flow_level = 1

            # flow 参数映射
            profile = {
                "wide": {
                    "n_mult": float(getattr(config, "FLOW_WIDE_N_MULT", 0.7)),
                    "spacing_mult": float(getattr(config, "FLOW_WIDE_SPACING_MULT", 1.5)),
                    "tp_mult": float(getattr(config, "FLOW_WIDE_TP_MULT", 1.3)),
                    "sl_mult": float(getattr(config, "FLOW_WIDE_SL_MULT", 0.95)),
                },
                "normal": {
                    "n_mult": float(getattr(config, "FLOW_NORMAL_N_MULT", 1.0)),
                    "spacing_mult": float(getattr(config, "FLOW_NORMAL_SPACING_MULT", 1.0)),
                    "tp_mult": float(getattr(config, "FLOW_NORMAL_TP_MULT", 1.0)),
                    "sl_mult": float(getattr(config, "FLOW_NORMAL_SL_MULT", 1.00)),
                },
                "tight": {
                    "n_mult": float(getattr(config, "FLOW_TIGHT_N_MULT", 1.3)),
                    "spacing_mult": float(getattr(config, "FLOW_TIGHT_SPACING_MULT", 0.7)),
                    "tp_mult": float(getattr(config, "FLOW_TIGHT_TP_MULT", 0.92)),
                    "sl_mult": float(getattr(config, "FLOW_TIGHT_SL_MULT", 1.05)),
                },
                "aggressive": {
                    "n_mult": float(getattr(config, "FLOW_AGGR_N_MULT", 1.2)),
                    "spacing_mult": float(getattr(config, "FLOW_AGGR_SPACING_MULT", 0.6)),
                    "tp_mult": float(getattr(config, "FLOW_AGGR_TP_MULT", 0.88)),
                    "sl_mult": float(getattr(config, "FLOW_AGGR_SL_MULT", 1.10)),
                },
            }
            p = profile.get(flow_state, profile["normal"])
            n_min = int(getattr(config, "FLOW_MIN_GRID_N", 3))
            n_max = int(getattr(config, "FLOW_MAX_GRID_N", 12))
            base_n = max(1, int(params.get("grid_N", config.GRID_NUM) or config.GRID_NUM))

            # 长无成交分档（15/45/90m）：逐步收窄 spacing、N 不降并小幅增加、tp 轻微下调
            st1 = int(getattr(config, "FLOW_STAGE1_AFTER_SEC", 900))
            st2 = int(getattr(config, "FLOW_STAGE2_AFTER_SEC", 2700))
            st3 = int(getattr(config, "FLOW_STAGE3_AFTER_SEC", 5400))
            if no_fill_sec >= st3:
                stage_spacing_mult = float(getattr(config, "FLOW_STAGE3_SPACING_MULT", 0.74))
                stage_n_add = int(getattr(config, "FLOW_STAGE3_N_ADD", 2))
                stage_tp_mult = float(getattr(config, "FLOW_STAGE3_TP_MULT", 0.88))
                stage_tag = "S3"
            elif no_fill_sec >= st2:
                stage_spacing_mult = float(getattr(config, "FLOW_STAGE2_SPACING_MULT", 0.82))
                stage_n_add = int(getattr(config, "FLOW_STAGE2_N_ADD", 1))
                stage_tp_mult = float(getattr(config, "FLOW_STAGE2_TP_MULT", 0.92))
                stage_tag = "S2"
            elif no_fill_sec >= st1:
                stage_spacing_mult = float(getattr(config, "FLOW_STAGE1_SPACING_MULT", 0.90))
                stage_n_add = int(getattr(config, "FLOW_STAGE1_N_ADD", 0))
                stage_tp_mult = float(getattr(config, "FLOW_STAGE1_TP_MULT", 0.96))
                stage_tag = "S1"
            else:
                stage_spacing_mult = 1.0
                stage_n_add = 0
                stage_tp_mult = 1.0
                stage_tag = "S0"

            raw_n = int(round(base_n * float(p["n_mult"])))
            if no_fill_sec >= st1:
                # 无成交阶段 N 不允许下调，优先保持密度
                raw_n = max(base_n, raw_n)
            raw_n += max(0, stage_n_add)
            new_n = min(n_max, max(n_min, raw_n))
            strategy.target_N = new_n
            # 风控优先：sentry N 上限钳制 Flow 输出
            # 今日盈利时：不做 sentry N 上限钳制，优先恢复成交密度
            _daily_pnl_now = float(features.get("daily_pnl", 0.0) or 0.0)
            _profit_mode = _daily_pnl_now > 3.0
            if sentry_override and isinstance(sentry_override, dict) and not _profit_mode:
                _sentry_cap = int(sentry_override.get("target_n", new_n) or new_n)
                if new_n > _sentry_cap:
                    logger.info(f"[优先级] Flow N={new_n} 被 sentry cap={_sentry_cap} 钳制")
                    strategy.target_N = _sentry_cap
            elif sentry_override and isinstance(sentry_override, dict) and _profit_mode and new_n > int(sentry_override.get("target_n", new_n) or new_n):
                logger.info(
                    f"[优先级] 今日盈利，放宽 sentry N 钳制 | daily_pnl={_daily_pnl_now:.2f} "
                    f"N={new_n} cap={int(sentry_override.get('target_n', new_n) or new_n)}"
                )

            old_upper = float(strategy.upper_pct or 0.0)
            old_lower = float(strategy.lower_pct or 0.0)
            max_range = float(getattr(config, "FLOW_MAX_RANGE_PCT", 0.05))
            if fill_widen_triggered:
                max_range = min(
                    max_range,
                    float(getattr(config, "FILL_INSTANT_WIDEN_MAX_RANGE_PCT", max_range))
                )
            min_range = float(getattr(config, "FLOW_MIN_RANGE_PCT", 0.012))
            spacing_mult = float(p["spacing_mult"]) * float(stage_spacing_mult) * instant_widen_mult
            strategy.upper_pct = min(max_range, max(min_range, old_upper * spacing_mult))
            strategy.lower_pct = min(max_range, max(min_range, old_lower * spacing_mult))

            # ATR 下限保护：spacing 不能低于市场噪音
            atr_min_mult = float(getattr(config, "FLOW_MIN_SPACING_ATR_MULT", 0.30))
            price_safe = max(1e-9, float(price or 0))
            atr_safe = max(0.0, float(atr_smoothed or atr or 0))
            min_step_pct = (atr_safe / price_safe) * atr_min_mult
            if strategy.target_N > 0 and min_step_pct > 0:
                cur_step_pct = (float(strategy.upper_pct) + float(strategy.lower_pct)) / max(1, int(strategy.target_N))
                if cur_step_pct < min_step_pct:
                    scale_up = min(3.0, (min_step_pct / max(1e-9, cur_step_pct)))
                    strategy.upper_pct = min(max_range, max(min_range, float(strategy.upper_pct) * scale_up))
                    strategy.lower_pct = min(max_range, max(min_range, float(strategy.lower_pct) * scale_up))

            # ── 成交行为最终控制权：连续同向成交 >= 阈值时，禁止收窄 spacing ──
            _consec_count = executor.get_consecutive_with_decay() if hasattr(executor, 'get_consecutive_with_decay') else getattr(executor, '_consecutive_count', 0)
            _consec_threshold = int(params.get("mode_max_consecutive_same_side", 0) or 0) or int(getattr(config, "CONSECUTIVE_FILL_SPACING_THRESHOLD", 2))
            if _consec_count >= _consec_threshold:
                # 决策层给的原始 spacing 作为底线
                _decision_upper = float(params.get("upper_pct", config.UPPER_PCT))
                _decision_lower = float(params.get("lower_pct", config.LOWER_PCT))
                # spacing 只能变宽，不能被 Flow 收紧
                if strategy.upper_pct < _decision_upper:
                    logger.info(
                        f"[优先级] 连续成交={_consec_count} | 阻止 spacing 收紧: "
                        f"upper {strategy.upper_pct*100:.3f}% → {_decision_upper*100:.3f}%"
                    )
                    strategy.upper_pct = _decision_upper
                if strategy.lower_pct < _decision_lower:
                    strategy.lower_pct = _decision_lower

            tp_mult = max(0.3, float(p["tp_mult"]) * float(stage_tp_mult))
            sl_mult = max(0.5, float(p.get("sl_mult", 1.0)))
            tp_min = float(getattr(config, "FLOW_TP_MIN", 0.0008))
            tp_max = float(getattr(config, "FLOW_TP_MAX", 0.02))
            sl_min = float(getattr(config, "FLOW_SL_MIN", 0.003))
            sl_max = float(getattr(config, "FLOW_SL_MAX", 0.08))
            strategy.Tp = min(tp_max, max(tp_min, float(strategy.Tp) * tp_mult))
            strategy._tp_set_by_pipeline = True  # Flow 调整后仍标记为管线 TP
            strategy.stop_loss_pct = min(sl_max, max(sl_min, float(strategy.stop_loss_pct) * sl_mult))

            # 资金评分 + 动态最小单笔
            leverage = max(1, int(getattr(config, "LEVERAGE", 1) or 1))
            min_order = float(getattr(config, "SENTRY_MIN_ORDER_USDT", 130))
            side_count = 2 if bool(getattr(config, "BIDIRECTIONAL_GRID", False)) else 1
            required_margin = (min_order * max(1, strategy.target_N) * side_count) / leverage
            free_capital_now = float(features.get("free_capital", 0) or 0)
            capital_score = (free_capital_now / required_margin) if required_margin > 0 else 0.0
            params["no_fill_mode"] = flow_state
            params["flow_state"] = flow_state
            params["flow_recent_fills"] = flow_recent_fills
            params["flow_bias"] = round(ai_flow_bias, 3)
            params["no_fill_capital_score"] = round(capital_score, 3)

            if bool(getattr(config, "DYNAMIC_MIN_ORDER_ENABLED", True)):
                relax_score = float(getattr(config, "DYNAMIC_MIN_ORDER_RELAX_SCORE", 2.0))
                relax_max = float(getattr(config, "DYNAMIC_MIN_ORDER_RELAX_MAX_RATIO", 0.20))
                abs_floor = float(getattr(config, "DYNAMIC_MIN_ORDER_ABS_FLOOR", 105))
                ex_floor = float(getattr(config, "MIN_ORDER_NOTIONAL_EXCHANGE_FLOOR", 100))
                if capital_score > relax_score:
                    relax_ratio = min(relax_max, (capital_score - relax_score) / max(1.0, relax_score) * relax_max)
                    dynamic_min_order_usdt = max(max(abs_floor, ex_floor), min_order * (1.0 - relax_ratio))
                else:
                    dynamic_min_order_usdt = min_order
            params["dynamic_min_order_usdt"] = round(dynamic_min_order_usdt, 2)

            no_fill_bucket = f"{flow_state}:{flow_level}"
            if evt and flow_state != prev_state:
                evt.flow_change(prev=prev_state, curr=flow_state,
                                no_fill_sec=no_fill_sec, recent_fills=flow_recent_fills,
                                flow_bias=ai_flow_bias)
            if flow_state != prev_state:
                cooldown_state["flow_state_ts"] = time.time()
            cooldown_state["flow_state"] = flow_state
            flow_state_ts = float(cooldown_state.get("flow_state_ts", time.time()) or time.time())
            params["flow_state_age_sec"] = int(max(0, time.time() - flow_state_ts))
            bias_tag = f" bias={ai_flow_bias:+.2f}" if ai_flow_bias != 0 else ""
            logger.info(
                f"[执行] Flow={flow_state}/{stage_tag}{bias_tag} no_fill={no_fill_sec}s fills{fill_window_sec//60}m={flow_recent_fills} "
                f"N={strategy.target_N} upper={strategy.upper_pct*100:.2f}% lower={strategy.lower_pct*100:.2f}% "
                f"tp={strategy.Tp*100:.2f}% sl={strategy.stop_loss_pct*100:.2f}%"
            )

            if no_fill_sec >= force_after:
                _allow_force_with_pos = bool(getattr(config, "FLOW_REBUILD_WITH_POSITION", True))
                _has_pos_now = abs(float(features.get("position_amount", 0) or 0)) > 0
                if _allow_force_with_pos or not _has_pos_now:
                    cooldown_state["force_rebuild"] = True
                    cooldown_state["flow_state"] = "aggressive"

            if isinstance(sentry_override, dict):
                if _profit_mode:
                    # 今日盈利模式：不把 N 再次压回 sentry cap
                    sentry_override["target_n"] = int(strategy.target_N or 1)
                else:
                    sentry_override["target_n"] = int(min(int(sentry_override.get("target_n", 999) or 999), int(strategy.target_N or 1)))
                sentry_override["n_range"] = (1, int(sentry_override["target_n"]))
                sentry_override["min_order_usdt"] = int(round(dynamic_min_order_usdt))

        # ── 参数溯源日志：显示各控制器最终输出 ──
        _sentry_n = sentry_override.get("target_n", "—") if isinstance(sentry_override, dict) else "—"
        logger.info(
            f"[溯源] 决策N={params.get('grid_N','?')} → Flow N={strategy.target_N} | "
            f"sentry_cap={_sentry_n} | "
            f"TP={strategy.Tp*100:.3f}% SL={strategy.stop_loss_pct*100:.2f}% | "
            f"lead={params.get('lead_side','-')}/{params.get('lead_stage','-')} "
            f"mode={params.get('lead_order_mode','-')} cap={float(params.get('lead_capital_scale',1.0) or 1.0):.2f} | "
            f"flow={flow_state} sentry={features.get('sentry_state','normal')}"
        )

        # 更新策略资金
        balance = trader.get_balance()
        if balance and balance.get("total", 0) > 0:
            strategy.total_capital = balance["total"]
            strategy.grid_capital = balance["total"] * config.GRID_CAPITAL_RATIO

        old_grid_info = strategy.last_grid_info
        # 传递连续成交信息给 strategy，用于 Core 层 spacing 扩张（带衰减）
        _consec_fill_info = {
            "count": executor.get_consecutive_with_decay() if hasattr(executor, 'get_consecutive_with_decay') else getattr(executor, '_consecutive_count', 0),
            "side": getattr(executor, '_consecutive_side', ""),
        }
        grid_info = strategy.compute_grid_dynamic(
            price, atr, smoothed_atr=atr_smoothed,
            available_capital=available_capital,
            capital_multiplier=framework_mult,
            grid_bias=grid_bias,
            side_allocation=side_allocation,
            sentry_override=sentry_override,
            consecutive_fill_info=_consec_fill_info,
        )

        if not grid_info:
            logger.warning("[执行] 网格资金不足")
            return

        rebuild, reason = strategy.should_rebuild_grid(grid_info, old_grid_info)

        if not executor.active_orders and executor.last_grid_info:
            rebuild, reason = True, "无活跃订单"
        if executor._grid_cycle_complete:
            rebuild, reason = True, "网格一轮完成"
        if cooldown_state.get("force_rebuild"):
            rebuild, reason = True, "冷却后重建"
            cooldown_state["force_rebuild"] = False
        # 无成交档位变化（mode/steps）时，直接触发一次重建，避免只”空算参数”不落地
        if action == Action.GRID and no_fill_bucket is not None:
            prev_bucket = cooldown_state.get("no_fill_step_bucket")
            if prev_bucket != no_fill_bucket:
                _allow_rebuild_with_pos = bool(getattr(config, "FLOW_REBUILD_WITH_POSITION", True))
                _has_pos_now = abs(float(features.get("position_amount", 0) or 0)) > 0
                if _allow_rebuild_with_pos or not _has_pos_now:
                    rebuild, reason = True, f"无成交档位重建({prev_bucket}->{no_fill_bucket})"
                else:
                    logger.info(f"[执行] 持仓中跳过档位重建 | {prev_bucket}->{no_fill_bucket}")
                cooldown_state["no_fill_step_bucket"] = no_fill_bucket
        if action == Action.GRID and fill_widen_triggered:
            rebuild, reason = True, "成交后立即放宽"

        # ── 重建保护：三条铁律 ──────────────────────────────
        # 铁律1: 成交优先级 > 参数正确性（危险白名单外一律受限）
        # 铁律2: 高成交概率订单拥有"生存权"（fill_score 保护）
        # 铁律3: 参数变化 ≠ 允许重建（存活时间保护）
        danger_reasons = {"冷却后重建", "无活跃订单", "网格一轮完成", "成交后立即放宽"}
        is_danger_bypass = reason in danger_reasons or str(reason).startswith("无成交超时重建(")
        _rb_blocked_by = ""
        _rb_elapsed = 0.0
        _rb_fill_score = 0.0
        _rb_price_drifted = False
        _rb_wanted = rebuild  # 铁律前是否想重建

        if rebuild and not is_danger_bypass and getattr(strategy, "_last_rebuild_time", 0) > 0:
            min_rebuild_sec = int(getattr(config, "MIN_REBUILD_INTERVAL_SEC", 600))
            _rb_elapsed = time.time() - strategy._last_rebuild_time

            # 铁律3: 最小存活时间保护
            if _rb_elapsed < min_rebuild_sec:
                logger.info(
                    f"[执行] 重建节流生效 | reason={reason} | {_rb_elapsed:.0f}s/{min_rebuild_sec}s"
                )
                rebuild = False
                _rb_blocked_by = "time_protect"

            # 铁律2: 成交窗口保护（fill_score = 距离/ATR，越小越可能成交）
            # 例外: 价格已漂移出网格范围 → 网格失效，不再保护
            if rebuild and executor.active_orders:
                current_price = float(features.get("price", 0) or 0)
                atr_ratio = float(features.get("atr_ratio", 0.005) or 0.005)
                if current_price > 0 and executor.last_grid_info:
                    grid_center = (float(executor.last_grid_info.get("P_high", 0)) +
                                   float(executor.last_grid_info.get("P_low", 0))) / 2.0
                    if grid_center > 0:
                        drift = abs(current_price - grid_center) / grid_center
                        drift_threshold = float(getattr(config, "GRID_DRIFT_OVERRIDE_MULT", 1.5)) * atr_ratio
                        if drift > drift_threshold:
                            _rb_price_drifted = True
                            logger.info(f"[执行] 价格漂移失效 | drift={drift:.3%} > {drift_threshold:.3%} | 允许重建")

                if not _rb_price_drifted and current_price > 0 and atr_ratio > 0:
                    min_fill_score = float("inf")
                    for order in executor.active_orders:
                        op = float(order.get("price", 0) or 0)
                        if op > 0:
                            dist = abs(current_price - op) / current_price
                            fill_score = dist / atr_ratio
                            if fill_score < min_fill_score:
                                min_fill_score = fill_score
                    _rb_fill_score = min_fill_score if min_fill_score < float("inf") else 0.0
                    base_fill_threshold = float(_mp.get("fill_score_block_threshold", getattr(config, "FILL_SCORE_BLOCK_THRESHOLD", 0.8)))
                    min_fill_threshold = float(_mp.get("fill_score_block_min_threshold", getattr(config, "FILL_SCORE_BLOCK_MIN_THRESHOLD", 0.3)))
                    relax_after_sec = int(_mp.get("fill_score_block_relax_after_sec", getattr(config, "FILL_SCORE_BLOCK_RELAX_AFTER_SEC", 1800)))
                    fill_score_threshold = base_fill_threshold
                    if relax_after_sec > 0:
                        no_fill_now = int(executor.get_no_fill_seconds() if hasattr(executor, "get_no_fill_seconds") else 0)
                        relax_ratio = min(1.0, max(0.0, float(no_fill_now) / float(relax_after_sec)))
                        # 无成交越久，越允许重建：阈值从 base 线性下降到 min。
                        fill_score_threshold = base_fill_threshold - (base_fill_threshold - min_fill_threshold) * relax_ratio
                    fill_score_threshold = max(min_fill_threshold, min(base_fill_threshold, fill_score_threshold))
                    if min_fill_score < fill_score_threshold:
                        no_fill_now = int(executor.get_no_fill_seconds() if hasattr(executor, "get_no_fill_seconds") else 0)
                        soft_after = int(getattr(config, "FILL_SCORE_SOFT_RELEASE_AFTER_SEC", 900))
                        hard_block_floor = float(getattr(config, "FILL_SCORE_HARD_BLOCK_THRESHOLD", 0.20))
                        soft_cap = float(getattr(config, "FILL_SCORE_SOFT_RELEASE_MAX_CHANGE", 0.35))
                        can_soft_release = (no_fill_now >= soft_after and old_grid_info is not None and grid_info is not None)
                        if min_fill_score <= hard_block_floor and no_fill_now < soft_after * 2:
                            can_soft_release = False

                        if can_soft_release:
                            old_u = float(old_grid_info.get("upper_pct", strategy.upper_pct) or strategy.upper_pct)
                            old_l = float(old_grid_info.get("lower_pct", strategy.lower_pct) or strategy.lower_pct)
                            old_tp = float(old_grid_info.get("Tp", strategy.Tp) or strategy.Tp)
                            old_n = int(old_grid_info.get("N", strategy.target_N) or strategy.target_N)

                            def _cap_delta(old_v, new_v):
                                old_v = float(old_v)
                                new_v = float(new_v)
                                cap = max(0.0, abs(old_v) * soft_cap)
                                if cap <= 0:
                                    return new_v
                                return max(old_v - cap, min(old_v + cap, new_v))

                            # 限幅放行：允许重建，但单次参数变化受限
                            strategy.upper_pct = _cap_delta(old_u, float(strategy.upper_pct))
                            strategy.lower_pct = _cap_delta(old_l, float(strategy.lower_pct))
                            strategy.Tp = _cap_delta(old_tp, float(strategy.Tp))
                            strategy.target_N = int(max(old_n, min(old_n + 1, int(strategy.target_N))))
                            # 关键修复：限幅后重新计算 grid_info，确保元数据与实际下单一致
                            _consec_fill_info2 = {
                                "count": executor.get_consecutive_with_decay() if hasattr(executor, 'get_consecutive_with_decay') else getattr(executor, '_consecutive_count', 0),
                                "side": getattr(executor, '_consecutive_side', ""),
                            }
                            _recalc = strategy.compute_grid_dynamic(
                                price, atr, smoothed_atr=atr_smoothed,
                                available_capital=available_capital,
                                grid_bias=grid_bias,
                                side_allocation=side_allocation,
                                sentry_override=sentry_override,
                                consecutive_fill_info=_consec_fill_info2,
                            )
                            if _recalc:
                                grid_info = _recalc
                                _rb_blocked_by = "fill_score_soft_release"
                                logger.info(
                                    f"[执行] 成交窗口限幅放行 | fill_score={min_fill_score:.2f}<{fill_score_threshold:.2f} "
                                    f"no_fill={no_fill_now}s cap={soft_cap:.0%} reason={reason}"
                                )
                            else:
                                logger.warning("[执行] 限幅放行后重算网格失败，回退为阻止重建")
                                rebuild = False
                                _rb_blocked_by = "fill_score_soft_recalc_failed"
                        else:
                            logger.info(
                                f"[执行] 成交窗口保护 | fill_score={min_fill_score:.2f}<{fill_score_threshold:.2f} | "
                                f"reason={reason} 已阻止"
                            )
                            rebuild = False
                            _rb_blocked_by = "fill_score"

        # 事件记录：所有触发重建意图的情况都记录
        if evt and _rb_wanted:
            evt.rebuild_attempt(
                reason=reason, allowed=rebuild, blocked_by=_rb_blocked_by,
                elapsed_sec=_rb_elapsed, fill_score=_rb_fill_score,
                price_drifted=_rb_price_drifted,
                grid_n=int(grid_info.get("N", 0) if grid_info else 0),
            )
        params["rebuild_reason"] = str(reason or "")
        params["rebuild_allowed"] = bool(rebuild)
        params["rebuild_blocked_by"] = str(_rb_blocked_by or "")
        params["rebuild_elapsed_sec"] = int(_rb_elapsed) if _rb_elapsed else 0
        params["rebuild_fill_score"] = round(float(_rb_fill_score or 0.0), 3)

        if grid_info and (rebuild or not executor.last_grid_info):
            logger.info(f"[执行] 部署网格: {reason} | action={action.value}")
            if executor.deploy_grid(grid_info):
                if strategy.activate_grid(grid_info, trader=trader):
                    if str(reason).startswith("无成交超时重建") and hasattr(executor, "reset_no_fill_timer"):
                        executor.reset_no_fill_timer(reason="rebuild")
                    mode = {
                        Action.GRID: "双向网格",
                        Action.TREND_LONG: "趋势做多",
                        Action.TREND_SHORT: "趋势做空",
                    }[action]
                    factors = params.get("factors", {})
                    send_telegram(
                        f"✅ *{mode}已部署*\n\n"
                        f"层数: {grid_info['N']} | "
                        f"范围: ${grid_info['P_low']:.0f}-${grid_info['P_high']:.0f}\n"
                        f"因子: {params.get('factor', 1.0)} "
                        f"(R={factors.get('risk', '-')} "
                        f"V={factors.get('vol', '-')} "
                        f"C={factors.get('capital', '-')})"
                    )
                else:
                    logger.warning("[执行] 网格激活被拒绝")
                    executor.cancel_all()
            else:
                logger.warning("[执行] 网格部署被拒绝")


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

def _try_mode_active_entry(action, trader, executor, strategy, features, params, cooldown_state,
                           mode_params=None, position_multiplier=1.0, allow_entry=True,
                           framework_reason=""):
    """
    趋势动作下的主动首单（市价）触发器。
    只在无仓时尝试一笔小仓位入场，随后仍由网格负责后续管理。
    """
    if action not in (Action.TREND_LONG, Action.TREND_SHORT):
        return False
    if not bool(params.get("allow_active_entry", True)):
        return False
    mode_params = mode_params or {}
    if not bool(mode_params.get("active_entry_enabled", False)):
        return False
    if not allow_entry:
        logger.info(f"[执行] 主动首单被仓位框架拦截 | {framework_reason}")
        return False
    position_multiplier = max(0.0, float(position_multiplier or 0.0))
    if position_multiplier <= 0:
        return False

    step = float(getattr(config, "ORDER_STEP_SIZE", 0.001) or 0.001)
    pos_amt = abs(float(features.get("position_amount", 0.0) or 0.0))
    if pos_amt >= step:
        return False

    # 避免已有入场挂单时再开主动首单
    if getattr(executor, "active_orders", None):
        has_entry = any(
            str(o.get("grid_side", "")) in ("long_entry", "short_entry")
            for o in executor.active_orders
        )
        if has_entry:
            return False

    price = float(features.get("price", 0.0) or 0.0)
    if price <= 0:
        return False

    adx = float(features.get("adx", 0.0) or 0.0)
    atr_ratio = float(features.get("atr_ratio", 0.0) or 0.0)
    adx_min = float(mode_params.get("active_entry_adx_min", 28.0))
    atr_min = float(mode_params.get("active_entry_atr_ratio_min", 0.005))
    atr_max = float(mode_params.get("active_entry_atr_ratio_max", 0.020))
    if adx < adx_min:
        return False
    if atr_ratio < atr_min or atr_ratio > atr_max:
        return False

    margin_ratio = float(features.get("margin_ratio", 0.0) or 0.0)
    margin_cap = float(mode_params.get("active_entry_max_margin_ratio", 0.40))
    if margin_ratio >= margin_cap:
        return False

    now = time.time()
    cooldown_sec = max(0, int(mode_params.get("active_entry_cooldown_sec", 1800)))
    last_ts = float(cooldown_state.get("active_entry_last_ts", 0.0) or 0.0)
    if cooldown_sec > 0 and (now - last_ts) < cooldown_sec:
        return False

    total_equity = float(features.get("total_equity", 0.0) or 0.0)
    leverage = max(1, int(getattr(config, "LEVERAGE", 1) or 1))
    if total_equity <= 0:
        return False

    target_margin_ratio = max(0.0, float(mode_params.get("active_entry_margin_ratio", 0.03)))
    notional = total_equity * target_margin_ratio * leverage * position_multiplier

    min_notional_cfg = float(mode_params.get("active_entry_min_notional_usdt", getattr(config, "MIN_ORDER_NOTIONAL", 130)))
    if getattr(config, "TRADING_MODE", "paper") == "paper":
        min_notional_cfg = max(float(getattr(config, "MIN_ORDER_NOTIONAL_PAPER", 0) or 0), min_notional_cfg)
    min_notional = max(0.0, min_notional_cfg)
    notional = max(notional, min_notional)

    max_notional_ratio = float(mode_params.get("active_entry_max_notional_ratio", 0.16))
    max_notional = total_equity * leverage * max(0.0, max_notional_ratio) * max(0.5, position_multiplier)
    if max_notional > 0:
        notional = min(notional, max_notional)

    qty = notional / price if price > 0 else 0.0
    qty = math.floor(qty / step) * step
    qty = round(max(0.0, qty), 6)
    if qty < step:
        return False
    if min_notional > 0 and qty * price < min_notional:
        return False

    # 主动首单前先同步一次，清理潜在幽灵 FIFO，防止后续重复记账
    try:
        if hasattr(executor, "_sync_pending_closes_with_position"):
            executor._sync_pending_closes_with_position()
    except Exception as e:
        logger.debug(f"[执行] 主动首单前FIFO同步失败: {e}")

    order_side = "buy" if action == Action.TREND_LONG else "sell"
    lead_stage = str(params.get("lead_stage", "") or "")
    probe_plan = _build_lead_probe_plan(action, features, params) if lead_stage == "probe" else None
    if lead_stage == "probe" and probe_plan:
        if probe_plan.get("mode") == "limit":
            client_order_id = f"probe_{probe_plan.get('note', 'entry')}_{int(now)}"
            order = trader.place_limit_order(order_side, float(probe_plan["price"]), qty, client_order_id=client_order_id)
            if not order:
                logger.warning("[执行] Probe 首单挂单失败")
                return False
            cooldown_state["active_entry_last_ts"] = now
            cooldown_state["active_entry_last_mode"] = str(params.get("trading_mode", "normal"))
            cooldown_state["active_entry_last_side"] = "long" if order_side == "buy" else "short"
            if hasattr(executor, "active_orders"):
                executor.active_orders.append({
                    "order_id": order.get("id"),
                    "side": order_side,
                    "grid_side": "long_entry" if order_side == "buy" else "short_entry",
                    "price": float(probe_plan["price"]),
                    "amount": qty,
                    "entry_price": float(probe_plan["price"]),
                    "layer": 0,
                    "est_profit": 0.0,
                    "_lead_probe": True,
                    "_lead_probe_note": str(probe_plan.get("note", "") or ""),
                })
            logger.warning(
                f"[执行] 🪝 Probe 挂单已提交 | side={order_side} qty={qty:.6f} "
                f"price=${float(probe_plan['price']):.2f} note={probe_plan.get('note','')}"
            )
            send_telegram(
                f"🪝 *Probe 挂单*\n\n"
                f"方向: {'多头' if order_side == 'buy' else '空头'}\n"
                f"价格: ${float(probe_plan['price']):.2f}\n"
                f"数量: {qty:.6f} BTC\n"
                f"模式: {probe_plan.get('note', '')}"
            )
            return True

        order = trader.place_market_order(order_side, qty)
    else:
        order = trader.place_market_order(order_side, qty)
    if not order:
        logger.warning("[执行] 主动首单触发但下单失败")
        return False

    fill_price = float(order.get("average", order.get("price", price)) or price)
    fill_qty = float(order.get("filled", qty) or qty)
    cooldown_state["active_entry_last_ts"] = now
    cooldown_state["active_entry_last_mode"] = str(params.get("trading_mode", "normal"))
    cooldown_state["active_entry_last_side"] = "long" if order_side == "buy" else "short"

    # 成交后写入 FIFO（开仓费率=TAKER），避免后续平仓手续费按 maker 低估
    side_key = "long" if order_side == "buy" else "short"
    try:
        if hasattr(executor, "_pending_closes"):
            executor._pending_closes.append({
                "entry_price": fill_price,
                "buy_price": fill_price,
                "amount": fill_qty,
                "side": side_key,
                "layer": 0,
                "entry_time": datetime.now().isoformat(),
                "entry_fee_rate": 0.0004,  # taker
                "_active_entry": True,
            })
            if hasattr(executor, "_save_state"):
                executor._save_state()
        if hasattr(executor, "_reconcile_fifo"):
            executor._reconcile_fifo()
    except Exception as e:
        logger.debug(f"[执行] 主动首单后FIFO写入失败: {e}")

    logger.warning(
        f"[执行] ⚡ 主动首单入场 | side={order_side} qty={fill_qty:.6f} "
        f"price=${fill_price:.2f} mode={params.get('trading_mode', 'normal')} "
        f"mult={position_multiplier:.2f} "
        f"adx={adx:.1f} atr_ratio={atr_ratio:.3%}"
    )
    send_telegram(
        f"⚡ *主动首单入场*\n\n"
        f"模式: {params.get('trading_mode', 'normal')}\n"
        f"方向: {'多头' if order_side == 'buy' else '空头'}\n"
        f"数量: {fill_qty:.6f} BTC @ ${fill_price:.2f}\n"
        f"仓位乘数: {position_multiplier:.2f}\n"
        f"触发: ADX={adx:.1f}, ATR={atr_ratio:.3%}"
    )
    return True


def _manage_exits(executor, trader, features):
    """管理已有持仓的退出（止损/止盈/成交检查）"""
    price = features.get("price", 0)
    if price <= 0:
        return False

    if hasattr(trader, 'set_current_price'):
        trader.set_current_price(price)

    layer_sl = executor.check_layer_stop_loss(price)
    if layer_sl > 0:
        send_telegram(f"⚠️ *分层止损* | 减仓 {layer_sl} 层 @ ${price:.0f}")

    if executor.check_stop_loss(price):
        send_telegram("🛑 *止损触发*")

    executor.check_time_based_take_profit(price)
    executor.check_time_based_exit(price)
    executor.check_trailing_stop(price)
    return bool(executor.check_fills())


def _force_close_position(trader, current_price):
    position = trader.get_position()
    if position and position.get("amount", 0) != 0:
        close_side = "sell" if position["amount"] > 0 else "buy"
        close_amount = abs(position["amount"])
        try:
            order = trader.place_market_order(close_side, close_amount, reduce_only=True)
            if order:
                logger.warning(f"[执行] 强制平仓: {close_side} {close_amount:.6f} BTC")
        except Exception as e:
            logger.error(f"[执行] 强制平仓失败: {e}")


def _reduce_position(trader, features, target_margin):
    position = trader.get_position()
    balance = trader.get_balance()
    price = features.get("price", 0)
    leverage = features.get("leverage", 5)

    if not position or not balance or price <= 0 or position.get("amount", 0) == 0:
        return False

    target_notional = balance["total"] * target_margin * leverage
    target_amount = target_notional / price
    excess = abs(position["amount"]) - target_amount
    step = float(getattr(config, "ORDER_STEP_SIZE", 0.001))

    if excess >= step:
        reduce_amount = math.floor(excess / step) * step
        if reduce_amount >= step:
            close_side = "sell" if position["amount"] > 0 else "buy"
            try:
                order = trader.place_market_order(close_side, reduce_amount, reduce_only=True)
                if order:
                    logger.warning(f"[执行] 主动减仓: {close_side} {reduce_amount:.6f} BTC")
                    send_telegram(
                        f"⚠️ *主动减仓*\n{close_side} {reduce_amount:.6f} BTC\n"
                        f"目标保证金: {target_margin*100:.0f}%"
                    )
                    return True
            except Exception as e:
                logger.error(f"[执行] 减仓失败: {e}")
    return False


def _build_sentry_override(features, params):
    sentry_state = features.get("sentry_state", "normal")
    return {
        "sentry_state": sentry_state,
        "interval_mult": params.get("spacing_mult", 1.0),
        "target_n": params.get("grid_N", config.GRID_NUM),
        "n_range": (1, params.get("grid_N", config.GRID_NUM)),
        "min_order_usdt": int(getattr(config, "SENTRY_MIN_ORDER_USDT", 130)),
        "asymmetric": "active" if sentry_state != "normal" else "none",
    }


def _build_lead_probe_plan(action, features, params):
    """为 probe 阶段生成双探针计划（回撤/回抽 + 突破/跌破）。"""
    if params:
        pullback_price = float(params.get("lead_probe_pullback_price", 0.0) or 0.0)
        breakout_trigger = float(params.get("lead_probe_breakout_trigger", 0.0) or 0.0)
        reference = float(params.get("lead_probe_reference", 0.0) or 0.0)
        breakout_ready = bool(params.get("lead_probe_breakout_ready", False))
        style = str(params.get("lead_probe_style", "") or "")
        if pullback_price > 0 and breakout_trigger > 0 and style:
            if action == Action.TREND_LONG:
                return {
                    "side": "buy",
                    "pullback_price": round(pullback_price, 2),
                    "pullback_note": "probe_pullback_long",
                    "breakout_trigger": round(breakout_trigger, 2),
                    "breakout_note": "probe_breakout_long",
                    "breakout_ready": breakout_ready,
                    "reference": round(reference, 2) if reference > 0 else 0.0,
                }
            if action == Action.TREND_SHORT:
                return {
                    "side": "sell",
                    "pullback_price": round(pullback_price, 2),
                    "pullback_note": "probe_rebound_short",
                    "breakout_trigger": round(breakout_trigger, 2),
                    "breakout_note": "probe_breakdown_short",
                    "breakout_ready": breakout_ready,
                    "reference": round(reference, 2) if reference > 0 else 0.0,
                }

    price = float(features.get("price", 0.0) or 0.0)
    atr = float(features.get("atr", features.get("atr_smoothed", 0.0)) or 0.0)
    recent_high = float(features.get("recent_high_4", price) or price)
    recent_low = float(features.get("recent_low_4", price) or price)
    if price <= 0 or atr <= 0:
        return None

    near_pct = float(getattr(config, "LEAD_PROBE_BREAKOUT_NEAR_PCT", 0.0015))
    pullback_atr = float(getattr(config, "LEAD_PROBE_PULLBACK_ATR", 0.45))
    breakout_atr = float(getattr(config, "LEAD_PROBE_BREAKOUT_ATR", 0.10))

    if action == Action.TREND_LONG:
        pullback_price = round(max(1.0, min(price - 0.01, price - atr * pullback_atr)), 2)
        breakout_trigger = round(max(price, recent_high + atr * breakout_atr), 2)
        return {
            "side": "buy",
            "pullback_price": pullback_price,
            "pullback_note": "probe_pullback_long",
            "breakout_trigger": breakout_trigger,
            "breakout_note": "probe_breakout_long",
            "breakout_ready": price >= (recent_high * (1.0 - near_pct)),
            "reference": round(recent_high, 2),
        }

    if action == Action.TREND_SHORT:
        rebound_price = round(max(price + 0.01, price + atr * pullback_atr), 2)
        breakdown_trigger = round(min(price, recent_low - atr * breakout_atr), 2)
        return {
            "side": "sell",
            "pullback_price": rebound_price,
            "pullback_note": "probe_rebound_short",
            "breakout_trigger": breakdown_trigger,
            "breakout_note": "probe_breakdown_short",
            "breakout_ready": price <= (recent_low * (1.0 + near_pct)),
            "reference": round(recent_low, 2),
        }

    return None


def _compute_active_entry_qty(features, mode_params, position_multiplier):
    price = float(features.get("price", 0.0) or 0.0)
    if price <= 0:
        return 0.0, 0.0, 0.0
    total_equity = float(features.get("total_equity", 0.0) or 0.0)
    leverage = max(1, int(getattr(config, "LEVERAGE", 1) or 1))
    if total_equity <= 0:
        return 0.0, 0.0, 0.0

    step = float(getattr(config, "ORDER_STEP_SIZE", 0.001) or 0.001)
    target_margin_ratio = max(0.0, float(mode_params.get("active_entry_margin_ratio", 0.03)))
    notional = total_equity * target_margin_ratio * leverage * position_multiplier

    min_notional_cfg = float(mode_params.get("active_entry_min_notional_usdt", getattr(config, "MIN_ORDER_NOTIONAL", 130)))
    if getattr(config, "TRADING_MODE", "paper") == "paper":
        min_notional_cfg = max(float(getattr(config, "MIN_ORDER_NOTIONAL_PAPER", 0) or 0), min_notional_cfg)
    min_notional = max(0.0, min_notional_cfg)
    notional = max(notional, min_notional)

    max_notional_ratio = float(mode_params.get("active_entry_max_notional_ratio", 0.16))
    max_notional = total_equity * leverage * max(0.0, max_notional_ratio) * max(0.5, position_multiplier)
    if max_notional > 0:
        notional = min(notional, max_notional)

    qty = notional / price if price > 0 else 0.0
    qty = math.floor(qty / step) * step
    qty = round(max(0.0, qty), 6)
    return qty, min_notional, step


def _cancel_lead_probe_orders(executor, trader, keep_side=None):
    """撤销旧的 probe 挂单，避免状态切换时残留双重入场。"""
    active = list(getattr(executor, "active_orders", []) or [])
    remain = []
    cancelled = 0
    for order in active:
        if not bool(order.get("_lead_probe", False)):
            remain.append(order)
            continue
        if keep_side and str(order.get("side", "")).lower() == str(keep_side).lower():
            remain.append(order)
            continue
        oid = order.get("order_id")
        if oid and hasattr(trader, "cancel_order"):
            try:
                ok = trader.cancel_order(oid)
                if ok is True:
                    cancelled += 1
                    continue
            except Exception:
                pass
        remain.append(order)
    executor.active_orders = remain
    return cancelled


def _handle_lead_probe_entries(action, trader, executor, features, params, cooldown_state,
                               mode_params=None, position_multiplier=1.0, allow_entry=True,
                               framework_reason=""):
    """完整版 probe：同时维护回撤/回抽探针和突破/跌破探针。"""
    if action not in (Action.TREND_LONG, Action.TREND_SHORT):
        return False
    if str(params.get("lead_stage", "") or "") != "probe":
        _cancel_lead_probe_orders(executor, trader)
        return False
    if not bool(params.get("allow_active_entry", True)):
        return False
    mode_params = mode_params or {}
    if not bool(mode_params.get("active_entry_enabled", False)):
        return False
    if not allow_entry:
        logger.info(f"[执行] Probe 被仓位框架拦截 | {framework_reason}")
        return False
    position_multiplier = max(0.0, float(position_multiplier or 0.0))
    if position_multiplier <= 0:
        return False

    step = float(getattr(config, "ORDER_STEP_SIZE", 0.001) or 0.001)
    pos_amt = abs(float(features.get("position_amount", 0.0) or 0.0))
    if pos_amt >= step:
        return False

    # 若已有非 probe 入场单，则不再额外挂 probe。
    if getattr(executor, "active_orders", None):
        has_non_probe_entry = any(
            str(o.get("grid_side", "")) in ("long_entry", "short_entry") and not bool(o.get("_lead_probe", False))
            for o in executor.active_orders
        )
        if has_non_probe_entry:
            return False

    price = float(features.get("price", 0.0) or 0.0)
    if price <= 0:
        return False

    adx = float(features.get("adx", 0.0) or 0.0)
    atr_ratio = float(features.get("atr_ratio", 0.0) or 0.0)
    adx_min = float(mode_params.get("active_entry_adx_min", 28.0))
    atr_min = float(mode_params.get("active_entry_atr_ratio_min", 0.005))
    atr_max = float(mode_params.get("active_entry_atr_ratio_max", 0.020))
    if adx < adx_min:
        return False
    if atr_ratio < atr_min or atr_ratio > atr_max:
        return False

    margin_ratio = float(features.get("margin_ratio", 0.0) or 0.0)
    margin_cap = float(mode_params.get("active_entry_max_margin_ratio", 0.40))
    if margin_ratio >= margin_cap:
        return False

    now = time.time()
    cooldown_sec = max(0, int(mode_params.get("active_entry_cooldown_sec", 1800)))
    last_ts = float(cooldown_state.get("active_entry_last_ts", 0.0) or 0.0)

    qty, min_notional, step = _compute_active_entry_qty(features, mode_params, position_multiplier)
    if qty < step or (min_notional > 0 and qty * price < min_notional):
        return False

    try:
        if hasattr(executor, "_sync_pending_closes_with_position"):
            executor._sync_pending_closes_with_position()
    except Exception as e:
        logger.debug(f"[执行] Probe 前FIFO同步失败: {e}")

    plan = _build_lead_probe_plan(action, features, params)
    if not plan:
        return False
    order_side = str(plan.get("side", "") or "").lower()
    if not order_side:
        return False

    active_probe_orders = [
        o for o in (getattr(executor, "active_orders", []) or [])
        if bool(o.get("_lead_probe", False)) and str(o.get("side", "")).lower() == order_side
    ]
    existing_pullback = next((o for o in active_probe_orders if str(o.get("_lead_probe_kind", "")) == "pullback"), None)

    # 1) 回撤/回抽探针：不存在时补一张限价单
    if existing_pullback is None:
        pullback_price = float(plan.get("pullback_price", 0.0) or 0.0)
        if pullback_price > 0:
            client_order_id = f"probe_{plan.get('pullback_note', 'pullback')}_{int(now)}"
            order = trader.place_limit_order(order_side, pullback_price, qty, client_order_id=client_order_id)
            if order:
                if hasattr(executor, "active_orders"):
                    executor.active_orders.append({
                        "order_id": order.get("id"),
                        "side": order_side,
                        "grid_side": "long_entry" if order_side == "buy" else "short_entry",
                        "price": pullback_price,
                        "amount": qty,
                        "entry_price": pullback_price,
                        "layer": 0,
                        "est_profit": 0.0,
                        "_lead_probe": True,
                        "_lead_probe_kind": "pullback",
                        "_lead_probe_note": str(plan.get("pullback_note", "") or ""),
                    })
                logger.warning(
                    f"[执行] 🪝 Probe 回撤挂单 | side={order_side} qty={qty:.6f} "
                    f"price=${pullback_price:.2f} note={plan.get('pullback_note','')}"
                )

    # 2) 突破/跌破探针：价格临近关键位时直接市价探针
    breakout_ready = bool(plan.get("breakout_ready", False))
    if breakout_ready and (cooldown_sec <= 0 or (now - last_ts) >= cooldown_sec):
        order = trader.place_market_order(order_side, qty)
        if order:
            _cancel_lead_probe_orders(executor, trader)
            fill_price = float(order.get("average", order.get("price", price)) or price)
            fill_qty = float(order.get("filled", qty) or qty)
            cooldown_state["active_entry_last_ts"] = now
            cooldown_state["active_entry_last_mode"] = str(params.get("trading_mode", "normal"))
            cooldown_state["active_entry_last_side"] = "long" if order_side == "buy" else "short"
            side_key = "long" if order_side == "buy" else "short"
            try:
                if hasattr(executor, "_pending_closes"):
                    executor._pending_closes.append({
                        "entry_price": fill_price,
                        "buy_price": fill_price,
                        "amount": fill_qty,
                        "side": side_key,
                        "layer": 0,
                        "entry_time": datetime.now().isoformat(),
                        "entry_fee_rate": 0.0004,
                        "_active_entry": True,
                        "_lead_probe_breakout": True,
                    })
                    if hasattr(executor, "_save_state"):
                        executor._save_state()
                if hasattr(executor, "_reconcile_fifo"):
                    executor._reconcile_fifo()
            except Exception as e:
                logger.debug(f"[执行] Probe 突破后FIFO写入失败: {e}")
            logger.warning(
                f"[执行] ⚡ Probe 突破入场 | side={order_side} qty={fill_qty:.6f} "
                f"price=${fill_price:.2f} trigger=${float(plan.get('breakout_trigger', price) or price):.2f} "
                f"note={plan.get('breakout_note','')}"
            )
            send_telegram(
                f"⚡ *Probe 突破入场*\n\n"
                f"方向: {'多头' if order_side == 'buy' else '空头'}\n"
                f"价格: ${fill_price:.2f}\n"
                f"数量: {fill_qty:.6f} BTC\n"
                f"触发线: ${float(plan.get('breakout_trigger', price) or price):.2f}"
            )
            return True

    # 仅挂出 probe 限价单也算 probe 已生效
    return existing_pullback is None


# ════════════════════════════════════════════════════════════════
# 主循环
# ════════════════════════════════════════════════════════════════

def run_realtime(force=False, external_stop_event=None, once=False, init_only=False, loop_delay=None):
    acquire_pid_lock(force=force)

    try:
        _run_realtime_inner(
            force=force,
            external_stop_event=external_stop_event,
            once=once,
            init_only=init_only,
            loop_delay=loop_delay,
        )
    finally:
        release_pid_lock()


def _build_runtime_objects(read_only=False):
    """构建策略系统最小运行时，仅保留 collect -> decide -> execute 链路。"""
    config.validate_config()
    config.check_api_keys()
    init_notifier(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)

    if config.TRADING_MODE == "paper":
        from core.paper_trader import PaperTrader

        trader = PaperTrader(
            clear_history_on_restart=bool(getattr(config, "PAPER_CLEAR_STATE_ON_STARTUP", True))
        )
        logger.info("模式: Paper")
    else:
        trader = BinanceTrader()
        logger.info("模式: Live")
        if (not read_only) and getattr(config, "PRESERVE_EXIT_ORDERS_ON_STARTUP", True):
            cancelled = _cancel_entry_orders_via_trader(trader)
            logger.info(f"[启动] 清理 {cancelled} 个旧入场单")

    if not read_only:
        trader.set_leverage(config.LEVERAGE)
    balance = trader.get_balance()
    if not balance:
        raise RuntimeError("无法获取账户信息")

    actual_capital = balance["total"] if balance.get("total", 0) > 0 else balance["free"]

    strategy = GridStrategy(
        symbol=config.SYMBOL,
        total_capital=actual_capital,
        grid_capital_ratio=config.GRID_CAPITAL_RATIO,
        buffer_ratio=config.BUFFER_RATIO,
        reserve_ratio=config.RESERVE_RATIO,
        L=config.LEVERAGE,
        N=config.GRID_NUM,
        upper_pct=config.UPPER_PCT,
        lower_pct=config.LOWER_PCT,
        Tp=config.TP_PER_GRID,
        stop_loss_pct=config.STOP_LOSS_PCT,
        max_trades_per_day=config.MAX_TRADES_PER_DAY,
        monthly_target=config.MONTHLY_TARGET,
    )
    strategy._is_paused = False
    strategy._pause_reason = None

    executor = GridExecutor(trader, strategy, clear_on_start=(config.TRADING_MODE == "paper"))
    cb = CircuitBreaker(
        max_api_failures=config.CIRCUIT_BREAKER_MAX_API_FAILURES,
        max_loss_per_hour=config.CIRCUIT_BREAKER_MAX_LOSS_PER_HOUR,
        min_loss_usdt=float(getattr(config, "CIRCUIT_BREAKER_MIN_LOSS_USDT", 0.0)),
        rapid_loss_pct=config.CIRCUIT_BREAKER_RAPID_LOSS_PCT,
    )
    executor.circuit_breaker = cb

    risk_manager = RiskManager(trader, strategy)
    risk_manager.initialize_session(balance["total"])

    collector = FeatureCollector(trader, ws_feed=None)
    collector.set_data_sources(
        market_data_fn=get_advanced_indicators,
        coinglass_fn=get_coinglass_bundle,
        sentiment_fn=fetch_binance_sentiment_bundle,
        aux_trend_fn=get_aux_trend_indicators,
    )

    return {
        "trader": trader,
        "balance": balance,
        "actual_capital": actual_capital,
        "strategy": strategy,
        "executor": executor,
        "circuit_breaker": cb,
        "risk_manager": risk_manager,
        "collector": collector,
        "decision_engine": DecisionEngine(),
        "meta_layer": MetaDecisionLayer(),
        "sentry": MarketSentry(),
        "bias_engine": PositionBiasEngine(),
    }


def _initialize_strategy_apis(trader):
    """显式初始化并预热测试版仍保留的 API。"""
    results = []

    balance = trader.get_balance()
    account_ok = bool(balance and (balance.get("total") is not None or balance.get("free") is not None))
    results.append(("account_api", account_ok, balance or {}))

    indicators = get_advanced_indicators(config.SYMBOL, include_coinglass=False)
    market_ok = bool(indicators and float(indicators.get("current_price", 0) or 0) > 0)
    results.append(("market_data_api", market_ok, {
        "price": float(indicators.get("current_price", 0) or 0) if indicators else 0,
        "rsi": indicators.get("rsi") if indicators else None,
    }))

    sentiment = fetch_binance_sentiment_bundle(config.SYMBOL)
    sentiment_ok = isinstance(sentiment, dict)
    results.append(("sentiment_api", sentiment_ok, sentiment or {}))

    aux = get_aux_trend_indicators(config.SYMBOL)
    aux_ok = isinstance(aux, dict)
    results.append(("aux_trend_api", aux_ok, aux or {}))

    if getattr(config, "COINGLASS_ENABLED", False):
        coinglass = get_coinglass_bundle()
        coinglass_ok = isinstance(coinglass, dict)
        results.append(("coinglass_api", coinglass_ok, coinglass or {}))
    else:
        results.append(("coinglass_api", True, {"status": "disabled"}))

    telegram_ready = bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)
    results.append(("telegram_notifier", True, {"configured": telegram_ready}))

    failed = []
    for name, ok, detail in results:
        if ok:
            logger.info(f"[API初始化] {name}: OK | {detail}")
        else:
            failed.append(name)
            logger.warning(f"[API初始化] {name}: FAIL | {detail}")

    if failed and bool(getattr(config, "TEST_INIT_STRICT", False)):
        raise RuntimeError(f"初始化失败: {', '.join(failed)}")

    return results


def _log_decision(decision):
    logger.info(
        f"[决策] {decision.action.value} | "
        f"regime={decision.regime.value} dir={decision.direction.value} "
        f"risk={decision.risk.value} | {decision.reason}"
    )
    if decision.params:
        factors = decision.params.get("factors", {})
        mode_tag = decision.params.get("trading_mode", "-")
        logger.info(
            f"[三因子] R={factors.get('risk', '-')} V={factors.get('vol', '-')} "
            f"C={factors.get('capital', '-')} → factor={decision.params.get('factor', '-')} "
            f"N={decision.params.get('grid_N', '-')} mode={mode_tag}"
        )


def _sleep_with_interrupt(seconds, external_stop_event=None):
    end_at = time.time() + max(0, int(seconds))
    while time.time() < end_at:
        if external_stop_event and external_stop_event.is_set():
            return True
        time.sleep(1)
    return False


def _run_realtime_inner(force=False, external_stop_event=None, once=False, init_only=False, loop_delay=None):
    # 重新获取 logger（确保使用当前 trading_mode）
    global logger
    logger = get_logger()

    logger.info("=" * 60)
    logger.info("  交易机器人 9.0 Test - Strategy Only")
    logger.info("=" * 60)

    # 登录后重新设置日志模式并刷新 logger
    set_trading_mode(config.TRADING_MODE)
    logger = get_logger()
    runtime = _build_runtime_objects(read_only=init_only)
    trader = runtime["trader"]
    actual_capital = runtime["actual_capital"]
    strategy = runtime["strategy"]
    executor = runtime["executor"]
    cb = runtime["circuit_breaker"]
    risk_manager = runtime["risk_manager"]
    collector = runtime["collector"]
    decision_engine = runtime["decision_engine"]
    meta_layer = runtime["meta_layer"]
    sentry = runtime["sentry"]
    bias_engine = runtime["bias_engine"]
    _initialize_strategy_apis(trader)

    send_telegram(
        f"🚀 *交易机器人 9.0 Test 已启动*\n\n"
        f"资金: ${actual_capital:.2f} | 杠杆: {config.LEVERAGE}x\n"
        f"架构: Strategy Only"
    )

    if init_only:
        logger.info("[启动] 初始化完成，按 --init-only 退出")
        return

    # ── 信号处理 ──
    _shutdown = False

    def _handle_shutdown(signum, frame):
        nonlocal _shutdown
        _shutdown = True
        logger.info(f"[主程序] 收到信号 {signal.Signals(signum).name}")

    if current_thread() is main_thread():
        signal.signal(signal.SIGTERM, _handle_shutdown)
        signal.signal(signal.SIGINT, _handle_shutdown)
    else:
        logger.debug("[主程序] 非主线程运行，跳过进程级信号注册")

    # ── 主循环状态 ──
    cooldown_state = {
        "until": 0,
        "force_rebuild": False,
        "flow_state": "normal",
        "flow_state_ts": time.time(),
        "no_fill_step_bucket": None,
        "position_framework": {
            "date": "",
            "trade_cursor": 0,
            "loss_streak": 0,
            "win_streak": 0,
            "day_net": 0.0,
            "last_trade_profit": 0.0,
            "last_trade_time": "",
            "last_log_sig": "",
            "last_log_ts": 0.0,
        },
    }
    sync_counter = 0
    consecutive_failures = 0
    sleep_sec = int(loop_delay or getattr(config, "TEST_LOOP_INTERVAL_SEC", 60))

    try:
        idx = 0
        while not _shutdown:
            if external_stop_event and external_stop_event.is_set():
                _shutdown = True
                logger.info("[主程序] 外部停止信号已收到")
                break
            idx += 1
            sync_counter += 1
            logger.info(f"{'='*40}")
            logger.info(f"[主循环] 第 {idx} 轮")

            # 冷却期
            if cooldown_state["until"] and time.time() < cooldown_state["until"]:
                remain = int(cooldown_state["until"] - time.time())
                logger.warning(f"[冷却] 剩余 {remain}s")
                snapshot = get_advanced_indicators(config.SYMBOL, include_coinglass=False) or {}
                price_now = float(snapshot.get("current_price", 0) or 0)
                if price_now and price_now > 0:
                    _manage_exits(executor, trader, {"price": price_now})
                if _sleep_with_interrupt(min(remain, sleep_sec), external_stop_event=external_stop_event):
                    _shutdown = True
                continue
            elif cooldown_state["until"]:
                cooldown_state["until"] = 0
                cooldown_state["force_rebuild"] = True
                try:
                    cb.reset(reason="冷却结束")
                except Exception:
                    pass
                send_telegram("✅ *冷却期结束*")

            # ══════════════════════════════════════
            # Step 1: COLLECT
            # ══════════════════════════════════════
            features = collector.collect(
                circuit_breaker=cb,
                sentry=sentry,
                bias_engine=bias_engine,
                risk_manager=risk_manager,
                strategy=strategy,
            )

            if not features.get("data_available"):
                consecutive_failures += 1
                logger.warning(f"[主循环] 数据获取失败 ({consecutive_failures}/5)")
                if consecutive_failures >= 5:
                    send_telegram("❌ *数据故障，程序退出*")
                    raise RuntimeError("数据持续故障，主程序退出")
                if _sleep_with_interrupt(sleep_sec, external_stop_event=external_stop_event):
                    _shutdown = True
                continue
            consecutive_failures = 0

            cb.record_api_call(True, 0)
            if features.get("total_equity"):
                cb.record_equity(features["total_equity"])

            # 周期性同步
            if sync_counter >= 5:
                sync_counter = 0
                try:
                    executor.sync_binance_state()
                except Exception as e:
                    logger.warning(f"[同步] {e}")

            strategy.last_indicators = features.get("_raw_indicators", {})
            _sync_position_framework_state(strategy, cooldown_state)
            features["_position_framework"] = _resolve_position_framework(features, cooldown_state)
            _log_position_framework(features["_position_framework"], cooldown_state)

            # 注入事件级特征：距上次成交时间 + 近 10 分钟成交次数 + 成交间隔节奏
            features["time_since_last_fill"] = executor.get_no_fill_seconds()
            features["fills_last_10min"] = executor.get_recent_fill_count(600)
            features["fill_interval_avg"] = executor.get_avg_fill_interval(10)

            # ══════════════════════════════════════
            # Step 1.5: UNDERSTAND（元决策层）
            # ══════════════════════════════════════
            context = meta_layer.understand(features)
            logger.info(f"[元决策] {context}")

            # ══════════════════════════════════════
            # Step 2: DECIDE
            # ══════════════════════════════════════
            decision = decision_engine.decide(features, context)
            _log_decision(decision)

            # ══════════════════════════════════════
            # Step 3: EXECUTE
            # ══════════════════════════════════════
            execute_decision(decision, executor, strategy, trader, features, cooldown_state, evt=None)

            if once:
                logger.info("[主循环] --once 完成，退出")
                break

            if _sleep_with_interrupt(sleep_sec, external_stop_event=external_stop_event):
                _shutdown = True

    except KeyboardInterrupt:
        logger.info("用户中断")
    except Exception as e:
        import traceback
        logger.critical(f"[主程序] 未预期错误: {e}\n{traceback.format_exc()}")
        send_telegram(f"❌ *程序崩溃: {e}*")
        raise
    finally:
        if getattr(config, "PRESERVE_EXIT_ORDERS_ON_SHUTDOWN", True):
            try:
                c = int(executor.cancel_entry_orders() or 0)
                c += int(_cancel_entry_orders_via_trader(trader) or 0)
                logger.info(f"[退出] 撤入场单 {c} 个，保留 TP/SL")
            except Exception:
                pass
        else:
            try:
                executor.cancel_all()
                trader.cancel_all_orders()
            except Exception:
                pass
        logger.info("[主程序] 已退出")


def main():
    parser = argparse.ArgumentParser(description="交易机器人 9.0 Test - Strategy Only")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--init-only", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop-delay", type=int, default=None)
    parser.add_argument("--mode", type=str, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.mode:
        mode = args.mode.lower()
        if mode not in ("paper", "live"):
            print(f"无效模式: {args.mode}")
            sys.exit(1)
        config.TRADING_MODE = mode
        set_trading_mode(mode)

    print(f"运行模式: {config.TRADING_MODE}")

    if args.status:
        if config.TRADING_MODE == "paper":
            from core.paper_trader import PaperTrader

            t = PaperTrader(clear_history_on_restart=False)
        else:
            t = BinanceTrader()
        b = t.get_balance()
        if b:
            print(f"余额: free=${b.get('free', 0):.2f} total=${b.get('total', 0):.2f}")
        p = t.get_position()
        if p and p["amount"] != 0:
            print(f"持仓: {p['amount']:.6f} BTC @ ${p['entry_price']:.2f}")
        else:
            print("无持仓")
    else:
        run_realtime(
            force=args.force,
            init_only=args.init_only,
            once=args.once,
            loop_delay=args.loop_delay,
        )


if __name__ == "__main__":
    main()

