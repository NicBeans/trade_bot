"""Runtime settings — in-memory overrides on top of .env config."""

import logging

logger = logging.getLogger(__name__)

CHANGEABLE_KEYS = {
    "grid_capital": float,
    "scalp_capital": float,
    "bot_mode": str,
    "risk_preset": str,
}


class RuntimeSettings:
    def __init__(self, base_settings):
        self._base = base_settings
        self._overrides: dict[str, any] = {}

    def get(self, key: str):
        """Get a setting value — override if set, otherwise base."""
        if key in self._overrides:
            return self._overrides[key]
        return getattr(self._base, key)

    def set(self, key: str, value) -> tuple[bool, str]:
        """Set a runtime override. Returns (success, message)."""
        if key not in CHANGEABLE_KEYS:
            return False, f"Setting '{key}' is not changeable at runtime"

        expected_type = CHANGEABLE_KEYS[key]
        try:
            typed_value = expected_type(value)
        except (ValueError, TypeError):
            return False, f"Invalid value for '{key}': expected {expected_type.__name__}"

        # Validation
        if key == "grid_capital" and typed_value < 0:
            return False, "Grid capital cannot be negative"
        if key == "scalp_capital" and typed_value < 0:
            return False, "Scalp capital cannot be negative"
        if key == "bot_mode" and typed_value not in ("supervised", "autonomous"):
            return False, "Bot mode must be 'supervised' or 'autonomous'"
        if key == "risk_preset" and typed_value not in ("conservative", "moderate", "aggressive"):
            return False, "Risk preset must be 'conservative', 'moderate', or 'aggressive'"

        old_value = self.get(key)
        self._overrides[key] = typed_value
        logger.info("Runtime setting changed: %s = %s (was %s)", key, typed_value, old_value)
        return True, f"{key} changed from {old_value} to {typed_value}"

    def get_all(self) -> dict:
        """Get all changeable settings with current values."""
        return {key: self.get(key) for key in CHANGEABLE_KEYS}

    def get_changes(self) -> dict:
        """Get only the overridden values."""
        return dict(self._overrides)
