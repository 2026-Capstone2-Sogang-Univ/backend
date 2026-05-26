from datetime import datetime

from app.weather import StaticWeatherProvider


def test_static_weather_provider_returns_module_3_features():
    provider = StaticWeatherProvider()

    assert provider.features_at(datetime(2013, 7, 8, 8, 0, 0)) == {
        "feat_weather_temp": 23.5,
        "feat_weather_prcp": 0.0,
        "feat_weather_wspd": 9.2,
        "feat_weather_rhum": 89.0,
        "feat_weather_cat": "clear",
    }


def test_static_weather_provider_source_name():
    assert StaticWeatherProvider().source_name == "static"
