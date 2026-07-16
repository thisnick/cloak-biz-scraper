"""The message a user gets when a pin has no build for their architecture.

Separate from test_instances.py because these are sync and that module marks
everything asyncio.
"""
from __future__ import annotations

from cloakbrowser.config import get_platform_tag

from app.services.instances import _diagnose_pin


class TestDiagnosePin:
    """A pin with no build for this arch 404s, and the package calls that
    transient ("retry in a moment"). It is not — it is permanent and it is the
    pin's fault. Say which.
    """

    ERR = RuntimeError(
        "Pro binary unavailable: Client error '404 Not Found' for url "
        "'https://cloakbrowser.dev/api/download/146.0.7680.177.5'. Your license is "
        "valid but the Pro binary could not be downloaded right now. Retry in a moment."
    )

    def test_names_the_pin_and_the_platform(self):
        msg = _diagnose_pin(self.ERR, "146.0.7680.177.5")
        assert "146.0.7680.177.5" in msg
        assert get_platform_tag() in msg

    def test_contradicts_the_retry_advice(self):
        msg = _diagnose_pin(self.ERR, "146.0.7680.177.5")
        assert "retrying will not help" in msg
        assert "Clear the pin" in msg

    def test_silent_when_unpinned(self):
        # Unpinned, a 404 is genuinely something else — don't blame a pin.
        assert _diagnose_pin(self.ERR, "") is None

    def test_silent_for_unrelated_failures(self):
        assert _diagnose_pin(RuntimeError("connection reset by peer"), "148.0.1.2") is None
