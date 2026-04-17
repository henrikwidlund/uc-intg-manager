"""
Constants and configuration for the Bootstrapper integration.

This integration is installed on the UC Remote to perform a self-update of the
Integration Manager. It receives all necessary data via the setup flow, carries
out the upgrade, then deletes itself.

:copyright: (c) 2025 by Jack Powell.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import os
from dataclasses import dataclass

# GitHub repository for Integration Manager releases
IM_GITHUB_OWNER = "JackJPowell"
IM_GITHUB_REPO = "uc-intg-manager"

# Asset filename pattern (regex) to match the correct tar.gz in a release
IM_ASSET_PATTERN = r"uc-intg-manager.*\.tar\.gz"

# Dev override: if set, bootstrapper downloads from this URL instead of GitHub
# E.g. http://192.168.1.10:8000/uc-intg-manager_driver-2.0.0-aarch64.tar.gz
DEV_DOWNLOAD_URL: str = os.getenv("UC_DEV_DOWNLOAD_URL", "")

# The bootstrapper's own driver ID (must match driver.json)
BOOTSTRAPPER_DRIVER_ID = "intg_bootstrapper_driver"

# Base path where UC Remote stores integration config dirs
UC_INTG_CONFIG_BASE = "/data/integrations"

# Remote API base port (loopback, no auth needed)
REMOTE_LOOPBACK_HOST = "127.0.0.1"
REMOTE_LOOPBACK_PORT = 80


@dataclass
class BootstrapperConfig:
    """
    Configuration captured from the setup flow.

    All four fields are populated programmatically by Integration Manager
    via ``send_setup_input`` — they are never entered by a human user.
    """

    identifier: str = "bootstrapper"
    # Human-readable name (satisfies BaseDeviceInterface.name requirement)
    name: str = "Integration Manager Bootstrapper"
    # Address of the loopback API (satisfies BaseDeviceInterface.address requirement)
    address: str = REMOTE_LOOPBACK_HOST
    # Tag / version string to install, e.g. "v2.1.0"
    target_version: str = ""
    # Driver ID of the currently installed Integration Manager, e.g. "intg_manager_driver_dev"
    manager_driver_id: str = ""
    # Serialised contents of manager.json (the IM driver's primary config)
    manager_data: str = "{}"
    # Serialised contents of config.json (the IM device/integration config)
    config_data: str = "[]"
