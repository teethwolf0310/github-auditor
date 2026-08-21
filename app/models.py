"""SQLAlchemy 表结构。"""
from datetime import datetime
from sqlalchemy import (
    BigInteger, Column, DateTime, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class UserRecord(Base):
    """一个评估过的 GitHub 账号的元信息 + 最近一次 scorecard。"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    handle = Column(String(64), unique=True, nullable=False, index=True)
    gh_id = Column(BigInteger)
    avatar_url = Column(String(512))
    name = Column(String(256))
    email = Column(String(256))
    blog = Column(String(512))
    company = Column(String(256))
    location = Column(String(256))
    bio = Column(Text)
    twitter_username = Column(String(64))
    hireable = Column(Integer)            # nullable → -1/0/1
    type_ = Column(String(32))            # User / Organization / Bot
    site_admin = Column(Integer)
    followers = Column(Integer)
    following = Column(Integer)
    public_repos = Column(Integer)
    public_gists = Column(Integer)
    gh_created_at = Column(DateTime)      # 账号注册时间
    gh_updated_at = Column(DateTime)      # 最近 profile 更新
    fetched_at = Column(DateTime, default=datetime.utcnow)  # 我们何时拉的
    raw_json = Column(Text)               # profile 原样 JSON（压缩大字段后再考虑）

    scorecards = relationship("Scorecard", back_populates="user", cascade="all, delete-orphan")
    repos = relationship("RepoRecord", back_populates="user", cascade="all, delete-orphan")
    events = relationship("EventRecord", back_populates="user", cascade="all, delete-orphan")


class RepoRecord(Base):
    __tablename__ = "repos"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    full_name = Column(String(256), nullable=False)   # owner/repo
    name = Column(String(128))
    owner_login = Column(String(64))
    is_fork = Column(Integer)                          # 0/1
    is_archived = Column(Integer)
    primary_language = Column(String(64))
    languages_total_kb = Column(Integer)               # sum of all language bytes
    stargazers = Column(Integer)
    forks = Column(Integer)
    open_issues = Column(Integer)
    subscribers = Column(Integer)
    size_kb = Column(Integer)
    description = Column(Text)
    topics = Column(String(512))
    gh_created_at = Column(DateTime)
    gh_pushed_at = Column(DateTime)
    license_spdx = Column(String(32))
    has_homepage = Column(Integer)
    default_branch = Column(String(64))
    contributors_total = Column(Integer)               # /contributors 端点取的（可能空）
    raw_json = Column(Text)

    user = relationship("UserRecord", back_populates="repos")
    __table_args__ = (
        UniqueConstraint("user_id", "full_name", name="uq_repo_user_fullname"),
        Index("idx_repo_owner", "owner_login"),
    )


class EventRecord(Base):
    """最近 N 条 public events（用于时序分析）。同时记录 received + performed。"""
    __tablename__ = "events"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    direction = Column(String(16))           # 'received' | 'performed'
    gh_event_id = Column(String(64))
    kind = Column(String(64))                # PushEvent / PullRequestEvent / …
    repo_full = Column(String(256))
    actor_login = Column(String(64))          # performed: 自己;  received: 对方
    event_ts = Column(DateTime, index=True)
    payload_json = Column(Text)              # 摘要

    user = relationship("UserRecord", back_populates="events")
    __table_args__ = (
        UniqueConstraint("user_id", "direction", "gh_event_id", name="uq_event"),
    )


class Scorecard(Base):
    """一次评估结果。同一 user 可有多条（历史）。"""
    __tablename__ = "scorecards"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    computed_at = Column(DateTime, default=datetime.utcnow, index=True)
    req_used = Column(Integer)               # 本次评估用了多少请求

    # 六维 0-100
    authenticity = Column(Integer)
    activity = Column(Integer)
    capability = Column(Integer)
    breadth = Column(Integer)
    longevity = Column(Integer)
    identity = Column(Integer)

    # 综合（权重和=100）
    total = Column(Integer)

    # evidence：每维度的子信号明细，用于 explainability
    evidence_json = Column(Text)

    user = relationship("UserRecord", back_populates="scorecards")
    __table_args__ = (
        Index("idx_scorecards_user_recent", "user_id", "computed_at"),
    )
