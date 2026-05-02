"""Tests for notification settings dataclasses and logic."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notification_settings import (  # noqa: E402
    DiscordNotificationConfig,
    HomeAssistantNotificationConfig,
    NotificationSettings,
    NotificationTriggers,
    NtfyNotificationConfig,
    PushoverNotificationConfig,
    WebhookNotificationConfig,
)


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


def test_home_assistant_defaults():
    cfg = HomeAssistantNotificationConfig()
    assert cfg.enabled is False
    assert cfg.url == ""
    assert cfg.token == ""
    assert cfg.service == "notify"


def test_webhook_defaults():
    cfg = WebhookNotificationConfig()
    assert cfg.enabled is False
    assert cfg.url == ""
    assert cfg.headers == {}


def test_pushover_defaults():
    cfg = PushoverNotificationConfig()
    assert cfg.enabled is False
    assert cfg.user_key == ""
    assert cfg.app_token == ""


def test_ntfy_defaults():
    cfg = NtfyNotificationConfig()
    assert cfg.enabled is False
    assert cfg.server == "https://ntfy.sh"
    assert cfg.topic == ""
    assert cfg.token == ""


def test_discord_defaults():
    cfg = DiscordNotificationConfig()
    assert cfg.enabled is False
    assert cfg.webhook_url == ""


def test_notification_triggers_defaults():
    triggers = NotificationTriggers()
    assert triggers.integration_update_available is True
    assert triggers.new_integration_in_registry is False
    assert triggers.integration_error_state is True
    assert triggers.orphaned_entities_detected is True
    assert triggers.firmware_update_available is True


def test_notification_settings_defaults():
    ns = NotificationSettings()
    assert isinstance(ns.home_assistant, HomeAssistantNotificationConfig)
    assert isinstance(ns.webhook, WebhookNotificationConfig)
    assert isinstance(ns.pushover, PushoverNotificationConfig)
    assert isinstance(ns.ntfy, NtfyNotificationConfig)
    assert isinstance(ns.discord, DiscordNotificationConfig)
    assert isinstance(ns.triggers, NotificationTriggers)


# ---------------------------------------------------------------------------
# is_any_enabled
# ---------------------------------------------------------------------------


def test_is_any_enabled_all_disabled():
    ns = NotificationSettings()
    assert ns.is_any_enabled() is False


@pytest.mark.parametrize(
    "provider",
    ["home_assistant", "webhook", "pushover", "ntfy", "discord"],
)
def test_is_any_enabled_single_provider(provider):
    ns = NotificationSettings()
    getattr(ns, provider).enabled = True
    assert ns.is_any_enabled() is True


def test_is_any_enabled_multiple_providers():
    ns = NotificationSettings()
    ns.home_assistant.enabled = True
    ns.discord.enabled = True
    assert ns.is_any_enabled() is True


# ---------------------------------------------------------------------------
# to_dict
# ---------------------------------------------------------------------------


def test_to_dict_contains_all_sections():
    ns = NotificationSettings()
    d = ns.to_dict()
    for key in ("home_assistant", "webhook", "pushover", "ntfy", "discord", "triggers"):
        assert key in d


def test_to_dict_nested_values():
    ns = NotificationSettings()
    ns.home_assistant.enabled = True
    ns.home_assistant.url = "http://ha.local"
    d = ns.to_dict()
    assert d["home_assistant"]["enabled"] is True
    assert d["home_assistant"]["url"] == "http://ha.local"


# ---------------------------------------------------------------------------
# _parse_settings_data
# ---------------------------------------------------------------------------


def test_parse_settings_data_empty():
    ns = NotificationSettings._parse_settings_data({})
    assert isinstance(ns, NotificationSettings)
    assert ns.is_any_enabled() is False


def test_parse_settings_data_with_ha():
    ns = NotificationSettings._parse_settings_data(
        {
            "home_assistant": {
                "enabled": True,
                "url": "http://homeassistant.local:8123",
                "token": "tok123",
                "service": "notify",
            }
        }
    )
    assert ns.home_assistant.enabled is True
    assert ns.home_assistant.url == "http://homeassistant.local:8123"
    assert ns.home_assistant.token == "tok123"


def test_parse_settings_data_with_webhook():
    ns = NotificationSettings._parse_settings_data(
        {
            "webhook": {
                "enabled": True,
                "url": "https://example.com/hook",
                "headers": {"X-Token": "secret"},
            }
        }
    )
    assert ns.webhook.enabled is True
    assert ns.webhook.headers == {"X-Token": "secret"}


def test_parse_settings_data_with_ntfy():
    ns = NotificationSettings._parse_settings_data(
        {
            "ntfy": {
                "enabled": True,
                "server": "https://ntfy.example.com",
                "topic": "alerts",
                "token": "ntfytok",
            }
        }
    )
    assert ns.ntfy.enabled is True
    assert ns.ntfy.server == "https://ntfy.example.com"
    assert ns.ntfy.topic == "alerts"


def test_parse_settings_data_with_triggers():
    ns = NotificationSettings._parse_settings_data(
        {
            "triggers": {
                "integration_update_available": False,
                "new_integration_in_registry": True,
                "integration_error_state": True,
                "orphaned_entities_detected": False,
                "firmware_update_available": True,
            }
        }
    )
    assert ns.triggers.integration_update_available is False
    assert ns.triggers.new_integration_in_registry is True
    assert ns.triggers.orphaned_entities_detected is False


def test_parse_settings_data_with_discord():
    ns = NotificationSettings._parse_settings_data(
        {
            "discord": {
                "enabled": True,
                "webhook_url": "https://discord.com/api/webhooks/123/abc",
            }
        }
    )
    assert ns.discord.enabled is True
    assert ns.discord.webhook_url == "https://discord.com/api/webhooks/123/abc"
