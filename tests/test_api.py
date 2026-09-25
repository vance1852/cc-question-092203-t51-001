"""接口冒烟测试：覆盖认证、鉴权、CRUD、换电一致性校验、幂等、并发与统计。"""
import threading
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import DATABASE_URL
from app.main import app
from app.models import IdempotentRequest
from app.schemas import SwapCreate
from app.seed import init_db
from app.services import swap_service

init_db()
client = TestClient(app)


def _login() -> str:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_login()}"}


def _make_station(headers, battery_ready=5, status="running", slot_total=None):
    body = {
        "name": f"测试站{uuid.uuid4().hex[:8]}",
        "slot_total": slot_total if slot_total is not None else max(battery_ready, 1),
        "battery_ready": battery_ready,
        "status": status,
    }
    resp = client.post("/api/stations", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _make_vehicle(headers, soc=10.0, vstatus="idle"):
    resp = client.post(
        "/api/vehicles",
        json={"plate": f"测{uuid.uuid4().hex[:8]}", "model": "换电测试车",
              "current_soc": soc, "status": vstatus},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_health():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_login_wrong_password():
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    assert resp.status_code == 401


def test_requires_auth():
    # 未带 token 访问受保护资源应被拦截
    resp = client.get("/api/stations")
    assert resp.status_code == 401


def test_me_and_chinese_encoding():
    resp = client.get("/api/auth/me", headers=_auth_headers())
    assert resp.status_code == 200
    # 中文显示名必须正确返回，验证 UTF-8 编码无乱码
    assert resp.json()["display_name"] == "平台管理员"


def test_seed_stations_present_with_chinese():
    resp = client.get("/api/stations", headers=_auth_headers())
    assert resp.status_code == 200
    stations = resp.json()
    assert len(stations) >= 4
    assert any("换电站" in s["name"] for s in stations)


def test_station_crud_and_validation():
    headers = _auth_headers()
    # 非法数据：满电电池数 > 仓位总数
    bad = client.post("/api/stations", json={"name": "测试站", "slot_total": 2, "battery_ready": 5}, headers=headers)
    assert bad.status_code == 422

    created = client.post(
        "/api/stations",
        json={"name": "西站测试换电站", "address": "测试路 1 号", "slot_total": 10, "battery_ready": 6},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    assert created.json()["name"] == "西站测试换电站"

    updated = client.put(f"/api/stations/{sid}", json={"status": "maintenance"}, headers=headers)
    assert updated.status_code == 200
    assert updated.json()["status"] == "maintenance"

    deleted = client.delete(f"/api/stations/{sid}", headers=headers)
    assert deleted.status_code == 204
    assert client.get(f"/api/stations/{sid}", headers=headers).status_code == 404


def test_vehicle_unique_plate():
    headers = _auth_headers()
    plate = f"测{uuid.uuid4().hex[:6]}"
    first = client.post("/api/vehicles", json={"plate": plate, "model": "测试车型"}, headers=headers)
    assert first.status_code == 201, first.text
    dup = client.post("/api/vehicles", json={"plate": plate}, headers=headers)
    assert dup.status_code == 409


def test_swap_flow_updates_state():
    headers = _auth_headers()
    station = _make_station(headers, battery_ready=3)
    vehicle = _make_vehicle(headers, soc=10.0)

    swap = client.post(
        "/api/swaps",
        json={"request_id": f"req-{uuid.uuid4().hex}", "vehicle_id": vehicle["id"],
              "station_id": station["id"], "soc_before": 10.0, "soc_after": 100.0},
        headers=headers,
    )
    assert swap.status_code == 201, swap.text
    assert swap.json()["station_name"] == station["name"]
    assert swap.json()["replayed"] is False

    # 车辆电量应更新、站点可用电池应减一
    v_after = client.get(f"/api/vehicles/{vehicle['id']}", headers=headers).json()
    assert v_after["current_soc"] == 100.0
    s_after = client.get(f"/api/stations/{station['id']}", headers=headers).json()
    assert s_after["battery_ready"] == 2


def test_swap_invalid_soc():
    headers = _auth_headers()
    station = _make_station(headers, battery_ready=3)
    vehicle = _make_vehicle(headers, soc=82.0)
    bad = client.post(
        "/api/swaps",
        json={"request_id": f"req-{uuid.uuid4().hex}", "vehicle_id": vehicle["id"],
              "station_id": station["id"], "soc_before": 90.0, "soc_after": 50.0},
        headers=headers,
    )
    # 上报电量与档案不一致（或换后不高于换前）均为 422
    assert bad.status_code == 422


def test_swap_rejects_offline_and_maintenance_station():
    headers = _auth_headers()
    vehicle = _make_vehicle(headers, soc=10.0)
    for station_status, code in (("offline", "STATION_OFFLINE"), ("maintenance", "STATION_MAINTENANCE")):
        station = _make_station(headers, battery_ready=3, status=station_status)
        resp = client.post(
            "/api/swaps",
            json={"request_id": f"req-{uuid.uuid4().hex}", "vehicle_id": vehicle["id"],
                  "station_id": station["id"], "soc_before": 10.0},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == code
        # 被拒绝的交易不得扣减库存、不得产生记录
        assert client.get(f"/api/stations/{station['id']}", headers=headers).json()["battery_ready"] == 3


def test_swap_rejects_fault_and_busy_vehicle():
    headers = _auth_headers()
    station = _make_station(headers, battery_ready=3)
    for vehicle_status, code in (("fault", "VEHICLE_FAULT"), ("charging", "VEHICLE_BUSY")):
        vehicle = _make_vehicle(headers, soc=10.0, vstatus=vehicle_status)
        resp = client.post(
            "/api/swaps",
            json={"request_id": f"req-{uuid.uuid4().hex}", "vehicle_id": vehicle["id"],
                  "station_id": station["id"], "soc_before": 10.0},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == code
        # 故障车绝不能被改成满电
        assert client.get(f"/api/vehicles/{vehicle['id']}", headers=headers).json()["current_soc"] == 10.0


def test_swap_rejects_reported_soc_mismatch():
    headers = _auth_headers()
    station = _make_station(headers, battery_ready=3)
    vehicle = _make_vehicle(headers, soc=23.5)
    resp = client.post(
        "/api/swaps",
        # 终端谎报换前电量 80（档案只有 23.5），即使换后 100 也必须拒绝
        json={"request_id": f"req-{uuid.uuid4().hex}", "vehicle_id": vehicle["id"],
              "station_id": station["id"], "soc_before": 80.0, "soc_after": 100.0},
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "SOC_MISMATCH"
    assert client.get(f"/api/stations/{station['id']}", headers=headers).json()["battery_ready"] == 3
    assert client.get(f"/api/vehicles/{vehicle['id']}", headers=headers).json()["current_soc"] == 23.5


def test_swap_record_uses_archive_soc_not_reported():
    headers = _auth_headers()
    station = _make_station(headers, battery_ready=3)
    vehicle = _make_vehicle(headers, soc=23.0)
    # 容差内的小偏差允许通过，但落库换前电量必须取档案值
    resp = client.post(
        "/api/swaps",
        json={"request_id": f"req-{uuid.uuid4().hex}", "vehicle_id": vehicle["id"],
              "station_id": station["id"], "soc_before": 23.5, "soc_after": 100.0},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["soc_before"] == 23.0


def test_idempotent_retry_returns_original_transaction():
    headers = _auth_headers()
    station = _make_station(headers, battery_ready=3)
    vehicle = _make_vehicle(headers, soc=10.0)
    request_id = f"req-{uuid.uuid4().hex}"
    body = {"request_id": request_id, "vehicle_id": vehicle["id"],
            "station_id": station["id"], "soc_before": 10.0, "soc_after": 100.0}

    first = client.post("/api/swaps", json=body, headers=headers)
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]

    # 断线后重提同一请求：查回原交易（200 + replayed），库存与记录不重复
    retry = client.post("/api/swaps", json=body, headers=headers)
    assert retry.status_code == 200, retry.text
    assert retry.json()["id"] == first_id
    assert retry.json()["replayed"] is True
    assert retry.json()["idempotency_key"] == request_id

    assert client.get(f"/api/stations/{station['id']}", headers=headers).json()["battery_ready"] == 2
    swaps = client.get("/api/swaps", headers=headers).json()
    assert sum(1 for s in swaps if s["idempotency_key"] == request_id) == 1


def test_same_idempotency_key_different_request_conflicts():
    headers = _auth_headers()
    station = _make_station(headers, battery_ready=3)
    v1 = _make_vehicle(headers, soc=10.0)
    v2 = _make_vehicle(headers, soc=10.0)
    request_id = f"req-{uuid.uuid4().hex}"

    first = client.post("/api/swaps", json={"request_id": request_id, "vehicle_id": v1["id"],
                                            "station_id": station["id"], "soc_before": 10.0}, headers=headers)
    assert first.status_code == 201, first.text

    # 同一编号挂到不同车辆上：必须返回可区分的冲突响应，且第二辆车不发生换电
    clash = client.post("/api/swaps", json={"request_id": request_id, "vehicle_id": v2["id"],
                                            "station_id": station["id"], "soc_before": 10.0}, headers=headers)
    assert clash.status_code == 409
    assert clash.json()["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert client.get(f"/api/vehicles/{v2['id']}", headers=headers).json()["current_soc"] == 10.0
    assert client.get(f"/api/stations/{station['id']}", headers=headers).json()["battery_ready"] == 2


def _service_session():
    from app.database import SessionLocal
    return SessionLocal()


def test_concurrent_last_battery_only_one_succeeds():
    headers = _auth_headers()
    station = _make_station(headers, battery_ready=1, slot_total=1)
    va = _make_vehicle(headers, soc=10.0)
    vb = _make_vehicle(headers, soc=10.0)

    outcomes = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def fire(request_id, vehicle_id):
        db = _service_session()
        barrier.wait()
        try:
            out = swap_service.submit_swap(
                db, SwapCreate(request_id=request_id, vehicle_id=vehicle_id,
                               station_id=station["id"], soc_before=10.0))
            with lock:
                outcomes.append(("ok", out.id))
        except swap_service.SwapError as exc:
            with lock:
                outcomes.append(("err", exc.code))
        finally:
            db.close()

    t1 = threading.Thread(target=fire, args=(f"req-{uuid.uuid4().hex}", va["id"]))
    t2 = threading.Thread(target=fire, args=(f"req-{uuid.uuid4().hex}", vb["id"]))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(r[0] for r in outcomes) == ["err", "ok"], outcomes
    assert [r[1] for r in outcomes if r[0] == "err"] == ["NO_AVAILABLE_BATTERY"]
    # 库存恰好扣一次、记录恰好一条
    assert client.get(f"/api/stations/{station['id']}", headers=headers).json()["battery_ready"] == 0


def test_concurrent_same_vehicle_only_one_succeeds():
    headers = _auth_headers()
    station = _make_station(headers, battery_ready=5)
    vehicle = _make_vehicle(headers, soc=20.0)

    outcomes = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def fire(request_id):
        db = _service_session()
        barrier.wait()
        try:
            out = swap_service.submit_swap(
                db, SwapCreate(request_id=request_id, vehicle_id=vehicle["id"],
                               station_id=station["id"], soc_before=20.0))
            with lock:
                outcomes.append(("ok", out.id))
        except swap_service.SwapError as exc:
            with lock:
                outcomes.append(("err", exc.code))
        finally:
            db.close()

    t1 = threading.Thread(target=fire, args=(f"req-{uuid.uuid4().hex}",))
    t2 = threading.Thread(target=fire, args=(f"req-{uuid.uuid4().hex}",))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert len([o for o in outcomes if o[0] == "ok"]) == 1, outcomes
    # 失败方必须是明确的业务冲突（无电池或车辆状态已变化），而不是静默双花
    err_codes = [o[1] for o in outcomes if o[0] == "err"]
    assert err_codes and err_codes[0] in {"NO_AVAILABLE_BATTERY", "VEHICLE_STATE_CHANGED", "SOC_MISMATCH"}
    assert client.get(f"/api/vehicles/{vehicle['id']}", headers=headers).json()["current_soc"] == 100.0
    # 同一车辆只有一条新记录、库存只扣一次
    assert client.get(f"/api/stations/{station['id']}", headers=headers).json()["battery_ready"] == 4


def test_concurrent_duplicate_request_id_collapses_to_one():
    headers = _auth_headers()
    station = _make_station(headers, battery_ready=1, slot_total=1)
    vehicle = _make_vehicle(headers, soc=10.0)
    request_id = f"req-{uuid.uuid4().hex}"

    outcomes = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def fire():
        db = _service_session()
        barrier.wait()
        try:
            out = swap_service.submit_swap(
                db, SwapCreate(request_id=request_id, vehicle_id=vehicle["id"],
                               station_id=station["id"], soc_before=10.0))
            with lock:
                outcomes.append(("ok", out.id, out.replayed))
        except swap_service.SwapError as exc:
            with lock:
                outcomes.append(("err", exc.code, False))
        finally:
            db.close()

    t1 = threading.Thread(target=fire)
    t2 = threading.Thread(target=fire)
    t1.start(); t2.start(); t1.join(); t2.join()

    # 两个终端同号并发：都成功且指向同一笔交易，库存只扣一次
    oks = [o for o in outcomes if o[0] == "ok"]
    assert len(oks) == 2, outcomes
    assert oks[0][1] == oks[1][1]
    assert client.get(f"/api/stations/{station['id']}", headers=headers).json()["battery_ready"] == 0


def test_idempotency_survives_restart():
    headers = _auth_headers()
    station = _make_station(headers, battery_ready=3)
    vehicle = _make_vehicle(headers, soc=10.0)
    request_id = f"req-{uuid.uuid4().hex}"
    first = client.post(
        "/api/swaps",
        json={"request_id": request_id, "vehicle_id": vehicle["id"],
              "station_id": station["id"], "soc_before": 10.0},
        headers=headers,
    )
    assert first.status_code == 201
    original_id = first.json()["id"]

    # 模拟服务重启：用全新引擎/会话重新打开同一个数据库文件重放请求
    restart_engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
    RestartSession = sessionmaker(bind=restart_engine)
    db = RestartSession()
    try:
        out = swap_service.submit_swap(
            db, SwapCreate(request_id=request_id, vehicle_id=vehicle["id"],
                           station_id=station["id"], soc_before=10.0))
        assert out.replayed is True
        assert out.id == original_id
        persisted = db.get(IdempotentRequest, request_id)
        assert persisted is not None
        assert persisted.swap_record_id == original_id
    finally:
        db.close()
        restart_engine.dispose()


def test_dashboard_stats():
    resp = client.get("/api/dashboard/stats", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["station_total"] >= 4
    assert data["vehicle_total"] >= 5
    assert "battery_ready_total" in data
