# 大型场馆人群安全与现场指挥

只使用Python标准库和SQLite的模块化服务，默认端口`8340`。支持场馆区域、入场口、容量、通道、安保岗位、医疗点、事件、限流、开放通道、疏散和医疗任务、人员到位、区域恢复、延迟重复事件和容量冲突。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：容量计算、事件优先级、状态机、团队冲突约束和预占配额规则。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验、版本控制和区域配额锁。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8340
```

## 核心对象

`venue`为场馆，`zone`为区域，`gate`为入场口，`post`为安保岗位，`medical_point`为医疗点，`incident`为事件，`task`为现场任务，`reservation`为区域入场配额预占。

## 入场配额预占

多个入场口可同时为同一区域提交预占，服务按区域串行校验并写入，不会超卖。预占必须给出`expires_at`失效时刻，状态机为`pending → confirmed / released / revoked / expired / invalidated`：

- `POST /api/reservations`（`zone_id`、`gate_id`、`count`、`expires_at`）创建预占；容量（含限流线）放不下时拒绝。
- `confirm`：确认到场才计入`current_occupancy`；确认时容量放不下则预占释放并报冲突。
- `cancel`：普通操作员只能撤销本入场口的预占（越权被挡下）；supervisor/coordinator/admin 强制撤销必须提供`reason`，结果记为`revoked`。
- 到期未确认的预占自动置为`expired`；释放的名额立即可被其他入场口使用。
- 区域`set_capacity`动作调整容量：所有未确认预占作废（`invalidated`）并重算剩余人数；新容量低于当前实际占用时拒绝。

区域详情（`GET /api/entities/<zone_id>`）附带`quota`：`capacity = current_occupancy + reserved_count + remaining_capacity`，三者始终对得上。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

容量和调度规则为可运行的简化模型，不接入闸机、视频分析、室内定位、消防联动或真实应急指挥系统。
