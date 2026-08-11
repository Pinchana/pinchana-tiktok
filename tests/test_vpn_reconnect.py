import time

import pytest

from pinchana_core.vpn import (
    GluetunController,
    VpnRotationCooldownError,
)


@pytest.mark.asyncio
async def test_reconnect_cooldown_is_explicit_instead_of_silent(monkeypatch):
    monkeypatch.setenv("VPN_ENABLED", "true")
    controller = GluetunController(rotation_cooldown=30)
    controller._last_rotation = time.monotonic()

    async def current_ip():
        return "203.0.113.10"

    monkeypatch.setattr(controller, "get_public_ip", current_ip)

    with pytest.raises(VpnRotationCooldownError) as exc_info:
        await controller.rotate_ip()

    assert 29 <= exc_info.value.retry_after <= 30
