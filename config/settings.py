from pydantic_settings import BaseSettings
from pydantic import Field
from enum import Enum
from pathlib import Path


class TradingMode(str, Enum):
    TESTNET = "testnet"
    MAINNET = "mainnet"


class BotMode(str, Enum):
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


class RiskPresetName(str, Enum):
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"


BINANCE_TESTNET_BASE_URL = "https://testnet.binance.vision"
BINANCE_MAINNET_BASE_URL = "https://api.binance.com"

BINANCE_TESTNET_WS_URL = "wss://testnet.binance.vision/ws"
BINANCE_MAINNET_WS_URL = "wss://stream.binance.com:9443/ws"


class Settings(BaseSettings):
    trading_mode: TradingMode = TradingMode.TESTNET

    binance_testnet_api_key: str = ""
    binance_testnet_api_secret: str = ""
    binance_mainnet_api_key: str = ""
    binance_mainnet_api_secret: str = ""

    database_url: str = "postgresql+asyncpg://tradebot:tradebot@localhost:5432/tradebot"

    discord_webhook_url: str = ""

    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8080

    risk_preset: RiskPresetName = RiskPresetName.MODERATE
    bot_mode: BotMode = BotMode.SUPERVISED

    # Risk parameter overrides (None = use preset default)
    override_grid_levels: int | None = None
    override_grid_range_pct: float | None = None
    override_stop_loss_pct: float | None = None
    override_max_capital_per_level_pct: float | None = None
    override_grid_reset_cooldown_seconds: int | None = None
    override_pause_on_range_exit: bool | None = None

    # Per-strategy capital caps (USD). Set to 0 to disable a strategy.
    grid_capital: float = 10.0
    scalp_capital: float = 10.0

    # Symbol overrides (empty = use screener)
    trading_symbol: str = ""   # Grid symbol
    scalp_symbol: str = ""     # Scalp symbol

    # Scalp strategy settings
    scalp_mode: str = "momentum"  # "momentum" or "mean_reversion"
    scalp_trigger_pct: float = 0.5
    scalp_trigger_window: int = 30       # seconds
    scalp_tp_pct: float = 0.4
    scalp_sl_pct: float = 0.3
    scalp_time_limit: int = 120          # seconds
    scalp_trade_pct: float = 50.0        # % of scalp capital per trade
    scalp_cooldown: int = 5              # seconds between trades

    model_config = {"env_file": str(Path(__file__).parent / ".env"), "extra": "ignore"}

    @property
    def binance_api_key(self) -> str:
        if self.trading_mode == TradingMode.TESTNET:
            return self.binance_testnet_api_key
        return self.binance_mainnet_api_key

    @property
    def binance_api_secret(self) -> str:
        if self.trading_mode == TradingMode.TESTNET:
            return self.binance_testnet_api_secret
        return self.binance_mainnet_api_secret

    @property
    def binance_base_url(self) -> str:
        if self.trading_mode == TradingMode.TESTNET:
            return BINANCE_TESTNET_BASE_URL
        return BINANCE_MAINNET_BASE_URL

    @property
    def binance_ws_url(self) -> str:
        if self.trading_mode == TradingMode.TESTNET:
            return BINANCE_TESTNET_WS_URL
        return BINANCE_MAINNET_WS_URL

    @property
    def is_testnet(self) -> bool:
        return self.trading_mode == TradingMode.TESTNET


settings = Settings()
