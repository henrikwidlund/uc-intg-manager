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

import ucapi
import device as _device_module
from const import RemoteConfig, is_external_mode
from data_migration import migrate
from device import IntegrationManagerDevice, _all_remote_configs
from discover import ManagerDiscovery
from log_handler import setup_log_handler
from setup import RemoteSetupFlow
from ucapi_framework import BaseConfigManager, BaseIntegrationDriver, get_config_path
from web_server import WebServer

_LOG = logging.getLogger(__name__)


def _remote_id_from_ws(websocket) -> str | None:
    """Map an inbound ucapi WebSocket to the identifier of the configured remote
    whose IP matches the client's remote_address. Returns None if no match."""
    if not websocket or not getattr(websocket, "remote_address", None):
        return None
    host = websocket.remote_address[0]
    for cfg in _all_remote_configs:
        if cfg.address == host:
            return cfg.identifier
    return None


class IntegrationManagerDriver(BaseIntegrationDriver):
    """
    Custom driver that dispatches connect/disconnect/standby events to the
    specific remote that originated them.

    Each Remote opens its own ucapi WebSocket to this integration. The ucapi
    library forwards the originating WebSocket as a `websocket` kwarg to
    event handlers (see `ucapi.api._wrap_event_listener`), letting us look
    up the remote by client IP.

    Fallbacks when `websocket` is missing or can't be mapped to a configured
    remote:

      * `connect` / `exit-standby` → connect every configured device
        (safe to fan out; reconnecting a healthy device is idempotent).
      * `disconnect` / `enter-standby` → disconnect the owner device only
        (first remote in config). Avoids mass-disconnect while still
        responding to the lifecycle signal that some remote went away.
    """

    def _owner_device(self) -> IntegrationManagerDevice | None:
        """Return the owner device (first in config) or None if unknown."""
        owner_id = (
            _all_remote_configs[0].identifier if _all_remote_configs else None
        )
        return self._device_instances.get(owner_id) if owner_id else None

    async def on_r2_connect_cmd(self, websocket=None) -> None:
        """Connect the originating remote (or all, if source unknown)."""
        # The integration-level DeviceState is intentionally only set to
        # CONNECTED here and never flipped back. In external mode the
        # integration host is reachable for the lifetime of the process
        # regardless of any single remote's lifecycle, so reporting
        # DISCONNECTED on a remote's standby/disconnect would mislead
        # every *other* still-connected remote that observes the
        # broadcast state. This matches the BaseIntegrationDriver default
        # (connect sets CONNECTED, disconnect/standby leave state alone).
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


async def _web_server_watchdog(interval: float = 30) -> None:
    """In external mode, keep the web server alive and remote-status fresh,
    independent of polling.

    Polling stops when a remote disconnects/standbys (or hasn't connected
    yet after a process restart), so the per-poll health check in
    device.py can't recover a dead Hypercorn thread or seed connectivity
    state by itself. This task:

      * respawns the web server when its background thread exits
        (`_running` flipped False) or the instance was cleared, and
      * probes every configured remote so the UI shows a real online
        status even when no device has called `establish_connection`
        yet.
    """
    # Brief delay so the eagerly-started server has time to bind before
    # the first probe runs.
    await asyncio.sleep(2)
    while True:
        try:
            if not _all_remote_configs:
                await asyncio.sleep(interval)
                continue
            ws = _device_module._web_server_instance
            if ws is None or not ws.is_running:
                _LOG.warning(
                    "Watchdog: web server not running (instance=%s) - restarting",
                    "present" if ws else "missing",
                )
                if ws is not None:
                    try:
                        # WebServer.stop() joins the Hypercorn thread (up to
                        # 5s). Run it off-loop so other driver/device tasks
                        # keep making progress during the restart window.
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
                    _LOG.error("Watchdog: restart attempt failed - will retry")
                    _device_module._web_server_instance = None
                    continue

            # Heartbeat probe — keeps `_remote_online` truthful while
            # polling is idle (no remote connected, just-restarted process).
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

    # In external/Docker mode, the web server must stay reachable independent
    # of any remote's polling lifecycle. On-remote installs rely on dock/charge
    # state instead and start the web server lazily.
    if is_external_mode():
        # Eager start covers the common case: container restarts with one or
        # more remotes already configured. First-run setup (no remotes yet)
        # falls through; the watchdog will bring up the server once a remote
        # is added via setup.
        if _all_remote_configs:
            _LOG.info(
                "External mode detected at boot - starting web server with %d configured remote(s)",
                len(_all_remote_configs),
            )
            ws = WebServer(remote_configs=_all_remote_configs)
            _device_module._web_server_instance = ws
            ws.start()
        else:
            _LOG.info(
                "External mode detected at boot - no remotes configured yet, watchdog will start the web server once setup completes",
            )

        # Watchdog runs for the lifetime of the driver process. It:
        #   * respawns the web server if its background thread dies,
        #   * brings up the server the first time after a remote is added
        #     via setup (handles the empty-config-at-boot case), and
        #   * probes remote connectivity so UI status is fresh independent
        #     of device polling.
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
