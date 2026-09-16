"""场地运营领域的最小起点。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Venue:
    """保存场地名称和所属区域。"""

    name: str
    zone: str


class VenueService:
    """提供场地服务的基础健康状态。"""

    def health(self) -> dict[str, str]:
        return {"service": "venue", "status": "ok"}

