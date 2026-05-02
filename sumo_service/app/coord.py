import traci


def sumo_to_latlng(x: float, y: float) -> tuple[float, float]:
    """TraCI 기반 정밀 변환. startup 캘리브레이션 용도에만 사용."""
    lng, lat = traci.simulation.convertGeo(x, y)
    return lat, lng


def latlng_to_sumo(lat: float, lng: float) -> tuple[float, float]:
    x, y = traci.simulation.convertGeo(lng, lat, fromGeo=True)
    return x, y


def make_affine_converter(
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
):
    """경계 코너 8개 값으로 SUMO→WGS84 선형 변환 클로저를 생성.

    TraCI 호출 없는 순수 산술 연산. 매 프레임 차량 좌표 변환에 사용.
    맨해튼 규모(~4km×21km)에서 선형 근사 오차 < 10m — 시각화 용도로 충분.
    """
    x_range = max_x - min_x
    y_range = max_y - min_y
    lat_range = max_lat - min_lat
    lng_range = max_lng - min_lng

    def convert(x: float, y: float) -> tuple[float, float]:
        lat = min_lat + (y - min_y) / y_range * lat_range
        lng = min_lng + (x - min_x) / x_range * lng_range
        return lat, lng

    return convert
