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

## 换电交易一致性

`POST /api/swaps` 在落库前完成一致校验，任一不满足即拒绝，并返回可区分的业务响应：

- 换电站须为「运营中」，维护中 / 离线返回 `409`；
- 车辆须处于可换状态（空闲 / 运营），换电中 / 故障返回 `409`；
- 终端上报的换前电量须与车辆档案一致，不一致返回 `409`，以档案为准；
- 站点无满电电池返回 `422`。

幂等与并发：

- 调用方可通过请求体 `request_id`（或 `Idempotency-Key` 请求头）提供幂等标识；
  重复提交（断线重提、重试）返回原交易（`HTTP 200`），标识持久化在换电记录中，
  重启后仍可追溯；同一标识对应不同请求内容返回 `409`。
- 库存扣减与车辆电量更新采用条件更新（CAS），并发争用最后一块电池或同一车辆时
  只有一笔成功；换电记录、车辆电量、站点库存在同一事务中提交，任一失败全部回滚。
- 可按 `GET /api/swaps?request_id=<标识>` 追溯幂等结果。

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

## 编码说明

源码与数据均为 UTF-8；FastAPI 响应为 UTF-8 JSON，中文不转义、不乱码。
Windows 控制台若为 GBK，仅影响终端打印观感，不影响接口返回。
