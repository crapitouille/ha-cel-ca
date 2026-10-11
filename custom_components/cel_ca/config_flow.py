"""Config flow of the CEL-CA integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

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
    ROLE_CONSUMER,
    ROLE_PRODUCER,
    SIGN_EXPORT_POSITIVE,
    SIGN_IMPORT_POSITIVE,
)
from .core.identity import Identity, IdentityError, parse_public_key
from .core.storage_access import AccessKey, AccessKeyError
from .core.storage_transport import probe_access
from .hub import setting


def _energy_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_ROLES, default=defaults.get(CONF_ROLES, [ROLE_PRODUCER, ROLE_CONSUMER])): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[ROLE_PRODUCER, ROLE_CONSUMER],
                    multiple=True,
                    translation_key=CONF_ROLES,
                )
            ),
            vol.Required(
                CONF_GRID_POWER_ENTITY,
                **({"default": defaults[CONF_GRID_POWER_ENTITY]} if defaults.get(CONF_GRID_POWER_ENTITY) else {}),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="power")),
            vol.Required(CONF_GRID_SIGN, default=defaults.get(CONF_GRID_SIGN, SIGN_IMPORT_POSITIVE)): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[SIGN_IMPORT_POSITIVE, SIGN_EXPORT_POSITIVE],
                    translation_key=CONF_GRID_SIGN,
                )
            ),
        }
    )


class CelConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a HEMS node of a CEL hosted on an Azure Storage Account."""

    VERSION = 2

    def __init__(self) -> None:
        self._identity: Identity | None = None
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options flow."""
        return CelOptionsFlow()

    async def _check_access_key(self, text: str, identity: Identity) -> tuple[AccessKey | None, dict[str, str]]:
        """Decode, match against the identity and test an access key."""
        try:
            key = AccessKey.decode(text)
            parse_public_key(key.apk)
        except (AccessKeyError, IdentityError):
            return None, {CONF_ACCESS_KEY: "invalid_access_key"}
        if key.id != identity.hems_id:
            return None, {CONF_ACCESS_KEY: "wrong_identity"}
        error = await probe_access(key, async_get_clientsession(self.hass))
        if error is not None:
            return None, {"base": error[0]}
        return key, {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Identity and CEL access key."""
        errors: dict[str, str] = {}
        if self._identity is None:
            self._identity = await self.hass.async_add_executor_job(Identity.generate)

        if user_input is not None:
            identity = self._identity
            if restored := (user_input.get(CONF_PRIVATE_KEY) or "").strip():
                try:
                    identity = Identity.from_private_b64(restored)
                except (IdentityError, ValueError):
                    errors[CONF_PRIVATE_KEY] = "invalid_private_key"
            if not errors:
                await self.async_set_unique_id(identity.hems_id)
                self._abort_if_unique_id_configured()
                key, errors = await self._check_access_key(user_input[CONF_ACCESS_KEY], identity)
                if key is not None:
                    self._identity = identity
                    self._data = {
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_COMMUNITY_ID: key.cid,
                        CONF_ADMIN_PUBLIC_KEY: key.apk,
                        CONF_ACCESS_KEY: key.encode(),
                        CONF_PRIVATE_KEY: identity.private_b64,
                    }
                    return await self.async_step_energy()

        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, self.hass.config.location_name)): str,
                vol.Required(CONF_ACCESS_KEY): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_PRIVATE_KEY): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"hems_id": self._identity.hems_id, "public_key": self._identity.public_b64},
        )

    @callback
    def _async_store_access_key(self, entry: ConfigEntry, key: AccessKey) -> ConfigFlowResult:
        """Save a new access key and finish the flow.

        A loaded entry reloads through its update listener (HA 2026.12+ refuses a
        second reload from the flow); an entry that failed to set up (no listener
        yet, e.g. migrated from the MQTT version) is reloaded explicitly.
        """
        updates = {CONF_ACCESS_KEY: key.encode(), CONF_COMMUNITY_ID: key.cid, CONF_ADMIN_PUBLIC_KEY: key.apk}
        if not entry.update_listeners:
            return self.async_update_reload_and_abort(entry, data_updates=updates)
        self.hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})
        reason = "reconfigure_successful" if self.source == SOURCE_RECONFIGURE else "reauth_successful"
        return self.async_abort(reason=reason)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The access key is missing (migration from MQTT), expired or was renewed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for a new access key, keeping the identity."""
        entry = self._get_reauth_entry()
        identity = Identity.from_private_b64(entry.data[CONF_PRIVATE_KEY])
        errors: dict[str, str] = {}
        if user_input is not None:
            key, errors = await self._check_access_key(user_input[CONF_ACCESS_KEY], identity)
            if key is not None:
                return self._async_store_access_key(entry, key)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCESS_KEY): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
            description_placeholders={"hems_id": identity.hems_id, "public_key": identity.public_b64},
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Paste a new access key before the current one expires (identity kept)."""
        entry = self._get_reconfigure_entry()
        identity = Identity.from_private_b64(entry.data[CONF_PRIVATE_KEY])
        errors: dict[str, str] = {}
        if user_input is not None:
            key, errors = await self._check_access_key(user_input[CONF_ACCESS_KEY], identity)
            if key is not None:
                return self._async_store_access_key(entry, key)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCESS_KEY): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
            description_placeholders={"hems_id": identity.hems_id, "public_key": identity.public_b64},
        )

    async def async_step_energy(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Roles and grid power measurement."""
        assert self._identity is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_ROLES]:
                errors[CONF_ROLES] = "no_role"
            else:
                return self.async_create_entry(
                    title=self._data[CONF_NAME],
                    data={**self._data, **user_input},
                    options=dict(DEFAULT_OPTIONS),
                )
        return self.async_show_form(
            step_id="energy",
            data_schema=_energy_schema(user_input or {}),
            errors=errors,
            description_placeholders={
                "hems_id": self._identity.hems_id,
                "public_key": self._identity.public_b64,
            },
        )


class CelOptionsFlow(OptionsFlow):
    """Tune the surplus publication and roles."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Single options step."""
        entry = self.config_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_ROLES]:
                errors[CONF_ROLES] = "no_role"
            else:
                return self.async_create_entry(data=user_input)
        current = {k: setting(entry, k) for k in (CONF_ROLES, CONF_GRID_POWER_ENTITY, CONF_GRID_SIGN)}
        schema = _energy_schema(current).extend(
            {
                vol.Required(CONF_OFFER_MARGIN_W, default=setting(entry, CONF_OFFER_MARGIN_W)): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=50_000)
                ),
                vol.Required(CONF_MIN_OFFER_W, default=setting(entry, CONF_MIN_OFFER_W)): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=100_000)
                ),
                vol.Required(CONF_OFFER_HORIZON_MIN, default=setting(entry, CONF_OFFER_HORIZON_MIN)): vol.All(
                    vol.Coerce(int), vol.Range(min=5, max=240)
                ),
                vol.Required(CONF_UPDATE_THRESHOLD_PCT, default=setting(entry, CONF_UPDATE_THRESHOLD_PCT)): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=100)
                ),
                vol.Required(CONF_SMOOTHING_S, default=setting(entry, CONF_SMOOTHING_S)): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=900)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
