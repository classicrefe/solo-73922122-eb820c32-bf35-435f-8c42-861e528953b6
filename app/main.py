"""馆藏目录抽检 API（FastAPI + SQLite）

质检流程：
1. 开单：POST /orders，固定随机种子 seed 与每馆名额 quota。
2. 换版：POST /catalog/versions，整版目录原子生效；失败保留原目录。
3. 领单：GET /selection，返回当前名单（含条目修订号）及待查状态。
4. 回报：POST /reports，带领单所见修订号；题名/分馆改过或条目曾删除，旧回报一律退回。

发放存储（按规则共享，不再逐单复制名单）：
- 同一 (seed, quota) 为一套规则（rules 表，全库唯一）；换版时每套规则只重算一次名单，
  多张单沿用同一规则不再各算各存。
- rule_bibs 每套规则按"曾入选的不同书目"存一行：entered_seq 为最近一次入选时的目录
  版本号，exited_seq 为落选时的目录版本号（NULL 表示当前在名单中）；只在该书目入选
  状态变化时更新，落选票目保留行不清除。
- 每开一单只新增 orders 中固定大小的 (rule_id, base_seq)：base_seq 为开单时最新目录
  版本号。回报受理范围 = 当前在名单中，或落选版本号 > base_seq —— 即"本单开出时或
  此后成功换版中入选过"；即使后来落选，只要书目还在目录中，就可凭现行修订号回报。
- 旧库逐单保存的 issued 记录保留为只读，仍只对原单有效，不并入共享记录。

一致性：开单/换版与对应发放在同一事务生效，写入失败整体回滚、保留原状态；
全部状态存于单个 SQLite 文件，重启后按已提交的发放受理回报。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

SCHEMA = """
-- 抽样规则：同一 (seed, quota) 全库唯一，名单按规则共享
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed INTEGER NOT NULL,
    quota INTEGER NOT NULL CHECK (quota >= 1),
    UNIQUE(seed, quota)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed INTEGER NOT NULL,
    quota INTEGER NOT NULL CHECK (quota >= 1),
    -- 固定大小的发放信息：所属规则 + 开单时最新目录版本号
    rule_id INTEGER REFERENCES rules(id),
    base_seq INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
-- 当前生效目录
CREATE TABLE IF NOT EXISTS entries (
    bib_no TEXT PRIMARY KEY,
    branch TEXT NOT NULL,
    title TEXT NOT NULL,
    revision INTEGER NOT NULL,
    version_id INTEGER NOT NULL REFERENCES catalog_versions(id)
);
-- 每条书目号的修订号记忆（含已删除条目的墓碑，保证修订号单调递增、绝不复用）
CREATE TABLE IF NOT EXISTS bib_revisions (
    bib_no TEXT PRIMARY KEY,
    last_revision INTEGER NOT NULL
);
-- 每套规则曾入选的书目（共享发放记录）：entered_seq 为最近一次入选时的目录版本号，
-- exited_seq 为落选时的目录版本号，NULL 表示当前在名单中
CREATE TABLE IF NOT EXISTS rule_bibs (
    rule_id INTEGER NOT NULL REFERENCES rules(id),
    bib_no TEXT NOT NULL,
    entered_seq INTEGER NOT NULL,
    exited_seq INTEGER,
    PRIMARY KEY (rule_id, bib_no)
);
-- 旧版逐单发放记录（遗留只读，仍只对原单有效；新单不再写入）
CREATE TABLE IF NOT EXISTS issued (
    order_id INTEGER NOT NULL REFERENCES orders(id),
    bib_no TEXT NOT NULL,
    issued_revision INTEGER NOT NULL,
    PRIMARY KEY (order_id, bib_no)
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    bib_no TEXT NOT NULL,
    revision INTEGER NOT NULL,
    match INTEGER NOT NULL CHECK (match IN (0, 1)),
    created_at TEXT NOT NULL
);
"""

ORDER_COLS = "id, seed, quota, rule_id, base_seq, created_at"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def priority(seed: int, bib_no: str) -> int:
    """每条书目的固定随机优先级：由开单种子与书目号决定，换版、重启均不变。"""
    digest = hashlib.sha256(f"{seed}:{bib_no}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


class OrderIn(BaseModel):
    seed: int
    quota: int = Field(ge=1, description="每馆名额")


class EntryIn(BaseModel):
    bib_no: str = Field(min_length=1, description="唯一书目号")
    branch: str = Field(min_length=1, description="分馆")
    title: str = Field(min_length=1, description="题名")


class CatalogIn(BaseModel):
    entries: list[EntryIn]


class ReportIn(BaseModel):
    bib_no: str
    revision: int = Field(ge=1, description="领单所见的条目修订号")
    match: bool = Field(description="题名是否相符")


def create_app(db_path: str) -> FastAPI:
    lock = threading.RLock()
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # ---------- 内部 helpers ----------

    def compute_selection(seed: int, quota: int) -> list[sqlite3.Row]:
        """按固定随机优先级逐馆重选；条目不足则全查。"""
        rows = conn.execute(
            "SELECT bib_no, branch, title, revision FROM entries"
        ).fetchall()
        by_branch: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            by_branch.setdefault(r["branch"], []).append(r)
        selected: list[sqlite3.Row] = []
        for branch in sorted(by_branch):
            items = by_branch[branch]
            items.sort(key=lambda r: (priority(seed, r["bib_no"]), r["bib_no"]))
            selected.extend(items[:quota])
        return selected

    def current_version_id() -> int:
        """当前生效目录版本号（尚无版本时为 0），作为发放记录的版本刻度。"""
        row = conn.execute(
            "SELECT id FROM catalog_versions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else 0

    def migrate_legacy() -> None:
        """旧库升级：orders 补 (rule_id, base_seq)，并按现行目录为每套规则回填当前名单。

        同一种子和名额只建一套规则；旧表 issued 的逐单记录保持原样，仍只对原单有效。
        """
        cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
        if "rule_id" in cols:
            return
        conn.execute("ALTER TABLE orders ADD COLUMN rule_id INTEGER REFERENCES rules(id)")
        conn.execute("ALTER TABLE orders ADD COLUMN base_seq INTEGER")
        base = current_version_id()
        rule_ids: dict[tuple[int, int], int] = {}
        for row in conn.execute("SELECT DISTINCT seed, quota FROM orders").fetchall():
            cur = conn.execute(
                "INSERT INTO rules(seed, quota) VALUES (?,?)",
                (row["seed"], row["quota"]),
            )
            rule_ids[(row["seed"], row["quota"])] = cur.lastrowid
        for (seed, quota), rule_id in rule_ids.items():
            for r in compute_selection(seed, quota):
                conn.execute(
                    "INSERT INTO rule_bibs(rule_id, bib_no, entered_seq) VALUES (?,?,?)",
                    (rule_id, r["bib_no"], base),
                )
            conn.execute(
                "UPDATE orders SET rule_id=?, base_seq=? WHERE seed=? AND quota=?",
                (rule_id, base, seed, quota),
            )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with lock:
            conn.executescript(SCHEMA)
            migrate_legacy()
            conn.commit()
        yield
        with lock:
            conn.close()

    app = FastAPI(title="馆藏目录抽检 API", version="1.0.0", lifespan=lifespan)

    def active_order() -> Optional[sqlite3.Row]:
        return conn.execute(
            f"SELECT {ORDER_COLS} FROM orders ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def resolve_order(order_id: Optional[int]) -> sqlite3.Row:
        if order_id is None:
            row = active_order()
            if row is None:
                raise HTTPException(status_code=409, detail="尚未开单")
            return row
        row = conn.execute(
            f"SELECT {ORDER_COLS} FROM orders WHERE id=?", (order_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="抽检单不存在")
        return row

    def was_issued(order: sqlite3.Row, bib_no: str) -> bool:
        """该书目是否在本单开出时或此后成功换版中入选过（含旧库逐单记录）。"""
        legacy = conn.execute(
            "SELECT 1 FROM issued WHERE order_id=? AND bib_no=?",
            (order["id"], bib_no),
        ).fetchone()
        if legacy is not None:
            return True  # 旧库逐单保存的记录，只对原单有效
        if order["rule_id"] is None:
            return False
        # 共享发放：当前在名单中，或落选版本晚于开单基准版本（即开单后曾入选）
        shared = conn.execute(
            "SELECT 1 FROM rule_bibs WHERE rule_id=? AND bib_no=? "
            "AND (exited_seq IS NULL OR exited_seq > ?)",
            (order["rule_id"], bib_no, order["base_seq"]),
        ).fetchone()
        return shared is not None

    def effective_report(order_id: int, bib_no: str, revision: int) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT id, match, created_at FROM reports "
            "WHERE order_id=? AND bib_no=? AND revision=? ORDER BY id DESC LIMIT 1",
            (order_id, bib_no, revision),
        ).fetchone()

    # ---------- 接口 ----------

    @app.get("/")
    def root():
        return {"service": "馆藏目录抽检 API", "docs": "/docs"}

    @app.post("/orders", status_code=201, summary="开单：固定随机种子与每馆名额")
    def create_order(payload: OrderIn):
        with lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                base_seq = current_version_id()
                rule = conn.execute(
                    "SELECT id FROM rules WHERE seed=? AND quota=?",
                    (payload.seed, payload.quota),
                ).fetchone()
                if rule is None:
                    # 新规则：名单只算这一次，此后由换版按状态变化增量维护
                    cur = conn.execute(
                        "INSERT INTO rules(seed, quota) VALUES (?,?)",
                        (payload.seed, payload.quota),
                    )
                    rule_id = cur.lastrowid
                    selected = compute_selection(payload.seed, payload.quota)
                    for r in selected:
                        conn.execute(
                            "INSERT INTO rule_bibs(rule_id, bib_no, entered_seq) "
                            "VALUES (?,?,?)",
                            (rule_id, r["bib_no"], base_seq),
                        )
                    issued_count = len(selected)
                else:
                    # 沿用既有规则的共享名单，本单只记固定大小的 (rule_id, base_seq)
                    rule_id = rule["id"]
                    issued_count = conn.execute(
                        "SELECT COUNT(*) AS c FROM rule_bibs "
                        "WHERE rule_id=? AND exited_seq IS NULL",
                        (rule_id,),
                    ).fetchone()["c"]
                cur = conn.execute(
                    "INSERT INTO orders(seed, quota, rule_id, base_seq, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (payload.seed, payload.quota, rule_id, base_seq, utcnow()),
                )
                order_id = cur.lastrowid
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {
            "order_id": order_id,
            "seed": payload.seed,
            "quota": payload.quota,
            "issued": issued_count,
        }

    @app.get("/orders/active", summary="当前生效抽检单")
    def get_active_order():
        with lock:
            conn.execute("BEGIN")
            try:
                order = active_order()
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        if order is None:
            raise HTTPException(status_code=404, detail="尚未开单")
        return {
            "id": order["id"],
            "seed": order["seed"],
            "quota": order["quota"],
            "created_at": order["created_at"],
        }

    @app.post("/catalog/versions", status_code=201, summary="换版：整版目录原子生效，失败保留原目录")
    def upload_catalog(payload: CatalogIn):
        bib_nos = [e.bib_no for e in payload.entries]
        if len(set(bib_nos)) != len(bib_nos):
            raise HTTPException(status_code=400, detail="目录中存在重复书目号，整版已拒收")
        with lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "INSERT INTO catalog_versions(entry_count, created_at) VALUES (?,?)",
                    (len(payload.entries), utcnow()),
                )
                version_id = cur.lastrowid
                current = {
                    r["bib_no"]: r
                    for r in conn.execute(
                        "SELECT bib_no, branch, title, revision FROM entries"
                    )
                }
                added = changed = unchanged = 0
                for e in payload.entries:
                    last = conn.execute(
                        "SELECT last_revision FROM bib_revisions WHERE bib_no=?",
                        (e.bib_no,),
                    ).fetchone()
                    last_rev = last["last_revision"] if last else 0
                    prev = current.get(e.bib_no)
                    if prev is not None and prev["title"] == e.title and prev["branch"] == e.branch:
                        rev = prev["revision"]
                        unchanged += 1
                    else:
                        # 题名或分馆改过、删除后重新录入：修订号单调递增，旧回报永久失效
                        rev = last_rev + 1
                        if prev is None:
                            added += 1
                        else:
                            changed += 1
                    conn.execute(
                        "INSERT INTO bib_revisions(bib_no, last_revision) VALUES (?,?) "
                        "ON CONFLICT(bib_no) DO UPDATE SET last_revision=excluded.last_revision",
                        (e.bib_no, rev),
                    )
                    conn.execute(
                        "INSERT INTO entries(bib_no, branch, title, revision, version_id) "
                        "VALUES (?,?,?,?,?) "
                        "ON CONFLICT(bib_no) DO UPDATE SET branch=excluded.branch, "
                        "title=excluded.title, revision=excluded.revision, "
                        "version_id=excluded.version_id",
                        (e.bib_no, e.branch, e.title, rev, version_id),
                    )
                keep = set(bib_nos)
                removed = 0
                for b in current:
                    if b not in keep:
                        conn.execute("DELETE FROM entries WHERE bib_no=?", (b,))
                        removed += 1
                # 换版后每套规则按固定优先级重选一次（同一种子和名额只算一次名单），
                # 发放记录只为入选状态变化的书目更新
                for rule in conn.execute("SELECT id, seed, quota FROM rules").fetchall():
                    selected = compute_selection(rule["seed"], rule["quota"])
                    selected_bibs = {r["bib_no"] for r in selected}
                    current_bibs = {
                        r["bib_no"]
                        for r in conn.execute(
                            "SELECT bib_no FROM rule_bibs "
                            "WHERE rule_id=? AND exited_seq IS NULL",
                            (rule["id"],),
                        )
                    }
                    for bib_no in sorted(selected_bibs - current_bibs):
                        # 新入选或落选后再入选：重置入选版本、清除落选标记
                        conn.execute(
                            "INSERT INTO rule_bibs(rule_id, bib_no, entered_seq, exited_seq) "
                            "VALUES (?,?,?,NULL) "
                            "ON CONFLICT(rule_id, bib_no) DO UPDATE SET "
                            "entered_seq=excluded.entered_seq, exited_seq=NULL",
                            (rule["id"], bib_no, version_id),
                        )
                    for bib_no in sorted(current_bibs - selected_bibs):
                        # 落选：只记落选版本，保留行（曾入选事实不清除）
                        conn.execute(
                            "UPDATE rule_bibs SET exited_seq=? WHERE rule_id=? AND bib_no=?",
                            (version_id, rule["id"], bib_no),
                        )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {
            "version_id": version_id,
            "total": len(payload.entries),
            "added": added,
            "changed": changed,
            "unchanged": unchanged,
            "removed": removed,
        }

    @app.get("/catalog/current", summary="当前生效目录")
    def get_current_catalog():
        with lock:
            conn.execute("BEGIN")
            try:
                version = conn.execute(
                    "SELECT id, entry_count, created_at FROM catalog_versions "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                rows = conn.execute(
                    "SELECT bib_no, branch, title, revision FROM entries "
                    "ORDER BY branch, bib_no"
                ).fetchall()
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {
            "version_id": version["id"] if version else None,
            "total": len(rows),
            "entries": [dict(r) for r in rows],
        }

    @app.get("/selection", summary="领单：当前名单及待查状态（同一事务读取生效目录与回报）")
    def get_selection(order_id: Optional[int] = None):
        with lock:
            conn.execute("BEGIN")
            try:
                order = resolve_order(order_id)
                selected = compute_selection(order["seed"], order["quota"])
                items = []
                for r in selected:
                    rep = effective_report(order["id"], r["bib_no"], r["revision"])
                    items.append(
                        {
                            "bib_no": r["bib_no"],
                            "branch": r["branch"],
                            "title": r["title"],
                            "revision": r["revision"],
                            "status": "pending"
                            if rep is None
                            else ("match" if rep["match"] else "mismatch"),
                            "reported_at": rep["created_at"] if rep else None,
                        }
                    )
                result = {
                    "order_id": order["id"],
                    "seed": order["seed"],
                    "quota": order["quota"],
                    "total": len(items),
                    "pending": sum(1 for i in items if i["status"] == "pending"),
                    "items": items,
                }
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return result

    @app.post("/reports", status_code=201, summary="回报题名是否相符（须带领单所见修订号）")
    def submit_report(payload: ReportIn, order_id: Optional[int] = None):
        with lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                order = resolve_order(order_id)
                if not was_issued(order, payload.bib_no):
                    raise HTTPException(status_code=409, detail="该条目未发给质检员，回报退回")
                entry = conn.execute(
                    "SELECT revision FROM entries WHERE bib_no=?", (payload.bib_no,)
                ).fetchone()
                if entry is None:
                    raise HTTPException(status_code=409, detail="该条目已删除，旧回报退回")
                if entry["revision"] != payload.revision:
                    raise HTTPException(
                        status_code=409, detail="条目修订号已变化（题名/分馆改过或曾删除），旧回报退回"
                    )
                cur = conn.execute(
                    "INSERT INTO reports(order_id, bib_no, revision, match, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (order["id"], payload.bib_no, payload.revision, int(payload.match), utcnow()),
                )
                report_id = cur.lastrowid
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {"report_id": report_id, "accepted": True}

    @app.get("/reports", summary="当前抽检单的回报记录（含是否仍有效）")
    def list_reports(order_id: Optional[int] = None):
        with lock:
            conn.execute("BEGIN")
            try:
                order = resolve_order(order_id)
                rows = conn.execute(
                    "SELECT r.id, r.bib_no, r.revision, r.match, r.created_at, "
                    "CASE WHEN e.bib_no IS NOT NULL AND e.revision = r.revision "
                    "THEN 1 ELSE 0 END AS effective "
                    "FROM reports r LEFT JOIN entries e ON e.bib_no = r.bib_no "
                    "WHERE r.order_id=? ORDER BY r.id",
                    (order["id"],),
                ).fetchall()
                result = {
                    "order_id": order["id"],
                    "reports": [
                        {
                            "report_id": r["id"],
                            "bib_no": r["bib_no"],
                            "revision": r["revision"],
                            "match": bool(r["match"]),
                            "effective": bool(r["effective"]),
                            "created_at": r["created_at"],
                        }
                        for r in rows
                    ],
                }
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return result

    return app


# 默认当前目录下的 qc.db；Docker 中通过 QC_DB_PATH=/data/qc.db 显式指定
DB_PATH = os.environ.get("QC_DB_PATH", "qc.db")
if os.path.dirname(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

app = create_app(DB_PATH)
