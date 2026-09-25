"""换电记录路由（需登录）。

换电交易链路的一致性设计：

- 落库前一致校验：站点须运营中、车辆须处于可换状态、终端上报的换前电量
  须与车辆档案一致，任一不满足即拒绝，并返回可区分的业务响应；
- 幂等：调用方通过请求体 request_id（或 Idempotency-Key 请求头）提供幂等标识，
  重复提交（断线重提、重试）返回原交易（HTTP 200）；标识持久化在换电记录中，
  重启后仍可追溯；
- 并发：库存扣减与车辆电量更新使用条件更新（CAS），并发争用最后一块电池或
  同一车辆时只有一笔成功；换电记录、车辆电量、站点库存在同一事务中提交，
  任一失败全部回滚。
"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import Station, SwapRecord, Vehicle
from ..schemas import SwapCreate, SwapOut

router = APIRouter(prefix="/api/swaps", tags=["换电记录"], dependencies=[Depends(get_current_user)])

# 电量浮点比较容差
_SOC_EPSILON = 1e-6

# 状态中文名，用于可区分的业务响应
_STATION_STATUS_CN = {"running": "运营中", "maintenance": "维护中", "offline": "离线"}
_VEHICLE_STATUS_CN = {"idle": "空闲", "running": "运营", "charging": "换电中", "fault": "故障"}
# 可换电的车辆状态
_SWAPPABLE_VEHICLE_STATUS = ("idle", "running")


def _to_out(record: SwapRecord) -> SwapOut:
    return SwapOut(
        id=record.id,
        vehicle_id=record.vehicle_id,
        station_id=record.station_id,
        soc_before=record.soc_before,
        soc_after=record.soc_after,
        swapped_at=record.swapped_at,
        request_id=record.request_id,
        vehicle_plate=record.vehicle.plate if record.vehicle else None,
        station_name=record.station.name if record.station else None,
    )


def _find_by_request_id(db: Session, request_id: str) -> Optional[SwapRecord]:
    return db.query(SwapRecord).filter(SwapRecord.request_id == request_id).first()


def _is_same_payload(record: SwapRecord, payload: SwapCreate) -> bool:
    """已存在的幂等记录与本次请求内容是否一致。"""
    return (
        record.vehicle_id == payload.vehicle_id
        and record.station_id == payload.station_id
        and abs(record.soc_before - payload.soc_before) <= _SOC_EPSILON
        and abs(record.soc_after - payload.soc_after) <= _SOC_EPSILON
    )


def _replay_existing(db: Session, request_id: Optional[str], payload: SwapCreate, response: Response) -> Optional[SwapOut]:
    """若同一幂等标识的交易已落库且内容一致，返回原交易（HTTP 200）。"""
    if not request_id:
        return None
    existing = _find_by_request_id(db, request_id)
    if existing and _is_same_payload(existing, payload):
        response.status_code = status.HTTP_200_OK
        return _to_out(existing)
    return None


def _execute_swap(db: Session, payload: SwapCreate, request_id: Optional[str]) -> SwapRecord:
    """校验并执行换电交易：校验失败或并发冲突时抛出 HTTPException，不产生任何落库。"""
    vehicle = db.get(Vehicle, payload.vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    station = db.get(Station, payload.station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    if payload.soc_after <= payload.soc_before:
        raise HTTPException(status_code=422, detail="换电后电量应高于换电前电量")
    if station.status != "running":
        label = _STATION_STATUS_CN.get(station.status, station.status)
        raise HTTPException(status_code=409, detail=f"换电站当前状态（{label}）不可进行换电")
    if vehicle.status not in _SWAPPABLE_VEHICLE_STATUS:
        label = _VEHICLE_STATUS_CN.get(vehicle.status, vehicle.status)
        raise HTTPException(status_code=409, detail=f"车辆当前状态（{label}）不可换电")
    if abs(payload.soc_before - vehicle.current_soc) > _SOC_EPSILON:
        raise HTTPException(
            status_code=409,
            detail=f"换前电量（{payload.soc_before}%）与车辆档案电量（{vehicle.current_soc}%）不一致",
        )

    # 换前电量以车辆档案为准，不信任终端上报之外的任何值
    soc_before = vehicle.current_soc

    # 原子扣减站点库存（条件更新）：并发争用最后一块电池时只有一笔成功
    result = db.execute(
        update(Station)
        .where(
            Station.id == station.id,
            Station.status == "running",
            Station.battery_ready > 0,
        )
        .values(battery_ready=Station.battery_ready - 1),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        db.rollback()
        # 区分失败原因：站点被并发改为非运营，或满电电池已被并发耗尽
        fresh = db.get(Station, station.id)
        if fresh is not None and fresh.status != "running":
            label = _STATION_STATUS_CN.get(fresh.status, fresh.status)
            raise HTTPException(status_code=409, detail=f"换电站当前状态（{label}）不可进行换电")
        raise HTTPException(status_code=422, detail="该换电站暂无满电电池可换")

    # 原子更新车辆电量（条件更新）：同一车辆的并发换电只有一笔成功
    result = db.execute(
        update(Vehicle)
        .where(
            Vehicle.id == vehicle.id,
            Vehicle.status.in_(_SWAPPABLE_VEHICLE_STATUS),
            Vehicle.current_soc == soc_before,
        )
        .values(current_soc=payload.soc_after),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="车辆电量或状态已被并发修改，本次换电冲突")

    # 换电记录、车辆电量、站点库存在同一事务中提交；任一失败全部回滚
    record = SwapRecord(
        vehicle_id=vehicle.id,
        station_id=station.id,
        soc_before=soc_before,
        soc_after=payload.soc_after,
        request_id=request_id or f"auto-{uuid.uuid4().hex}",
    )
    db.add(record)
    db.commit()
    return record


@router.get("", response_model=list[SwapOut])
def list_swaps(request_id: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(SwapRecord)
    if request_id:
        query = query.filter(SwapRecord.request_id == request_id)
    records = query.order_by(SwapRecord.swapped_at.desc()).all()
    return [_to_out(r) for r in records]


@router.post("", response_model=SwapOut, status_code=status.HTTP_201_CREATED)
def create_swap(
    payload: SwapCreate,
    response: Response,
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(default=None),
):
    # 幂等标识：请求体 request_id 优先，其次 Idempotency-Key 请求头
    request_id = payload.request_id or idempotency_key

    # 1) 幂等重放：同一标识的重复请求（断线重提、重试）直接返回原交易
    if request_id:
        existing = _find_by_request_id(db, request_id)
        if existing:
            if not _is_same_payload(existing, payload):
                raise HTTPException(status_code=409, detail="幂等标识已被其他换电请求使用")
            response.status_code = status.HTTP_200_OK
            return _to_out(existing)

    # 2) 一致校验 + 原子落库；任何失败路径都先尝试查回原交易再报错
    try:
        record = _execute_swap(db, payload, request_id)
    except IntegrityError:
        # 并发下同标识请求已抢先入库（唯一约束兜底）：返回原交易
        db.rollback()
        replay = _replay_existing(db, request_id, payload, response)
        if replay:
            return replay
        if request_id and _find_by_request_id(db, request_id):
            raise HTTPException(status_code=409, detail="幂等标识已被其他换电请求使用")
        raise HTTPException(status_code=500, detail="换电交易提交失败，请重试")
    except HTTPException:
        # 校验/并发冲突失败：若是同标识请求的并发重提，查回原交易
        db.rollback()
        replay = _replay_existing(db, request_id, payload, response)
        if replay:
            return replay
        raise

    db.refresh(record)
    return _to_out(record)
