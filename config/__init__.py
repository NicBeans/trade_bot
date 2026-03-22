from dataclasses import replace
from config.settings import settings
from config.presets import PRESETS, RiskPreset


def get_effective_preset() -> RiskPreset:
    """Build preset from base + any env var overrides."""
    base = PRESETS[settings.risk_preset.value]
    overrides = {}
    if settings.override_grid_levels is not None:
        overrides["grid_levels"] = settings.override_grid_levels
    if settings.override_grid_range_pct is not None:
        overrides["grid_range_pct"] = settings.override_grid_range_pct
    if settings.override_stop_loss_pct is not None:
        overrides["stop_loss_pct"] = settings.override_stop_loss_pct
    if settings.override_max_capital_per_level_pct is not None:
        overrides["max_capital_per_level_pct"] = settings.override_max_capital_per_level_pct
    if settings.override_grid_reset_cooldown_seconds is not None:
        overrides["grid_reset_cooldown_seconds"] = settings.override_grid_reset_cooldown_seconds
    if settings.override_pause_on_range_exit is not None:
        overrides["pause_on_range_exit"] = settings.override_pause_on_range_exit
    if overrides:
        return replace(base, **overrides)
    return base
