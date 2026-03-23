from datetime import datetime
from typing import Optional, List
from sqlmodel import Field, SQLModel, create_engine, Session, select, Relationship

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    hashed_password: str
    api_key: str = Field(index=True, unique=True)
    role: str = "user" # "admin" or "user"
    quota_limit: int = 100000
    quota_used: int = 0
    is_active: bool = True
    
    records: List["UsageRecord"] = Relationship(back_populates="user")

class UsageRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    
    user: Optional[User] = Relationship(back_populates="records")

sqlite_file_name = "ghost.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

engine = create_engine(sqlite_url, echo=False)

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    seed_default_user()

def seed_default_user():
    from apps.server.auth.security import get_password_hash
    with Session(engine) as session:
        statement = select(User).where(User.username == "ghost")
        existing = session.exec(statement).first()
        if not existing:
            new_user = User(
                username="ghost",
                hashed_password=get_password_hash("ghost-dev-secret"),
                api_key="ghost-dev-2026",
                role="admin",
                quota_limit=999999
            )
            session.add(new_user)
            session.commit()

def log_usage(user_id: int, model: str, input_tokens: int, output_tokens: int, cost: float):
    with Session(engine) as session:
        record = UsageRecord(
            user_id=user_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            estimated_cost=cost
        )
        session.add(record)
        # Update user quota
        user = session.get(User, user_id)
        if user:
            user.quota_used += (input_tokens + output_tokens)
        session.commit()

def get_user_by_api_key(api_key: str) -> Optional[User]:
    with Session(engine) as session:
        statement = select(User).where(User.api_key == api_key)
        return session.exec(statement).first()

def get_user_by_username(username: str) -> Optional[User]:
    with Session(engine) as session:
        statement = select(User).where(User.username == username)
        return session.exec(statement).first()
