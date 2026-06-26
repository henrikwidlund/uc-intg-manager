"""
Integration Manager Driver.

This is the main entry point for the integration manager. It initializes
the driver, sets up logging, and starts the integration API.

:copyright: (c) 2025.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os
import socket

import ucapi
import device as _device_module
from const import RemoteConfig, WEB_SERVER_PORT, is_external_mode
from data_migration import migrate
from device import IntegrationManagerDevice, _all_remote_configs
from discover import ManagerDiscovery
from log_handler import setup_log_handler
from setup import RemoteSetupFlow
from ucapi_framework import BaseConfigManager, BaseIntegrationDriver, get_config_path
from web_server import WebServer

_LOG = logging.getLogger(__name__)


def _remote_id_from_ws(websocket) -> str | None:
    """Identifier of the configured remote whose IP matches the WebSocket peer."""
    if not websocket or not getattr(websocket, "remote_address", None):
        return None
    host = websocket.remote_address[0]
    for cfg in _all_remote_configs:
        if cfg.address == host:
            return cfg.identifier
    return None


class IntegrationManagerDriver(BaseIntegrationDriver):
    """Dispatch ucapi events to the originating remote (by client IP).

    Falls back when the websocket kwarg is missing or unmappable:
      * connect / exit-standby → all configured devices
      * disconnect / enter-standby → owner only (first in config)
    """

    def _owner_device(self) -> IntegrationManagerDevice | None:
        """Return the owner device (first in config) or None if unknown."""
        owner_id = (
            _all_remote_configs[0].identifier if _all_remote_configs else None
        )
        return self._device_instances.get(owner_id) if owner_id else None

    async def on_r2_connect_cmd(self, websocket=None) -> None:
        """Connect the originating remote (or all, if source unknown)."""
        # DeviceState is set once and never flipped back: in external mode
        # the host is reachable while the process is alive, and the state
        # broadcasts to every connected remote.
        await self.api.set_device_state(ucapi.DeviceStates.CONNECTED)
        rid = _remote_id_from_ws(websocket)
        device = self._device_instances.get(rid) if rid else None
        if device:
            _LOG.debug("Connect command from %s", rid)
            self._loop.create_task(device.connect())
        else:
            _LOG.debug("Connect command without identifiable source - connecting all")
            for d in self._device_instances.values():
                self._loop.create_task(d.connect())
        self._loop.create_task(self._recheck_all_connectivity(delay=3))

    async def on_r2_disconnect_cmd(self, websocket=None) -> None:
        """Disconnect the originating remote, or the owner as a fallback."""
        rid = _remote_id_from_ws(websocket)
        device = self._device_instances.get(rid) if rid else None
        if device:
            _LOG.debug("Disconnect command from %s", rid)
            self._loop.create_task(device.disconnect())
            return
        owner = self._owner_device()
        if owner:
            _LOG.debug(
                "Disconnect command without identifiable source - falling back to owner %s",
                owner.identifier,
            )
            self._loop.create_task(owner.disconnect())
        else:
            _LOG.debug("Disconnect command without identifiable source - no owner to fall back to")

    async def on_r2_enter_standby(self, websocket=None) -> None:
        """Disconnect the remote that entered standby, or the owner as a fallback."""
        rid = _remote_id_from_ws(websocket)
        device = self._device_instances.get(rid) if rid else None
        if device:
            _LOG.debug("Enter standby from %s", rid)
            self._loop.create_task(device.disconnect())
            return
        owner = self._owner_device()
        if owner:
            _LOG.debug(
                "Enter standby without identifiable source - falling back to owner %s",
                owner.identifier,
            )
            self._loop.create_task(owner.disconnect())
        else:
            _LOG.debug("Enter standby without identifiable source - no owner to fall back to")

    async def on_r2_exit_standby(self, websocket=None) -> None:
        """Reconnect the originating remote (or all, if source unknown)."""
        rid = _remote_id_from_ws(websocket)
        device = self._device_instances.get(rid) if rid else None
        if device:
            _LOG.debug("Exit standby from %s", rid)
            self._loop.create_task(device.connect())
        else:
            _LOG.debug(
                "Exit standby without identifiable source - reconnecting all"
            )
            for d in self._device_instances.values():
                self._loop.create_task(d.connect())
        self._loop.create_task(self._recheck_all_connectivity(delay=3))

    async def _recheck_all_connectivity(self, delay: float = 3) -> None:
        """Wait briefly for connections to settle, then update all remote online statuses."""
        await asyncio.sleep(delay)
        ws = _device_module._web_server_instance
        if ws and ws.is_running:
            _LOG.debug(
                "Rechecking connectivity for all remotes after connect/exit-standby"
            )
            await ws.check_all_remote_connectivity()


def _web_server_port_reachable(port: int = WEB_SERVER_PORT, timeout: float = 2.0) -> bool:
    """Return True if a TCP connect to 127.0.0.1:port succeeds."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


async def _web_server_watchdog(interval: float = 30) -> None:
    """Restart the web server if it stops serving and refresh remote status.

    Restart triggers: instance cleared, `is_running` is False, or the
    listening port stops accepting connections. The connectivity probe
    runs every cycle to keep status fresh while device polling is idle.
    """
    # Let the server bind before the first probe.
    await asyncio.sleep(0.2)
    while True:
        try:
            if not _all_remote_configs:
                await asyncio.sleep(interval)
                continue
            ws = _device_module._web_server_instance
            # socket.connect_ex blocks; run off-loop.
            port_alive = (
                await asyncio.to_thread(_web_server_port_reachable)
                if ws is not None
                else False
            )
            if ws is None or not ws.is_running or not port_alive:
                _LOG.warning(
                    "Watchdog: web server unhealthy (instance=%s, running=%s, port_alive=%s) - restarting",
                    "present" if ws else "missing",
                    getattr(ws, "is_running", False),
                    port_alive,
                )
                if ws is not None:
                    try:
                        # stop() joins the Hypercorn thread (up to 5s).
                        await asyncio.to_thread(ws.stop)
                    except Exception as e:
                        _LOG.warning("Watchdog: stop() during cleanup failed: %s", e)
                new_ws = WebServer(remote_configs=_all_remote_configs)
                _device_module._web_server_instance = new_ws
                new_ws.start()
                await asyncio.sleep(0.5)
                if new_ws.is_running:
                    _LOG.info("Watchdog: web server restarted successfully")
                    ws = new_ws
                else:
                    _LOG.error(
                        "Watchdog: restart attempt failed - retrying in %ds",
                        interval,
                    )
                    _device_module._web_server_instance = None
                    await asyncio.sleep(interval)
                    continue

            try:
                await ws.check_all_remote_connectivity()
            except Exception as e:
                _LOG.debug("Watchdog connectivity probe failed: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOG.error("Watchdog loop error: %s", e, exc_info=True)
        await asyncio.sleep(interval)


async def main():
    """Start the Integration Manager driver."""
    logging.basicConfig()

    # Set up the ring buffer log handler to capture logs for the web UI
    setup_log_handler()

    # Configure logging level from environment variable
    level = os.getenv("UC_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("driver").setLevel(level)
    logging.getLogger("device").setLevel(level)
    logging.getLogger("setup").setLevel(level)
    logging.getLogger("web_server").setLevel(level)
    logging.getLogger("remote_api").setLevel(level)
    logging.getLogger("github_api").setLevel(level)
    logging.getLogger("integration_service").setLevel(level)
    logging.getLogger("data_migration").setLevel(level)
    logging.getLogger("backup_service").setLevel(level)

    # Force migration to v2.0 format if needed
    # This ensures all subsequent code can assume v2.0 structure
    migrate()

    # Initialize the integration driver
    # This integration doesn't expose entities - it's purely a web UI
    driver = IntegrationManagerDriver(
        device_class=IntegrationManagerDevice,
        entity_classes=[],  # No entities exposed
        driver_id="intg_manager_driver",
    )

    # Configure the device config manager
    driver.config_manager = BaseConfigManager(
        get_config_path(driver.api.config_dir_path),
        driver.on_device_added,
        driver.on_device_removed,
        config_class=RemoteConfig,
    )

    # Register all configured devices from config file
    await driver.register_all_configured_devices()

    # External mode: web server is independent of remote lifecycle.
    # On-remote installs start it lazily from dock/charge state.
    if is_external_mode():
        if _all_remote_configs:
            _LOG.info(
                "External mode detected at boot - starting web server with %d configured remote(s)",
                len(_all_remote_configs),
            )
            ws = WebServer(remote_configs=_all_remote_configs)
            _device_module._web_server_instance = ws
            ws.start()
        else:
            # No remotes yet; watchdog brings the server up after setup.
            _LOG.info(
                "External mode detected at boot - no remotes configured yet",
            )

        asyncio.create_task(_web_server_watchdog(interval=30))

    # Set up the setup handler
    discovery = ManagerDiscovery("_uc-remote._tcp.local.", timeout=3)
    setup_handler = RemoteSetupFlow.create_handler(driver, discovery=discovery)

    # Initialize the API with the driver configuration
    await driver.api.init("driver.json", setup_handler)

    # Keep the driver running
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
