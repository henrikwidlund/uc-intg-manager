"""
Setup Flow for the Bootstrapper integration.

The bootstrapper setup is NOT interactive — all fields are populated
programmatically by Integration Manager via ``send_setup_input``.

Flow driven by IM:
1. IM calls start_setup(reconfigure=False)
2. Framework shows restore_from_backup screen → IM answers "true"
3. Framework shows restore_data textarea → IM sends a JSON blob containing:
       {"target_version": "...", "manager_driver_id": "...",
        "manager_data": "...", "config_data": "..."}
4. _handle_restore_response unpacks the blob into BootstrapperConfig

:copyright: (c) 2025 by Jack Powell.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import json
import logging
from typing import Any

from const import BootstrapperConfig
from ucapi import IntegrationSetupError, RequestUserInput, SetupComplete, SetupError, UserDataResponse
from ucapi_framework import BaseSetupFlow

_LOG = logging.getLogger("setup_flow")


class BootstrapperSetupFlow(BaseSetupFlow[BootstrapperConfig]):
    """
    Setup flow for the bootstrapper integration.

    Driven entirely by Integration Manager — never shown to a human user.
    """

    async def _handle_restore_response(
        self, msg: UserDataResponse
    ) -> SetupComplete | SetupError | RequestUserInput:
        """
        Intercept the framework's restore_data screen.

        IM packs all four required fields as a JSON object inside restore_data:
            {
                "target_version":    "<tag>",
                "manager_driver_id": "<driver_id>",
                "manager_data":      "<json string>",
                "config_data":       "<json string>"
            }
        """
        raw = msg.input_values.get("restore_data", "").strip()
        _LOG.info("Bootstrapper: _handle_restore_response received %d bytes", len(raw))

        if not raw:
            _LOG.error("Bootstrapper: restore_data is empty")
            return SetupError(IntegrationSetupError.OTHER)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _LOG.error("Bootstrapper: restore_data is not valid JSON: %s", exc)
            return SetupError(IntegrationSetupError.OTHER)

        target_version    = payload.get("target_version", "").strip()
        manager_driver_id = payload.get("manager_driver_id", "").strip()
        manager_data      = payload.get("manager_data", "{}").strip() or "{}"
        config_data       = payload.get("config_data", "[]").strip() or "[]"

        if not target_version:
            _LOG.error("Bootstrapper: missing target_version in restore payload")
            return SetupError(IntegrationSetupError.OTHER)
        if not manager_driver_id:
            _LOG.error("Bootstrapper: missing manager_driver_id in restore payload")
            return SetupError(IntegrationSetupError.OTHER)

        _LOG.info(
            "Bootstrapper: restore payload parsed — target=%s driver_id=%s "
            "manager_data=%d bytes config_data=%d bytes",
            target_version, manager_driver_id, len(manager_data), len(config_data),
        )

        cfg = BootstrapperConfig(
            identifier="bootstrapper",
            target_version=target_version,
            manager_driver_id=manager_driver_id,
            manager_data=manager_data,
            config_data=config_data,
        )
        self.config.add_or_update(cfg)
        return SetupComplete()

    """
    Setup flow for the bootstrapper integration.

    This flow is driven entirely by Integration Manager, not by the user.
    IM installs the bootstrapper, starts its setup, and immediately sends
    a single ``send_setup_input`` call with all four required values:

    - ``target_version``    — IM release tag to install (e.g. "v2.1.0")
    - ``manager_driver_id`` — driver ID of the currently running IM instance
    - ``manager_data``      — serialised contents of IM's manager.json
    - ``config_data``       — serialised contents of IM's config.json

    After setup completes the ``BootstrapperDevice`` fires the upgrade.
    """

    def get_manual_entry_form(self) -> RequestUserInput:
        """
        Return the setup form definition.

        These fields are received programmatically from Integration Manager.
        The field IDs MUST match the keys sent in ``send_setup_input`` from
        ``web_server.py``.

        :return: RequestUserInput describing the four setup fields.
        """
        _LOG.debug("Bootstrapper: building setup form (programmatic, not user-facing)")
        return RequestUserInput(
            {"en": "Integration Manager Bootstrapper"},
            [
                {
                    "id": "target_version",
                    "label": {"en": "Target Version"},
                    "field": {"text": {"value": ""}},
                },
                {
                    "id": "manager_driver_id",
                    "label": {"en": "Manager Driver ID"},
                    "field": {"text": {"value": ""}},
                },
                {
                    "id": "manager_data",
                    "label": {"en": "Manager Data (JSON)"},
                    "field": {"text": {"value": "{}"}},
                },
                {
                    "id": "config_data",
                    "label": {"en": "Config Data (JSON)"},
                    "field": {"text": {"value": "[]"}},
                },
            ],
        )

    async def query_device(
        self, input_values: dict[str, Any]
    ) -> BootstrapperConfig | SetupError | RequestUserInput:
        """
        Validate and store the programmatic setup data from Integration Manager.

        :param input_values: Dictionary of field values sent by IM.
        :return: A populated BootstrapperConfig on success, or SetupError on failure.
        """
        _LOG.info("Bootstrapper: query_device called with keys: %s", list(input_values.keys()))

        target_version = input_values.get("target_version", "").strip()
        manager_driver_id = input_values.get("manager_driver_id", "").strip()
        manager_data = input_values.get("manager_data", "{}").strip()
        config_data = input_values.get("config_data", "[]").strip()

        # Validate required fields
        if not target_version:
            _LOG.error("Bootstrapper: missing required field 'target_version'")
            return SetupError(IntegrationSetupError.OTHER)

        if not manager_driver_id:
            _LOG.error("Bootstrapper: missing required field 'manager_driver_id'")
            return SetupError(IntegrationSetupError.OTHER)

        if not manager_data:
            _LOG.warning("Bootstrapper: manager_data is empty, using empty dict")
            manager_data = "{}"

        if not config_data:
            _LOG.warning("Bootstrapper: config_data is empty, using empty list")
            config_data = "[]"

        _LOG.info(
            "Bootstrapper: setup validated — target=%s, driver_id=%s, "
            "manager_data_len=%d, config_data_len=%d",
            target_version,
            manager_driver_id,
            len(manager_data),
            len(config_data),
        )

        return BootstrapperConfig(
            identifier="bootstrapper",
            target_version=target_version,
            manager_driver_id=manager_driver_id,
            manager_data=manager_data,
            config_data=config_data,
        )
