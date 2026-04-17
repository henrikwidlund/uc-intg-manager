"""
Loopback Remote API Client for the Bootstrapper.

Provides a minimal async HTTP client that talks to the UC Remote's own
REST API over the loopback interface (127.0.0.1:80).  No PIN or API key
is required because integration processes run in a trusted context on
the remote itself.

Only the methods required by the bootstrapper upgrade pipeline are
implemented here.

:copyright: (c) 2025 by Jack Powell.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any

import aiohttp

from const import REMOTE_LOOPBACK_HOST, REMOTE_LOOPBACK_PORT

_LOG = logging.getLogger("sync_api")

# Default timeout for API calls (connect, total) in seconds
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(connect=10, total=40)
# Longer timeout for install operations
_INSTALL_TIMEOUT = aiohttp.ClientTimeout(connect=30, total=150)


class RemoteAPIError(Exception):
    """Raised when a loopback Remote API call fails."""


class LoopbackRemoteClient:
    """
    Async client for the UC Remote's loopback REST API.

    Designed for use as an async context manager::

        async with LoopbackRemoteClient(api_key="Bearer ...") as client:
            await client.delete_driver("intg_manager_driver")

    Requests go to ``http://127.0.0.1:80/api`` authenticated with the API key
    extracted from the first entry in the ``config_data`` setup payload.
    """

    def __init__(
        self,
        host: str = REMOTE_LOOPBACK_HOST,
        port: int = REMOTE_LOOPBACK_PORT,
        api_key: str = "",
    ) -> None:
        """
        Initialise the loopback client.

        :param host: Loopback hostname or IP (default: 127.0.0.1).
        :param port: API port (default: 80).
        :param api_key: Bearer token from the remote's config.json entry.
        """
        self._base_url = f"http://{host}:{port}/api"
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None
        _LOG.debug(
            "LoopbackRemoteClient: base_url=%s auth=%s",
            self._base_url,
            "api_key" if api_key else "none",
        )

    # ------------------------------------------------------------------
    # Context manager / session lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "LoopbackRemoteClient":
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        self._session = aiohttp.ClientSession(
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
            connector=aiohttp.TCPConnector(ssl=False),
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------
    # Low-level request helper
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        endpoint: str,
        timeout: aiohttp.ClientTimeout | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        Perform an authenticated HTTP request to the Remote API.

        :param method: HTTP method (GET, POST, PUT, DELETE, …).
        :param endpoint: API path, e.g. ``/intg/drivers/foo``.
        :param timeout: Optional per-request timeout override.
        :raises RemoteAPIError: On authentication failure, HTTP error, or
            connection error.
        :return: Parsed JSON response body, or None if the response was empty.
        """
        if self._session is None:
            raise RemoteAPIError(
                "LoopbackRemoteClient must be used as an async context manager"
            )

        url = f"{self._base_url}{endpoint}"
        _LOG.debug("LoopbackRemoteClient: %s %s", method, url)

        request_kwargs = dict(kwargs)
        if timeout:
            request_kwargs["timeout"] = timeout

        try:
            async with self._session.request(method, url, **request_kwargs) as response:
                if response.status == 401:
                    raise RemoteAPIError(
                        f"Authentication failed for {method} {endpoint}"
                    )
                if response.status == 403:
                    raise RemoteAPIError(
                        f"Access forbidden for {method} {endpoint}"
                    )
                if response.status >= 400:
                    body = await response.text()
                    raise RemoteAPIError(
                        f"API error {response.status} for {method} {endpoint}: {body}"
                    )
                text = await response.text()
                if text:
                    return await response.json(content_type=None)
                return None
        except aiohttp.ClientError as exc:
            raise RemoteAPIError(
                f"Connection error for {method} {endpoint}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Driver management
    # ------------------------------------------------------------------

    async def get_instances(self, driver_id: str) -> list[str]:
        """
        Return the integration instance IDs for a given driver.

        :param driver_id: The driver ID to query.
        :return: List of instance ID strings (may be empty).
        """
        result = await self._request("GET", f"/intg/instances?driver_id={driver_id}")
        if result is None:
            return []
        items = result.get("items", result) if isinstance(result, dict) else result
        return [i["id"] for i in items if isinstance(i, dict) and "id" in i]

    async def delete_instance(self, instance_id: str) -> None:
        """
        Delete a single integration instance.

        :param instance_id: The instance ID to delete.
        :raises RemoteAPIError: If the API call fails.
        """
        _LOG.info("LoopbackRemoteClient: deleting instance '%s' …", instance_id)
        await self._request("DELETE", f"/intg/instances/{instance_id}")
        _LOG.info("LoopbackRemoteClient: instance '%s' deleted", instance_id)

    async def delete_driver(self, driver_id: str) -> bool:
        """
        Delete an integration driver.

        This call also removes the driver's config directory from the remote
        filesystem, which is the reason both manager.json and config.json
        must be serialised before this call is made.

        :param driver_id: The driver ID to delete (e.g. "intg_manager_driver").
        :raises RemoteAPIError: If the API call fails.
        :return: True on success.
        """
        _LOG.info("LoopbackRemoteClient: deleting driver '%s' …", driver_id)
        await self._request("DELETE", f"/intg/drivers/{driver_id}")
        _LOG.info("LoopbackRemoteClient: driver '%s' deleted", driver_id)
        return True

    # ------------------------------------------------------------------
    # Driver installation
    # ------------------------------------------------------------------

    async def install_integration(
        self, archive_data: bytes, filename: str
    ) -> dict[str, Any]:
        """
        Install an integration driver from a tar.gz archive.

        :param archive_data: Raw bytes of the ``.tar.gz`` archive.
        :param filename: Filename used in the multipart form upload.
        :raises RemoteAPIError: If the install request fails.
        :return: Response JSON from the remote (may be ``{"status": "ok"}``).
        """
        if self._session is None:
            raise RemoteAPIError(
                "LoopbackRemoteClient must be used as an async context manager"
            )

        url = f"{self._base_url}/intg/install"
        _LOG.info(
            "LoopbackRemoteClient: installing '%s' (%d bytes) …",
            filename,
            len(archive_data),
        )

        form = aiohttp.FormData()
        form.add_field(
            "file",
            archive_data,
            filename=filename,
            content_type="application/x-gzip",
        )

        try:
            async with self._session.post(
                url, data=form, timeout=_INSTALL_TIMEOUT
            ) as response:
                if response.status == 401:
                    raise RemoteAPIError("Authentication failed during install")
                if response.status == 403:
                    raise RemoteAPIError("Access forbidden during install")
                if response.status >= 400:
                    body = await response.text()
                    raise RemoteAPIError(
                        f"Install failed with status {response.status}: {body}"
                    )
                text = await response.text()
                if text:
                    result = await response.json(content_type=None)
                    _LOG.info(
                        "LoopbackRemoteClient: install of '%s' succeeded: %s",
                        filename,
                        result,
                    )
                    return result
                _LOG.info(
                    "LoopbackRemoteClient: install of '%s' succeeded (empty response)",
                    filename,
                )
                return {"status": "ok"}
        except aiohttp.ClientError as exc:
            raise RemoteAPIError(f"Install request failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Integration setup flow
    # ------------------------------------------------------------------

    async def start_setup(self, driver_id: str) -> dict[str, Any]:
        """
        Initiate the setup flow for an integration driver.

        Sends ``POST /intg/setup`` with a JSON body containing the driver ID,
        which causes the Remote to send a ``DriverSetupRequest`` event to the
        integration via WebSocket.

        :param driver_id: The driver ID to start setup for.
        :raises RemoteAPIError: If the API call fails.
        :return: Response JSON from the remote.
        """
        _LOG.info("LoopbackRemoteClient: starting setup for '%s' …", driver_id)
        payload = {"driver_id": driver_id, "reconfigure": False, "setup_data": {}}
        result = await self._request("POST", "/intg/setup", json=payload)
        _LOG.debug("LoopbackRemoteClient: start_setup response: %s", result)
        return result or {}

    async def get_setup(self, driver_id: str) -> dict[str, Any]:
        """
        Poll the current setup state for an integration driver.

        Sends ``GET /intg/setup/{driver_id}`` and returns the state object.
        The ``state`` field will be ``"WAIT_USER_ACTION"`` once the integration
        is ready to receive setup input.

        :param driver_id: The driver ID to query.
        :raises RemoteAPIError: If the API call fails.
        :return: Response JSON, e.g. ``{"state": "WAIT_USER_ACTION", ...}``.
        """
        _LOG.debug("LoopbackRemoteClient: polling setup state for '%s' …", driver_id)
        result = await self._request("GET", f"/intg/setup/{driver_id}")
        _LOG.debug("LoopbackRemoteClient: setup state for '%s': %s", driver_id, result)
        return result or {}

    async def send_setup_input(
        self, driver_id: str, input_values: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Send user-data input to an integration's setup flow.

        Sends ``PUT /intg/setup/{driver_id}`` with ``input_values`` as the
        body, which causes the Remote to forward a ``UserDataResponse`` to the
        integration via WebSocket.

        :param driver_id: The driver ID to send input to.
        :param input_values: Key/value pairs matching the integration's form fields.
        :raises RemoteAPIError: If the API call fails.
        :return: Response JSON from the remote.
        """
        _LOG.info(
            "LoopbackRemoteClient: sending setup input for '%s' (keys: %s) …",
            driver_id,
            list(input_values.keys()),
        )
        result = await self._request(
            "PUT",
            f"/intg/setup/{driver_id}",
            json={"input_values": input_values},
        )
        _LOG.debug("LoopbackRemoteClient: send_setup_input response: %s", result)
        return result or {}
