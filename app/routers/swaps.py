"""换电记录路由（需登录）。"""
import json

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import SwapRecord
from ..schemas import SwapCreate, SwapOut
from ..services import swap_service

router = APIRouter(prefix="/api/swaps", tags=["换电记录"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[SwapOut])
def list_swaps(db: Session = Depends(get_db)):
    records = db.query(SwapRecord).order_by(SwapRecord.swapped_at.desc()).all()
    return [
        SwapOut(
            id=r.id,
            vehicle_id=r.vehicle_id,
            station_id=r.station_id,
            soc_before=r.soc_before,
            soc_after=r.soc_after,
            swapped_at=r.swapped_at,
            idempotency_key=r.idempotency_key,
            vehicle_plate=r.vehicle.plate if r.vehicle else None,
            station_name=r.station.name if r.station else None,
        )
        for r in records
    ]


@router.post("", response_model=SwapOut, status_code=status.HTTP_201_CREATED)
def create_swap(payload: SwapCreate, db: Session = Depends(get_db)):
    try:
        result = swap_service.submit_swap(db, payload)
    except swap_service.SwapError as exc:
        # 可区分的业务响应：code 供终端程序化区分（离线、故障、电量不符、
        # 无电池、幂等冲突等），重放不在这里——重放走正常 200/201。
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, "code": exc.code},
        )

    # 幂等重放返回原交易，使用 200 以区分“本次新建（201）”与“查回原单（200）”
    if result.replayed:
        return JSONResponse(status_code=200, content=json.loads(result.model_dump_json()))
    return result
