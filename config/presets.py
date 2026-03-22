from dataclasses import dataclass


@dataclass
class RiskPreset:
    name: str
    grid_levels: int
    grid_range_pct: float  # e.g. 0.05 = ±5%
    stop_loss_pct: float | None  # e.g. 0.10 = -10% of capital, None = disabled
    max_capital_per_level_pct: float  # e.g. 0.10 = 10%
    grid_reset_cooldown_seconds: int
    pause_on_range_exit: bool


PRESETS = {
    "conservative": RiskPreset(
        name="conservative",
        grid_levels=5,
        grid_range_pct=0.03,
        stop_loss_pct=0.05,
        max_capital_per_level_pct=0.15,
        grid_reset_cooldown_seconds=1800,
        pause_on_range_exit=True,
    ),
    "moderate": RiskPreset(
        name="moderate",
        grid_levels=10,
        grid_range_pct=0.05,
        stop_loss_pct=0.10,
        max_capital_per_level_pct=0.10,
        grid_reset_cooldown_seconds=600,
        pause_on_range_exit=False,
    ),
    "aggressive": RiskPreset(
        name="aggressive",
        grid_levels=20,
        grid_range_pct=0.10,
        stop_loss_pct=None,
        max_capital_per_level_pct=0.05,
        grid_reset_cooldown_seconds=120,
        pause_on_range_exit=False,
    ),
}
