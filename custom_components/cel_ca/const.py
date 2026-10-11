"""Constants of the CEL-CA integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "cel_ca"
MANUFACTURER: Final = "WattKeeper"

CONF_NAME: Final = "name"
CONF_COMMUNITY_ID: Final = "community_id"
CONF_ADMIN_PUBLIC_KEY: Final = "admin_public_key"
CONF_PRIVATE_KEY: Final = "private_key"
CONF_ACCESS_KEY: Final = "access_key"
CONF_GRID_POWER_ENTITY: Final = "grid_power_entity"
CONF_GRID_SIGN: Final = "grid_sign"
CONF_ROLES: Final = "roles"

CONF_OFFER_MARGIN_W: Final = "offer_margin_w"
CONF_MIN_OFFER_W: Final = "min_offer_w"
CONF_OFFER_HORIZON_MIN: Final = "offer_horizon_min"
CONF_UPDATE_THRESHOLD_PCT: Final = "update_threshold_pct"
CONF_SMOOTHING_S: Final = "smoothing_s"

SIGN_IMPORT_POSITIVE: Final = "import_positive"
SIGN_EXPORT_POSITIVE: Final = "export_positive"

ROLE_PRODUCER: Final = "producer"
ROLE_CONSUMER: Final = "consumer"

DEFAULT_OPTIONS: Final = {
    CONF_OFFER_MARGIN_W: 100,
    CONF_MIN_OFFER_W: 500,
    CONF_OFFER_HORIZON_MIN: 15,
    CONF_UPDATE_THRESHOLD_PCT: 10,
    CONF_SMOOTHING_S: 60,
}

TICK_INTERVAL: Final = timedelta(seconds=10)
STATE_POLL_S: Final = 15.0  # shared state (offers, presence...) read from Azure Table
INBOX_POLL_S: Final = 5.0  # inbox (requests / answers) read from Azure Queue
REQUEST_TIMEOUT_S: Final = 30.0
STORAGE_VERSION: Final = 1
SAVE_DELAY_S: Final = 30
PERIODIC_SAVE_S: Final = 300

EVENT_CEL: Final = f"{DOMAIN}_event"
ISSUE_NOT_ENROLLED: Final = "not_enrolled"
ISSUE_KEY_EXPIRING: Final = "access_key_expiring"
KEY_EXPIRY_WARNING_DAYS: Final = 30

SERVICE_RESERVE: Final = "reserve"
SERVICE_RESERVE_GLOBAL: Final = "reserve_global"
SERVICE_CANCEL: Final = "cancel_reservation"
SERVICE_PUBLISH_OFFER: Final = "publish_offer"
SERVICE_WITHDRAW_OFFER: Final = "withdraw_offer"
SERVICE_GENERATE_DASHBOARD: Final = "generate_dashboard"

ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_POWER_W: Final = "power_w"
ATTR_MIN_POWER_W: Final = "min_power_w"
ATTR_DURATION_MIN: Final = "duration_min"
ATTR_PRODUCER_ID: Final = "producer_id"
ATTR_OFFER_ID: Final = "offer_id"
ATTR_START: Final = "start"
ATTR_RESERVATION_ID: Final = "reservation_id"


def signal_update(entry_id: str) -> str:
    """Dispatcher signal for state updates of an entry."""
    return f"{DOMAIN}_update_{entry_id}"
