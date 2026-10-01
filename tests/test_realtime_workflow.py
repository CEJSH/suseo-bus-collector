import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from busarrival.collector import Collector, run_collector
from busarrival.config import Config
from busarrival.models import Provider


class RealtimeWorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def test_seoul_polled_when_key_configured(self):
        cfg = Config(seoul_api_key="seoul", gyeonggi_api_key="gg", incheon_api_key="ic")
        db = AsyncMock()
        with patch("busarrival.collector.Database.connect", AsyncMock(return_value=db)), \
             patch.object(Collector, "run", AsyncMock()) as run, \
             patch("busarrival.collector.build_providers", return_value={Provider.SEOUL: AsyncMock()}) as build:
            await run_collector(cfg)
        self.assertEqual(build.call_args.args[0].seoul_api_key, "seoul")
        run.assert_awaited_once()
        db.close.assert_awaited_once()

    async def test_invalid_duration_before_db_access(self):
        with patch("busarrival.collector.Database.connect", AsyncMock()) as connect:
            with self.assertRaises(ValueError):
                await run_collector(Config(realtime_duration_hours=0))
            connect.assert_not_awaited()

    async def test_missing_routes_fail_before_polling(self):
        collector = Collector(Config(), AsyncMock(), {Provider.INCHEON: AsyncMock()})
        collector.ensure_routes = AsyncMock()
        collector.cycle = AsyncMock()
        with patch.object(asyncio.get_running_loop(), "add_signal_handler"):
            with self.assertRaisesRegex(RuntimeError, "no realtime routes"):
                await collector.run()
        collector.cycle.assert_not_awaited()
