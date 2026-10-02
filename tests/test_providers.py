"""기관별 API 응답 파싱 테스트 (httpx 응답을 respx로 모킹)."""
import asyncio
import unittest

import httpx
import respx

from busarrival.providers.base import ApiError
from busarrival.providers.gyeonggi import GyeonggiProvider
from busarrival.providers.incheon import IncheonProvider
from busarrival.providers.seoul import SeoulProvider


def run(coro):
    return asyncio.run(coro)


async def call(cls, method, *args):
    async with httpx.AsyncClient() as client:
        return await getattr(cls(client, "TESTKEY-9f3a", retries=1), method)(*args)


def seoul_body(items, code="0"):
    return {"msgHeader": {"headerCd": code, "headerMsg": "ok"}, "msgBody": {"itemList": items}}


def gg_body(key, items, code="0"):
    return {"response": {"msgHeader": {"resultCode": code}, "msgBody": {key: items}}}


class SeoulTest(unittest.TestCase):
    @respx.mock
    def test_positions(self):
        respx.get("http://ws.bus.go.kr/api/rest/buspos/getBusPosByRtid").respond(json=seoul_body([
            {"vehId": "111", "plainNo": "서울74사1111", "sectOrd": "12", "stopFlag": "1",
             "dataTm": "20260930080010", "lastStnId": "122000001", "isrunyn": "1"},
            {"vehId": "222", "plainNo": "서울74사2222", "sectOrd": "30", "stopFlag": "0", "isrunyn": "1",
             "sectDist": "0.1", "fullSectDist": "0.4"},
            {"vehId": "444", "sectOrd": "31", "stopFlag": "0", "isrunyn": "1"},
            {"vehId": "333", "sectOrd": "5", "isrunyn": "0"},
        ]))
        ps = run(call(SeoulProvider, "vehicle_positions", "100100001"))
        self.assertEqual([(p.vehicle_id, p.seq, p.section_frac) for p in ps], [("111", 12, 0.0), ("222", 30, 0.25), ("444", 31, None)])
        self.assertEqual(ps[0].data_time.isoformat(), "2026-09-30T08:00:10+09:00")

    @respx.mock
    def test_single_item_dict_and_no_data(self):
        route = respx.get("http://ws.bus.go.kr/api/rest/busRouteInfo/getStaionByRoute")
        route.respond(json=seoul_body({"seq": "1", "station": "S1", "stationNm": "수서역", "arsId": "23001",
                                       "gpsX": "127.1", "gpsY": "37.48"}))
        stops = run(call(SeoulProvider, "route_stops", "R"))
        self.assertEqual((stops[0].station_id, stops[0].ars_id, stops[0].lat), ("S1", "23001", 37.48))
        route.respond(json=seoul_body(None, code="4"))
        self.assertEqual(run(call(SeoulProvider, "route_stops", "R")), [])

    @respx.mock
    def test_error_code_raises(self):
        respx.get("http://ws.bus.go.kr/api/rest/buspos/getBusPosByRtid").respond(json=seoul_body(None, code="7"))
        with self.assertRaises(ApiError):
            run(call(SeoulProvider, "vehicle_positions", "R"))

    @respx.mock
    def test_gateway_key_error_raises(self):
        respx.get("http://ws.bus.go.kr/api/rest/buspos/getBusPosByRtid").respond(
            text="<OpenAPI_ServiceResponse><cmmMsgHeader><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR"
                 "</returnAuthMsg><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")
        with self.assertRaisesRegex(ApiError, "SERVICE_KEY_IS_NOT_REGISTERED"):
            run(call(SeoulProvider, "vehicle_positions", "R"))

    @respx.mock
    def test_auth_error_not_retried_and_key_redacted(self):
        route = respx.get("http://ws.bus.go.kr/api/rest/buspos/getBusPosByRtid").respond(
            401, json={"error": "Unauthorized", "message": "유효하지 않은 서비스키입니다: 등록되지 않은 서비스키", "status": 401})

        async def go():
            async with httpx.AsyncClient() as client:
                await SeoulProvider(client, "SECRET+/KEY==", retries=3).vehicle_positions("R")
        with self.assertRaisesRegex(ApiError, "등록되지 않은 서비스키") as cm:
            run(go())
        self.assertEqual(route.call_count, 1)
        self.assertNotIn("SECRET", str(cm.exception))



class GyeonggiTest(unittest.TestCase):
    BASE = "https://apis.data.go.kr/6410000"

    @respx.mock
    def test_positions_state_codes(self):
        respx.get(f"{self.BASE}/buslocationservice/v2/getBusLocationListv2").respond(json=gg_body("busLocationList", [
            {"vehId": 1, "plateNo": "경기70아1", "stationSeq": 3, "stateCd": 1},
            {"vehId": 2, "plateNo": "경기70아2", "stationSeq": 7, "stateCd": 0},
        ]))
        ps = run(call(GyeonggiProvider, "vehicle_positions", "234000001"))
        self.assertEqual([(p.vehicle_id, p.seq, p.section_frac) for p in ps], [("1", 3, 0.0), ("2", 7, None)])


class ArsWhitespaceTest(unittest.TestCase):
    @respx.mock
    def test_gyeonggi_mobile_no_is_trimmed(self):
        respx.get("https://apis.data.go.kr/6410000/busrouteservice/v2/getBusRouteStationListv2").respond(
            json=gg_body("busRouteStationList", [
                {"stationSeq": 1, "stationId": 1, "stationName": "a", "mobileNo": " 23406", "x": 127.1, "y": 37.4},
                {"stationSeq": 2, "stationId": 2, "stationName": "b", "mobileNo": " ", "x": 127.1, "y": 37.5}]))
        stops = run(call(GyeonggiProvider, "route_stops", "234000001"))
        self.assertEqual([s.ars_id for s in stops], ["23406", None])


class IncheonTest(unittest.TestCase):
    BASE = "https://apis.data.go.kr/6280000"

    @respx.mock
    def test_xml_positions(self):
        respx.get(f"{self.BASE}/busLocationService/getBusRouteLocation").respond(text="""<?xml version="1.0"?>
<ServiceResult><msgHeader><resultCode>0</resultCode><resultMsg>OK</resultMsg></msgHeader>
<msgBody><itemList><BUSID>7001</BUSID><BUS_NUM_PLATE>인천70바1</BUS_NUM_PLATE><LATEST_STOPSEQ>15</LATEST_STOPSEQ>
<LATEST_STOP_ID>168000001</LATEST_STOP_ID></itemList></msgBody></ServiceResult>""")
        ps = run(call(IncheonProvider, "vehicle_positions", "165000001"))
        self.assertEqual([(p.vehicle_id, p.plate_no, p.seq) for p in ps], [("7001", "인천70바1", 15)])

    @respx.mock
    def test_route_stops_tm_coordinates_converted(self):
        from pyproj import Transformer
        x, y = Transformer.from_crs("EPSG:4326", "EPSG:2097", always_xy=True).transform(126.7052, 37.4563)
        respx.get(f"{self.BASE}/busRouteService/getBusRouteSectionList").respond(text=f"""<ServiceResult>
<msgHeader><resultCode>0</resultCode></msgHeader><msgBody>
<itemList><BSTOPID>1</BSTOPID><BSTOPNM>A</BSTOPNM><BSTOPSEQ>1</BSTOPSEQ><POSX>{x}</POSX><POSY>{y}</POSY></itemList>
<itemList><BSTOPID>2</BSTOPID><BSTOPNM>B</BSTOPNM><BSTOPSEQ>2</BSTOPSEQ><POSX>126.71</POSX><POSY>37.46</POSY></itemList>
</msgBody></ServiceResult>""")
        stops = run(call(IncheonProvider, "route_stops", "165000001"))
        self.assertAlmostEqual(stops[0].lat, 37.4563, places=4)
        self.assertAlmostEqual(stops[0].lon, 126.7052, places=4)
        self.assertEqual((stops[1].lat, stops[1].lon), (37.46, 126.71))

    @respx.mock
    def test_xml_no_data(self):
        respx.get(f"{self.BASE}/busLocationService/getBusRouteLocation").respond(
            text="<ServiceResult><msgHeader><resultCode>4</resultCode></msgHeader></ServiceResult>")
        self.assertEqual(run(call(IncheonProvider, "vehicle_positions", "R")), [])


if __name__ == "__main__":
    unittest.main()
