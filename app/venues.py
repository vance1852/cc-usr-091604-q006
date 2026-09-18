"""场地运营领域的最小起点(兼容层)。

完整实现已拆分为 app.models(领域模型)、app.storage(持久化)、
app.service(业务服务)。这里保留起点代码的导入路径,
供既有冒烟测试与旧调用方使用。
"""

from app.models import Venue
from app.service import VenueService

__all__ = ["Venue", "VenueService"]
