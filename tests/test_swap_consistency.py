"""换电交易一致性测试：状态校验、电量核对、幂等重放与并发争用。"""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.database import engine
from app.main import app
from app.seed import init_db

init_db()
client = TestClient(app)


def _headers() -> dict:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _make_station(headers, **overrides) -> dict:
    body = {
        "name": f"一致性测试站{uuid.uuid4().hex[:6]}",
        "slot_total": 10,
        "battery_ready": 5,
        "status": "running",
    }
    body.update(overrides)
    resp = client.post("/api/stations", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _make_vehicle(headers, **overrides) -> dict:
    body = {"plate": f"测EV{uuid.uuid4().hex[:5]}", "current_soc": 20.0, "status": "idle"}
    body.update(overrides)
    resp = client.post("/api/vehicles", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _swap_payload(vehicle, station, **overrides) -> dict:
    body = {
        "vehicle_id": vehicle["id"],
        "station_id": station["id"],
        "soc_before": vehicle["current_soc"],
        "soc_after": 100.0,
    }
    body.update(overrides)
    return body


def _get_station(headers, station_id) -> dict:
    return client.get(f"/api/stations/{station_id}", headers=headers).json()


def _get_vehicle(headers, vehicle_id) -> dict:
    return client.get(f"/api/vehicles/{vehicle_id}", headers=headers).json()


def _swaps_of_station(headers, station_id) -> list:
    return [s for s in client.get("/api/swaps", headers=headers).json() if s["station_id"] == station_id]


def _post_swap_concurrently(headers: dict, payloads: list) -> list:
    """多线程同时提交换电请求（每个线程独立的 TestClient）。"""
    barrier = threading.Barrier(len(payloads))

    def _send(body):
        local = TestClient(app)
        barrier.wait(timeout=10)
        return local.post("/api/swaps", json=body, headers=headers)

    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        return list(pool.map(_send, payloads))


# ---------- 落库前一致校验 ----------


def test_offline_station_rejected():
    headers = _headers()
    station = _make_station(headers, status="offline")
    vehicle = _make_vehicle(headers)
    resp = client.post("/api/swaps", json=_swap_payload(vehicle, station), headers=headers)
    assert resp.status_code == 409
    assert "离线" in resp.json()["detail"]
    # 未落库、未扣库存、未改电量
    assert _swaps_of_station(headers, station["id"]) == []
    assert _get_station(headers, station["id"])["battery_ready"] == 5
    assert _get_vehicle(headers, vehicle["id"])["current_soc"] == 20.0


def test_maintenance_station_rejected():
    headers = _headers()
    station = _make_station(headers, status="maintenance")
    vehicle = _make_vehicle(headers)
    resp = client.post("/api/swaps", json=_swap_payload(vehicle, station), headers=headers)
    assert resp.status_code == 409
    assert "维护中" in resp.json()["detail"]
    assert _swaps_of_station(headers, station["id"]) == []


def test_fault_vehicle_rejected():
    headers = _headers()
    station = _make_station(headers)
    vehicle = _make_vehicle(headers, status="fault", current_soc=9.0)
    resp = client.post("/api/swaps", json=_swap_payload(vehicle, station), headers=headers)
    assert resp.status_code == 409
    assert "故障" in resp.json()["detail"]
    # 故障车辆不得被改成满电
    assert _get_vehicle(headers, vehicle["id"])["current_soc"] == 9.0
    assert _get_station(headers, station["id"])["battery_ready"] == 5
    assert _swaps_of_station(headers, station["id"]) == []


def test_charging_vehicle_rejected():
    headers = _headers()
    station = _make_station(headers)
    vehicle = _make_vehicle(headers, status="charging")
    resp = client.post("/api/swaps", json=_swap_payload(vehicle, station), headers=headers)
    assert resp.status_code == 409
    assert "换电中" in resp.json()["detail"]
    assert _swaps_of_station(headers, station["id"]) == []


def test_soc_before_must_match_vehicle_archive():
    headers = _headers()
    station = _make_station(headers)
    vehicle = _make_vehicle(headers, current_soc=33.0)
    # 终端上报的换前电量与车辆档案不一致：拒绝且不写入
    resp = client.post("/api/swaps", json=_swap_payload(vehicle, station, soc_before=10.0), headers=headers)
    assert resp.status_code == 409
    assert "不一致" in resp.json()["detail"]
    assert _get_vehicle(headers, vehicle["id"])["current_soc"] == 33.0
    assert _get_station(headers, station["id"])["battery_ready"] == 5
    assert _swaps_of_station(headers, station["id"]) == []


# ---------- 幂等 ----------


def test_duplicate_request_returns_original_transaction():
    headers = _headers()
    station = _make_station(headers, battery_ready=3)
    vehicle = _make_vehicle(headers)
    payload = _swap_payload(vehicle, station, request_id=f"req-{uuid.uuid4().hex}")

    first = client.post("/api/swaps", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    # 断线后原样重提：返回原交易而不是再下一单
    second = client.post("/api/swaps", json=payload, headers=headers)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["request_id"] == payload["request_id"]
    # 库存只扣一次、记录只有一条
    assert _get_station(headers, station["id"])["battery_ready"] == 2
    assert len(_swaps_of_station(headers, station["id"])) == 1
    assert _get_vehicle(headers, vehicle["id"])["current_soc"] == 100.0
    # 可按幂等标识追溯
    traced = client.get("/api/swaps", params={"request_id": payload["request_id"]}, headers=headers).json()
    assert [s["id"] for s in traced] == [first.json()["id"]]


def test_idempotency_survives_restart():
    headers = _headers()
    station = _make_station(headers)
    vehicle = _make_vehicle(headers)
    payload = _swap_payload(vehicle, station, request_id=f"req-{uuid.uuid4().hex}")
    first = client.post("/api/swaps", json=payload, headers=headers)
    assert first.status_code == 201, first.text

    # 模拟服务重启：丢弃全部数据库连接，重新从文件打开
    engine.dispose()

    replay = client.post("/api/swaps", json=payload, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert _get_station(headers, station["id"])["battery_ready"] == 4


def test_request_id_reused_with_different_payload_conflicts():
    headers = _headers()
    station = _make_station(headers)
    vehicle = _make_vehicle(headers)
    key = f"req-{uuid.uuid4().hex}"
    first = client.post("/api/swaps", json=_swap_payload(vehicle, station, request_id=key), headers=headers)
    assert first.status_code == 201, first.text
    # 同一幂等标识对应不同请求内容：可区分的冲突响应
    other_vehicle = _make_vehicle(headers)
    conflict = client.post("/api/swaps", json=_swap_payload(other_vehicle, station, request_id=key), headers=headers)
    assert conflict.status_code == 409
    assert "幂等标识" in conflict.json()["detail"]


# ---------- 并发争用 ----------


def test_concurrent_last_battery_deducted_once():
    headers = _headers()
    station = _make_station(headers, battery_ready=1)
    v1 = _make_vehicle(headers)
    v2 = _make_vehicle(headers)
    payloads = [
        _swap_payload(v1, station, request_id=f"req-{uuid.uuid4().hex}"),
        _swap_payload(v2, station, request_id=f"req-{uuid.uuid4().hex}"),
    ]
    responses = _post_swap_concurrently(headers, payloads)
    # 两个终端争用最后一块电池：只有一笔成功，另一笔收到无电池业务响应
    assert sorted(r.status_code for r in responses) == [201, 422]
    assert _get_station(headers, station["id"])["battery_ready"] == 0  # 库存只扣一次
    assert len(_swaps_of_station(headers, station["id"])) == 1


def test_concurrent_same_vehicle_single_success():
    headers = _headers()
    station = _make_station(headers, battery_ready=2)
    vehicle = _make_vehicle(headers, current_soc=30.0)
    payloads = [
        _swap_payload(vehicle, station, request_id=f"req-{uuid.uuid4().hex}"),
        _swap_payload(vehicle, station, request_id=f"req-{uuid.uuid4().hex}"),
    ]
    responses = _post_swap_concurrently(headers, payloads)
    # 同一车辆的并发换电：只有一笔成功，另一笔收到冲突响应
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert _get_vehicle(headers, vehicle["id"])["current_soc"] == 100.0
    assert _get_station(headers, station["id"])["battery_ready"] == 1  # 只扣一块电池
    assert len(_swaps_of_station(headers, station["id"])) == 1


def test_concurrent_identical_request_replays_original():
    headers = _headers()
    station = _make_station(headers, battery_ready=2)
    vehicle = _make_vehicle(headers)
    payload = _swap_payload(vehicle, station, request_id=f"req-{uuid.uuid4().hex}")
    responses = _post_swap_concurrently(headers, [payload, dict(payload)])
    # 两台终端同时提交同一请求：拿到同一笔交易，库存只扣一次
    assert sorted(r.status_code for r in responses) == [200, 201]
    assert len({r.json()["id"] for r in responses}) == 1
    assert _get_station(headers, station["id"])["battery_ready"] == 1
    assert len(_swaps_of_station(headers, station["id"])) == 1


# ---------- 普通换电不受影响 ----------


def test_normal_swap_still_works():
    headers = _headers()
    station = _make_station(headers, battery_ready=4)
    vehicle = _make_vehicle(headers, current_soc=15.0)
    resp = client.post("/api/swaps", json=_swap_payload(vehicle, station), headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["soc_before"] == 15.0
    assert body["soc_after"] == 100.0
    assert body["request_id"]  # 未提供幂等标识时服务端兜底生成
    assert _get_vehicle(headers, vehicle["id"])["current_soc"] == 100.0
    assert _get_station(headers, station["id"])["battery_ready"] == 3
