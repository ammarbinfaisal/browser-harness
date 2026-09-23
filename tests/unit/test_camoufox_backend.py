import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

spec = importlib.util.spec_from_file_location(
    "camoufox_backend", Path(__file__).resolve().parents[2] / "src/browser_harness/camoufox.py"
)
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class CamoufoxBackendTests(IsolatedAsyncioTestCase):
    async def test_reads_use_the_injected_page_and_bound_observations(self):
        target = SimpleNamespace(inner_text=AsyncMock(return_value="x" * 30000),
                                 aria_snapshot=AsyncMock(return_value="- button Save"))
        page = SimpleNamespace(url="https://fixture.test", title=AsyncMock(return_value="Fixture"),
                               locator=Mock(return_value=target))
        result = await backend.execute(page, "observe", {})
        self.assertEqual(len(result["text"]), 20000)
        self.assertEqual(result["aria"], "- button Save")
        page.locator.assert_called_once_with("body")

    async def test_ambiguous_frames_fail_before_mutation(self):
        frame = SimpleNamespace(url="https://fixture.test/frame")
        page = SimpleNamespace(frames=[frame, frame], locator=Mock())
        with self.assertRaisesRegex(ValueError, "exactly one"):
            await backend.execute(page, "fill", {"frameUrl": frame.url, "selector": "input", "text": "data"})
        page.locator.assert_not_called()

    async def test_fill_targets_the_selected_frame(self):
        target = SimpleNamespace(fill=AsyncMock())
        frame = SimpleNamespace(url="https://fixture.test/frame", locator=Mock(return_value=target))
        page = SimpleNamespace(url="https://fixture.test", frames=[frame])
        result = await backend.execute(page, "fill", {"frameUrl": frame.url, "selector": "input", "text": "data", "timeoutMs": 5})
        target.fill.assert_awaited_once_with("data", timeout=5)
        self.assertEqual(result, {"url": page.url})

    async def test_missing_target_fails_before_dispatch(self):
        click = AsyncMock(side_effect=TimeoutError("target absent"))
        page = SimpleNamespace(locator=Mock(return_value=SimpleNamespace(click=click)))
        with self.assertRaises(backend.TargetNotReadyError):
            await backend.execute(page, "click", {"selector": "a", "timeoutMs": 50})
        click.assert_awaited_once_with(trial=True, timeout=50)

    async def test_failed_effects_propagate_without_retry(self):
        click = AsyncMock(side_effect=[None, TimeoutError("outcome unknown")])
        page = SimpleNamespace(locator=Mock(return_value=SimpleNamespace(click=click)))
        with self.assertRaises(TimeoutError):
            await backend.execute(page, "click", {"selector": "button"})
        self.assertEqual(click.await_count, 2)
        self.assertTrue(click.await_args_list[0].kwargs["trial"])
        self.assertNotIn("trial", click.await_args_list[1].kwargs)

    async def test_successful_click_dispatches_once_with_remaining_budget(self):
        click = AsyncMock()
        page = SimpleNamespace(url="https://fixture.test", locator=Mock(return_value=SimpleNamespace(click=click)))
        self.assertEqual(await backend.execute(page, "click", {"selector": '[role="link"]'}), {"url": page.url})
        self.assertEqual(click.await_count, 2)
        self.assertGreater(click.await_args.kwargs["timeout"], 0)
        self.assertLessEqual(click.await_args.kwargs["timeout"], 15000)
