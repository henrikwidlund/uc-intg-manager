"""
Integration Manager Bootstrapper Driver.

This is the main entry point for the bootstrapper integration.  The
bootstrapper has no entities and no user-facing discovery — it exists
solely to carry out a single Integration Manager self-update operation
and then remove itself.

:copyright: (c) 2025 by Jack Powell.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os

from const import BootstrapperConfig
from device import BootstrapperDevice
from setup import BootstrapperSetupFlow
from ucapi_framework import BaseConfigManager, BaseIntegrationDriver, get_config_path

_LOG = logging.getLogger("driver")


async def main() -> None:
    """Start the bootstrapper integration driver."""
    logging.basicConfig()

    level = os.getenv("UC_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("driver").setLevel(level)
    logging.getLogger("device").setLevel(level)
    logging.getLogger("setup_flow").setLevel(level)
    logging.getLogger("github_api").setLevel(level)
    logging.getLogger("sync_api").setLevel(level)

    _LOG.info("Integration Manager Bootstrapper starting up")

    # Bootstrapper creates no entities — entity_classes stays empty.
    # require_connection_before_registry=True ensures that on_device_added
    # calls async_add_configured_device → device.connect() → verify_connection(),
    # which is where the upgrade task is scheduled.
    driver = BaseIntegrationDriver(
        device_class=BootstrapperDevice,
        entity_classes=[],  # type: ignore[arg-type]
        require_connection_before_registry=True,
    )

    # Single, static config entry — no multiple devices to manage
    driver.config_manager = BaseConfigManager(
        get_config_path(driver.api.config_dir_path),
        driver.on_device_added,
        driver.on_device_removed,
        config_class=BootstrapperConfig,
    )

    # Load any previously saved config (e.g. after a crash/restart)
    await driver.register_all_configured_devices()

    # Setup handler — no discovery, programmatic setup only
    setup_handler = BootstrapperSetupFlow.create_handler(driver, discovery=None)

    _LOG.info("Bootstrapper driver initialised — waiting for setup from Integration Manager")

    await driver.api.init("driver.json", setup_handler)

    # Keep the process alive until the remote kills it (during self-delete)
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
