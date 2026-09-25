# 换电站运营管理平台（纯后端）

新能源物流车换电站后台管理的纯后端 API 服务，提供站点、车辆和换电记录的统一管理能力。

## 技术栈

- FastAPI + Uvicorn
- SQLAlchemy + SQLite（本地文件，开箱即用）
- PyJWT（JWT 鉴权）
- 密码哈希用标准库 `hashlib.pbkdf2_hmac`，无额外依赖

所有数据本地、离线可运行，不依赖任何外部服务。

## 运行

```bash
pip install -r requirements.txt
python run.py
```

服务启动在 `http://127.0.0.1:7634`，首次启动自动建表并灌入种子数据。
交互式文档：`http://127.0.0.1:7634/docs`。

## 内置账号

首次启动自动创建唯一管理员（本平台只有 admin 一个角色）：

- 用户名：`admin`
- 密码：`admin123`

## 已实现的基础功能

- 登录签发 JWT、获取当前用户（`/api/auth/login`、`/api/auth/me`）
- 换电站增删改查（`/api/stations`）
- 车辆增删改查（`/api/vehicles`）
- 换电记录查询与登记（`/api/swaps`，会联动更新车辆电量与站点可用电池）
- 仪表盘统计（`/api/dashboard/stats`）
- 健康检查（`/api/health`）

除 `login` 与 `health` 外，所有接口均需携带 `Authorization: Bearer <token>`。

## 换电交易的一致性与幂等保证

`POST /api/swaps` 是一条完整的交易链路，落库前统一校验、单事务提交：

- **请求必须带调用方幂等标识 `request_id`**（终端生成，同一笔业务的重试必须复用）。
  重复请求查回原交易（HTTP `200`，响应体 `replayed: true`）；同一编号挂不同
  车辆/站点返回 `409` 且错误码为 `IDEMPOTENCY_KEY_CONFLICT`。幂等登记持久化在
  数据库中，服务重启后重放仍可追溯。新建成功返回 `201`。
- **落库前一致校验**：站点必须 `running`（离线/维护分别返回
  `STATION_OFFLINE` / `STATION_MAINTENANCE`）；车辆不得为 `fault`/`charging`
  （`VEHICLE_FAULT` / `VEHICLE_BUSY`）；终端上报的换前电量必须与车辆档案一致
  （±1 个百分点，超出返回 `SOC_MISMATCH`）。记录中的 `soc_before` 永远以车辆
  档案为准，终端上报值仅留痕在 `client_soc_before`。
- **并发安全**：写事务以 `BEGIN IMMEDIATE` 串行化，库存扣减与车辆电量更新均为
  条件 UPDATE（`battery_ready > 0` / CAS）。两个终端争用最后一块电池或同一
  车辆时恰好一笔成功，另一笔返回 `NO_AVAILABLE_BATTERY` 等业务错误，库存不会
  被重复消耗。
- **原子性**：换电记录、车辆电量、站点库存、幂等登记在同一事务内提交，任一
  步失败全部回滚。

所有业务错误均返回结构化响应：`{"detail": "中文说明", "code": "ERROR_CODE"}`。

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

## 编码说明

源码与数据均为 UTF-8；FastAPI 响应为 UTF-8 JSON，中文不转义、不乱码。
Windows 控制台若为 GBK，仅影响终端打印观感，不影响接口返回。
