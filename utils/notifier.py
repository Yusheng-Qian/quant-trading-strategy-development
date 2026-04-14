# notifier.py - 通知模块
"""
通知系统
"""
import requests
import time
from utils.logger import get_logger

logger = get_logger()

class TelegramNotifier:
    """Telegram 通知器"""
    
    def __init__(self, bot_token, chat_id, timeout=5, max_retries=3):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.max_retries = max_retries
        self.api_url = f"https://api.telegram.org/bot{bot_token}" if bot_token else None
    
    def send(self, message, parse_mode=None):
        if not self.bot_token or not self.chat_id:
            logger.info(f"[通知] {message}")
            return False
        
        data = {
            "chat_id": self.chat_id,
            "text": message,
        }
        
        if parse_mode:
            data["parse_mode"] = parse_mode
        
        for attempt in range(self.max_retries):
            try:
                resp = requests.post(
                    f"{self.api_url}/sendMessage",
                    json=data,
                    timeout=self.timeout
                )
                if resp.status_code == 200:
                    return True
                else:
                    logger.warning(f"[Telegram] 发送失败: {resp.text}")
            except Exception as e:
                logger.warning(f"[Telegram] 错误: {e}")
                time.sleep(2 ** attempt)
        
        return False

_notifier = None
_muted = False          # 回测期间静默，不发 Telegram

def mute_telegram():
    """回测期间调用，屏蔽所有 Telegram 消息"""
    global _muted
    _muted = True

def unmute_telegram():
    """回测结束后恢复"""
    global _muted
    _muted = False

def init_notifier(bot_token, chat_id):
    global _notifier
    _notifier = TelegramNotifier(bot_token, chat_id)
    if bot_token and chat_id:
        logger.info("[Telegram] 通知器已初始化")

def send_telegram(message, parse_mode=None):
    if _muted:
        return False
    if _notifier:
        return _notifier.send(message, parse_mode)
    else:
        logger.info(f"[通知] {message}")
        return False

def notify_strategy_start():
    import config
    msg = f"🚀 交易机器人已启动\n\n"
    msg += f"资金: ${config.TOTAL_CAPITAL} USDT\n"
    msg += f"杠杆: {config.LEVERAGE}x\n"
    msg += f"网格: {config.GRID_NUM} 层"
    send_telegram(msg, parse_mode=None)

def notify_trade_executed(side, price, amount, profit=None):
    emoji = "📈" if side == "buy" else "📉"
    side_cn = "买入" if side == "buy" else "卖出"
    msg = f"{emoji} {side_cn}成交\n\n价格: ${price:.2f}\n数量: {amount:.6f} BTC"
    if profit is not None:
        msg += f"\n利润: ${profit:+.2f}"
    send_telegram(msg, parse_mode=None)

def notify_stop_loss():
    send_telegram("🛑 止损触发! 所有持仓已平仓", parse_mode=None)

def notify_circuit_breaker(action, reason):
    emoji = "🔴" if action == "EMERGENCY_CLOSE" else "🟡"
    msg = f"{emoji} 断路器触发\n\n动作: {action}\n原因: {reason}"
    send_telegram(msg, parse_mode=None)

def notify_error(error_msg):
    send_telegram(f"❌ 错误: {error_msg}", parse_mode=None)

def notify_status(summary):
    msg = "📊 状态摘要\n\n"
    for k, v in summary.items():
        msg += f"{k}: {v}\n"
    send_telegram(msg, parse_mode=None)

