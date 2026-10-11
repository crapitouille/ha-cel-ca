"""Glue between Home Assistant and the protocol core."""

from __future__ import annotations

from datetime import date, datetime
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfPower
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACCESS_KEY,
    CONF_ADMIN_PUBLIC_KEY,
    CONF_COMMUNITY_ID,
    CONF_GRID_POWER_ENTITY,
    CONF_GRID_SIGN,
    CONF_MIN_OFFER_W,
    CONF_NAME,
    CONF_OFFER_HORIZON_MIN,
    CONF_OFFER_MARGIN_W,
    CONF_PRIVATE_KEY,
    CONF_ROLES,
    CONF_SMOOTHING_S,
    CONF_UPDATE_THRESHOLD_PCT,
    DEFAULT_OPTIONS,
    DOMAIN,
    EVENT_CEL,
    INBOX_POLL_S,
    ISSUE_KEY_EXPIRING,
    ISSUE_NOT_ENROLLED,
    KEY_EXPIRY_WARNING_DAYS,
    MANUFACTURER,
    PERIODIC_SAVE_S,
    REQUEST_TIMEOUT_S,
    SAVE_DELAY_S,
    SIGN_EXPORT_POSITIVE,
    STATE_POLL_S,
    STORAGE_VERSION,
    TICK_INTERVAL,
    signal_update,
)
from .core.identity import Identity, parse_public_key
from .core.node import SW_VERSION, CelNode, NodeConfig
from .core.storage_access import AccessKey, AccessKeyError
from .core.storage_transport import StorageTransport
from .core.transport import Transport

# Home Assistant 2026.8+ links devices by device id (DeviceInfo.via_device_id).
_VIA_DEVICE_ID = "via_device_id" in DeviceInfo.__annotations__

_LOGGER = logging.getLogger(__name__)


def setting(entry: ConfigEntry, key: str) -> Any:
    """Option value, falling back to the initial data, then to the default."""
    if key in entry.options:
        return entry.options[key]
    if key in entry.data:
        return entry.data[key]
    return DEFAULT_OPTIONS.get(key)


def ms_to_iso(value: int | None) -> str | None:
    """Unix ms to ISO-8601 UTC."""
    if value is None:
        return None
    return dt_util.utc_from_timestamp(value / 1000).isoformat()


def datetime_to_ms(value: datetime) -> int:
    """Datetime (naive = local time) to unix ms."""
    return int(dt_util.as_utc(value).timestamp() * 1000)


def access_key_expiry(entry: ConfigEntry) -> date | None:
    """Expiry date written in the CEL access key (None if unknown)."""
    try:
        return date.fromisoformat(AccessKey.decode(entry.data[CONF_ACCESS_KEY]).exp[:10])
    except (KeyError, ValueError, AccessKeyError):
        return None


async def create_transport(hass: HomeAssistant, entry: ConfigEntry) -> Transport:
    """Azure Storage transport (patched in tests)."""
    return StorageTransport(
        AccessKey.decode(entry.data[CONF_ACCESS_KEY]),
        session=async_get_clientsession(hass),
        state_interval=STATE_POLL_S,
        inbox_interval=INBOX_POLL_S,
    )


def node_config_from_entry(entry: ConfigEntry, auto_offer: bool) -> NodeConfig:
    """Map config entry settings to the node configuration."""
    return NodeConfig(
        name=entry.data.get(CONF_NAME) or entry.title,
        roles=frozenset(setting(entry, CONF_ROLES) or ()),
        auto_offer=auto_offer,
        offer_margin_w=int(setting(entry, CONF_OFFER_MARGIN_W)),
        min_offer_w=int(setting(entry, CONF_MIN_OFFER_W)),
        offer_horizon_ms=int(setting(entry, CONF_OFFER_HORIZON_MIN)) * 60_000,
        update_threshold_pct=int(setting(entry, CONF_UPDATE_THRESHOLD_PCT)),
        smoothing_ms=int(setting(entry, CONF_SMOOTHING_S)) * 1000,
        # answers travel through polled inboxes: allow for a few polling periods
        request_timeout_s=REQUEST_TIMEOUT_S,
    )


class CelHub:
    """One HEMS node inside Home Assistant."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.identity = Identity.from_private_b64(entry.data[CONF_PRIVATE_KEY])
        self.admin_public_key = parse_public_key(entry.data[CONF_ADMIN_PUBLIC_KEY])
        self.community_id: str = entry.data[CONF_COMMUNITY_ID]
        self.grid_entity: str | None = setting(entry, CONF_GRID_POWER_ENTITY)
        self.export_positive = setting(entry, CONF_GRID_SIGN) == SIGN_EXPORT_POSITIVE
        self.node: CelNode | None = None
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._unsubs: list = []
        self._issue_active: bool | None = None
        self._reauth_started = False
        self._last_save = time.monotonic()
        # Community snapshot, refreshed on every node update (read by entities).
        self.participants: dict[str, dict[str, Any]] = {}
        self.participant_rows: list[dict[str, Any]] = []
        self.totals: dict[str, int] = {}
        self._synced_serial: int | None = None
        self.device_id: str | None = None
        self.access_key_expires = access_key_expiry(entry)
        self._key_issue_active: bool | None = None

    @property
    def hems_id(self) -> str:
        """Own id."""
        return self.identity.hems_id

    @property
    def signal(self) -> str:
        """Dispatcher signal."""
        return signal_update(self.entry.entry_id)

    @staticmethod
    def now_ms() -> int:
        """Wall clock in ms."""
        return int(time.time() * 1000)

    # ------------------------------------------------------------ lifecycle
    async def async_start(self) -> None:
        """Create the node, restore state and connect."""
        self._register_own_device()
        stored = await self._store.async_load() or {}
        config = node_config_from_entry(self.entry, bool(stored.get("auto_offer", True)))
        transport = await create_transport(self.hass, self.entry)
        self.node = CelNode(
            self.identity, self.community_id, self.admin_public_key, transport, config, clock=self.now_ms
        )
        self.node.restore_state(stored.get("node"))
        self.node.on_event = self._on_event
        self._unsubs.append(self.node.add_listener(self._on_node_update))
        if self.grid_entity:
            self._unsubs.append(
                async_track_state_change_event(self.hass, [self.grid_entity], self._on_grid_change)
            )
        self._unsubs.append(async_track_time_interval(self.hass, self._async_tick, TICK_INTERVAL))
        await self.node.async_start()
        self.refresh_community()

    async def async_stop(self) -> None:
        """Disconnect and persist."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self.node is not None:
            await self.node.async_stop()
            await self._store.async_save(self._data_to_save())

    def _data_to_save(self) -> dict[str, Any]:
        assert self.node is not None
        return {"node": self.node.export_state(), "auto_offer": self.node.config.auto_offer}

    def schedule_save(self) -> None:
        """Debounced save after a state change."""
        self._schedule_save()

    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY_S)

    # -------------------------------------------------------------- inputs
    def _grid_import_w(self, state: State | None) -> float | None:
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit == UnitOfPower.KILO_WATT:
            value *= 1000
        elif unit == UnitOfPower.MEGA_WATT:
            value *= 1_000_000
        return -value if self.export_positive else value

    @callback
    def _on_grid_change(self, event: Event[EventStateChangedData]) -> None:
        value = self._grid_import_w(event.data["new_state"])
        if value is not None and self.node is not None:
            self.node.record_grid_power(value)

    async def _async_tick(self, _now: datetime | None = None) -> None:
        if self.node is None:
            return
        if self.grid_entity:
            value = self._grid_import_w(self.hass.states.get(self.grid_entity))
            if value is not None:
                self.node.record_grid_power(value)
        await self.node.async_tick()
        # Store.async_delay_save is a debounce: calling it on every tick would
        # postpone the write forever, so ticks save explicitly at a slow pace.
        now = time.monotonic()
        if now - self._last_save >= PERIODIC_SAVE_S:
            self._last_save = now
            await self._store.async_save(self._data_to_save())

    async def async_tick(self) -> None:
        """Public tick (used by tests and services)."""
        await self._async_tick()

    # ------------------------------------------------------------- outputs
    @callback
    def _on_node_update(self) -> None:
        self._update_issue()
        self._update_key_expiry_issue()
        self._check_auth()
        self.refresh_community()
        async_dispatcher_send(self.hass, self.signal)

    @callback
    def _update_key_expiry_issue(self) -> None:
        """Warn in Repairs a month before the CEL access key expires."""
        if self.access_key_expires is None:
            return
        days_left = (self.access_key_expires - dt_util.utcnow().date()).days
        expiring = days_left <= KEY_EXPIRY_WARNING_DAYS
        if expiring == self._key_issue_active:
            return
        self._key_issue_active = expiring
        issue_id = f"{ISSUE_KEY_EXPIRING}_{self.entry.entry_id}"
        if expiring:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_KEY_EXPIRING,
                translation_placeholders={
                    "name": self.entry.title,
                    "hems_id": self.hems_id,
                    "expires": self.access_key_expires.isoformat(),
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    @callback
    def _check_auth(self) -> None:
        """An access key refused by Azure (expired, renewed...) asks the user for a new one."""
        transport = self.node.transport if self.node else None
        if getattr(transport, "auth_failed", False) and not self._reauth_started:
            self._reauth_started = True
            self.entry.async_start_reauth(self.hass)

    @callback
    def refresh_community(self) -> None:
        """Recompute the participants snapshot and drop devices of departed members."""
        assert self.node is not None
        rows = self.node.participants()
        self.participant_rows = rows
        self.participants = {r["hems_id"]: r for r in rows}
        self.totals = self.node.community_totals(participants=rows)
        serial = self.node.registry.serial
        if serial and serial != self._synced_serial:
            self._synced_serial = serial
            self._remove_departed_peer_devices()

    def own_device_info(self) -> DeviceInfo:
        """Device of this HEMS node."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.hems_id)},
            name=self.entry.title,
            manufacturer=MANUFACTURER,
            model=f"CEL node ({self.community_id})",
            sw_version=SW_VERSION,
            serial_number=self.hems_id,
        )

    @callback
    def _register_own_device(self) -> None:
        """Register the node device first, so participants can be linked to it."""
        device = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id, **self.own_device_info()
        )
        self.device_id = device.id

    def via_own_device(self) -> dict[str, Any]:
        """DeviceInfo keys linking a participant device to this node.

        Home Assistant 2026.8+ takes the device id (``via_device_id``); the
        identifier tuple (``via_device``) is deprecated there and refused for
        some callers since 2026.10.
        """
        if _VIA_DEVICE_ID:
            return {"via_device_id": self.device_id} if self.device_id else {}
        return {"via_device": (DOMAIN, self.hems_id)}

    def peer_device_identifier(self, peer_id: str) -> tuple[str, str]:
        """Device identifier of a participant, scoped to this HEMS."""
        return (DOMAIN, f"{self.hems_id}:{peer_id}")

    @callback
    def _remove_departed_peer_devices(self) -> None:
        registry = dr.async_get(self.hass)
        prefix = f"{self.hems_id}:"
        for device in dr.async_entries_for_config_entry(registry, self.entry.entry_id):
            for domain, ident in device.identifiers:
                if domain == DOMAIN and ident.startswith(prefix) and ident[len(prefix):] not in self.participants:
                    registry.async_remove_device(device.id)
                    break

    @callback
    def _update_issue(self) -> None:
        assert self.node is not None
        # Only complain once we are connected and a registry was received.
        missing = self.node.connected and self.node.registry.serial > 0 and not self.node.enrolled
        if missing == self._issue_active:
            return
        self._issue_active = missing
        issue_id = f"{ISSUE_NOT_ENROLLED}_{self.entry.entry_id}"
        if missing:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_NOT_ENROLLED,
                translation_placeholders={
                    "name": self.entry.title,
                    "hems_id": self.hems_id,
                    "public_key": self.identity.public_b64,
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    @callback
    def _on_event(self, kind: str, data: dict[str, Any]) -> None:
        payload = {"entry_id": self.entry.entry_id, "hems_id": self.hems_id, "type": kind, **data}
        for key in ("start", "end"):
            if isinstance(payload.get(key), int) and payload[key] > 0:
                payload[f"{key}_iso"] = ms_to_iso(payload[key])
        self.hass.bus.async_fire(EVENT_CEL, payload)
        self._schedule_save()

    # ------------------------------------------------------------- actions
    async def async_set_auto_offer(self, enabled: bool) -> None:
        """Enable/disable automatic surplus publication."""
        assert self.node is not None
        self.node.config.auto_offer = enabled
        await self.node.async_tick()
        # Store.async_delay_save is a debounce: calling it on every tick would
        # postpone the write forever, so ticks save explicitly at a slow pace.
        now = time.monotonic()
        if now - self._last_save >= PERIODIC_SAVE_S:
            self._last_save = now
            await self._store.async_save(self._data_to_save())

    def reservation_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Service response with readable times (also inside a group's reservations)."""
        out = dict(result)
        out["start"] = ms_to_iso(result.get("start"))
        out["end"] = ms_to_iso(result.get("end"))
        if "reservations" in result:
            out["reservations"] = [
                {**r, "start": ms_to_iso(r.get("start")), "end": ms_to_iso(r.get("end"))} for r in result["reservations"]
            ]
        return out
