import traci


def sumo_to_latlng(x: float, y: float) -> tuple[float, float]:
    """TraCI 기반 SUMO -> WGS84 정밀 변환."""
    lng, lat = traci.simulation.convertGeo(x, y)
    return lat, lng


def latlng_to_sumo(lat: float, lng: float) -> tuple[float, float]:
    x, y = traci.simulation.convertGeo(lng, lat, fromGeo=True)
    return x, y
