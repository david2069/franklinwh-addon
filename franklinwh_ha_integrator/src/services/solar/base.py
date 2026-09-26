"""Abstract base class for solar forecast providers."""
from abc import ABC, abstractmethod
from typing import List, Dict, Any


class SolarForecastProvider(ABC):
    """
    All providers return a normalised list of 30-min intervals:
    [
        {"timestamp": "2026-04-23T06:00:00+10:00", "pv_kw": 1.25, "period_mins": 30},
        ...
    ]
    Timestamps are ISO-8601 with timezone offset.
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.api_key: str = config.get("api_key", "")
        self.sync_interval_mins: int = config.get("sync_interval_mins", 60)

    @abstractmethod
    def get_forecast(
        self,
        lat: float,
        lon: float,
        installation: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Fetch and normalise 30-min solar production forecast slots."""
        pass

    @abstractmethod
    def validate_connection(self) -> bool:
        """Test the API connection without heavy data pull."""
        pass
