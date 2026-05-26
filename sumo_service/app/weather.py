from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

WeatherPayload = dict[str, float | str]


class WeatherProvider(Protocol):
    @property
    def source_name(self) -> str:
        ...

    def features_at(self, at: datetime) -> WeatherPayload:
        ...


@dataclass(frozen=True)
class StaticWeatherProvider:
    temp: float = 23.5
    prcp: float = 0.0
    wspd: float = 9.2
    rhum: float = 89.0
    cat: str = "clear"

    @property
    def source_name(self) -> str:
        return "static"

    def features_at(self, at: datetime) -> WeatherPayload:
        return {
            "feat_weather_temp": self.temp,
            "feat_weather_prcp": self.prcp,
            "feat_weather_wspd": self.wspd,
            "feat_weather_rhum": self.rhum,
            "feat_weather_cat": self.cat,
        }
