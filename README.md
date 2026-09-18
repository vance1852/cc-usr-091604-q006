# 体育场地服务

体育中心场地运营服务:登记场地分区与可承载项目、维护窗口、临时封闭,
处理预约申请、支付、审核确认、改期与取消退款,并提供日历、冲突解释、
替代建议与审计接口。

## 运行

```bash
python3 -m unittest discover -s tests -v
```

## 目录

- `app/models.py`：领域模型(场地/分区/维护窗口/封闭/预约/支付/确认单/退款/审计)
- `app/storage.py`：SQLite 持久化(重启不丢状态;唯一约束支撑幂等)
- `app/service.py`：业务服务 `VenueService`(锁 + 事务、冲突计算、退款规则、审计)
- `app/venues.py`：兼容层,保留起点代码的导入路径
- `tests/`：预约流程、封闭维护、并发锁定、重启持久化测试

## 领域模型要点

- **分区占用关系**:每个分区声明占用的资源单元 `shares`。同场两分区
  `shares` 有交集且时段重叠即互相阻塞。例:全场 `("L","R")`、
  左半 `("L",)`、右半 `("R",)` —— 约全场阻塞两个半场,约左半不阻塞右半。
- **跨午夜时段**:一律用带时区的绝对区间表达,跨午夜/跨周无需特判;
  日历按区间交集落在每一天。
- **清场缓冲**:分区可配 `buffer_minutes`,预约实际占用到
  `end + buffer`(`hold_until`),背靠背场次自动留出清场时间。
- **预约状态机**:`PENDING_PAYMENT → PAID → CONFIRMED`,
  另有 `AFFECTED`(受封闭影响)/`REJECTED`/`CANCELLED`。
  `PAID/CONFIRMED/AFFECTED` 占用场地——支付即锁定,杜绝"两班都付钱同时到场"。

## 核心接口

```python
svc = VenueService(db_path="venue.db")     # 状态落 SQLite,重启不丢

# 登记
svc.register_venue("东区五人制场", zone="东区", venue_id="east")
svc.register_zone("east", "全场", capacity=20, sports=("足球",),
                  shares=("L", "R"), zone_id="east:full", buffer_minutes=15)
svc.register_maintenance_window("east", "草坪养护", weekday=6,
                                start_time="22:00", duration=timedelta(hours=4))

# 预约流程:申请 → 支付(幂等) → 审核(生成带版本确认单)
b = svc.create_booking("east", "east:full", "足球", start, end, "青训A", 12)
svc.pay(b.booking_id, payment_id="gw-123", amount=b.quoted_price)
conf = svc.review(b.booking_id, operator="运营员")   # Confirmation v1

# 封闭:发布 / 提前结束;封闭期间不得新建,受影响预约只能改期或取消
closure = svc.publish_closure("east", zone_ids=(), start=s, end=e,
                              reason="草坪养护", actor="维护员")
svc.end_closure(closure.closure_id, actor="维护员")   # 提前结束,自动恢复

# 改期(确认单版本递增) / 按规则取消(记录退款依据)
svc.reschedule(b.booking_id, new_start, new_end, actor="运营员")  # → v2
refund = svc.cancel(b.booking_id, actor="客服", reason="closure") # 全额退款

# 查询:日历 / 冲突解释 / 替代建议 / 审计
svc.calendar("east", date(2026, 9, 20))
svc.explain("east", "east:full", "足球", start, end, party_size=12)
svc.suggest_alternatives("足球", start, end, party_size=12)
svc.audit_trail("east", start, end)        # 某时间段的全部决策来源
```

## 并发与幂等

- 所有写操作在 `threading.RLock` + `BEGIN IMMEDIATE` 事务内串行执行:
  并发抢同一时段,只有一人能支付锁定,绝不产生双重占用。
- 支付按 `payment_id` 幂等(唯一约束),重复回调返回原记录;
  确认按 `(booking_id, version)` 幂等,重复确认版本不变。
- 全部状态落 SQLite,服务重启后占用关系、确认单版本与审计日志不丢。

## 退款规则

| 情形 | 退款 |
| --- | --- |
| 场地封闭/维护导致取消 | 全额 |
| 开场前 24 小时以上 | 全额 |
| 开场前 4~24 小时 | 50% |
| 开场前 4 小时内 | 不退 |

每笔退款都记录 `basis`(退款依据)并写入审计,管理员可按时间段追溯全部决策来源。
