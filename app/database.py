"""数据库连接与会话管理。"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL

# SQLite 需要关闭同线程检查以配合 FastAPI 的依赖注入；
# timeout 对应 busy_timeout，写锁被占用时等待而不是立刻报 database is locked。
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 5},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


# SQLite 默认以 BEGIN DEFERRED 开启事务（读时不加锁、首次写才尝试加锁），
# 两个并发事务可能同时读到“还有 1 块电池”再各自扣减。改为每个事务一开始
# 就执行 BEGIN IMMEDIATE 立即获取 RESERVED 写锁，把所有写事务串行化，
# 从数据库层面杜绝最后一块电池被重复消耗、同一车辆被并发换电。
@event.listens_for(engine, "connect")
def _sqlite_connect(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
    # 关闭 pysqlite 的隐式事务，事务边界完全交给下面的 begin 事件控制
    dbapi_connection.isolation_level = None


@event.listens_for(engine, "begin")
def _sqlite_begin_immediate(conn):
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def get_db():
    """FastAPI 依赖：提供一个数据库会话，请求结束后关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
