"""Pydantic 数据模型（请求体与响应体）。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------- 认证 ----------
class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str

    model_config = {"from_attributes": True}


# ---------- 换电站 ----------
class StationBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    address: str = ""
    slot_total: int = Field(0, ge=0)
    battery_ready: int = Field(0, ge=0)
    status: str = Field("running", pattern="^(running|maintenance|offline)$")


class StationCreate(StationBase):
    pass


class StationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    address: Optional[str] = None
    slot_total: Optional[int] = Field(None, ge=0)
    battery_ready: Optional[int] = Field(None, ge=0)
    status: Optional[str] = Field(None, pattern="^(running|maintenance|offline)$")


class StationOut(StationBase):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------- 车辆 ----------
class VehicleBase(BaseModel):
    plate: str = Field(..., min_length=1, max_length=32)
    model: str = ""
    battery_capacity: float = Field(100.0, gt=0)
    current_soc: float = Field(100.0, ge=0, le=100)
    status: str = Field("idle", pattern="^(idle|running|charging|fault)$")


class VehicleCreate(VehicleBase):
    pass


class VehicleUpdate(BaseModel):
    plate: Optional[str] = Field(None, min_length=1, max_length=32)
    model: Optional[str] = None
    battery_capacity: Optional[float] = Field(None, gt=0)
    current_soc: Optional[float] = Field(None, ge=0, le=100)
    status: Optional[str] = Field(None, pattern="^(idle|running|charging|fault)$")


class VehicleOut(VehicleBase):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------- 换电记录 ----------
class SwapCreate(BaseModel):
    # 调用方（作业终端）生成的幂等标识：同一笔业务重试必须复用同一个值。
    request_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-:#]+$")
    vehicle_id: int
    station_id: int
    # 终端上报的换前电量，仅作留痕与比对；落库以车辆档案中的真实电量为准
    soc_before: float = Field(..., ge=0, le=100)
    soc_after: float = Field(100.0, ge=0, le=100)


class SwapOut(BaseModel):
    id: int
    vehicle_id: int
    station_id: int
    soc_before: float
    soc_after: float
    swapped_at: datetime
    idempotency_key: Optional[str] = None
    vehicle_plate: Optional[str] = None
    station_name: Optional[str] = None
    # True 表示本次响应来自幂等重放（返回的是已落库的原交易）
    replayed: bool = False

    model_config = {"from_attributes": True}


# ---------- 仪表盘 ----------
class DashboardStats(BaseModel):
    station_total: int
    station_running: int
    vehicle_total: int
    vehicle_fault: int
    swap_today: int
    battery_ready_total: int
