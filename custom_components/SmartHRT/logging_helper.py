"""Helper module for consistent logging across SmartHRT components.

This module provides utilities to ensure log messages can be dissociated
when multiple SmartHRT instances are configured, allowing clear
identification of which instance generated each log entry.
"""

from homeassistant.config_entries import ConfigEntry


def get_log_prefix(entry: ConfigEntry, name: str | None = None) -> str:
    """Get a log prefix for an instance.

    Returns a formatted prefix containing the instance name and entry ID.
    The entry ID is truncated to 8 characters for readability.

    Examples:
        >>> get_log_prefix(entry, "Main Heating")
        "[Main Heating#a1b2c3d4]"

    Args:
        entry: The ConfigEntry for the SmartHRT instance
        name: The instance name (optional, defaults to entry title if not provided)

    Returns:
        A formatted prefix string like "[Name#EntryID]"
    """
    display_name = name or entry.title or "SmartHRT"
    entry_id_short = entry.entry_id[:8]
    return f"[{display_name}#{entry_id_short}]"
