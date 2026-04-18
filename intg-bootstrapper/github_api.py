"""
GitHub API Client for the Bootstrapper.

Handles downloading a specific Integration Manager release asset from GitHub.

:copyright: (c) 2025 by Jack Powell.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
import re
import ssl
from typing import Any

import aiohttp
import certifi

_LOG = logging.getLogger("github_api")

GITHUB_API_BASE = "https://api.github.com"

# Timeout for general GitHub API calls
_API_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Longer timeout for binary downloads (30 s connect, 5 min total)
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(connect=30, total=330)


class GitHubAPIError(Exception):
    """Exception raised when GitHub API calls fail."""


class GitHubClient:
    """
    Minimal async GitHub API client used by the bootstrapper.

    Only implements the methods needed to locate and download a specific
    release asset.
    """

    def __init__(self) -> None:
        """Initialise the GitHub API client."""
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return (or lazily create) the shared aiohttp session."""
        if self._session is None or self._session.closed:
            headers = {
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "uc-intg-bootstrapper",
            }
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=_API_TIMEOUT,
                connector=connector,
            )
        return self._session

    async def close(self) -> None:
        """Close the underlying aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            _LOG.debug("GitHubClient: session closed")

    # ------------------------------------------------------------------
    # Release look-up helpers
    # ------------------------------------------------------------------

    async def get_release_by_tag(
        self, owner: str, repo: str, tag: str
    ) -> dict[str, Any] | None:
        """
        Fetch a specific release by tag name.

        :param owner: GitHub repository owner.
        :param repo: GitHub repository name.
        :param tag: Release tag (e.g. "v2.1.0").
        :return: Release JSON dict or None if not found.
        """
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/tags/{tag}"
        _LOG.debug("GitHubClient: GET %s", url)
        session = await self._get_session()
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    release = await response.json()
                    _LOG.info(
                        "GitHubClient: found release %s for %s/%s",
                        tag,
                        owner,
                        repo,
                    )
                    return release
                if response.status == 404:
                    _LOG.warning(
                        "GitHubClient: release tag '%s' not found for %s/%s",
                        tag,
                        owner,
                        repo,
                    )
                    return None
                if response.status == 403:
                    _LOG.warning(
                        "GitHubClient: rate limit exceeded for %s/%s", owner, repo
                    )
                    return None
                _LOG.warning(
                    "GitHubClient: unexpected status %d for %s/%s tag %s",
                    response.status,
                    owner,
                    repo,
                    tag,
                )
                return None
        except aiohttp.ClientError as exc:
            _LOG.error(
                "GitHubClient: connection error fetching release %s/%s@%s: %s",
                owner,
                repo,
                tag,
                exc,
            )
            return None

    async def get_latest_release(self, owner: str, repo: str) -> dict[str, Any] | None:
        """
        Fetch the latest published release.

        :param owner: GitHub repository owner.
        :param repo: GitHub repository name.
        :return: Release JSON dict or None.
        """
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/latest"
        _LOG.debug("GitHubClient: GET %s", url)
        session = await self._get_session()
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    return await response.json()
                if response.status == 404:
                    _LOG.warning(
                        "GitHubClient: no releases found for %s/%s", owner, repo
                    )
                    return None
                if response.status == 403:
                    _LOG.warning(
                        "GitHubClient: rate limit exceeded for %s/%s", owner, repo
                    )
                    return None
                return None
        except aiohttp.ClientError as exc:
            _LOG.error(
                "GitHubClient: connection error fetching latest release for %s/%s: %s",
                owner,
                repo,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def download_release_asset(
        self,
        owner: str,
        repo: str,
        asset_pattern: str | None = None,
        version: str | None = None,
    ) -> tuple[bytes, str] | None:
        """
        Download a release asset (tar.gz) matching an optional filename pattern.

        :param owner: GitHub repository owner.
        :param repo: GitHub repository name.
        :param asset_pattern: Regex pattern to match against asset filenames.
                              Falls back to any ``.tar.gz`` file if None.
        :param version: Release tag to download. Uses latest release if None.
        :return: Tuple of (raw_bytes, filename) or None on failure.
        """
        # Resolve the release object
        if version:
            _LOG.info(
                "GitHubClient: looking up release %s for %s/%s", version, owner, repo
            )
            release = await self.get_release_by_tag(owner, repo, version)
            if not release:
                _LOG.error(
                    "GitHubClient: could not find release '%s' for %s/%s",
                    version,
                    owner,
                    repo,
                )
                return None
        else:
            _LOG.info("GitHubClient: fetching latest release for %s/%s", owner, repo)
            release = await self.get_latest_release(owner, repo)
            if not release:
                _LOG.error(
                    "GitHubClient: could not fetch latest release for %s/%s",
                    owner,
                    repo,
                )
                return None

        tag = release.get("tag_name", "unknown")
        assets: list[dict[str, Any]] = release.get("assets", [])

        if not assets:
            _LOG.error(
                "GitHubClient: release %s for %s/%s has no assets", tag, owner, repo
            )
            return None

        _LOG.debug(
            "GitHubClient: release %s has %d asset(s): %s",
            tag,
            len(assets),
            [a.get("name") for a in assets],
        )

        # Select the matching asset
        target_asset: dict[str, Any] | None = None
        if asset_pattern:
            try:
                pattern = re.compile(asset_pattern)
            except re.error as exc:
                _LOG.error(
                    "GitHubClient: invalid asset_pattern regex '%s': %s",
                    asset_pattern,
                    exc,
                )
                return None

            for asset in assets:
                if pattern.search(asset.get("name", "")):
                    target_asset = asset
                    _LOG.debug(
                        "GitHubClient: matched asset '%s' with pattern '%s'",
                        asset.get("name"),
                        asset_pattern,
                    )
                    break
        else:
            # Fall back to the first .tar.gz asset
            for asset in assets:
                if ".tar.gz" in asset.get("name", ""):
                    target_asset = asset
                    break

        if not target_asset:
            _LOG.error(
                "GitHubClient: no matching asset found in release %s for %s/%s "
                "(pattern=%s)",
                tag,
                owner,
                repo,
                asset_pattern,
            )
            return None

        download_url: str = target_asset.get("browser_download_url", "")
        filename: str = target_asset.get("name", "archive.tar.gz")

        if not download_url:
            _LOG.error("GitHubClient: asset '%s' has no browser_download_url", filename)
            return None

        _LOG.info("GitHubClient: downloading '%s' from %s …", filename, download_url)

        session = await self._get_session()
        try:
            async with session.get(
                download_url,
                headers={"Accept": "application/octet-stream"},
                timeout=_DOWNLOAD_TIMEOUT,
            ) as response:
                if response.status == 200:
                    data = await response.read()
                    _LOG.info(
                        "GitHubClient: downloaded '%s' — %d bytes", filename, len(data)
                    )
                    return data, filename
                _LOG.error(
                    "GitHubClient: download of '%s' failed with status %d",
                    filename,
                    response.status,
                )
                return None
        except aiohttp.ClientError as exc:
            _LOG.error(
                "GitHubClient: connection error downloading '%s': %s", filename, exc
            )
            return None
