"""换电交易服务。

交易链路的核心约束：
1. 落库前一致校验：站点必须运营中、车辆必须可换、终端上报的换前电量必须
   与车辆档案一致（在容差内），且站点仍有满电电池。
2. 并发安全：数据库对写事务使用 BEGIN IMMEDIATE 串行化，库存扣减再用
   条件 UPDATE（battery_ready > 0）兜底，最后一块电池不会被双花。
3. 幂等：调用方携带 request_id，同一请求重放返回原交易；相同编号但
   内容不同返回可区分的冲突错误。登记与交易同事务提交，重启后仍可追溯。
4. 原子性：换电记录、车辆电量、站点库存、幂等登记在同一事务内提交，
   任一步失败全部回滚。
"""
import hashlib
import json

from sqlalchemy.orm import Session

from ..models import IdempotentRequest, Station, SwapRecord, Vehicle
from ..schemas import SwapCreate, SwapOut

# 终端上报换前电量与车辆档案电量的允许偏差（百分点）
SOC_MATCH_TOLERANCE = 1.0


class SwapError(Exception):
    """可区分的业务错误：code 供调用方程序化处理，message 供运营查看。"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _fingerprint(payload: SwapCreate) -> str:
    """对业务内容指纹（不含 request_id），用于识别“同号不同单”。"""
    raw = json.dumps(
        {
            "vehicle_id": payload.vehicle_id,
            "station_id": payload.station_id,
            "soc_after": payload.soc_after,
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _to_out(record: SwapRecord, replayed: bool = False) -> SwapOut:
    return SwapOut(
        id=record.id,
        vehicle_id=record.vehicle_id,
        station_id=record.station_id,
        soc_before=record.soc_before,
        soc_after=record.soc_after,
        swapped_at=record.swapped_at,
        idempotency_key=record.idempotency_key,
        vehicle_plate=record.vehicle.plate if record.vehicle else None,
        station_name=record.station.name if record.station else None,
        replayed=replayed,
    )


def _replay(existing: IdempotentRequest) -> SwapOut:
    """从持久化的响应快照还原原交易（重启后同样可查回）。"""
    data = json.loads(existing.response_json)
    out = SwapOut.model_validate(data)
    out.replayed = True
    return out


def submit_swap(db: Session, payload: SwapCreate) -> SwapOut:
    """提交一笔换电交易；重复请求返回原交易（replayed=True）。

    调用方无需自行开启事务：进入本方法后的第一条 SQL 即通过
    BEGIN IMMEDIATE 取得写锁，commit/rollback 均在本方法内完成。
    """
    fingerprint = _fingerprint(payload)

    # ① 幂等查重（在写锁内，并发的同号请求会在此串行排队）
    existing = db.get(IdempotentRequest, payload.request_id)
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise SwapError(
                409,
                "IDEMPOTENCY_KEY_CONFLICT",
                "请求编号已被另一笔换电使用，不能复用编号提交不同的车辆或站点",
            )
        return _replay(existing)

    # ② 存在性校验
    vehicle = db.get(Vehicle, payload.vehicle_id)
    if vehicle is None:
        db.rollback()
        raise SwapError(404, "VEHICLE_NOT_FOUND", "车辆不存在")
    station = db.get(Station, payload.station_id)
    if station is None:
        db.rollback()
        raise SwapError(404, "STATION_NOT_FOUND", "换电站不存在")

    # ③ 站点运营状态校验：离线站、维护站不得完成交易
    if station.status == "offline":
        db.rollback()
        raise SwapError(422, "STATION_OFFLINE", "换电站已离线，不能完成换电")
    if station.status == "maintenance":
        db.rollback()
        raise SwapError(422, "STATION_MAINTENANCE", "换电站维护中，暂停换电")

    # ④ 车辆可换状态校验：故障车不得被改成满电，换电/充电中的车不可再换
    if vehicle.status == "fault":
        db.rollback()
        raise SwapError(422, "VEHICLE_FAULT", "车辆处于故障状态，不能换电")
    if vehicle.status == "charging":
        db.rollback()
        raise SwapError(422, "VEHICLE_BUSY", "车辆正在换电/充电中，请勿重复提交")

    # ⑤ 实际电量一致校验：终端上报值必须与车辆档案一致；
    #    落库的 soc_before 永远以档案为准，不信任终端上报。
    archive_soc = vehicle.current_soc
    if abs(payload.soc_before - archive_soc) > SOC_MATCH_TOLERANCE:
        db.rollback()
        raise SwapError(
            422,
            "SOC_MISMATCH",
            f"终端上报换前电量 {payload.soc_before} 与车辆档案电量 {archive_soc} 不一致",
        )
    if payload.soc_after <= archive_soc:
        db.rollback()
        raise SwapError(422, "SOC_NOT_IMPROVED", "换电后电量应高于换前电量")

    # ⑥ 库存预检（条件 UPDATE 才是最终凭据，这里先给出可读的业务错误）
    if station.battery_ready <= 0:
        db.rollback()
        raise SwapError(422, "NO_AVAILABLE_BATTERY", "该换电站暂无满电电池可换")

    # ⑦ 条件扣减：只有 battery_ready > 0 才会生效。
    #    写锁已串行化并发事务，条件更新再兜一层底，杜绝最后一块电池被双花。
    updated = (
        db.query(Station)
        .filter(Station.id == station.id, Station.battery_ready > 0)
        .update({Station.battery_ready: Station.battery_ready - 1}, synchronize_session=False)
    )
    if updated == 0:
        db.rollback()
        raise SwapError(422, "NO_AVAILABLE_BATTERY", "该换电站暂无满电电池可换")

    # ⑧ 同一事务内写记录、更新车辆电量。
    #    车辆更新同样带条件（CAS：档案电量必须仍是本事务读到的值），
    #    与库存的条件更新一起构成双重凭据，防止车辆被并发改写。
    record = SwapRecord(
        vehicle_id=vehicle.id,
        station_id=station.id,
        soc_before=archive_soc,
        client_soc_before=payload.soc_before,
        soc_after=payload.soc_after,
        idempotency_key=payload.request_id,
    )
    changed = (
        db.query(Vehicle)
        .filter(
            Vehicle.id == vehicle.id,
            Vehicle.current_soc == archive_soc,
            Vehicle.status.in_(("idle", "running")),
        )
        .update({Vehicle.current_soc: payload.soc_after}, synchronize_session=False)
    )
    if changed == 0:
        db.rollback()
        raise SwapError(409, "VEHICLE_STATE_CHANGED", "车辆状态已被其他交易改变，请刷新后重试")
    db.add(record)
    db.flush()  # 取 record.id / swapped_at，供响应快照与幂等登记引用

    out = _to_out(record)
    registration = IdempotentRequest(
        idempotency_key=payload.request_id,
        request_fingerprint=fingerprint,
        swap_record_id=record.id,
        response_json=out.model_dump_json(),
    )
    db.add(registration)

    # ⑨ 统一提交：记录、车辆、库存、幂等登记要么全落库，要么全回滚。
    #    唯一约束冲突只可能来自极端竞态，按重放/冲突重新判定。
    try:
        db.commit()
    except Exception:
        db.rollback()
        existing = db.get(IdempotentRequest, payload.request_id)
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise SwapError(
                    409,
                    "IDEMPOTENCY_KEY_CONFLICT",
                    "请求编号已被另一笔换电使用，不能复用编号提交不同的车辆或站点",
                )
            return _replay(existing)
        raise

    db.refresh(record)
    return _to_out(record)
