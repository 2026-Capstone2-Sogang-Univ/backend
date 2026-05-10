import sumolib
import traci


def sumo_to_latlng(x: float, y: float) -> tuple[float, float]:
    """TraCI 기반 정밀 변환. startup 캘리브레이션(경계 코너 4점) 용도에만 사용."""
    lng, lat = traci.simulation.convertGeo(x, y)
    return lat, lng


def latlng_to_sumo(lat: float, lng: float) -> tuple[float, float]:
    x, y = traci.simulation.convertGeo(lng, lat, fromGeo=True)
    return x, y


def make_sumolib_converter(net_path: str):
    """Load SUMO net once, return fast (x, y) → (lat, lng) converter.

    .net.xml의 projection 메타데이터를 사용해 pyproj로 변환.
    매 프레임 차량 좌표 변환에 사용 — TraCI 호출 없는 pure Python 구현.
    정확도는 TraCI convertGeo와 동일.
    """
    net = sumolib.net.readNet(net_path, withInternal=False)

    def convert(x: float, y: float) -> tuple[float, float]:
        lng, lat = net.convertXY2LonLat(x, y)
        return lat, lng

    return convert
