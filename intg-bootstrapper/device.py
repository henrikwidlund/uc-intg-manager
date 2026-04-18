"""
Bootstrapper Device.

This module contains the ``BootstrapperDevice`` class, which performs the
Integration Manager self-update after setup completes.

Upgrade sequence (all over the UC Remote's loopback REST API):
1. Download the target IM release tar.gz from GitHub.
2. Delete the old IM driver (wipes its config directory).
3. Install the new IM driver from the downloaded archive.
4. Restore manager.json and config.json to the new IM config directory.
5. Delete the bootstrapper driver (self-delete).

:copyright: (c) 2025 by Jack Powell.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import json
import logging

import aiohttp

from const import (
    BOOTSTRAPPER_DRIVER_ID,
    DEV_DOWNLOAD_URL,
    IM_ASSET_PATTERN,
    IM_GITHUB_OWNER,
    IM_GITHUB_REPO,
    BootstrapperConfig,
)
from github_api import GitHubClient
from sync_api import LoopbackRemoteClient, RemoteAPIError
from ucapi_framework import StatelessHTTPDevice

_LOG = logging.getLogger("device")


class BootstrapperDevice(StatelessHTTPDevice):
    """
    Device class for the bootstrapper integration.

    No entities are created. On ``verify_connection`` the device schedules
    the upgrade as a background task so the framework's setup flow can
    complete normally before the heavy work starts.
    """

    def __init__(self, config: BootstrapperConfig, **_kwargs) -> None:
        """
        Initialise the bootstrapper device.

        :param config: Populated BootstrapperConfig from the setup flow.
        """
        super().__init__(device_config=config)
        self._config = config
        self._upgrade_task: asyncio.Task | None = None

        # Extract the API key and remote address from the first entry in config_data.
        # config_data is the serialised contents of config.json — a JSON array of
        # remote configs, each with an "api_key" and "address" field.
        self._api_key, self._remote_address = self._parse_remote_credentials(
            config.config_data
        )

        _LOG.info(
            "BootstrapperDevice initialised — target=%s driver_id=%s remote=%s auth=%s",
            config.target_version,
            config.manager_driver_id,
            self._remote_address,
            "api_key" if self._api_key else "none",
        )

    # ------------------------------------------------------------------
    # BaseDeviceInterface abstract property implementations
    # ------------------------------------------------------------------

    @property
    def identifier(self) -> str:
        """Return the device identifier."""
        return self._config.identifier

    @property
    def name(self) -> str:
        """Return the device name."""
        return self._config.name

    @property
    def address(self) -> str | None:
        """Return the device address (loopback)."""
        return self._config.address

    @property
    def log_id(self) -> str:
        """Return the log identifier."""
        return self.name

    @staticmethod
    def _parse_remote_credentials(config_data: str) -> tuple[str, str]:
        """
        Parse the API key and remote address from the serialised config.json.

        config.json is a JSON array; we use the first entry only.
        Returns (api_key, address) — both may be empty strings if parsing fails.

        :param config_data: Serialised JSON string from the setup payload.
        :return: Tuple of (api_key, address).
        """
        try:
            entries = json.loads(config_data)
            if not isinstance(entries, list) or not entries:
                _LOG.warning(
                    "Bootstrapper: config_data is not a non-empty list — no auth available"
                )
                return "", ""
            first = entries[0]
            api_key = first.get("api_key", "")
            address = first.get("address", "")
            if not api_key:
                _LOG.warning(
                    "Bootstrapper: no api_key found in config_data entry — calls will be unauthenticated"
                )
            else:
                _LOG.debug("Bootstrapper: extracted api_key for remote at %s", address)
            return api_key, address
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            _LOG.error(
                "Bootstrapper: failed to parse config_data for credentials: %s", exc
            )
            return "", ""

    # ------------------------------------------------------------------
    # StatelessHTTPDevice overrides
    # ------------------------------------------------------------------

    async def verify_connection(self) -> None:
        """
        Called by the framework after setup completes.

        Schedules the upgrade as a fire-and-forget background task so the
        setup flow returns success immediately. Raises no exception so the
        framework considers the device connected.
        """
        if self._upgrade_task is not None and not self._upgrade_task.done():
            _LOG.warning(
                "Bootstrapper: upgrade already in progress — ignoring duplicate verify_connection"
            )
            return
        _LOG.info(
            "Bootstrapper: verify_connection — scheduling upgrade to %s",
            self._config.target_version,
        )
        self._upgrade_task = asyncio.create_task(
            self._run_upgrade(), name="bootstrapper_upgrade"
        )
        # Short yield so the task is actually scheduled before we return
        await asyncio.sleep(0)

    # ------------------------------------------------------------------
    # Upgrade pipeline
    # ------------------------------------------------------------------

    async def _run_upgrade(self) -> None:
        """
        Main upgrade coroutine.  Runs all steps in sequence and logs each one.

        Any exception is caught at the top level and logged so the task does
        not die silently.
        """
        _LOG.info("=== Bootstrapper upgrade starting ===")
        _LOG.info(
            "Target version: %s  |  Manager driver ID: %s",
            self._config.target_version,
            self._config.manager_driver_id,
        )

        try:
            # Step 1 — Download the new IM archive from GitHub
            archive_bytes, filename = await self._download_manager_archive()

            # Step 2 — Delete the old IM driver (config dir wiped by the remote)
            await self._delete_old_manager()

            # Step 3 — Wait briefly to give the remote time to clean up
            _LOG.debug("Bootstrapper: pausing 3 s after driver deletion …")
            await asyncio.sleep(3)

            # Step 4 — Install the new IM driver
            installed_driver_id = await self._install_new_manager(
                archive_bytes, filename
            )

            # Step 5 — Wait for the Remote to register and start the new driver
            _LOG.debug("Bootstrapper: pausing 5 s for driver registration …")
            await asyncio.sleep(5)

            # Step 6 — Run the new IM's setup flow to restore config.json
            #           (reconnects IM to the remotes) and hand off manager.json
            #           (restores integration configs / settings).
            await self._setup_new_manager(installed_driver_id)

            _LOG.info("=== Bootstrapper upgrade complete — self-deleting ===")

            # Step 7 — Delete ourselves (bootstrapper)
            await self._self_delete()

        except Exception as exc:  # pylint: disable=broad-except
            _LOG.exception("=== Bootstrapper upgrade FAILED: %s ===", exc)

    # ------------------------------------------------------------------
    # Step implementations
    # ------------------------------------------------------------------

    async def _download_manager_archive(self) -> tuple[bytes, str]:
        """
        Download the target IM release archive from GitHub.

        :raises RuntimeError: If the download fails.
        :return: Tuple of (archive_bytes, filename).
        """
        # Dev override: skip GitHub and download directly from a local URL.
        if DEV_DOWNLOAD_URL:
            _LOG.warning(
                "Bootstrapper: DEV_DOWNLOAD_URL set — downloading from %s",
                DEV_DOWNLOAD_URL,
            )
            filename = DEV_DOWNLOAD_URL.rstrip("/").split("/")[-1] or "archive.tar.gz"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        DEV_DOWNLOAD_URL,
                        timeout=aiohttp.ClientTimeout(connect=10, total=120),
                    ) as resp:
                        if resp.status != 200:
                            raise RuntimeError(
                                f"Dev download failed: HTTP {resp.status} from {DEV_DOWNLOAD_URL}"
                            )
                        archive_bytes = await resp.read()
                _LOG.info(
                    "Bootstrapper: dev-downloaded %s (%d bytes)",
                    filename,
                    len(archive_bytes),
                )
                return archive_bytes, filename
            except aiohttp.ClientError as exc:
                raise RuntimeError(f"Dev download error: {exc}") from exc

        _LOG.info(
            "Bootstrapper: downloading %s/%s @ %s …",
            IM_GITHUB_OWNER,
            IM_GITHUB_REPO,
            self._config.target_version,
        )
        client = GitHubClient()
        try:
            result = await client.download_release_asset(
                owner=IM_GITHUB_OWNER,
                repo=IM_GITHUB_REPO,
                asset_pattern=IM_ASSET_PATTERN,
                version=self._config.target_version,
            )
        finally:
            await client.close()

        if result is None:
            raise RuntimeError(
                f"Failed to download IM archive for version {self._config.target_version}"
            )

        archive_bytes, filename = result
        _LOG.info(
            "Bootstrapper: downloaded %s (%d bytes)", filename, len(archive_bytes)
        )
        return archive_bytes, filename

    async def _delete_old_manager(self) -> None:
        """
        Delete the currently installed IM driver via the loopback API.

        Performs a two-step delete per variant: instances first, then the
        driver.  Tries the configured driver ID and common deduplicated
        suffixes ("2"–"5") that the remote may have assigned.  A 404 on
        any step is treated as "already gone" and is not an error.

        :raises RemoteAPIError: If a non-404 delete request fails.
        """
        base_driver_id = self._config.manager_driver_id
        # Try the exact ID plus the deduplication suffixes the remote appends
        candidates = [base_driver_id] + [f"{base_driver_id}{n}" for n in range(2, 6)]
        _LOG.info(
            "Bootstrapper: deleting old IM driver '%s' (and any deduped variants) …",
            base_driver_id,
        )
        async with LoopbackRemoteClient(
            host=self._remote_address, api_key=self._api_key
        ) as client:
            for driver_id in candidates:
                # Step 1 — delete all instances for this variant first
                try:
                    instance_ids = await client.get_instances(driver_id)
                    for instance_id in instance_ids:
                        try:
                            await client.delete_instance(instance_id)
                        except RemoteAPIError as e:
                            _LOG.warning(
                                "Failed to delete instance '%s': %s", instance_id, e
                            )
                except RemoteAPIError as e:
                    if "404" in str(e) or "NOT_FOUND" in str(e):
                        pass  # driver doesn't exist, nothing to delete
                    else:
                        _LOG.warning(
                            "Could not retrieve instances for '%s': %s", driver_id, e
                        )
                # Step 2 — delete the driver itself (404 = already gone)
                try:
                    await client.delete_driver(driver_id)
                    _LOG.info("Bootstrapper: deleted driver variant '%s'", driver_id)
                except RemoteAPIError as e:
                    if "404" in str(e) or "NOT_FOUND" in str(e):
                        pass  # not present, that's fine
                    else:
                        raise
        _LOG.info("Bootstrapper: old IM driver cleanup complete")

    async def _install_new_manager(self, archive_bytes: bytes, filename: str) -> None:
        """
        Install the new IM driver from the downloaded archive.

        :param archive_bytes: Raw bytes of the tar.gz archive.
        :param filename: Original filename of the archive (used as multipart name).
        :raises RemoteAPIError: If the install request fails.
        """
        _LOG.info(
            "Bootstrapper: installing new IM driver from '%s' (%d bytes) …",
            filename,
            len(archive_bytes),
        )
        async with LoopbackRemoteClient(
            host=self._remote_address, api_key=self._api_key
        ) as client:
            result = await client.install_integration(archive_bytes, filename)
        _LOG.info("Bootstrapper: install response: %s", result)
        # The remote may assign a deduplicated ID (e.g. intg_manager_driver_dev2)
        # if the previous driver wasn't fully purged.  Use whatever the remote returned.
        installed_id: str = result.get("driver_id", self._config.manager_driver_id)
        if installed_id != self._config.manager_driver_id:
            _LOG.warning(
                "Bootstrapper: remote assigned driver_id '%s' instead of expected '%s' — using installed id",
                installed_id,
                self._config.manager_driver_id,
            )
        return installed_id

    async def _setup_new_manager(self, installed_driver_id: str) -> None:
        """
        Drive the freshly installed IM through its setup flow to restore state.

        The new IM starts with no ``config.json`` and no ``manager.json``.  We
        run its setup flow programmatically:

        1. ``start_setup()``         → IM returns ``RESTORE_PROMPT`` screen
        2. ``send_setup_input({restore_from_backup: true})``
                                     → IM returns ``RESTORE`` screen
        3. ``send_setup_input({restore_data: config_data, manager_data: manager_data})``
                                     → IM calls ``restore_from_backup_json`` (reconnects
                                       to the remotes) **and** writes manager.json.
                                       Returns ``SetupComplete``.

        The ``manager_data`` field is handled by an override of
        ``_handle_restore_response`` in ``intg-manager/setup.py``.

        :raises RuntimeError: If the new IM setup does not reach WAIT_USER_ACTION.
        :raises RemoteAPIError: If any API call fails.
        """
        driver_id = installed_driver_id
        _LOG.info("Bootstrapper: starting setup on new IM driver '%s' …", driver_id)

        async with LoopbackRemoteClient(
            host=self._remote_address, api_key=self._api_key
        ) as client:
            # Step 6a — Initiate setup (retry until driver is ready)
            for attempt in range(10):
                try:
                    await client.start_setup(driver_id)
                    break
                except RemoteAPIError as e:
                    if ("404" in str(e) or "NOT_FOUND" in str(e)) and attempt < 9:
                        _LOG.debug(
                            "Bootstrapper: driver '%s' not ready yet (attempt %d/10), retrying in 3 s …",
                            driver_id,
                            attempt + 1,
                        )
                        await asyncio.sleep(3)
                    else:
                        raise
            await asyncio.sleep(2)

            # Step 6b — Poll until WAIT_USER_ACTION
            for attempt in range(6):
                state_resp = await client.get_setup(driver_id)
                state = state_resp.get("state", "")
                _LOG.debug(
                    "Bootstrapper: setup state for '%s' (attempt %d): %s",
                    driver_id,
                    attempt + 1,
                    state,
                )
                if state == "WAIT_USER_ACTION":
                    break
                await asyncio.sleep(3)
            else:
                raise RuntimeError(
                    f"New IM driver '{driver_id}' did not reach WAIT_USER_ACTION "
                    f"after setup start (last state: {state!r})"
                )

            # Step 6c — Accept the restore-prompt screen (request restore)
            _LOG.info(
                "Bootstrapper: sending restore-prompt response to '%s' …", driver_id
            )
            await client.send_setup_input(driver_id, {"restore_from_backup": "true"})
            await asyncio.sleep(2)

            # Poll again — IM should now be on the RESTORE screen
            for attempt in range(4):
                state_resp = await client.get_setup(driver_id)
                state = state_resp.get("state", "")
                if state == "WAIT_USER_ACTION":
                    break
                await asyncio.sleep(2)

            # Step 6d — Send both config.json and manager.json in one pass.
            # IM's overridden _handle_restore_response will:
            #   • call config.restore_from_backup_json(restore_data) → reconnects remotes
            #   • write manager_data to MANAGER_DATA_FILE
            _LOG.info(
                "Bootstrapper: sending restore data (%d config bytes, %d manager bytes) to '%s' …",
                len(self._config.config_data),
                len(self._config.manager_data),
                driver_id,
            )
            await client.send_setup_input(
                driver_id,
                {
                    "restore_data": self._config.config_data,
                    "manager_data": self._config.manager_data,
                },
            )

        _LOG.info(
            "Bootstrapper: new IM setup complete — config and manager data restored"
        )

    async def _self_delete(self) -> None:
        """
        Delete the bootstrapper driver itself via the loopback API.

        After this call the bootstrapper process will be killed by the remote.
        Any code after this point may not execute.
        """
        _LOG.info("Bootstrapper: self-deleting driver '%s' …", BOOTSTRAPPER_DRIVER_ID)
        try:
            async with LoopbackRemoteClient(
                host=self._remote_address, api_key=self._api_key
            ) as client:
                await client.delete_driver(BOOTSTRAPPER_DRIVER_ID)
        except RemoteAPIError as exc:
            # The remote may kill the process before the response arrives — log
            # and continue rather than raising, since the outcome is already done.
            _LOG.warning(
                "Bootstrapper: self-delete returned an error (may be expected): %s",
                exc,
            )
