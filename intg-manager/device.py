"""
Device Communication Module.

This module handles communication with the Unfolded Circle Remote.
It manages connections, polls power status, and controls the web server.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
import os
import socket
from asyncio import AbstractEventLoop
from datetime import datetime
from typing import Any
import asyncio

from const import (
    RemoteConfig,
    Settings,
    POWER_POLL_INTERVAL,
    VERSION_CHECK_INTERVAL_POLLS,
    WEB_SERVER_PORT,
)
from remote_api import RemoteAPIClient, RemoteAPIError
from web_server import WebServer
from ucapi_framework import BaseConfigManager, PollingDevice, BaseIntegrationDriver

_LOG = logging.getLogger(__name__)


class IntegrationManagerDevice(PollingDevice):
    """
    Device class representing the connection to the Unfolded Circle Remote.

    This class handles:
    - Polling the remote for power/dock status
    - Starting/stopping the web server based on dock status
    - Managing the remote API connection
    """

    def __init__(
        self,
        device_config: RemoteConfig,
        loop: AbstractEventLoop | None,
        config_manager: BaseConfigManager | None = None,
        driver: BaseIntegrationDriver | None = None,
    ) -> None:
        """
        Initialize the device.

        :param device_config: Configuration for this device
        :param loop: Event loop for async operations
        :param config_manager: Configuration manager instance
        """
        super().__init__(
            device_config=device_config,
            loop=loop,
            config_manager=config_manager,
            poll_interval=POWER_POLL_INTERVAL,
            driver=driver,
        )

        self._device_config: RemoteConfig = device_config

        # Load user settings
        self._settings = Settings.load()

        # Initialize the Remote API client
        self._client = RemoteAPIClient(
            address=device_config.address,
            pin=device_config.pin if device_config.pin else None,
            api_key=device_config.api_key if device_config.api_key else None,
        )

        # Web server instance
        self._web_server: WebServer | None = None

        # Track dock state
        self._is_docked: bool = False
        self._connected: bool = False

        # Track if we're running in external/Docker mode
        self._is_external: bool = False

        # Poll counter for periodic version checking
        self._poll_count: int = 0

        # Last backup date for scheduling
        self._last_backup_date: str | None = None

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def identifier(self) -> str:
        """Return the device identifier."""
        return self._device_config.identifier

    @property
    def name(self) -> str:
        """Return the device name."""
        return self._device_config.name

    @property
    def address(self) -> str | None:
        """Return the device address."""
        return self._device_config.address

    @property
    def log_id(self) -> str:
        """Return a log identifier for debugging."""
        return self.name if self.name else self.identifier

    @property
    def is_docked(self) -> bool:
        """Return whether the remote is currently docked."""
        return self._is_docked

    # =========================================================================
    # Connection Management
    # =========================================================================

    async def establish_connection(self) -> None:
        """Establish connection to the remote (required by PollingDevice)."""
        _LOG.debug("[%s] Connecting to remote at %s", self.log_id, self.address)

        try:
            # Test connection
            if await self._client.test_connection():
                self._connected = True
                _LOG.info("[%s] Connected to remote", self.log_id)

                # Check if we're running in Docker/external mode
                # In Docker, UC_CONFIG_HOME is set to /config
                self._is_external = os.getenv("UC_CONFIG_HOME", "").startswith(
                    "/config"
                )

                if self._is_external:
                    # Running in Docker - always start web server immediately
                    _LOG.info(
                        "[%s] Running in external/Docker mode - starting web server",
                        self.log_id,
                    )
                    self._is_docked = True  # Treat as always "docked" in Docker mode
                    await self._on_docked()
                else:
                    # Running on Remote - check dock state and start web server if charging
                    try:
                        self._is_docked = await self._client.is_docked()
                        if self._is_docked:
                            _LOG.info(
                                "[%s] Remote is charging at startup (dock or wireless)",
                                self.log_id,
                            )
                            await self._on_docked()
                        else:
                            _LOG.info(
                                "[%s] Remote is on battery at startup", self.log_id
                            )
                    except RemoteAPIError as e:
                        _LOG.warning(
                            "[%s] Failed to check initial charging status: %s",
                            self.log_id,
                            e,
                        )
                    except Exception as e:
                        # This catches web server startup failures
                        _LOG.error(
                            "[%s] Error during startup initialization: %s",
                            self.log_id,
                            e,
                        )
            else:
                raise RemoteAPIError("Connection test failed")

        except RemoteAPIError as e:
            _LOG.error("[%s] Failed to connect: %s", self.log_id, e)
            self._connected = False
            raise

    async def disconnect(self) -> None:
        """Disconnect from the remote."""
        _LOG.debug("[%s] Disconnecting from remote", self.log_id)

        # Stop web server if running
        if self._web_server and self._web_server.is_running:
            self._web_server.stop()
            self._web_server = None

        # Close API client
        await self._client.close()
        self._connected = False

        # Let base class handle stopping the polling
        await super().disconnect()

    async def verify_connection(self) -> None:
        """
        Verify connection to the remote and emit current state.

        This method is called by the framework to check device connectivity.
        """
        _LOG.debug("[%s] Verifying connection to remote", self.log_id)

        try:
            if await self._client.test_connection():
                self._connected = True
                _LOG.debug("[%s] Connection verified", self.log_id)
            else:
                self._connected = False
                _LOG.warning("[%s] Connection verification failed", self.log_id)
        except RemoteAPIError as err:
            _LOG.error("[%s] Connection verification failed: %s", self.log_id, err)
            self._connected = False
            raise

    # =========================================================================
    # Polling Implementation
    # =========================================================================

    async def poll_device(self) -> None:
        """
        Poll the remote for charging status (required by PollingDevice).

        This method is called periodically by the PollingDevice base class.
        It checks if the remote is charging (docked or wireless) and starts/stops
        the web server accordingly. Also triggers periodic version checks for
        installed integrations.

        When running in external/Docker mode, skip charging status checks since
        the web server should always be running.
        """
        self._poll_count += 1

        try:
            # Skip dock polling in external/Docker mode - web server always runs
            if not self._is_external:
                was_docked = self._is_docked
                self._is_docked = await self._client.is_docked()

                # Handle dock state changes
                if self._is_docked and not was_docked:
                    # Remote just docked - start web server
                    await self._on_docked()
                elif not self._is_docked and was_docked:
                    # Remote just undocked - stop web server
                    await self._on_undocked()

            # Periodic version check (every VERSION_CHECK_INTERVAL_POLLS polls)
            # Only check when docked and web server is running
            if (
                self._is_docked
                and self._web_server
                and self._web_server.is_running
                and self._poll_count % VERSION_CHECK_INTERVAL_POLLS == 0
            ):
                await self._check_integration_versions()

            # Periodic backup check - only when docked and web server is running
            if self._is_docked and self._web_server and self._web_server.is_running:
                await self._check_scheduled_backup()

            # Check for integration error states (disconnected, error, etc.) - runs every poll
            if self._web_server and self._web_server.is_running:
                self._web_server.check_error_states()

            # Fetch repository data batch (every 60 polls = 30 minutes at 30s each)
            # Only check when web server is running - internal rate limiting ensures max 10 requests/hour
            if (
                self._web_server
                and self._web_server.is_running
                and self._poll_count % 60 == 0
            ):
                self._web_server.fetch_repository_batch()

            # Web server health check - verify server is actually accessible when it should be running
            await self._check_web_server_health()

        except RemoteAPIError as e:
            _LOG.warning("[%s] Failed to poll power status: %s", self.log_id, e)

    async def _on_docked(self) -> None:
        """Handle remote being docked/charging - start web server."""
        _LOG.info("[%s] Remote charging started - starting web server", self.log_id)

        try:
            if self._web_server is None:
                self._web_server = WebServer(
                    address=self._device_config.address,
                    pin=self._device_config.pin if self._device_config.pin else None,
                    api_key=self._device_config.api_key
                    if self._device_config.api_key
                    else None,
                )

            if not self._web_server.is_running:
                self._web_server.start()

                # Give the server thread a moment to start and verify it didn't fail
                # The server sets _running = True immediately, but actual startup happens in background
                await asyncio.sleep(0.5)

                if self._web_server.is_running:
                    _LOG.info("[%s] Web server started successfully", self.log_id)

                    # Trigger initial checks on startup
                    _LOG.info(
                        "[%s] Triggering initial integration checks...", self.log_id
                    )
                    try:
                        # Check for version updates
                        # self._web_server.refresh_integration_versions()

                        # Check for new integrations in registry
                        # self._web_server.check_new_integrations()

                        # Check for orphaned entities in activities (async version for startup)
                        await self._web_server.check_orphaned_entities_async()

                        # TEMPORARILY DISABLED FOR TESTING - RESTORE LATER
                        # Check for new system messages from GitHub
                        # self._web_server.check_system_messages()
                        # END TEMPORARY DISABLE

                        _LOG.info(
                            "[%s] Initial integration checks complete", self.log_id
                        )
                    except Exception as e:
                        _LOG.warning(
                            "[%s] Initial integration checks failed: %s", self.log_id, e
                        )
                else:
                    _LOG.error(
                        "[%s] Web server failed to start (check logs for port conflicts)",
                        self.log_id,
                    )
                    self._web_server = None
        except Exception as e:
            _LOG.error(
                "[%s] Failed to start web server: %s", self.log_id, e, exc_info=True
            )
            self._web_server = None

    async def _on_undocked(self) -> None:
        """Handle remote being undocked/unplugged - conditionally stop web server."""
        if not self._settings.shutdown_on_battery:
            _LOG.info(
                "[%s] Remote on battery - web server remains running (shutdown_on_battery=False)",
                self.log_id,
            )
            return

        _LOG.info("[%s] Remote on battery - stopping web server", self.log_id)

        if self._web_server and self._web_server.is_running:
            self._web_server.stop()
            _LOG.info("[%s] Web server stopped", self.log_id)

    async def _check_integration_versions(self) -> None:
        """
        Check for updates to installed integrations.

        This is called periodically during polling to refresh version info.
        The web server caches this data for display in the UI.
        """
        if not self._web_server:
            return

        _LOG.info("[%s] Checking for integration updates...", self.log_id)
        try:
            # Trigger the web server to refresh version data
            # This updates the cached update availability info and sends update notifications
            # self._web_server.refresh_integration_versions()

            # Check for new integrations in registry
            # self._web_server.check_new_integrations()

            # Check for orphaned entities in activities
            self._web_server.check_orphaned_entities()

            # TEMPORARILY DISABLED FOR TESTING - RESTORE LATER
            # Check for new system messages from GitHub
            # self._web_server.check_system_messages()
            # END TEMPORARY DISABLE

            _LOG.debug("[%s] Integration checks complete", self.log_id)
        except Exception as e:
            _LOG.warning("[%s] Failed to check integrations: %s", self.log_id, e)

    def _is_backup_time(self, backup_time_str: str) -> bool:
        """
        Check if the current time matches the scheduled backup time.

        :param backup_time_str: Time string in "HH:MM" format
        :return: True if it's time to backup
        """

        try:
            now = datetime.now()
            current_date = now.strftime("%Y-%m-%d")

            # Check if we already backed up today
            if self._last_backup_date == current_date:
                return False

            # Parse the backup time
            backup_hour, backup_minute = map(int, backup_time_str.split(":"))

            # Check if current time matches (within the polling window)
            current_hour = now.hour
            current_minute = now.minute

            # Allow a 5-minute window around the scheduled time
            if backup_hour == current_hour:
                time_diff = abs(current_minute - backup_minute)
                return time_diff <= 5

            return False
        except (ValueError, AttributeError) as e:
            _LOG.warning(
                "[%s] Invalid backup time format '%s': %s",
                self.log_id,
                backup_time_str,
                e,
            )
            return False

    async def _check_scheduled_backup(self) -> None:
        """
        Check if it's time for scheduled backup and perform if needed.

        This is called during each poll cycle when docked and web server is running.
        """
        if not self._web_server:
            return

        try:
            # Load current settings to check if backups are enabled
            settings = Settings.load()

            if not settings.backup_configs:
                return  # Automatic backups disabled

            if not self._is_backup_time(settings.backup_time):
                return  # Not backup time yet

            _LOG.info(
                "[%s] Starting scheduled backup at %s",
                self.log_id,
                settings.backup_time,
            )

            # Perform the backup via web server (run in executor since it's sync)
            backup_result = await self._loop.run_in_executor(
                None, self._web_server.perform_scheduled_backup
            )

            # Update last backup date on success
            if backup_result:
                self._last_backup_date = datetime.now().strftime("%Y-%m-%d")
                _LOG.info("[%s] Scheduled backup completed successfully", self.log_id)
            else:
                _LOG.warning("[%s] Scheduled backup failed", self.log_id)

        except Exception as e:
            _LOG.error("[%s] Error during scheduled backup: %s", self.log_id, e)

    async def _check_web_server_health(self) -> None:
        """
        Check if the web server is healthy and restart if needed.

        This verifies that the web server is actually accessible when it should be running.
        If the server is supposed to be running but is not responding, it performs cleanup
        and restarts the server.

        Called during each poll cycle when the web server should be active.
        """
        # Only check if we think the server should be running
        if not self._web_server or not self._web_server.is_running:
            return

        # Determine if web server should actually be running based on current conditions
        should_be_running = False

        if self._is_external:
            # External/Docker mode - always running
            should_be_running = True
        elif self._is_docked:
            # Docked - always running
            should_be_running = True
        elif not self._settings.shutdown_on_battery:
            # On battery but configured to keep running
            should_be_running = True

        if not should_be_running:
            return  # Server should not be running, skip health check

        # Test if server is actually accessible
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)  # 2 second timeout
            result = sock.connect_ex(("127.0.0.1", WEB_SERVER_PORT))
            sock.close()

            if result == 0:
                # Server is accessible
                return

            # Server is not accessible but should be running
            _LOG.warning(
                "[%s] Web server should be running but is not accessible on port %d - attempting restart",
                self.log_id,
                WEB_SERVER_PORT,
            )

        except Exception as e:
            _LOG.warning(
                "[%s] Failed to check web server health: %s - attempting restart",
                self.log_id,
                e,
            )

        # Server is not healthy - perform cleanup and restart
        try:
            # Stop the current server instance (cleanup)
            if self._web_server:
                try:
                    self._web_server.stop()
                except Exception as e:
                    _LOG.warning(
                        "[%s] Error stopping unhealthy web server: %s", self.log_id, e
                    )

            await asyncio.sleep(1)

            # Create new server instance
            self._web_server = WebServer(
                address=self._device_config.address,
                pin=self._device_config.pin if self._device_config.pin else None,
                api_key=self._device_config.api_key
                if self._device_config.api_key
                else None,
            )

            # Start the server
            self._web_server.start()

            # Give it a moment to start
            await asyncio.sleep(0.5)

            if self._web_server.is_running:
                _LOG.info(
                    "[%s] Web server successfully restarted after health check failure",
                    self.log_id,
                )
            else:
                _LOG.error(
                    "[%s] Web server failed to restart after health check failure",
                    self.log_id,
                )
                self._web_server = None

        except Exception as e:
            _LOG.error(
                "[%s] Failed to restart web server during health check: %s",
                self.log_id,
                e,
                exc_info=True,
            )
            self._web_server = None

    # =========================================================================
    # Command Handling
    # =========================================================================

    async def send_command(self, command: str, *_args: Any, **_kwargs: Any) -> None:
        """
        Send a command to the device.

        This integration doesn't have traditional commands as it's a manager UI.

        :param command: Command to send
        :param _args: Positional arguments (unused)
        :param _kwargs: Keyword arguments (unused)
        """
        _LOG.debug("[%s] Command received: %s (not implemented)", self.log_id, command)
