# 大型场馆人群安全与现场指挥

只使用Python标准库和SQLite的模块化服务，默认端口`8340`。支持场馆区域、入场口、容量、通道、安保岗位、医疗点、事件、限流、开放通道、疏散和医疗任务、人员到位、区域恢复、延迟重复事件和容量冲突。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：容量计算、事件优先级、状态机和团队冲突约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8340
```

## 核心对象

`venue`为场馆，`zone`为区域，`gate`为入场口，`post`为安保岗位，`medical_point`为医疗点，`incident`为事件，`task`为现场任务，`reservation`为入场配额预占。

## 入场配额预占

为避免多个入场口同时提交预占挤爆区域，预占采用原子容量校验：`BEGIN IMMEDIATE` 事务内先扫掉该区域已过期的预占，再统计有效预占与实际占用，三者之和不超过容量才插入。

- 预占必须写清失效时刻 `expires_at`；状态为 `reserved`（占名额）。
- 确认到场（`confirm`）后状态变 `confirmed`，同时区域 `current_occupancy` 加上预占人数——确认到场才算占用。
- 失效（`expires_at` 已过）或确认失败/撤销即释放（`released`），释放的名额立即还给容量，别的入场口可接着预占。
- 区域容量变更（`change_capacity`）会作废所有未确认的预占并重算剩余人数；容量放不下则拒绝该次预占。
- 操作员只能确认/释放自己创建的预占（`operator_id` 校验），越权返回 403；指挥员强制撤销（`force_release`）任意预占但必须写 `reason`。
- 区域详情（`GET /api/entities/<zone_id>`）返回 `actual_occupancy`、`reserved_count`、`remaining_capacity`，三者恒等对得上。

预占接口：`POST /api/reservations`、`GET /api/reservations`、`POST /api/entities/<id>/actions`（`confirm` / `release` / `force_release`）。区域容量变更：`POST /api/entities/<zone_id>/actions`（`change_capacity`）。

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
