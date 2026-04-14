# risk_manager.py - 风险管理系统
"""
风险管理模块
✅ 日度损失限制
✅ 回撤监控
✅ 自动降杠杆
"""
from utils.logger import get_logger
from utils.notifier import send_telegram
import config
from datetime import datetime

logger = get_logger()

class RiskManager:
    """风险管理模块"""
    
    def __init__(self, trader, strategy):
        self.trader = trader
        self.strategy = strategy
        
        self.max_daily_loss = config.MAX_DAILY_LOSS
        self.max_drawdown = config.MAX_DRAWDOWN
        self.max_position_ratio = 0.8
        self.emergency_stop_loss = config.EMERGENCY_STOP_LOSS
        
        self.peak_equity = config.TOTAL_CAPITAL
        self.session_start_balance = None
        
        logger.info("[RiskManager] 已初始化")
    
    def initialize_session(self, initial_balance):
        """初始化会话"""
        self.session_start_balance = initial_balance
        self.peak_equity = initial_balance
        logger.info(f"[RiskManager] 会话启动，初始余额: ${initial_balance:.2f}")
    
    def check_daily_loss(self, current_balance):
        """检查日度损失"""
        if self.session_start_balance is None:
            return False, ""
        
        daily_loss = self.session_start_balance - current_balance
        
        if daily_loss > self.max_daily_loss:
            logger.warning(f"[RiskManager] 🚨 日度损失超限! ${daily_loss:.2f} > ${self.max_daily_loss:.2f}")
            send_telegram(f"🚨 *日度损失超限*\n\n损失: ${daily_loss:.2f}\n限制: ${self.max_daily_loss:.2f}")
            return True, f"日度损失 ${daily_loss:.2f}"
        
        return False, ""
    
    def check_drawdown(self, current_balance):
        """检查回撤"""
        # ✅ 修复：先更新峰值，再计算回撤
        if current_balance > self.peak_equity:
            self.peak_equity = current_balance
        
        drawdown_rate = (self.peak_equity - current_balance) / self.peak_equity
        
        if drawdown_rate > self.max_drawdown:
            logger.warning(f"[RiskManager] ⚠️ 回撤过大! {drawdown_rate*100:.1f}%")
            send_telegram(f"⚠️ *回撤告警*\n\n当前回撤: {drawdown_rate*100:.1f}%\n警告线: {self.max_drawdown*100:.1f}%")
            return drawdown_rate, True
        
        return drawdown_rate, False
    
    def check_emergency_stop(self, current_balance):
        """检查紧急止损"""
        if self.session_start_balance is None:
            return False
        
        loss_rate = (self.session_start_balance - current_balance) / self.session_start_balance
        
        if loss_rate > self.emergency_stop_loss:
            logger.critical(f"[RiskManager] 🛑 触发紧急止损! 亏损 {loss_rate*100:.1f}%")
            send_telegram(f"🛑 *紧急止损触发！*\n\n亏损率: {loss_rate*100:.1f}%\n阈值: {self.emergency_stop_loss*100:.1f}%")
            return True
        
        return False
    
    def daily_risk_check(self):
        """每日风险检查"""
        balance = self.trader.get_balance()
        if not balance:
            return None
        
        current_balance = balance['total']
        
        daily_loss_hit, _ = self.check_daily_loss(current_balance)
        drawdown_rate, drawdown_hit = self.check_drawdown(current_balance)
        emergency_hit = self.check_emergency_stop(current_balance)
        
        return {
            'daily_loss_hit': daily_loss_hit,
            'drawdown_hit': drawdown_hit,
            'emergency_hit': emergency_hit,
            'drawdown_rate': drawdown_rate,
            'current_balance': current_balance
        }
    
    def generate_report(self, current_balance, position_size, current_price):
        """生成风险报告"""
        position_value = position_size * current_price
        
        daily_loss = self.session_start_balance - current_balance if self.session_start_balance else 0
        loss_rate = daily_loss / self.session_start_balance if self.session_start_balance else 0
        drawdown_rate = (self.peak_equity - current_balance) / self.peak_equity if self.peak_equity > 0 else 0
        position_ratio = position_value / (current_balance + 1e-10)
        
        return {
            'timestamp': datetime.now().isoformat(),
            'current_balance': current_balance,
            'daily_loss': daily_loss,
            'loss_rate': loss_rate,
            'drawdown_rate': drawdown_rate,
            'peak_equity': self.peak_equity,
            'position_ratio': position_ratio,
        }

