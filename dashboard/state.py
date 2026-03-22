"""Shared state for dashboard — holds bot reference."""

_bot = None


def set_bot(bot):
    global _bot
    _bot = bot


def get_bot():
    return _bot
