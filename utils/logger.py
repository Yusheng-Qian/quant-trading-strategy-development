# logger.py - 日志模块
"""
日志记录模块
✅ 按天一个文件（如 live_BTC_Grid_2026-03-21.log）
✅ 自动清理超过保留天数的旧日志
✅ 模拟盘/实盘日志分离
✅ 实时刷新（无缓冲）
"""
import logging
import os
import sys
import glob as _glob
from datetime import datetime, timedelta
import config


# 模块级变量：运行模式（由 main.py 在启动时设置）
_trading_mode = None
_LOG_RETENTION_DAYS = int(getattr(config, "LOG_RETENTION_DAYS", 7))


def set_trading_mode(mode):
    """
    设置交易模式，影响后续日志文件名
    必须在第一次 get_logger() 之前调用
    """
    global _trading_mode
    _trading_mode = mode


class FlushStreamHandler(logging.StreamHandler):
    """每条日志写完立即 flush 的控制台 handler"""
    def emit(self, record):
        super().emit(record)
        self.flush()


class DailyFileHandler(logging.FileHandler):
    """按天自动轮转的文件 handler，每条写完立即 flush"""

    def __init__(self, log_dir, log_name, retention_days=7, **kwargs):
        self._log_dir = log_dir
        self._log_name = log_name
        self._retention_days = retention_days
        self._current_date = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(log_dir, f"{log_name}_{self._current_date}.log")
        super().__init__(path, mode="a", encoding="utf-8", **kwargs)

    def emit(self, record):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._current_date:
            self._rotate(today)
        super().emit(record)
        self.flush()

    def _rotate(self, today):
        self.close()
        self._current_date = today
        self.baseFilename = os.path.join(
            self._log_dir, f"{self._log_name}_{today}.log"
        )
        self.stream = self._open()
        _cleanup_old_logs(self._log_dir, self._log_name, self._retention_days)


def _cleanup_old_logs(log_dir, log_name, retention_days):
    """清理超过保留天数的旧日志文件"""
    try:
        cutoff = datetime.now() - timedelta(days=retention_days)
        for f in _glob.glob(os.path.join(log_dir, f"{log_name}_*.log")):
            basename = os.path.basename(f)
            # live_BTC_Grid_2026-03-21.log → 2026-03-21
            date_str = basename.replace(f"{log_name}_", "").replace(".log", "")
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    os.remove(f)
            except ValueError:
                pass  # 旧格式（按月）不处理
    except Exception:
        pass


def get_logger(name="BTC_Grid"):
    """
    获取日志记录器

    每天一个日志文件，自动清理旧文件：
      - paper 模式: logs/paper_BTC_Grid_2026-03-21.log
      - live 模式:  logs/live_BTC_Grid_2026-03-21.log
      - 未设置:     logs/BTC_Grid_2026-03-21.log

    所有 handler 均实时 flush，`tail -f` 可立即看到新日志。
    """
    base_log_dir = os.environ.get("LOG_DIR", getattr(config, "LOG_DIR", "data/logs"))

    if _trading_mode:
        log_name = f"{_trading_mode}_{name}"
        # live/paper/backtest 写入对应子文件夹
        log_dir = os.path.join(base_log_dir, _trading_mode)
    else:
        log_name = name
        log_dir = os.path.join(base_log_dir, "misc")

    logger_key = f"{name}_{_trading_mode or 'default'}"
    logger = logging.getLogger(logger_key)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    realtime_flush = getattr(config, "LOG_REALTIME_FLUSH", True)
    # ═══ 控制台处理器 ═══
    console_handler = (FlushStreamHandler(sys.stdout) if realtime_flush else logging.StreamHandler(sys.stdout))
    console_handler.setLevel(logging.INFO)

    mode_tag = f"[{_trading_mode.upper()}] " if _trading_mode else ""
    formatter = logging.Formatter(
        f"[%(asctime)s] [%(levelname)s] {mode_tag}%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # ═══ 文件处理器 — 按天轮转，自动清理 ═══
    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = DailyFileHandler(
            log_dir, log_name, retention_days=_LOG_RETENTION_DAYS
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except (OSError, PermissionError) as exc:
        logger.warning(
            f"无法写入日志目录 '{log_dir}' ({exc}); 仅输出控制台日志。"
        )

    return logger

