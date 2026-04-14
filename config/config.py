# config.py - 统一配置文件
"""
交易机器人 - 统一配置
整合所有模块的配置
"""
import os
import uuid
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

# 脚本目录
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 运行时文件目录（状态文件/PID/stop flag/token）
RUNTIME_DIR = os.path.join(_SCRIPT_DIR, "data", "runtime")
os.makedirs(RUNTIME_DIR, exist_ok=True)

GRID_STATE_FILE = os.path.join(RUNTIME_DIR, "grid_state.json")
PAPER_STATE_FILE = os.path.join(RUNTIME_DIR, "paper_state.json")
GRID_BOT_PID_FILE = os.path.join(RUNTIME_DIR, "grid_bot.pid")
TRADING_BOT_PID_FILE = os.path.join(RUNTIME_DIR, "trading_bot.pid")
BOT_STOP_FLAG_FILE = os.path.join(RUNTIME_DIR, ".bot_stop")
DASHBOARD_TOKEN_FILE = os.path.join(RUNTIME_DIR, ".dashboard_token")

# ════════════════════════════════════════════════════════════════════
# 🔐 安全配置
# ════════════════════════════════════════════════════════════════════

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "")

# ✅ 修复：不在导入时 raise，改为延迟检查（Paper 模式不需要 API Key）
_API_KEYS_CONFIGURED = bool(BINANCE_API_KEY and BINANCE_SECRET)

def check_api_keys():
    """检查 API 密钥（仅 live 模式需要）"""
    if TRADING_MODE == "live" and not _API_KEYS_CONFIGURED:
        raise ValueError("❌ Live 模式需要在 .env 文件中设置 BINANCE_API_KEY 和 BINANCE_SECRET")
    if not _API_KEYS_CONFIGURED:
        import logging
        logging.getLogger("BTC_Grid").warning("⚠️ 未配置 Binance API 密钥，仅 Paper 模式可用")

# Telegram 通知
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
LOG_DIR = os.getenv("LOG_DIR", "data/logs")
LOG_REALTIME_FLUSH = True
LOG_RETENTION_DAYS = 7          # 运行日志保留天数
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "")
COINGLASS_ENABLED = bool(COINGLASS_API_KEY)

# Dashboard 认证 Token
def _load_or_generate_dashboard_token():
    """从环境变量或文件加载 token，不存在则生成并保存"""
    env_token = os.getenv("DASHBOARD_TOKEN", "")
    if env_token:
        return env_token

    token_candidates = [
        DASHBOARD_TOKEN_FILE,
        os.path.join(_SCRIPT_DIR, ".dashboard_token"),
    ]

    for token_file in token_candidates:
        if not os.path.exists(token_file):
            continue
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                token = f.read().strip()
            if not token:
                continue
            if token_file != DASHBOARD_TOKEN_FILE:
                try:
                    with open(DASHBOARD_TOKEN_FILE, "w", encoding="utf-8") as wf:
                        wf.write(token)
                except Exception:
                    pass
            return token
        except Exception:
            continue

    # 生成新 token 并保存
    token = str(uuid.uuid4())
    for path in (DASHBOARD_TOKEN_FILE, os.path.join(_SCRIPT_DIR, ".dashboard_token")):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(token)
            break
        except Exception:
            continue
    return token

DASHBOARD_TOKEN = _load_or_generate_dashboard_token()
DASHBOARD_ENABLED = True
DASHBOARD_WEB_ENABLED = os.getenv("DASHBOARD_WEB_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 5001
DASHBOARD_RETRY_SEC = 10
DASHBOARD_MANUAL_START = os.getenv("DASHBOARD_MANUAL_START", "1").strip().lower() in ("1", "true", "yes", "on")

# Dashboard 登录凭据
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD_HASH = os.getenv("DASHBOARD_PASSWORD_HASH", "")
# 登录校验开关（API + Web 通用）：
# 默认策略：若已配置密码哈希则开启，否则关闭。
DASHBOARD_LOGIN_REQUIRED = os.getenv(
    "DASHBOARD_LOGIN_REQUIRED",
    "1" if str(DASHBOARD_PASSWORD_HASH or "").strip() else "0",
).strip().lower() in ("1", "true", "yes", "on")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", DASHBOARD_TOKEN)


def generate_password_hash(password):
    """生成密码哈希。运行: python -c "from config import generate_password_hash; generate_password_hash('你的密码')" """
    import hashlib
    salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000).hex()
    print(f"DASHBOARD_PASSWORD_HASH={salt}${h}")

# ════════════════════════════════════════════════════════════════════
# 📊 行情数据配置
# ════════════════════════════════════════════════════════════════════

BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_HTTP_PROXY = os.getenv("BINANCE_HTTP_PROXY", "").strip()
KLINE_INTERVAL = "30m"
KLINE_LIMIT = 250
COINGLASS_BASE_URL = "https://open-api-v4.coinglass.com"
COINGLASS_EXCHANGE = "Binance"
COINGLASS_LIQUIDATION_SYMBOL = "BTCUSDT"
COINGLASS_LIQUIDATION_INTERVAL = "4h"
COINGLASS_LIQUIDATION_LIMIT = 1
COINGLASS_REFRESH_SEC = 1800  # 30分钟刷新一次，降低请求频率
COINGLASS_EXCHANGE_RANK_REFRESH_SEC = 300
COINGLASS_TIMEOUT_SEC = 5
COINGLASS_LIQUIDATION_MIN_TOTAL = 200000
COINGLASS_LIQUIDATION_IMBALANCE_MED = 0.20
COINGLASS_LIQUIDATION_IMBALANCE_STRONG = 0.50
COINGLASS_SSL_VERIFY = True
COINGLASS_USE_CURL_FALLBACK = True
COINGLASS_OI_INTERVAL = "4h"
COINGLASS_OI_LIMIT = 1
COINGLASS_FUNDING_INTERVAL = "4h"
COINGLASS_FUNDING_LIMIT = 1
COINGLASS_LS_INTERVAL = "4h"
COINGLASS_LS_LIMIT = 1
COINGLASS_TAKER_INTERVAL = "4h"
COINGLASS_TAKER_LIMIT = 1
COINGLASS_ORDERBOOK_RANGE = 0.02
COINGLASS_ORDERBOOK_LIMIT = 1
COINGLASS_FALLBACK_TO_LAST = True
COINGLASS_FAIL_COOLDOWN_SEC = 120
COINGLASS_UPGRADE_COOLDOWN_SEC = 86400
COINGLASS_AUX_ONLY = True
COINGLASS_ONLY_RANGING = True
COINGLASS_GRID_RANGE_THRESHOLD = 0.10
COINGLASS_HOBBYIST_BASE_ONLY = True  # 仅使用Hobbyist稳定可用接口，避免 ls/taker/orderbook 401/失败日志
COINGLASS_FETCH_OI = False
COINGLASS_FETCH_FUNDING = False
COINGLASS_FETCH_EXCHANGE_RANK = False

# ════════════════════════════════════════════════════════════════════
# 💼 策略参数
# ════════════════════════════════════════════════════════════════════

SYMBOL = "BTCUSDT"
TOTAL_CAPITAL = 695          # 初始资金 (匹配实际账户余额)

# Binance BTCUSDT 合约下单精度
ORDER_STEP_SIZE = 0.001      # BTC 最小下单步长
MIN_ORDER_NOTIONAL = 110     # Binance 最小名义价值 (USDT)；下调到110提升挂单密度
# Paper 模式最低名义价值阈值（仅模拟盘生效，避免风险缩仓后“部署成功但0挂单”）
MIN_ORDER_NOTIONAL_PAPER = 0
LEVERAGE = 3                 # 杠杆倍数（降杠杆，先稳回撤）
GRID_NUM = 7                 # 网格层数（提高基线密度，提升成交率/样本速度）
UPPER_PCT = 0.02             # 上界涨幅 2.0% (从3%收窄)
LOWER_PCT = 0.02             # 下界跌幅 2.0% (从3%收窄)
# 按有效层数自动调整网格范围：
# - N 下调(例如 6->3) 时放宽范围，减少噪音触发
# - N 恢复时自动缩窄回基准范围
AUTO_RANGE_BY_EFFECTIVE_N = True
RANGE_BY_N_MAX_MULT = 1.35    # 放宽上限倍数（2% * 1.35 = 2.7%，避免降层后过宽）
RANGE_BY_N_POWER = 0.35       # 放宽曲线（更温和，减少“越放越远”）
TP_PER_GRID = 0.0060         # 每格止盈 0.60%（提高成交频率）
# 🎯 动态止盈（基于 ATR 自适应）
DYNAMIC_TP_ENABLED = True          # 启用 ATR 动态 TP
DYNAMIC_TP_K = 0.90                # ATR/Price → TP 缩放因子（降低，让TP更贴近实际波动）
DYNAMIC_TP_MIN = 0.0050            # TP 下限 0.50%
DYNAMIC_TP_MAX = 0.0300            # TP 上限 3.00%
# 🕐 孤儿 TP 时间衰减（持仓越久 → TP 越近 → 快走）
ORPHAN_TP_DECAY_30MIN = 0.85       # 持仓 >30min → TP × 0.85（放缓衰减，给TP更多成交机会）
ORPHAN_TP_DECAY_60MIN = 0.70       # 持仓 >60min → TP × 0.70
ORPHAN_TP_FLOOR = 0.0050           # 衰减后 TP 最低 0.50%
# 🕐 时间止盈（长时间未止盈 → 收窄TP并重挂）
TIME_TP_ENABLED = True
TIME_TP_HOLDING_MIN = 60           # 持仓超过 60 分钟才启动时间止盈重挂（从45放宽）
TIME_TP_DECAY_MID = 0.90           # 60~90min：TP × 0.90
TIME_TP_DECAY_60MIN = 0.70         # >90min：TP × 0.70
TIME_TP_DECAY_120MIN = 0.55        # >120min：TP × 0.55
TIME_TP_FLOOR = 0.0050             # 时间止盈下限 0.50%
TIME_TP_REPRICE_DELTA = 0.0006     # 新旧TP价差低于0.06%则不重挂（防抖）
TIME_TP_REFRESH_COOLDOWN_SEC = 300 # 时间止盈重挂冷却（秒）
TIME_TP_AGGRESSIVE_LOSS_GATE = 0.006  # 浮亏超过0.6%时直接收窄到TP下限
# 🕐 时间止损 + 节奏止损（持仓不动 / 市场不配合 → 减仓）
TIME_SL_ENABLED = True             # 启用时间止损
TIME_SL_HOLDING_MIN = 90           # 持仓超过 90 分钟且亏损 → 减仓（从45放宽，给TP充分时间）
TIME_SL_REDUCE_PCT = 0.10          # 时间止损减仓比例（降低被迫割肉冲击）
TIME_SL_MIN_LOSS_PCT = 0.0080      # 时间止损亏损门槛（0.8%以内不砍，避免震荡市反复割肉）
TIME_SL_FORCE_EXIT_HOLDING_MIN = 360  # 超长持仓强制时间止损（放宽到360分钟）
TIME_SL_FORCE_EXIT_MIN_LOSS_PCT = 0.0040  # 超时强平也需最小亏损门槛（0.40%）
RHYTHM_SL_ENABLED = True           # 启用节奏止损
RHYTHM_SL_RATIO = 2.0              # 近期成交间隔 / 基线间隔 ≥ 2 倍 → 结构失效（从1.6放宽，减少误触发）
RHYTHM_SL_REDUCE_PCT = 0.15        # 节奏止损减仓比例（从20%降到15%）
RHYTHM_SL_MIN_SAMPLES = 10         # 节奏止损最少成交笔数（冷启动保护）
RHYTHM_SL_MIN_LOSS_PCT = 0.0080    # 节奏止损亏损门槛（从0.40%提高到0.80%，和时间止损对齐）
TIME_EXIT_COOLDOWN_SEC = 900       # 时间/节奏止损冷却间隔（从600s放宽到900s）

STOP_LOSS_PCT = 0.05         # 止损 5%
# 分层止损（逐层减仓）：
# - 先按每笔入场价计算独立止损线，触发后仅减掉该层
# - 再由 STOP_LOSS_PCT 作为整仓兜底
LAYER_STOP_LOSS_ENABLED = True
LAYER_STOP_LOSS_PCT = 0.025  # 每层止损 2.5%
LAYER_STOP_LOSS_MAX_PER_CYCLE = 2  # 每轮最多执行的分层止损次数
BIDIRECTIONAL_GRID = True    # 双向网格（多+空）
GRID_SPACING = "arithmetic"  # 网格类型: arithmetic(等差) / geometric(等比)
AUTO_GRID_SPACING = True     # 根据波动自动切换等差/等比
GRID_SPACING_VOL_THRESHOLD = 0.012  # ATR/Price 超过阈值使用等比
# 多空动态配比（基于实时+12小时窗口）
DYNAMIC_SIDE_ALLOCATION_ENABLED = True
SIDE_LOOKBACK_HOURS = 12
SIDE_MIN_LONG_RATIO = 0.20
SIDE_MAX_LONG_RATIO = 0.80
SIDE_WEIGHT_PRICE_MOM = 0.35
SIDE_WEIGHT_OI_MOM = 0.20
SIDE_WEIGHT_TOP_RATIO = 0.25
SIDE_WEIGHT_ACCOUNT_RATIO = 0.20
AUTO_TREND_FILTER = True     # 启用趋势过滤（阶段二）
TREND_FILTER_METHOD = "adx"  # adx / vhf / hybrid
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 32.0
TREND_CONFIRM_ENABLED = True
TREND_CONFIRM_INTERVAL = "15m"
ADX_FAST_TREND_THRESHOLD = 32.0
TREND_CONFIRM_REFRESH_SEC = 60
VHF_PERIOD = 28
VHF_TREND_THRESHOLD = 0.30
TREND_COOLDOWN_SEC = 600     # 趋势熔断冷却期（秒）
TREND_DIRECTIONAL_ENABLED = True
TREND_DIRECTIONAL_MIN_SCORE = 0.20   # 方向化最小综合分（来自 side_allocation.score）
TREND_DIRECTIONAL_MIN_OI_MOM = 0.00  # OI 12h 动量下限，低于该值不做方向化
# MetaDecision 趋势兜底：即使决策仍为 range/grid，也可按趋势概率强制单边
META_TREND_BIAS_ENABLED = True
META_TREND_BIAS_PROB_THRESHOLD = 0.55   # trend_up/down 概率达到阈值即触发（从0.48提高，减少误触发）
META_TREND_BIAS_MIN_GAP = 0.05          # 趋势概率需领先对侧至少 5%
FLASH_CRASH_ENABLED = True
FLASH_CRASH_WINDOW_SEC = 60          # 1分钟窗口
FLASH_CRASH_DROP_PCT = 0.03          # 跌幅 > 3% 触发
FLASH_CRASH_PAUSE_SEC = 600          # 暂停补仓 10 分钟
FLASH_CRASH_SPIKE_PCT = 0.03         # 反向插针也触发（上涨 3%）
DISTANCE_TO_DEFAULT_WARN_PCT = 0.02  # 距离强平/止损边界 < 2% 警告
DISTANCE_TO_DEFAULT_EXIT_PCT = 0.01  # 距离强平/止损边界 < 1% 暂停并撤单
DISTANCE_TO_DEFAULT_RESUME_PCT = 0.03  # 距离边界恢复到 3% 自动恢复
ATR_RESUME_RATIO = 0.7              # ATR 回落阈值（当前 ATR <= 上次暂停 ATR * 0.7）
MAX_TRADES_PER_DAY = 30      # 日最大交易次数 (层数增加，允许更多交易)
MONTHLY_TARGET = 300         # 月度目标
# 目标盈利规则：每 $1000 资金对应 $10 日目标
DAILY_TARGET_CAPITAL_STEP = 1000
DAILY_TARGET_PROFIT_STEP = 10
DAILY_TARGET_MIN = 10
# 日目标达成后是否停止交易（用户要求不停止）
DAILY_TARGET_STOP_ON_HIT = False
# 日目标超额（默认 200%）后放缓交易频率与减仓
DAILY_TARGET_SLOWDOWN_MULT = 2.0
DAILY_TARGET_SLOWDOWN_CAP_RATIO = 0.5
DAILY_TARGET_SLOWDOWN_GRID_MULT = 0.7
DAILY_TARGET_SLOWDOWN_TP_MULT = 1.15

# ════════════════════════════════════════════════════════════════════
# 💰 资金分配
# ════════════════════════════════════════════════════════════════════

GRID_CAPITAL_RATIO = 1.00
BUFFER_RATIO = 0.00
RESERVE_RATIO = 0.00
TREND_CAPITAL_RATIO = 0.25   # 趋势入场使用网格资金比例
GRID_FREE_CAP_RATIO = 0.95   # 可用保证金占用上限（提高挂单数量）
PLAN_CAPITAL_INCLUDE_ENTRY_MARGIN = True   # 规划资金时回补本机器人入场挂单冻结保证金，避免“挂单后即缩网格”
PLAN_CAPITAL_RESTORE_RATIO = 1.0           # 回补比例(0~1)，1.0=全额回补
PRESERVE_EXIT_ORDERS_ON_STARTUP = True     # 启动时仅撤入场单，保留已有 TP/SL 退出单
PRESERVE_EXIT_ORDERS_ON_SHUTDOWN = True    # 退出时仅撤入场单，保留已有 TP/SL 退出单
MARGIN_CAP_WARN_RATIO = 0.10  # 仅当网格资金缩减 >=10% 时输出 WARNING
MARGIN_CAP_WARN_COOLDOWN_SEC = 300  # 保证金缩减告警冷却期（秒）

# ════════════════════════════════════════════════════════════════════
# 🛡️ 风险管理配置
# ════════════════════════════════════════════════════════════════════

# 风险管理器
MAX_DAILY_LOSS = 50          # 单日最大损失 $50（小本金先止血）
MAX_DRAWDOWN = 0.30         # 最大回撤 30% (从20%提高到30%)
EMERGENCY_STOP_LOSS = 0.50  # 紧急止损 50% (从30%提高到50%)
EMERGENCY_COOLDOWN_SEC = 1800  # 紧急事件冷却期 30 分钟
POSITION_COOLDOWN_SEC = 900   # 过曝后持仓冷却期（秒）
MAX_REDEPLOY_MARGIN_RATIO = 0.50  # 自动重建网格前允许的最大保证金占比
ADAPTIVE_FORCE_CONSERVATIVE_MARGIN_RATIO = 0.50  # 超过该占比强制保守模式
MIN_LAYER_CAPITAL_USDT = 110  # 层数安全下限：可用资金/该值 得到最大层数（匹配 MIN_NOTIONAL=110）
MIN_REBUILD_INTERVAL_SEC = 300  # 硬重建节流：两次重建的最小间隔（缩短到5分钟，降低长时间卡重建）
STRATEGY_APPLY_LOCAL_REBUILD_COOLDOWN = False  # 关闭 strategy 内部冷却，统一由 main 执行层裁决
FILL_SCORE_BLOCK_THRESHOLD = 0.8  # 成交窗口保护：fill_score < 此值时禁止重建（铁律2）
FILL_SCORE_BLOCK_MIN_THRESHOLD = 0.30  # 长时间无成交时，成交窗口保护阈值可放宽到该下限
FILL_SCORE_BLOCK_RELAX_AFTER_SEC = 1800  # 达到该无成交时长后，阈值完成一次线性放宽
GRID_DRIFT_OVERRIDE_MULT = 1.5   # 价格漂移失效：drift > 1.5×ATR 时允许重建（网格已失效）
TP_REBUILD_CHANGE_THRESHOLD = 0.04  # TP 变化小于 4% 不触发重建（更贴近当前1.5%-16%波动）
TP_CHANGE_CONFIRM_ROUNDS = 3  # TP 变更需连续确认轮数（避免频繁“想重建”）
MAX_GRID_ATR_MULT = 2.0          # 最外层不超过 2.0×ATR（砍掉摆设层，集中资金）
MIN_GRID_N = 2                   # 最小层数保护（防止N塌缩成"双点交易"）
GRID_PROB_WEIGHTS_ENABLED = True # 按成交概率分配资金（内层多、外层少）
GRID_PROB_WEIGHT_K = 1.0        # 衰减系数 k，weight=exp(-k×fill_score)
GRID_PROB_WEIGHT_MIN = 0.05     # 最小权重下限（保证远层不为零）
GRID_TARGET_ZONE_ALLOC_ENABLED = True   # 启用目标区间资金峰值分配（近价中等/中段更多/尾端更少）
GRID_TARGET_ZONE_CENTER = 0.45          # 目标区间中心(0~1): 0=最近层, 1=最远层
GRID_TARGET_ZONE_SIGMA = 0.20           # 目标区间宽度，越小越集中
GRID_TARGET_ZONE_BOOST = 1.60           # 目标区间加权强度（提高，让中段层显著增配）
GRID_PROBE_ALLOC_MULT = 0.50            # 最近层资金倍率：压低最近层占比，避免过度集中
GRID_TAIL_ALLOC_MULT = 0.42             # 最远层资金倍率：降低尾端摆设资金
GRID_MAX_EXPOSURE_RATIO = 0.25  # 结构性敞口上限：全网格成交后总名义 ≤ equity×此值
GRID_MAX_WORST_DRAWDOWN = 0.15  # 最坏回撤上限：价格到最外层时浮亏 ≤ equity×此值
MAX_CONSECUTIVE_SAME_SIDE = 2   # 连续同方向入场成交上限（超过则阻断该方向）

# ── Binance 只读 API 韧性 ──
BINANCE_READ_API_HARD_FAIL_COOLDOWN_SEC = 120  # 4xx 等硬失败后的接口冷却，避免每轮打满重试
BALANCE_CACHE_MAX_AGE_SEC = 900  # 余额接口失败时，允许回退使用最近一次成功余额的最大年龄

# ── 连续成交 spacing 扩张 ──
CONSECUTIVE_FILL_SPACING_K = 0.20        # 每多一笔同向成交，间距增加 20%
CONSECUTIVE_FILL_SPACING_MAX_MULT = 3.0  # 最大扩张倍数
CONSECUTIVE_FILL_SPACING_THRESHOLD = 2   # 触发扩张的最低连续成交数
CONSECUTIVE_DECAY_SEC = 300              # 无成交每300秒衰减1级（spacing恢复呼吸）
CONSECUTIVE_FILL_WEIGHT_DAMPEN = 0.3     # 连续≥3时，概率权重k增大此倍数（降低Core集中度）
N_DOWNSIZE_REBUILD_COOLDOWN_SEC = 30   # 层数下降时更快重建
N_UPSIZE_REBUILD_COOLDOWN_SEC = 120    # 层数上调冷却缩短（更快进入高密度）
N_CHANGE_CONFIRM_ROUNDS = 2            # N 变更需连续确认轮数
N_SMALL_CHANGE_CONFIRM_ROUNDS = 2      # N 小幅变化确认轮数（从默认5降到2）
N_REBUILD_MIN_DIFF = 2                 # N 变化小于该值时默认不重建
N_REBUILD_CAPITAL_CHANGE_THRESHOLD = 0.15  # 若有效资金变化超过该比例，允许小幅N变更重建
# 有旧持仓时的保护模式：仅保留止盈/止损管理，不再挂新的网格入场单
# 默认关闭：允许同向分层加仓 + 分层止盈减仓
EXISTING_POSITION_TP_ONLY = False
POSITION_REBUILD_MAX_PER_HOUR = 4      # 有仓状态每小时最多允许全量重建次数（基础档从2提到4）
POSITION_REBUILD_ATR_RATIO_MID = 0.0045   # ATR/Price >= 该值，进入中波动档（匹配当前0.46%-0.50%）
POSITION_REBUILD_ATR_RATIO_HIGH = 0.0065  # ATR/Price >= 该值，进入高波动档
POSITION_REBUILD_MAX_PER_HOUR_MID = 6    # 中波动档每小时重建上限
POSITION_REBUILD_MAX_PER_HOUR_HIGH = 8   # 高波动档每小时重建上限
POSITION_HEDGE_OPPOSITE_LAYERS = 1     # 有仓时保留的反向对冲层数（按最远层优先）
POSITION_HEDGE_OPPOSITE_SIZE_MULT = 0.5  # 反向对冲层下单量缩放
POSITION_HEDGE_OPPOSITE_MAX_LAYERS = 2   # 反向对冲层硬上限
POSITION_HEDGE_NO_FILL_RELAX_AFTER_SEC = 3600  # 无成交超过1小时，自动放宽反向层数量
POSITION_HEDGE_NO_FILL_EXTRA_LAYERS = 1   # 触发放宽时额外允许的反向层数
POSITION_HEDGE_TP_BUFFER_RATIO = 0.0015   # 近TP保护带（0.15%）：保护带内反向层继续跳过
PRESERVE_ORDERS_WHEN_IN_POSITION = True  # 有仓且存在有效挂单时，优先保留挂单避免全撤全挂

# ════════════════════════════════════════════════════════════════════
# 🎛️ 三档交易模式（AI 自适应选择）
# ════════════════════════════════════════════════════════════════════
# 当前激活模式（启动默认，运行中由 AI / 决策引擎动态切换）
TRADING_MODE_ACTIVE = "normal"
# AI 模式选择开关：True=由 AI 根据行情自动选档，False=固定用上面的值
TRADING_MODE_AI_SELECT = True
# 手动模式覆盖：为空=自动换挡；设置为 conservative/normal/aggressive 时强制锁定该档
TRADING_MODE_MANUAL_OVERRIDE = ""
# 模式切换最小间隔（秒），防抖
TRADING_MODE_SWITCH_COOLDOWN_SEC = 1800
# 回测间隔（秒），每 4 小时回测一次模式表现
TRADING_MODE_BACKTEST_INTERVAL_SEC = 14400
# ── AI 模式选择分阶段门槛 ──
# 阶段1(冷启动): 样本 < PHASE2 → 纯规则选模式
# 阶段2(观察期): PHASE2 ≤ 样本 < PHASE3 → 规则 + AI shadow（记录不执行）
# 阶段3(试运行): PHASE3 ≤ 样本 < PHASE4 且胜率≥50% → AI 仅 normal↔其他
# 阶段4(全权):   样本 ≥ PHASE4 且单模式≥200且胜率≥52% → AI 自由切换
TRADING_MODE_PHASE2_SAMPLES = 300       # 进入观察期的最低有效样本数
TRADING_MODE_PHASE3_SAMPLES = 800       # 进入试运行的最低有效样本数
TRADING_MODE_PHASE4_SAMPLES = 1500      # 进入全权的最低有效样本数
TRADING_MODE_PHASE3_WIN_RATE = 0.50     # 试运行门槛胜率
TRADING_MODE_PHASE4_WIN_RATE = 0.52     # 全权门槛胜率
TRADING_MODE_PHASE4_MIN_PER_MODE = 200  # 全权要求每个模式的最低样本数

# ── 日度学习系统 (DailyLearner) ──
DAILY_LEARNER_BACKTEST_DAYS = 7          # 每次回测使用的天数
DAILY_LEARNER_GATE_DAYS = 100            # AI 模式切换解锁需要的最低天数
DAILY_LEARNER_GATE_WIN_RATE = 0.60       # AI 模式切换解锁需要的最低推荐准确率
DAILY_LEARNER_GATE_MIN_AI_RECS = 30      # 解锁前 Qwen 推荐最少样本数
DAILY_LEARNER_MIN_SWITCH_INTERVAL_SEC = 86400  # 模式切换最小间隔（24小时）
DAILY_LEARNER_TRIGGER_HOUR = 4           # 每日触发时间（凌晨4点）
DAILY_LEARNER_ENABLED = True             # 是否启用日度学习

# ── 三态可信度执行层 ──
LEAD_WATCH_CAPITAL_SCALE = 0.35
LEAD_PROBE_CAPITAL_SCALE = 0.15
LEAD_CONFIRM_CAPITAL_SCALE = 0.55
LEAD_WATCH_MAJOR_RATIO = 0.65
LEAD_PROBE_GRID_N_MAX = 2
LEAD_CONFIRM_GRID_N_MAX = 3
LEAD_PROBE_PULLBACK_ATR = 0.45
LEAD_PROBE_BREAKOUT_ATR = 0.10
LEAD_PROBE_BREAKOUT_NEAR_PCT = 0.0015

TRADING_MODES = {
    # ── 保守模式：低波动 / 震荡窄幅 / 高回撤阶段 ──
    "conservative": {
        "leverage": 2,
        "grid_num": 5,
        "upper_pct": 0.015,
        "lower_pct": 0.015,
        "tp_per_grid": 0.007,
        "dynamic_tp_min": 0.005,
        "dynamic_tp_max": 0.020,
        "stop_loss_pct": 0.03,
        "grid_max_exposure_ratio": 0.15,
        "max_redeploy_margin_ratio": 0.40,
        "position_cooldown_sec": 600,
        "flow_tighten_after_sec": 900,
        "flow_aggressive_after_sec": 2700,
        "fill_score_block_threshold": 0.72,
        "fill_score_block_min_threshold": 0.30,
        "fill_score_block_relax_after_sec": 1200,
        "meta_trend_bias_prob_threshold": 0.60,
        "sentry_normal_n_max": 7,
        "max_consecutive_same_side": 2,
        "position_rebuild_max_per_hour": 4,
        "position_rebuild_max_per_hour_mid": 6,
        "position_rebuild_max_per_hour_high": 8,
        "position_hedge_opposite_layers": 1,
        "position_hedge_opposite_max_layers": 2,
        "position_hedge_no_fill_relax_after_sec": 1800,
        "position_hedge_no_fill_extra_layers": 1,
        "position_hedge_tp_buffer_ratio": 0.0012,
        # 趋势动作下的主动首单（市价）参数：只在无仓时触发
        "active_entry_enabled": True,
        "active_entry_margin_ratio": 0.020,       # 首单目标占用保证金（占总权益）
        "active_entry_adx_min": 30.0,             # 趋势强度门槛
        "active_entry_atr_ratio_min": 0.006,      # 波动下限（ATR/Price）
        "active_entry_atr_ratio_max": 0.018,      # 波动上限（避免极端冲击）
        "active_entry_cooldown_sec": 3600,        # 首单触发冷却
        "active_entry_max_margin_ratio": 0.32,    # 账户保证金占比上限
        "active_entry_max_notional_ratio": 0.12,  # 首单名义价值上限（占 equity*L）
        "active_entry_min_notional_usdt": 130,    # 首单最小名义价值
        "description": "低杠杆、宽TP、紧margin，适合窄幅震荡或高回撤期",
    },
    # ── 普通模式：标准行情 ──
    "normal": {
        "leverage": 3,
        "grid_num": 7,
        "upper_pct": 0.020,
        "lower_pct": 0.020,
        "tp_per_grid": 0.006,
        "dynamic_tp_min": 0.005,
        "dynamic_tp_max": 0.025,
        "stop_loss_pct": 0.05,
        "grid_max_exposure_ratio": 0.25,
        "max_redeploy_margin_ratio": 0.50,
        "position_cooldown_sec": 600,
        "flow_tighten_after_sec": 900,
        "flow_aggressive_after_sec": 2700,
        "fill_score_block_threshold": 0.60,
        "fill_score_block_min_threshold": 0.25,
        "fill_score_block_relax_after_sec": 600,
        "meta_trend_bias_prob_threshold": 0.55,
        "sentry_normal_n_max": 10,
        "max_consecutive_same_side": 2,
        "position_rebuild_max_per_hour": 6,
        "position_rebuild_max_per_hour_mid": 8,
        "position_rebuild_max_per_hour_high": 12,
        "position_hedge_opposite_layers": 1,
        "position_hedge_opposite_max_layers": 2,
        "position_hedge_no_fill_relax_after_sec": 1200,
        "position_hedge_no_fill_extra_layers": 2,
        "position_hedge_tp_buffer_ratio": 0.0010,
        # 趋势动作下的主动首单（市价）参数：只在无仓时触发
        "active_entry_enabled": True,
        "active_entry_margin_ratio": 0.030,
        "active_entry_adx_min": 27.0,
        "active_entry_atr_ratio_min": 0.005,
        "active_entry_atr_ratio_max": 0.020,
        "active_entry_cooldown_sec": 2400,
        "active_entry_max_margin_ratio": 0.36,
        "active_entry_max_notional_ratio": 0.16,
        "active_entry_min_notional_usdt": 130,
        "description": "均衡参数，适合标准波动行情",
    },
    # ── 激进模式：高波动 / 趋势明确 / 单边行情 ──
    "aggressive": {
        "leverage": 5,
        "grid_num": 10,
        "upper_pct": 0.030,
        "lower_pct": 0.030,
        "tp_per_grid": 0.005,
        "dynamic_tp_min": 0.005,
        "dynamic_tp_max": 0.030,
        "stop_loss_pct": 0.08,
        "grid_max_exposure_ratio": 0.35,
        "max_redeploy_margin_ratio": 0.55,
        "position_cooldown_sec": 300,
        "flow_tighten_after_sec": 900,
        "flow_aggressive_after_sec": 2700,
        "fill_score_block_threshold": 0.55,
        "fill_score_block_min_threshold": 0.25,
        "fill_score_block_relax_after_sec": 600,
        "meta_trend_bias_prob_threshold": 0.50,
        "sentry_normal_n_max": 12,
        "max_consecutive_same_side": 3,
        "position_rebuild_max_per_hour": 8,
        "position_rebuild_max_per_hour_mid": 12,
        "position_rebuild_max_per_hour_high": 16,
        "position_hedge_opposite_layers": 2,
        "position_hedge_opposite_max_layers": 3,
        "position_hedge_no_fill_relax_after_sec": 900,
        "position_hedge_no_fill_extra_layers": 2,
        "position_hedge_tp_buffer_ratio": 0.0007,
        # 趋势动作下的主动首单（市价）参数：只在无仓时触发
        "active_entry_enabled": True,
        "active_entry_margin_ratio": 0.050,
        "active_entry_adx_min": 24.0,
        "active_entry_atr_ratio_min": 0.0045,
        "active_entry_atr_ratio_max": 0.025,
        "active_entry_cooldown_sec": 1200,
        "active_entry_max_margin_ratio": 0.40,
        "active_entry_max_notional_ratio": 0.22,
        "active_entry_min_notional_usdt": 130,
        "description": "高杠杆、密网格、快节奏，适合高波动或趋势行情",
    },
}

def get_mode_params(mode=None):
    """获取指定模式的参数字典，默认返回当前激活模式"""
    mode = mode or TRADING_MODE_ACTIVE
    return TRADING_MODES.get(mode, TRADING_MODES["normal"])


def get_manual_mode_override():
    """返回手动覆盖模式（无覆盖时返回空字符串）。"""
    mode = str(globals().get("TRADING_MODE_MANUAL_OVERRIDE", "") or "").strip().lower()
    return mode if mode in TRADING_MODES else ""

# ════════════════════════════════════════════════════════════════════
# 🛡️ 三维风控 (MarketSentry + GridCoordinator)
# ════════════════════════════════════════════════════════════════════

SENTRY_VIOLENT_PCT = 0.008          # |变化| >= 0.8% 判定为剧烈波动
SENTRY_MEMORY_CANDLES = 24          # 12H 窗口 = 24 根 30min K线
SENTRY_MIN_ORDER_USDT = 110         # 单笔硬性底线（下调到110提升可挂层数）

# NORMAL 状态: 全速进攻
SENTRY_NORMAL_N_MIN = 6
SENTRY_NORMAL_N_MAX = 10

# WARNING 状态: 历史警觉
SENTRY_WARNING_N_MIN = 4
SENTRY_WARNING_N_MAX = 6
SENTRY_WARNING_INTERVAL_MULT = 1.5  # 间距拉宽 1.5x

# EMERGENCY 状态: 紧急避险
SENTRY_EMERGENCY_N_MIN = 2
SENTRY_EMERGENCY_N_MAX = 3
SENTRY_EMERGENCY_INTERVAL_MULT = 2.5  # 间距拉宽 2.5x

# 非对称防御
SENTRY_ASYMMETRIC_WIDEN = 1.8       # 危险侧拉宽倍率
SENTRY_ASYMMETRIC_TIGHTEN = 0.7     # 退出侧收紧倍率

# 断路器
CIRCUIT_BREAKER_MAX_API_FAILURES = 5      # 连续API失败次数
CIRCUIT_BREAKER_MAX_LOSS_PER_HOUR = 5     # 每小时最大亏损笔数（放宽，避免小额噪音触发暂停）
CIRCUIT_BREAKER_MIN_LOSS_USDT = 0.80      # 小于该净亏损不计入“亏损笔数”（过滤手续费级噪音）
CIRCUIT_BREAKER_MAX_LATENCY_MS = 2000     # 最大延迟ms
CIRCUIT_BREAKER_RAPID_LOSS_PCT = 0.05     # 5分钟快速下跌5%触发熔断

# Binance 读取接口稳定性参数（仅用于查询，不影响下单幂等）
BINANCE_HTTP_TIMEOUT_MS = 20000
BINANCE_RECV_WINDOW_MS = 20000
BINANCE_READ_API_RETRIES = 5
BINANCE_READ_API_BACKOFF_SEC = 1.0
BINANCE_TIME_SYNC_RETRY_COOLDOWN_SEC = 30
PNL_SUMMARY_CACHE_TTL_SEC = 600      # 盈亏汇总缓存秒数，降低主循环对 income API 压力
ORDER_STATUS_MISS_SYNC_EVERY = 3     # 订单状态连续查询失败时，每 N 次触发一次状态同步
# NO_FILL_* 已迁移到 FLOW_*（见下方兼容层映射），避免双参数源冲突
# 成交后即时放宽：不等待无成交阈值与重建节流
FILL_INSTANT_WIDEN_ENABLED = True
FILL_INSTANT_WIDEN_MULT = 1.08       # 成交后本轮立即放宽 8%
FILL_INSTANT_WIDEN_MAX_RANGE_PCT = 0.05  # 即时放宽范围上限

# 成交驱动网格（Flow-Driven Grid）
FLOW_FILL_WINDOW_SEC = 1800          # recent_fill 统计窗口（30分钟）
FLOW_TIGHTEN_AFTER_SEC = 900         # 无成交15分钟 -> tight
FLOW_AGGRESSIVE_AFTER_SEC = 2700     # 无成交45分钟 -> aggressive
FLOW_FORCE_REBUILD_AFTER_SEC = 7200  # 无成交120分钟 -> 强制重建并重置到 aggressive
FLOW_FORCE_REBUILD_COOLDOWN_SEC = 1800  # 强制重建冷却（15min -> 30min）
FLOW_FILLS_TO_NORMAL = 2             # 近期成交>=2：aggressive->tight / tight->normal
FLOW_FILLS_TO_WIDE = 4               # 近期成交>=4：进入 wide
FLOW_AGGRESSIVE_MAX_POSITION_RATIO = 0.50  # 仓位占比过高时禁用 aggressive

FLOW_MIN_GRID_N = 3
FLOW_MAX_GRID_N = 12
FLOW_MIN_RANGE_PCT = 0.012
FLOW_MAX_RANGE_PCT = 0.05
FLOW_MIN_SPACING_ATR_MULT = 0.30     # spacing 下限 = ATR/Price * 系数

FLOW_WIDE_N_MULT = 0.70
FLOW_WIDE_SPACING_MULT = 1.50
FLOW_WIDE_TP_MULT = 1.20
FLOW_WIDE_SL_MULT = 0.95

FLOW_NORMAL_N_MULT = 1.00
FLOW_NORMAL_SPACING_MULT = 1.00
FLOW_NORMAL_TP_MULT = 1.00
FLOW_NORMAL_SL_MULT = 1.00

FLOW_TIGHT_N_MULT = 1.15
FLOW_TIGHT_SPACING_MULT = 0.70
FLOW_TIGHT_TP_MULT = 0.90
FLOW_TIGHT_SL_MULT = 1.05

FLOW_AGGR_N_MULT = 1.20
FLOW_AGGR_SPACING_MULT = 0.60
FLOW_AGGR_TP_MULT = 0.85
FLOW_AGGR_SL_MULT = 1.10

# 长无成交分档策略（15/45/90 分钟）
FLOW_STAGE1_AFTER_SEC = 900           # 15 分钟
FLOW_STAGE2_AFTER_SEC = 2700          # 45 分钟
FLOW_STAGE3_AFTER_SEC = 5400          # 90 分钟
FLOW_STAGE1_SPACING_MULT = 0.90       # 轻微收窄
FLOW_STAGE2_SPACING_MULT = 0.82       # 中度收窄
FLOW_STAGE3_SPACING_MULT = 0.74       # 明显收窄
FLOW_STAGE1_N_ADD = 0                 # N 不降
FLOW_STAGE2_N_ADD = 1                 # N 小幅增加
FLOW_STAGE3_N_ADD = 1                 # N 再增加
FLOW_STAGE1_TP_MULT = 0.95            # tp 轻微下调
FLOW_STAGE2_TP_MULT = 0.90
FLOW_STAGE3_TP_MULT = 0.85

FLOW_TP_MIN = 0.005
FLOW_TP_MAX = 0.030
FLOW_SL_MIN = 0.003
FLOW_SL_MAX = 0.08

# 兼容层（Deprecated）：NO_FILL_* 统一映射到 FLOW_*（单一真源）
NO_FILL_WIDEN_ENABLED = True
NO_FILL_WIDEN_AFTER_SEC = FLOW_TIGHTEN_AFTER_SEC
NO_FILL_WIDEN_STEP_SEC = 900
NO_FILL_WIDEN_MULT = FLOW_WIDE_SPACING_MULT
NO_FILL_WIDEN_MAX_STEPS = 8
NO_FILL_WIDEN_MAX_RANGE_PCT = FLOW_MAX_RANGE_PCT
NO_FILL_ADAPTIVE_CAPITAL_MODE = True
NO_FILL_CAPITAL_TIGHT_THRESHOLD = 0.90
NO_FILL_CAPITAL_WIDEN_THRESHOLD = 1.40
NO_FILL_TIGHTEN_MULT = FLOW_TIGHT_SPACING_MULT
NO_FILL_TIGHTEN_MIN_RANGE_PCT = FLOW_MIN_RANGE_PCT
NO_FILL_FORCE_REBUILD_SEC = FLOW_FORCE_REBUILD_AFTER_SEC
NO_FILL_FORCE_REBUILD_COOLDOWN_SEC = FLOW_FORCE_REBUILD_COOLDOWN_SEC
NO_FILL_DENSIFY_ENABLED = False
NO_FILL_DENSIFY_AFTER_SEC = FLOW_TIGHTEN_AFTER_SEC
NO_FILL_DENSIFY_N_PER_STEP = 1
NO_FILL_MIN_GRID_N = FLOW_MIN_GRID_N
NO_FILL_MAX_GRID_N = FLOW_MAX_GRID_N

# Flow Bias AI（阶段2：AI 影响 flow 切换时机）
FLOW_BIAS_ENABLED = False             # 暂停 flow_bias AI（先回到规则主导）
FLOW_BIAS_MIN_AI_WEIGHT = 0.08        # ai_weight 低于该值时，flow_bias 不生效（语义一致：AI静默）
FLOW_BIAS_FUZZY_ADX_LOW = 20.0        # 模糊区下界（AI可增强）
FLOW_BIAS_FUZZY_ADX_HIGH = 30.0       # 模糊区上界（AI可增强）
FLOW_BIAS_FUZZY_BONUS_WEIGHT = 0.05   # tight/aggressive 且模糊区时，给 AI 的额外权重
FLOW_BIAS_MAX_SEC = 600               # AI 最大偏移秒数（±600s）
FLOW_BIAS_HYSTERESIS = 0.15           # 滞回死区：|bias| < 0.15 时不偏移
FLOW_BIAS_TIGHTEN_CLAMP = (300, 3600)   # tight 阈值 clamp 范围
FLOW_BIAS_AGGRESSIVE_CLAMP = (600, 7200) # aggressive 阈值 clamp 范围
FLOW_BIAS_INERTIA = 0.7                  # EMA 惯性系数：0.7=70%保留上轮，减少抖动
FLOW_BIAS_TIME_WEIGHT = 0.6           # flow_bias 标签：成交速度权重
FLOW_BIAS_PNL_WEIGHT = 0.4            # flow_bias 标签：盈利质量权重
FLOW_BIAS_EXPECTED_PNL_PCT = 0.002    # 期望单次成交收益（占权益比例）
FLOW_REBUILD_WITH_POSITION = False    # 档位变化时，持仓中是否也允许触发重建

# 成交窗口保护：从“硬阻止”改为“超时后限幅放行”
FILL_SCORE_SOFT_RELEASE_AFTER_SEC = 600    # 无成交达到该时长后可限幅放行
FILL_SCORE_SOFT_RELEASE_MAX_CHANGE = 0.50  # 放行时单次参数最大变更比例
FILL_SCORE_HARD_BLOCK_THRESHOLD = 0.20     # 低于该 fill_score 且无成交不久时仍硬阻止

# 插针防守（Spike Guard）
SPIKE_GUARD_ENABLED = True
SPIKE_GUARD_TRIGGER_PCT = 0.008      # 触发针刺防守阈值（0.8%）
SPIKE_GUARD_REDUCE_PCT = 0.012       # 触发主动减仓阈值（1.2%）
SPIKE_GUARD_EMERGENCY_PCT = 0.018    # 触发强制平仓阈值（1.8%）
SPIKE_GUARD_PAUSE_SEC = 900          # 轻度针刺暂停新入场时长
SPIKE_GUARD_REDUCE_TARGET_MARGIN = 0.35  # 中度针刺减仓目标保证金占比
SPIKE_GUARD_EMERGENCY_COOLDOWN_SEC = 1800  # 重度针刺强平后的冷却时长
SPIKE_GUARD_NOTICE_COOLDOWN_SEC = 300  # 通知节流（秒）

# ⚡ 快通道风控（User Data Stream 实时成交检测）
FAST_POSITION_SPIKE_RATIO = 1.5       # 仓位突增阈值（当前仓位 > 基准 × 1.5 时触发）
FAST_FILL_CLUSTER_COUNT = 3           # 成交簇：N 秒内同方向加仓成交笔数阈值
FAST_FILL_CLUSTER_WINDOW = 10.0       # 成交簇：时间窗口（秒）
FAST_ONE_SIDE_COUNT = 3               # 单边累积：同方向成交笔数阈值
FAST_ONE_SIDE_WINDOW = 60.0           # 单边累积：时间窗口（秒）

# 资金动态放松（仅在资金评分高时，允许更低单笔名义，提升可部署层数）
MIN_ORDER_NOTIONAL_EXCHANGE_FLOOR = 100   # 交易所名义价值硬底线
DYNAMIC_MIN_ORDER_ENABLED = True
DYNAMIC_MIN_ORDER_RELAX_SCORE = 2.0       # 资金评分高于该值开始放松
DYNAMIC_MIN_ORDER_RELAX_MAX_RATIO = 0.20  # 最大放松幅度（20%）
DYNAMIC_MIN_ORDER_ABS_FLOOR = 105         # 绝对下限（仍高于交易所100）

# AI 动态权重（防低成交阶段负反馈）
AI_WEIGHT_FLOOR = 0.04               # AI 权重下限（观察模式）
AI_WEIGHT_BASE_CAP = 0.08            # 默认上限（未通过门控前，降低AI干扰）
AI_WEIGHT_MAX = 0.50                 # 最高上限（持续优秀后可放权至 50%）
AI_WEIGHT_UP_STEP = 0.06             # 每轮最大上调步长（上升快）
AI_WEIGHT_DOWN_STEP = 0.02           # 每轮最大下调步长（下降慢）
AI_WEIGHT_BOOTSTRAP_MIN_RECORDS = 10  # 冷启动最低记录数
AI_WEIGHT_BOOTSTRAP_VALUE = 0.08     # 冷启动权重（低干预）
AI_EFFECTIVE_MIN_SAMPLES = 25        # 进入稳定评估所需有效样本数
AI_EFFECTIVE_PNL_POSITIVE = 0.0      # 判定“盈利样本”的 PnL 门槛
AI_EFFECTIVE_PNL_UNIT = 1.0          # 平均 PnL KPI 标定单位（USDT）
AI_ALLOW_GRID_N_ADJUST = False       # 仅允许 AI 微调低风险参数（默认禁止改 N）
AI_ALLOW_SPACING_ADJUST = True       # 允许 AI 微调 spacing
AI_STAGNATION_GUARD_ENABLED = True   # AI 输出退化保护
AI_STAGNATION_WINDOW = 40            # 观察窗口（轮）
AI_STAGNATION_DOMINANCE = 0.75       # 单一输出占比超过阈值则视为退化
AI_STAGNATION_MIN_WEIGHT = 0.10      # 仅在 AI 权重大于该值时触发降权
AI_GATE_ENABLED = True               # 三项门控：净利润/最大回撤/手续费占比
AI_GATE_WINDOW = 120                 # 门控评估窗口
AI_GATE_ACTIVE_WEIGHT = 0.08         # ai_weight ≥ 该值计为 AI 组样本
AI_GATE_MIN_ACTIVE_SAMPLES = 20      # AI 组最小有效样本
AI_GATE_MIN_BASELINE_SAMPLES = 20    # 基线组最小有效样本
AI_GATE_NET_MARGIN = 0.0             # 净利润优于基线的最小边际
AI_GATE_DD_MARGIN = 0.0              # 最大回撤优于基线的最小边际
AI_GATE_FEE_MARGIN = 0.0             # 手续费占比优于基线的最小边际
AI_RELEASE_STREAK_REQUIRED = 3       # 连续通过门控 N 次后提升可用上限
AI_RELEASE_CAP_STEP = 0.05           # 每次放权提升的上限幅度
AI_LABEL_REQUIRE_FILL = True         # 训练标签默认仅使用有成交样本（避免无成交污染）
AI_LABEL_PNL_SCALE_PCT = 0.01        # pnl 归一化尺度（按权益百分比）
AI_LABEL_PNL_WEIGHT = 1.0            # 奖励函数：pnl 权重
AI_LABEL_DRAWDOWN_WEIGHT = 0.7       # 奖励函数：回撤惩罚权重
AI_LABEL_FILL_WEIGHT = 0.3           # 奖励函数：成交率奖励权重
AI_LABEL_HOLD_PENALTY_WEIGHT = 0.1   # 奖励函数：长时间无成交惩罚
AI_LABEL_MIN_ATR_RATIO = 0.001       # 最低波动率过滤：低于此值的样本视为无效市场状态
AI_DATA_EVENT_DRIVEN_ONLY = True            # 仅事件驱动采样（成交/仓位变化），抑制轮询噪声
AI_DATA_POSITION_DELTA_MIN = 1e-6           # 仓位变化判定阈值（BTC）
AI_TRAIN_SCHEMA_VERSION = 17                # 当前训练样本 schema 版本
AI_TRAIN_ACCEPT_LEGACY_SCHEMA = False       # 是否允许旧 schema 进入训练
AI_TRAIN_MIN_CLEAN_SAMPLES_TRIGGER = 80     # AutoTrainer 触发前最低净样本数
AI_TRAIN_MIN_INTERVAL_SEC = 3600            # 两次成功训练的最短间隔
AI_TRAIN_SAMPLE_FLOOR_BASE = 80             # 基础训练样本门槛
AI_TRAIN_SAMPLE_FLOOR_MID = 120             # 中段样本门槛
AI_TRAIN_SAMPLE_FLOOR_HIGH = 160            # 高段样本门槛
AI_TRAIN_REQUIRE_FULL_FEATURES = True      # 训练仅接受完整特征维度（默认强制17维）
AI_TRAIN_MIN_FILLED_RATIO = 0.03           # 原始样本中 had_fill 最低占比（低于则拒训）
AI_TRAIN_MAX_ZERO_PNL_RATIO = 0.80         # 原始样本中 pnl=0 的最高占比
AI_TRAIN_MIN_NEGATIVE_SAMPLES = 8          # 训练集最低亏损样本数（防止只学盈利）
AI_TRAIN_MIN_POSITIVE_SAMPLES = 8          # 训练集最低盈利样本数
AI_TRAIN_MIN_SIDE_RATIO = 0.12             # 正负标签最小占比（动态）
AI_TRAIN_LABEL_CENTER_ENABLED = True       # 训练前按中位数中心化 score，避免单边标签
AI_TRAIN_LABEL_CENTER_MIN_SAMPLES = 40     # 样本数达到后才启用 score 中心化
AI_TRAIN_LABEL_CENTER_IMBALANCE_RATIO = 0.20  # 单边占比低于该值时触发中心化
AI_TRAIN_STRICT_QUALITY_GATE = True        # 启用训练前质量硬拦截

# 本地 Qwen（Ollama）偏置层
QWEN_ENABLED = False
QWEN_OLLAMA_URL = "http://127.0.0.1:11434"
# 可选模型: "qwen3:8b", "qwen2.5:7b"
QWEN_MODEL = "qwen2.5:7b-instruct-q4_K_M"
QWEN_TIMEOUT_SEC = 25.0               # 冷启动+高负载时更稳，避免 12s 频繁超时
QWEN_KEEP_ALIVE = "30m"              # 保持模型常驻，减少重复装载延迟
QWEN_RETRY_COUNT = 1                  # 超时/短暂连接异常时自动重试 1 次
QWEN_INTERVAL_SEC = 900              # 至少 15 分钟更新一次，避免高频调用
QWEN_MIN_CONFIDENCE = 0.40
QWEN_ADX_FUZZY_LOW = 20.0
QWEN_ADX_FUZZY_HIGH = 30.0
QWEN_GRID_N_MULT_BOUNDS = (0.90, 1.15)
QWEN_SPACING_MULT_BOUNDS = (0.85, 1.20)
QWEN_FLOW_BIAS_BOUNDS = (-0.60, 0.60)
QWEN_BLEND_RATIO = 0.15              # 双通道融合中 Qwen 的最大混合强度（低）
QWEN_MAX_WEIGHT = 0.05               # Qwen 权重上限（低影响）
QWEN_WEIGHT_CONF_SCALE = 0.10        # 置信度到权重的缩放
QWEN_TEMPERATURE = 0.15
QWEN_TOP_P = 0.85
QWEN_NUM_CTX = 2048

# ── Qwen 模式推荐配置 ──
QWEN_MODE_ENABLED = True                 # 是否启用 Qwen 模式推荐（需 QWEN_ENABLED=True）
QWEN_MODE_TIMEOUT_SEC = 30.0             # 模式推荐 prompt 更长，给足时间避免超时
QWEN_MODE_RETRY_COUNT = 1                # 模式推荐异常自动重试 1 次
QWEN_MODE_MIN_CONFIDENCE = 0.50          # 最低置信度
QWEN_MODE_NUM_CTX = 4096                 # 上下文窗口
QWEN_MODE_TEMPERATURE = 0.3             # 温度（稍高，允许更多推理）

# ── 市场分析（新闻过滤 + 恐惧指数 + 24h方向） ──
MARKET_ANALYSIS_ENABLED = True
MARKET_ANALYSIS_REFRESH_SEC = 600        # 结果缓存秒数（避免每轮都请求新闻/LLM）
MARKET_ANALYSIS_NEWS_TOP_K = 5

MARKET_ANALYSIS_QWEN_ENABLED = True      # 需 QWEN_ENABLED=True 才会生效
MARKET_ANALYSIS_QWEN_BLEND = 0.25        # Qwen 对方向概率的最大混合系数
MARKET_ANALYSIS_QWEN_TIMEOUT_SEC = 25.0
MARKET_ANALYSIS_QWEN_RETRY_COUNT = 1
MARKET_ANALYSIS_QWEN_TEMPERATURE = 0.2
MARKET_ANALYSIS_QWEN_NUM_CTX = 4096

MARKET_NEWS_ENABLED = True
MARKET_NEWS_REFRESH_SEC = 300
MARKET_NEWS_MAX_ITEMS = 20
MARKET_NEWS_SOURCE_URLS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
)

# ════════════════════════════════════════════════════════════════════
# 🧠 Decision Engine（9.0 新增）
# ════════════════════════════════════════════════════════════════════

TREND_CONFIRM_ROUNDS = 2   # 趋势确认所需连续轮数
RANGE_CONFIRM_ROUNDS = 2   # 震荡确认所需连续轮数
N_HYSTERESIS_BAND = 0.35   # N 变更滞回带（防 3↔4 抖动）

# ════════════════════════════════════════════════════════════════════
# 📊 分层止盈配置
# ════════════════════════════════════════════════════════════════════

TIERED_TP_ENABLED = True
TIERED_TP_LEVELS = [
    {"pct": 0.005, "ratio": 0.4},   # 第1层：0.5% 止盈，平40%仓位（快速锁利）
    {"pct": 0.010, "ratio": 0.35},   # 第2层：1.0% 止盈，平35%仓位（提高成交频率）
    {"pct": 0.020, "ratio": 0.25},   # 第3层：2.0% 止盈，平剩余25%（保留趋势收益）
]
TRAILING_STOP_ENABLED = True
TRAILING_STOP_ACTIVATION_PCT = 0.02  # 浮盈达2%时激活移动止盈
TRAILING_STOP_CALLBACK_PCT = 0.008   # 回撤0.8%触发平仓

# ════════════════════════════════════════════════════════════════════
# 💹 资金费率套利
# ════════════════════════════════════════════════════════════════════

FUNDING_ARB_ENABLED = True
FUNDING_ARB_THRESHOLD = 0.0005    # 费率超过0.05%时触发
FUNDING_ARB_MAX_CAPITAL_PCT = 0.2  # 最多用20%资金套利
FUNDING_ARB_MIN_EXPECTED_PROFIT = 0.5  # 最小预期利润$0.5
FUNDING_GRID_FILTER_ENABLED = True
FUNDING_RATE_ANNUALIZATION = 1095     # 8小时结算 * 365
EXPECTED_GRID_YIELD_DAILY = 0.006     # 预期日收益 0.6%
FUNDING_RATE_BIAS_THRESHOLD = 0.0002  # 资金费率方向偏置阈值
FUNDING_RATE_REFRESH_SEC = 300
EXIT_LARGE_POSITION_USDT = 5000       # 超过该名义价值使用 TWAP
TWAP_SLICES = 5                       # TWAP 分片数
TWAP_INTERVAL_SEC = 5                 # 分片间隔秒数

# ════════════════════════════════════════════════════════════════════
# 📖 订单簿分析
# ════════════════════════════════════════════════════════════════════

ORDERBOOK_ENABLED = False
ORDERBOOK_DEPTH_LIMIT = 20
ORDERBOOK_WALL_THRESHOLD = 2.0  # 挂单量超过平均值2倍视为大单

# ════════════════════════════════════════════════════════════════════
# 🧪 回测参数
# ════════════════════════════════════════════════════════════════════

BACKTEST_START_DATE = "2024-01-01"
BACKTEST_END_DATE = "2024-12-31"
BACKTEST_INITIAL_CAPITAL = 1000

# ════════════════════════════════════════════════════════════════════
# 🎛️ 运行模式

# 运行模式：paper / live
TRADING_MODE = os.getenv("TRADING_MODE", "paper").lower()

# Paper 模式配置
PAPER_INITIAL_CAPITAL = 5000  # 模拟资金
PAPER_CLEAR_STATE_ON_STARTUP = os.getenv(
    "PAPER_CLEAR_STATE_ON_STARTUP", "1"
).strip().lower() in ("1", "true", "yes", "on")

# 仓位乘数框架（叠加在 conservative / normal / aggressive 之上）
POSITION_FRAMEWORK_ENABLED = os.getenv(
    "POSITION_FRAMEWORK_ENABLED", "1"
).strip().lower() in ("1", "true", "yes", "on")
POSITION_FRAMEWORK_VOL_LOW_MAX = 0.0025
POSITION_FRAMEWORK_VOL_SWEET_MAX = 0.0080
POSITION_FRAMEWORK_VOL_HIGH_MAX = 0.0120
POSITION_FRAMEWORK_MULT_LOW = 0.90
POSITION_FRAMEWORK_MULT_SWEET = 1.35
POSITION_FRAMEWORK_MULT_HIGH = 0.75
POSITION_FRAMEWORK_MULT_EXTREME = 0.25
POSITION_FRAMEWORK_LOSS_STREAK_THRESHOLD = 2
POSITION_FRAMEWORK_LOSS_STREAK_MAX_MULT = 0.70
POSITION_FRAMEWORK_DAILY_SOFT_LOCK_PCT = 0.02
POSITION_FRAMEWORK_DAILY_SOFT_LOCK_MULT = 0.60
POSITION_FRAMEWORK_DAILY_HARD_LOCK_PCT = 0.03
POSITION_FRAMEWORK_LOG_COOLDOWN_SEC = 180

# AI 数据/模型保洁
AI_REGISTRY_AUTO_REPAIR = os.getenv(
    "AI_REGISTRY_AUTO_REPAIR", "1"
).strip().lower() in ("1", "true", "yes", "on")
AI_JANITOR_ENABLED = os.getenv(
    "AI_JANITOR_ENABLED", "1"
).strip().lower() in ("1", "true", "yes", "on")
AI_JANITOR_INTERVAL_SEC = int(os.getenv("AI_JANITOR_INTERVAL_SEC", "21600"))
AI_JANITOR_REWRITE_RAW = os.getenv(
    "AI_JANITOR_REWRITE_RAW", "1"
).strip().lower() in ("1", "true", "yes", "on")

# ════════════════════════════════════════════════════════════════════
# ✅ 参数验证
# ════════════════════════════════════════════════════════════════════

def validate_config():
    errors = []
    
    if GRID_NUM < 1:
        errors.append("GRID_NUM 必须 >= 1")
    
    if LEVERAGE < 1 or LEVERAGE > 125:
        errors.append("LEVERAGE 必须在 1-125 之间")
    
    if not (0 < TP_PER_GRID < 0.2):
        errors.append("TP_PER_GRID 应在 0-20% 之间")
    
    if TOTAL_CAPITAL <= 0:
        errors.append("TOTAL_CAPITAL 必须 > 0")
    
    total_ratio = GRID_CAPITAL_RATIO + BUFFER_RATIO + RESERVE_RATIO
    if abs(total_ratio - 1.0) > 0.01:
        errors.append(f"资金分配比例之和应为 1.0，现在是 {total_ratio}")
    
    if errors:
        raise ValueError("❌ 配置参数错误:\n  " + "\n  ".join(errors))
    
    print("✅ 配置参数验证通过")
    return True

# ✅ 修复：不在导入时自动执行，改为 main.py 中显式调用
# validate_config()

# ════════════════════════════════════════════════════════════════════
# 📋 配置摘要
# ════════════════════════════════════════════════════════════════════

CONFIG_SUMMARY = f"""
═══════════════════════════════════════════════════════════
交易机器人配置
═══════════════════════════════════════════════════════════
交易对:        {SYMBOL}
初始资金:      ${TOTAL_CAPITAL} USDT
杠杆倍数:      {LEVERAGE}x
网格数量:      {GRID_NUM} 层

风险管理:
  日最大损失:  ${MAX_DAILY_LOSS}
  最大回撤:    {MAX_DRAWDOWN*100}%
  紧急止损:    {EMERGENCY_STOP_LOSS*100}%
  
断路器:
  API失败上限:    {CIRCUIT_BREAKER_MAX_API_FAILURES}
  每时亏损上限:  {CIRCUIT_BREAKER_MAX_LOSS_PER_HOUR}
  快速下跌熔断:  {CIRCUIT_BREAKER_RAPID_LOSS_PCT*100}%
═══════════════════════════════════════════════════════════
"""

# ════════════════════════════════════════════════════════════════════
# Test 版默认覆盖
# ════════════════════════════════════════════════════════════════════

DASHBOARD_ENABLED = False
DASHBOARD_WEB_ENABLED = False
DASHBOARD_MANUAL_START = False
DASHBOARD_LOGIN_REQUIRED = False
TRADING_MODE_AI_SELECT = False
QWEN_ENABLED = False
QWEN_MODE_ENABLED = False
MARKET_ANALYSIS_ENABLED = False
MARKET_ANALYSIS_QWEN_ENABLED = False
MARKET_NEWS_ENABLED = False
FUNDING_ARB_ENABLED = False
DAILY_LEARNER_ENABLED = False
AI_REGISTRY_AUTO_REPAIR = False
AI_JANITOR_ENABLED = False
TEST_LOOP_INTERVAL_SEC = int(os.getenv("TEST_LOOP_INTERVAL_SEC", "60"))
TEST_INIT_STRICT = os.getenv("TEST_INIT_STRICT", "0").strip().lower() in ("1", "true", "yes", "on")

