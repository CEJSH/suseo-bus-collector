import asyncio
import unittest

import httpx
import respx

from busarrival.providers.base import ApiError
from busarrival.providers.gyeonggi import GyeonggiProvider


class StopMappingTest(unittest.TestCase):
    @respx.mock
    def test_exact_number_and_ambiguous_ids(self):
        endpoint = respx.get('https://apis.data.go.kr/6410000/busstationservice/v2/getBusStationListv2')

        async def query():
            async with httpx.AsyncClient() as client:
                p = GyeonggiProvider(client, 'TEST', retries=1)
                endpoint.respond(json={'response': {'msgHeader': {'resultCode': 0}, 'msgBody': {
                    'busStationList': [{'mobileNo': ' 46104', 'stationId': 240001009, 'stationName': '수능리'},
                                       {'mobileNo': '146104', 'stationId': 99}]}}})
                self.assertEqual(await p.resolve_stop('46104', '원본명'), ('240001009', '수능리'))
                endpoint.respond(json={'response': {'msgHeader': {'resultCode': 0}, 'msgBody': {
                    'busStationList': [{'mobileNo': '46104', 'stationId': 1},
                                       {'mobileNo': '46104', 'stationId': 2}]}}})
                with self.assertRaises(ApiError):
                    await p.resolve_stop('46104', '원본명')

        asyncio.run(query())
