"""数据库模型。

业务主题：新能源物流车换电站运营管理。
"""
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .database import Base


class User(Base):
    """后台用户（本平台只有 admin 一个管理员角色）。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    display_name = Column(String(64), nullable=False, default="管理员")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Station(Base):
    """换电站。"""

    __tablename__ = "stations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    address = Column(String(256), nullable=False, default="")
    # 电池仓位总数与当前满电可换电池数
    slot_total = Column(Integer, nullable=False, default=0)
    battery_ready = Column(Integer, nullable=False, default=0)
    # 运营状态：running 运营中 / maintenance 维护中 / offline 离线
    status = Column(String(16), nullable=False, default="running")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swaps = relationship("SwapRecord", back_populates="station")


class Vehicle(Base):
    """新能源物流车。"""

    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True, index=True)
    plate = Column(String(32), unique=True, nullable=False, index=True)
    model = Column(String(64), nullable=False, default="")
    battery_capacity = Column(Float, nullable=False, default=100.0)  # kWh
    current_soc = Column(Float, nullable=False, default=100.0)  # 0-100 百分比
    # 状态：idle 空闲 / running 运营 / charging 换电中 / fault 故障
    status = Column(String(16), nullable=False, default="idle")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swaps = relationship("SwapRecord", back_populates="vehicle")


class SwapRecord(Base):
    """换电记录。"""

    __tablename__ = "swap_records"

    id = Column(Integer, primary_key=True, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False, index=True)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=False, index=True)
    # soc_before 以车辆档案中的真实换前电量为准（不信任终端上报）；
    # client_soc_before 仅留痕终端上报值，便于排查“上报与档案不一致”。
    soc_before = Column(Float, nullable=False, default=0.0)
    client_soc_before = Column(Float, nullable=True)
    soc_after = Column(Float, nullable=False, default=100.0)
    swapped_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    # 调用方提供的幂等标识，成功交易唯一；断线重提凭此查回原交易。
    idempotency_key = Column(String(64), unique=True, nullable=True, index=True)

    vehicle = relationship("Vehicle", back_populates="swaps")
    station = relationship("Station", back_populates="swaps")


class IdempotentRequest(Base):
    """换电请求的幂等登记。

    记录在换电事务内与交易一同提交：只要键存在，就代表对应交易已落库，
    进程重启后仍可凭键追溯原交易结果。
    """

    __tablename__ = "idempotent_requests"

    idempotency_key = Column(String(64), primary_key=True)
    # 相同键但请求体不同（车/站不一致）时用于判定冲突
    request_fingerprint = Column(String(64), nullable=False)
    swap_record_id = Column(
        Integer, ForeignKey("swap_records.id"), unique=True, nullable=False
    )
    # 首次成功响应快照，重放时原样返回
    response_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swap_record = relationship("SwapRecord")
