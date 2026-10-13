"""Worker failures carry a fixed reason code read from Playwright's call log, never the log itself."""
import pytest

from browser_harness.playwright_worker import failure_reason

LOG = "Locator.click: Timeout 30000ms exceeded.\nCall log:\n  - waiting for locator('a')\n  - locator resolved to <a>x</a>\n  - attempting click action\n"


@pytest.mark.parametrize("error, reason", [
    (LOG + "    - waiting for element to be visible, enabled and stable\n    - <div class=\"backdrop\"></div> intercepts pointer events\n  - retrying click action", "intercepted_by_other_element"),
    (LOG + "    - waiting for element to be visible, enabled and stable\n    - element is not visible", "not_visible"),
    (LOG + "    - element is not enabled", "not_enabled"),
    (LOG + "  - click action done\n  - waiting for scheduled navigations to finish", "waiting_for_navigation"),
    ("Locator.click: Timeout 30000ms exceeded.\nCall log:\n  - waiting for get_by_text(\"New York, NY\")", "no_match"),
    ("Error: strict mode violation: get_by_text(\"Blue\") resolved to 3 elements", "multiple_matches"),
    ("Page.goto: Timeout 30000ms exceeded.\nCall log:\n  - navigating to \"https://x/\", waiting until \"load\"", "waiting_for_load_state"),
    ("anything else", None),
])
def test_failure_reason(error, reason):
    assert failure_reason(error) == reason
