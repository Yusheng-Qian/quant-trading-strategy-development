# binance_trader.py - Binance 交易执行模块
import ccxt
import re
import time
import math
import os
from datetime import datetime, timedelta, timezone
from utils.logger import get_logger
import config

logger = get_logger()

# 最小下单量
MIN_BTC_AMOUNT = 0.001

class BinanceTrader:
    """Binance 合约交易执行"""

    def __init__(self):
        proxy_url = self._resolve_proxy_url()
        exchange_cfg = {
            'apiKey': config.BINANCE_API_KEY,
            'secret': config.BINANCE_SECRET,
            'enableRateLimit': True,
            'timeout': int(getattr(config, 'BINANCE_HTTP_TIMEOUT_MS', 15000)),
            'options': {
                'adjustForTimeDifference': True,
                'recvWindow': int(getattr(config, 'BINANCE_RECV_WINDOW_MS', 20000)),
            }
        }
        if proxy_url:
            # 显式注入 ccxt 代理（实测比仅依赖环境变量更稳定）
            exchange_cfg['proxies'] = {
                'http': proxy_url,
                'https': proxy_url,
            }
        # 使用 USD-M 专用客户端，避免在部分网络环境下误走 dapi(coin-M) 导致启动失败
        self.exchange = ccxt.binanceusdm(exchange_cfg)
        if proxy_url:
            logger.info(f"[BinanceTrader] 已启用代理: {self._mask_proxy(proxy_url)}")
        self.symbol = 'BTC/USDT:USDT'
        self._read_api_retries = int(getattr(config, 'BINANCE_READ_API_RETRIES', 3))
        self._read_api_backoff = float(getattr(config, 'BINANCE_READ_API_BACKOFF_SEC', 0.8))
        self._read_api_hard_fail_cooldown_sec = float(getattr(config, "BINANCE_READ_API_HARD_FAIL_COOLDOWN_SEC", 120))
        self._read_api_cooldown_until = {}
        self._time_sync_retry_cooldown_sec = float(getattr(config, 'BINANCE_TIME_SYNC_RETRY_COOLDOWN_SEC', 30))
        self._last_time_sync_retry_ts = 0.0
        self._pnl_summary_cache_ttl_sec = int(getattr(config, "PNL_SUMMARY_CACHE_TTL_SEC", 600))
        self._balance_cache_max_age_sec = int(getattr(config, "BALANCE_CACHE_MAX_AGE_SEC", 900))
        self._last_balance = None
        self._last_balance_ts = 0.0
        self._last_balance_fallback_warn_ts = 0.0
        # 本地 ReduceOnly 挂单量追踪（防止 get_open_orders 延迟导致重复挂单）
        self._local_ro_reserved = {"buy": 0.0, "sell": 0.0}
        self._pnl_summary_cache = {}
        self._sync_time_difference(force=True)
        logger.info("[BinanceTrader] 已初始化")

    def _resolve_proxy_url(self):
        """解析用于交易所 HTTP 请求的代理地址。"""
        candidates = [
            str(getattr(config, 'BINANCE_HTTP_PROXY', '') or '').strip(),
            os.getenv('BINANCE_HTTP_PROXY', '').strip(),
            os.getenv('HTTPS_PROXY', '').strip(),
            os.getenv('https_proxy', '').strip(),
            os.getenv('HTTP_PROXY', '').strip(),
            os.getenv('http_proxy', '').strip(),
        ]
        for raw in candidates:
            if not raw:
                continue
            if raw.startswith(('http://', 'https://', 'socks5://', 'socks5h://')):
                return raw
            return f"http://{raw}"
        return ""

    def _mask_proxy(self, proxy_url):
        s = str(proxy_url or "")
        m = re.match(r'^([a-zA-Z0-9+.-]+://)([^:/@]+)(:\d+)?$', s)
        if not m:
            return s
        scheme = m.group(1)
        host = m.group(2)
        port = m.group(3) or ""
        if host in ("127.0.0.1", "localhost", "::1"):
            return f"{scheme}{host}{port}"
        if len(host) <= 6:
            host_masked = "***"
        else:
            host_masked = host[:3] + "***" + host[-2:]
        return f"{scheme}{host_masked}{port}"

    def _sanitize_error(self, err):
        """脱敏错误消息，避免日志泄露签名参数。"""
        msg = str(err)
        msg = re.sub(r'signature=[A-Za-z0-9]+', 'signature=***', msg)
        return msg

    def _classify_error(self, err):
        """粗分类错误类型，便于判断是否可重试。"""
        msg = str(err).lower()
        status = None
        m = re.search(r'\b([45]\d{2})\b', msg)
        if m:
            try:
                status = int(m.group(1))
            except Exception:
                status = None

        # Binance 区域限制（应视为硬失败，不做重试）
        if "restricted location" in msg or "eligibility" in msg:
            return 'http_forbidden', False

        if '-1021' in msg or 'outside of the recvwindow' in msg:
            return 'timestamp_drift', True
        if '-1022' in msg or 'signature for this request is not valid' in msg:
            return 'signature_invalid', False
        if status in (401, 403, 451):
            return 'http_forbidden', False
        if status == 429:
            return 'rate_limited', True
        if status is not None and 400 <= status < 500:
            return 'http_client', False
        if status is not None and status >= 500:
            return 'http_server', True
        if '429' in msg or 'rate limit' in msg or 'too many requests' in msg:
            return 'rate_limited', True
        if any(k in msg for k in ['timeout', 'timed out', 'connection', 'network', 'ssl', 'eof']):
            return 'network', True
        # 某些 ccxt 异常只返回 "<exchange> GET https://..."，不带底层异常类型
        # 对只读接口可安全重试，避免把瞬时抖动直接记为失败
        if ' get https://' in msg and 'binance' in msg:
            return 'exchange_transient', True
        return 'unknown', False

    def _sync_time_difference(self, force=False):
        """与交易所做时间差同步，缓解 -1021 时间戳漂移。"""
        now = time.time()
        if (not force) and (now - self._last_time_sync_retry_ts < self._time_sync_retry_cooldown_sec):
            return
        try:
            self.exchange.load_time_difference()
            self._last_time_sync_retry_ts = now
            logger.warning("[BinanceTrader] 已执行时间差同步（load_time_difference）")
        except Exception as e:
            logger.warning(f"[BinanceTrader] 时间差同步失败: {self._sanitize_error(e)}")

    def _call_read_api_with_retry(self, action_name, func, default_value):
        """
        只用于只读接口：
        - 网络类错误：指数退避重试
        - 时间戳漂移：先同步时间差再重试
        """
        now = time.time()
        cooldown_until = float(self._read_api_cooldown_until.get(action_name, 0.0) or 0.0)
        if cooldown_until > now:
            return default_value
        retries = max(1, self._read_api_retries)
        for attempt in range(1, retries + 1):
            try:
                return func()
            except Exception as e:
                err_type, retryable = self._classify_error(e)
                msg = self._sanitize_error(e)
                is_last = attempt >= retries
                if err_type == 'timestamp_drift':
                    self._sync_time_difference(force=True)
                    retryable = True
                if err_type in {"http_forbidden", "http_client", "signature_invalid"}:
                    self._read_api_cooldown_until[action_name] = time.time() + self._read_api_hard_fail_cooldown_sec
                if is_last or not retryable:
                    logger.error(f"[BinanceTrader] {action_name}失败[{err_type}] (尝试 {attempt}/{retries}): {msg}")
                    break
                delay = self._read_api_backoff * (2 ** (attempt - 1))
                logger.warning(
                    f"[BinanceTrader] {action_name}失败[{err_type}] (尝试 {attempt}/{retries}): {msg} | "
                    f"{delay:.1f}s 后重试"
                )
                time.sleep(delay)
        return default_value

    def get_balance(self):
        """获取账户余额"""
        balance = self._call_read_api_with_retry(
            "获取余额",
            lambda: self.exchange.fetch_balance({'type': 'future'}),
            default_value=None,
        )
        if not balance:
            now = time.time()
            if self._last_balance and (now - self._last_balance_ts) <= self._balance_cache_max_age_sec:
                if (now - self._last_balance_fallback_warn_ts) > 60:
                    age = int(now - self._last_balance_ts)
                    logger.warning(f"[BinanceTrader] 获取余额失败，使用缓存余额回退 | age={age}s")
                    self._last_balance_fallback_warn_ts = now
                return dict(self._last_balance)
            return None
        parsed = {
            'free': balance.get('free', {}).get('USDT', 0),
            'total': balance.get('total', {}).get('USDT', 0),
            'used': balance.get('used', {}).get('USDT', 0)
        }
        self._last_balance = dict(parsed)
        self._last_balance_ts = time.time()
        return parsed

    def get_position(self):
        """获取当前持仓"""
        positions = self._call_read_api_with_retry(
            "获取持仓",
            lambda: self.exchange.fetch_positions(),
            default_value=None,
        )
        if not positions:
            return None
        for pos in positions:
            symbol = pos.get('symbol', '')
            # Check both BTCUSDT and BTC/USDT:USDT formats
            if ('BTC' in symbol and 'USDT' in symbol) and float(pos.get('info', {}).get('positionAmt', 0)) != 0:
                amt = float(pos['info']['positionAmt'])
                return {
                    'amount': amt,
                    'entry_price': float(pos['entryPrice']),
                    'unrealized_pnl': float(pos['unrealizedPnl'])
                }
        return None

    def get_open_orders(self):
        """获取当前挂单"""
        return self._call_read_api_with_retry(
            "获取挂单",
            lambda: self.exchange.fetch_open_orders(self.symbol),
            default_value=None,
        )

    def cancel_all_orders(self):
        """撤销所有挂单"""
        try:
            orders = self.get_open_orders()
            if orders is None:
                logger.warning("[BinanceTrader] 撤销全部挂单失败：当前挂单状态未知")
                return None
            for order in orders:
                self.exchange.cancel_order(order['id'], self.symbol)
            remaining = self.get_open_orders()
            if remaining is None:
                logger.warning("[BinanceTrader] 撤销全部挂单后状态未知，保留本地订单跟踪")
                return None
            self._local_ro_reserved = {"buy": 0.0, "sell": 0.0}
            logger.info(f"[BinanceTrader] 已撤销 {len(orders) - len(remaining)} 个挂单")
            return max(0, len(orders) - len(remaining))
        except Exception as e:
            logger.error(f"[BinanceTrader] 撤销挂单失败: {self._sanitize_error(e)}")
            return None

    def cancel_order(self, order_id):
        """撤销单个挂单"""
        try:
            self.exchange.cancel_order(order_id, self.symbol)
            remaining = self.get_open_orders()
            if remaining is not None:
                remaining_ids = {str(o.get("id")) for o in remaining if o.get("id") is not None}
                if str(order_id) in remaining_ids:
                    logger.warning(f"[BinanceTrader] 撤销订单 {order_id} 后仍在挂单簿中")
                    return False
            logger.info(f"[BinanceTrader] 已撤销订单 {order_id}")
            return True
        except Exception as e:
            remaining = self.get_open_orders()
            if remaining is not None:
                remaining_ids = {str(o.get("id")) for o in remaining if o.get("id") is not None}
                if str(order_id) not in remaining_ids:
                    logger.info(f"[BinanceTrader] 订单 {order_id} 已不在挂单簿中，按已撤销处理")
                    return True
            logger.warning(f"[BinanceTrader] 撤销订单 {order_id} 失败: {self._sanitize_error(e)}")
            return None

    def adjust_local_ro_reserved(self, side, delta, reason=""):
        """调整本地 reduceOnly 预留量（用于撤单/成交后的容量回收）。"""
        key = str(side or "").lower()
        if key not in ("buy", "sell"):
            return 0.0
        try:
            delta = float(delta or 0.0)
        except Exception:
            delta = 0.0
        before = float(self._local_ro_reserved.get(key, 0.0) or 0.0)
        after = round(max(0.0, before + delta), 6)
        self._local_ro_reserved[key] = after
        if abs(delta) >= MIN_BTC_AMOUNT:
            logger.debug(
                f"[BinanceTrader] reduceOnly本地预留调整[{key}] {before:.6f}->{after:.6f} "
                f"(delta={delta:+.6f})" + (f" | {reason}" if reason else "")
            )
        return after

    def get_reduce_only_capacity(self, side):
        """
        估算 reduceOnly 可用容量。
        返回: (available, reserved, max_reducible, exchange_reserved, local_reserved)
        """
        key = str(side or "").lower()
        if key not in ("buy", "sell"):
            return 0.0, 0.0, 0.0, 0.0, 0.0
        pos = self.get_position()
        if not pos:
            return 0.0, 0.0, 0.0, 0.0, float(self._local_ro_reserved.get(key, 0.0) or 0.0)
        pos_amt = float(pos.get('amount', 0.0) or 0.0)
        max_reducible = abs(pos_amt)
        is_closing_long = (key == 'sell' and pos_amt > 0)
        is_closing_short = (key == 'buy' and pos_amt < 0)
        if not (is_closing_long or is_closing_short):
            return 0.0, 0.0, round(max_reducible, 6), 0.0, float(self._local_ro_reserved.get(key, 0.0) or 0.0)

        exchange_reserved = 0.0
        open_orders = self.get_open_orders() or []
        for o in open_orders:
            info = o.get('info', {}) if isinstance(o, dict) else {}
            ro = info.get('reduceOnly')
            if isinstance(ro, str):
                ro = ro.lower() == 'true'
            if not bool(ro):
                continue
            if str(o.get('side', '')).lower() != key:
                continue
            rem = o.get('remaining', None)
            if rem is None:
                rem = o.get('amount', 0)
            try:
                exchange_reserved += abs(float(rem or 0.0))
            except Exception:
                continue

        local_reserved = float(self._local_ro_reserved.get(key, 0.0) or 0.0)
        reserved = max(exchange_reserved, local_reserved)
        step = float(getattr(config, 'ORDER_STEP_SIZE', MIN_BTC_AMOUNT) or MIN_BTC_AMOUNT)
        available = max(0.0, max_reducible - reserved)
        if step > 0:
            available = math.floor(available / step) * step
        return (
            round(max(0.0, available), 6),
            round(max(0.0, reserved), 6),
            round(max(0.0, max_reducible), 6),
            round(max(0.0, exchange_reserved), 6),
            round(max(0.0, local_reserved), 6),
        )

    def get_order_status(self, order_id):
        """
        获取订单状态
        返回: order info 或 None
        """
        order = self._call_read_api_with_retry(
            f"查询订单 {order_id}",
            lambda: self.exchange.fetch_order(order_id, self.symbol),
            default_value=None,
        )
        if order is not None:
            return order

        # 回退：部分瞬时异常下 fetch_order 失败，但订单仍在 open_orders 中
        try:
            open_orders = self.get_open_orders() or []
            for o in open_orders:
                if str(o.get("id")) == str(order_id):
                    o = dict(o)
                    o["status"] = o.get("status") or "open"
                    return o
        except Exception:
            pass
        return None

    def place_limit_order(self, side, price, amount, client_order_id=None, reduce_only=False, skip_ro_check=False):
        """
        下限价单
        side: 'buy' 或 'sell'
        price: 价格
        amount: 数量 (BTC)
        client_order_id: 可选的客户端订单ID，用于标记订单类型
        """
        # 确保最小下单量
        if amount < MIN_BTC_AMOUNT:
            logger.warning(f"[BinanceTrader] 数量 {amount} < 最小 {MIN_BTC_AMOUNT}，跳过")
            return None

        try:
            # reduceOnly 预检查：避免在"无可减仓位 / 已被其它 reduceOnly 订单占满"时触发 -2022
            if reduce_only and not skip_ro_check:
                pos = self.get_position()
                if not pos or abs(float(pos.get('amount', 0.0))) < MIN_BTC_AMOUNT:
                    logger.warning("[BinanceTrader] reduceOnly 跳过：当前无可减仓位")
                    return None

                pos_amt = float(pos.get('amount', 0.0))
                is_closing_long = (side == 'sell' and pos_amt > 0)
                is_closing_short = (side == 'buy' and pos_amt < 0)
                if not (is_closing_long or is_closing_short):
                    logger.warning(
                        f"[BinanceTrader] reduceOnly 跳过：方向不匹配 | side={side} position={pos_amt:.6f}"
                    )
                    return None

                available, reserved, max_reducible, exchange_reserved, local_reserved = self.get_reduce_only_capacity(side)

                if available < MIN_BTC_AMOUNT:
                    logger.warning(
                        f"[BinanceTrader] reduceOnly 跳过：可用减仓量不足 | "
                        f"可减={max_reducible:.6f} 已占用={reserved:.6f} 可用={available:.6f} "
                        f"(交易所={exchange_reserved:.6f} 本地={local_reserved:.6f})"
                    )
                    return None

                if amount > available + 1e-9:
                    original = amount
                    amount = available
                    logger.info(
                        f"[BinanceTrader] reduceOnly 数量收敛: {original:.6f} -> {amount:.6f} "
                        f"(可用减仓量)"
                    )

            extra = {}
            if client_order_id:
                extra['clientOrderId'] = client_order_id
            if reduce_only:
                # 仅减仓：用于止盈/止损单，避免误开反向仓位
                extra['reduceOnly'] = True

            order = self.exchange.create_order(
                symbol=self.symbol,
                type='limit',
                side=side,
                amount=amount,
                price=price,
                params=extra,
            )
            logger.info(f"[BinanceTrader] ✅ {side.upper()} 限价单: {price} x {amount} BTC" + (f" | ID:{client_order_id}" if client_order_id else ""))
            if reduce_only:
                self.adjust_local_ro_reserved(side, amount, reason="下单占用")
            return order
        except Exception as e:
            err_str = str(e)
            if '-2022' in err_str or 'ReduceOnly' in err_str:
                logger.warning(f"[BinanceTrader] reduceOnly 被拒(可能已有等量挂单): {side} {amount:.6f}")
            else:
                logger.error(f"[BinanceTrader] ❌ 下单失败: {self._sanitize_error(e)}")
            return None

    def place_market_order(self, side, amount, reduce_only=False):
        """
        下市价单
        side: 'buy' 或 'sell'
        amount: 数量 (BTC)
        """
        if amount < MIN_BTC_AMOUNT:
            logger.warning(f"[BinanceTrader] 市价单数量 {amount} 小于最小值 {MIN_BTC_AMOUNT}")
            return None
        try:
            extra = {}
            if reduce_only:
                pos = self.get_position()
                if not pos or abs(float(pos.get('amount', 0.0) or 0.0)) < MIN_BTC_AMOUNT:
                    logger.warning("[BinanceTrader] reduceOnly 市价单跳过：当前无可减仓位")
                    return None
                available, reserved, max_reducible, _, _ = self.get_reduce_only_capacity(side)
                if available < MIN_BTC_AMOUNT:
                    logger.warning(
                        f"[BinanceTrader] reduceOnly 市价单跳过：可用减仓量不足 | "
                        f"可减={max_reducible:.6f} 已占用={reserved:.6f} 可用={available:.6f}"
                    )
                    return None
                if amount > available + 1e-9:
                    logger.info(
                        f"[BinanceTrader] reduceOnly 市价单数量收敛: {amount:.6f} -> {available:.6f}"
                    )
                    amount = available
                if amount < MIN_BTC_AMOUNT:
                    return None
                extra["reduceOnly"] = True
            order = self.exchange.create_order(
                symbol=self.symbol,
                type='market',
                side=side,
                amount=amount,
                params=extra,
            )
            logger.info(
                f"[BinanceTrader] ✅ {side.upper()} 市价单: {amount} BTC"
                + (" | reduceOnly" if reduce_only else "")
            )
            return order
        except Exception as e:
            logger.error(f"[BinanceTrader] ❌ 市价单失败: {self._sanitize_error(e)}")
            return None

    def close_position(self, side, amount, reduce_only=True):
        """平仓"""
        return self.place_market_order(side, amount, reduce_only=reduce_only)

    def set_leverage(self, leverage):
        """设置杠杆"""
        try:
            self.exchange.set_leverage(leverage, self.symbol)
            logger.info(f"[BinanceTrader] 杠杆已设置为 {leverage}x")
        except Exception as e:
            logger.error(f"[BinanceTrader] 设置杠杆失败: {self._sanitize_error(e)}")

    # ════════════════════════════════════════════════════════════════════
    # ✅ 新增：从 Binance 获取真实盈亏数据
    # ════════════════════════════════════════════════════════════════════

    def get_income_history(self, income_type=None, limit=100, start_time=None, end_time=None):
        """
        获取合约账户收益流水（真实盈亏数据）

        Args:
            income_type: 收益类型过滤
                - 'REALIZED_PNL': 已实现盈亏
                - 'FUNDING_FEE': 资金费率
                - 'COMMISSION': 手续费
                - None: 全部类型
            limit: 返回记录数（最大1000）
            start_time: 开始时间(ms)
            end_time: 结束时间(ms)

        Returns:
            list[dict]: 收益记录列表
        """
        def _fetch_income():
            params = {'limit': limit}
            if income_type:
                params['incomeType'] = income_type
            if start_time:
                params['startTime'] = int(start_time)
            if end_time:
                params['endTime'] = int(end_time)

            # 优先使用 ccxt 标准接口，兼容不同版本
            if hasattr(self.exchange, 'fetch_income_history'):
                response = self.exchange.fetch_income_history(symbol=self.symbol, limit=limit, params=params)
            elif hasattr(self.exchange, 'fapiPrivateGetIncome'):
                response = self.exchange.fapiPrivateGetIncome(params)
            else:
                response = self.exchange.private_get_income(params)
            return response

        response = self._call_read_api_with_retry(
            "获取收益流水",
            _fetch_income,
            default_value=None,
        )
        if response is None:
            return []

        records = []
        for item in response:
            records.append({
                'symbol': item.get('symbol', ''),
                'income_type': item.get('incomeType', ''),
                'income': float(item.get('income', 0)),
                'asset': item.get('asset', 'USDT'),
                'time': int(item.get('time', 0)),
                'info': item.get('info', ''),
                'trade_id': item.get('tradeId', ''),
            })

        logger.debug(f"[BinanceTrader] 收益流水 type={income_type or 'ALL'} count={len(records)}")
        return records

    def get_pnl_summary(self, days=30):
        """
        获取指定天数内的盈亏汇总

        Args:
            days: 回溯天数

        Returns:
            dict: {
                'total_realized_pnl': float,   # 已实现总盈亏
                'total_funding_fee': float,     # 资金费总收支
                'total_commission': float,       # 手续费总计
                'net_pnl': float,                # 净盈亏
                'trade_count': int,              # 交易笔数
                'win_count': int,                # 盈利笔数
                'loss_count': int,               # 亏损笔数
                'win_rate': float,               # 胜率
                'daily_pnl': dict,               # 按日汇总 {date_str: pnl}
            }
        """
        import time as _time
        from collections import defaultdict

        cache_key = int(days)
        now = _time.time()
        cache_item = self._pnl_summary_cache.get(cache_key)
        if cache_item and (now - cache_item.get("ts", 0) <= self._pnl_summary_cache_ttl_sec):
            return cache_item["data"]

        end_time = int(now * 1000)
        start_time = int((now - days * 86400) * 1000)

        # 获取已实现盈亏
        realized = self.get_income_history('REALIZED_PNL', limit=1000,
                                            start_time=start_time, end_time=end_time)
        # 获取资金费率
        funding = self.get_income_history('FUNDING_FEE', limit=1000,
                                           start_time=start_time, end_time=end_time)
        # 获取手续费
        commission = self.get_income_history('COMMISSION', limit=1000,
                                              start_time=start_time, end_time=end_time)

        total_realized = sum(r['income'] for r in realized)
        total_funding = sum(r['income'] for r in funding)
        total_commission = sum(r['income'] for r in commission)

        # 将 REALIZED_PNL 按"交易事件"聚合后再统计胜率：
        # - 优先用 trade_id 聚合，避免同一笔成交被拆成多条流水
        # - 无 trade_id 时回退为时间戳键
        realized_by_event = {}
        for r in realized:
            key = str(r.get('trade_id') or f"ts:{r.get('time', 0)}")
            realized_by_event[key] = realized_by_event.get(key, 0.0) + float(r.get('income', 0.0))

        event_pnls = list(realized_by_event.values())
        win_count = sum(1 for v in event_pnls if v > 0)
        loss_count = sum(1 for v in event_pnls if v < 0)
        breakeven_count = sum(1 for v in event_pnls if abs(v) < 1e-12)
        # 交易笔数使用有效样本（盈+亏），排除保本噪声
        trade_count = win_count + loss_count

        # 按日汇总
        daily_pnl = defaultdict(float)
        for r in realized:
            day_str = datetime.fromtimestamp(r['time'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
            daily_pnl[day_str] += r['income']

        result = {
            'total_realized_pnl': round(total_realized, 2),
            'total_funding_fee': round(total_funding, 2),
            'total_commission': round(total_commission, 2),
            'net_pnl': round(total_realized + total_funding + total_commission, 2),
            'trade_count': trade_count,
            'raw_realized_count': len(realized),
            'win_count': win_count,
            'loss_count': loss_count,
            'breakeven_count': breakeven_count,
            'win_rate': round(win_count / trade_count, 4) if trade_count > 0 else 0,
            'daily_pnl': dict(daily_pnl),
            'days': days,
        }
        self._pnl_summary_cache[cache_key] = {"ts": now, "data": result}
        return result

    def get_trade_history(self, limit=50, since=None):
        """
        获取最近成交记录

        Returns:
            list[dict]: 成交记录
        """
        def _fetch_trades():
            since_ms = None
            if since is not None:
                try:
                    since_ms = int(float(since) * 1000)
                except Exception:
                    since_ms = None

            return self.exchange.fetch_my_trades(self.symbol, since=since_ms, limit=limit)

        trades = self._call_read_api_with_retry(
            "获取成交记录",
            _fetch_trades,
            default_value=None,
        )
        if trades is None:
            return []

        result = []
        for t in trades:
            result.append({
                'id': t['id'],
                'time': t['datetime'],
                'side': t['side'],
                'price': float(t['price']),
                'amount': float(t['amount']),
                'cost': float(t['cost']),
                'fee': float(t['fee']['cost']) if t.get('fee') else 0,
            })
        return result

