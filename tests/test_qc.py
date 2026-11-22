"""关键流程自测：开单、换版、领单、回报、失败保留、重启沿用、共享发放与旧库迁移。"""

import os
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import create_app, priority  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_entries(spec):
    """spec: {branch: [(bib_no, title), ...]} -> 扁平条目列表"""
    return [
        {"bib_no": bib, "branch": branch, "title": title}
        for branch, items in spec.items()
        for bib, title in items
    ]


def upload(client, spec):
    resp = client.post("/catalog/versions", json={"entries": make_entries(spec)})
    assert resp.status_code == 201, resp.text
    return resp.json()


def selection(client, order_id=None):
    resp = client.get("/selection", params={} if order_id is None else {"order_id": order_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


def selected_bibs(sel):
    return {item["bib_no"] for item in sel["items"]}


def report(client, bib_no, revision, match=True, order_id=None):
    return client.post(
        "/reports",
        params={} if order_id is None else {"order_id": order_id},
        json={"bib_no": bib_no, "revision": revision, "match": match},
    )


def lower_priority_bib(seed, anchor, prefix="X"):
    """找一个比 anchor 优先级更小（更先被选中）的书目号。"""
    anchor_p = priority(seed, anchor)
    i = 0
    while True:
        cand = f"{prefix}{i:03d}"
        if priority(seed, cand) < anchor_p:
            return cand
        i += 1


# 1. 开单后按固定种子与每馆名额抽选；条目不足则全查；名单确定可复现
def test_quota_and_take_all_when_insufficient(client):
    client.post("/orders", json={"seed": 7, "quota": 2})
    upload(client, {
        "A": [("B001", "书名一"), ("B002", "书名二"), ("B003", "书名三")],
        "B": [("B010", "书名十")],  # 不足名额，全查
    })
    sel = selection(client)
    assert sel["total"] == 3
    assert sel["pending"] == 3
    per_branch = {}
    for item in sel["items"]:
        per_branch.setdefault(item["branch"], []).append(item["bib_no"])
        assert item["status"] == "pending"
        assert item["revision"] == 1
    assert len(per_branch["A"]) == 2  # 名额 2
    assert per_branch["B"] == ["B010"]  # 不足则全查
    # 名单确定：重复查询、原样重传后均不变
    assert selection(client)["items"] == sel["items"]
    upload(client, {
        "A": [("B001", "书名一"), ("B002", "书名二"), ("B003", "书名三")],
        "B": [("B010", "书名十")],
    })
    assert selected_bibs(selection(client)) == selected_bibs(sel)


# 2. 回报须带领单所见修订号；状态随之更新
def test_report_accept_and_status(client):
    client.post("/orders", json={"seed": 7, "quota": 2})
    upload(client, {"A": [("B001", "书名一"), ("B002", "书名二")]})
    sel = selection(client)
    item = sel["items"][0]
    # 修订号不符 -> 退回
    resp = report(client, item["bib_no"], item["revision"] + 1)
    assert resp.status_code == 409
    # 未发条目 -> 退回
    resp = report(client, "NOPE", 1)
    assert resp.status_code == 409
    # 正确回报 -> 受理
    resp = report(client, item["bib_no"], item["revision"])
    assert resp.status_code == 201
    sel = selection(client)
    assert sel["pending"] == 1
    got = {i["bib_no"]: i for i in sel["items"]}[item["bib_no"]]
    assert got["status"] == "match"


# 3. 题名改过 -> 旧回报退回；即使改回原名也同样退回（修订号单调递增）
def test_title_change_invalidates_old_report(client):
    client.post("/orders", json={"seed": 7, "quota": 5})
    upload(client, {"A": [("B001", "原题名")]})
    rev1 = selection(client)["items"][0]["revision"]
    report(client, "B001", rev1)

    # 换版改题名 -> 修订号 +1，旧结论失效，回到待查
    upload(client, {"A": [("B001", "新题名")]})
    item = selection(client)["items"][0]
    assert item["revision"] == rev1 + 1
    assert item["status"] == "pending"
    resp = report(client, "B001", rev1)
    assert resp.status_code == 409

    reports = client.get("/reports").json()["reports"]
    assert reports[0]["effective"] is False

    # 改回原名仍是新修订号，旧回报依旧退回
    upload(client, {"A": [("B001", "原题名")]})
    item = selection(client)["items"][0]
    assert item["revision"] == rev1 + 2
    resp = report(client, "B001", rev1)
    assert resp.status_code == 409


# 4. 条目曾删除 -> 旧回报退回；重新录入后旧回报仍退回，须按新修订号重报
def test_delete_and_reentry_invalidates(client):
    client.post("/orders", json={"seed": 7, "quota": 5})
    upload(client, {"A": [("B001", "题名"), ("B002", "留存")]})
    rev1 = selection(client)["items"][0]["revision"]
    report(client, "B001", rev1)

    # 换版删除 B001 -> 旧回报退回（已删除）
    res = upload(client, {"A": [("B002", "留存")]})
    assert res["removed"] == 1
    resp = report(client, "B001", rev1)
    assert resp.status_code == 409

    # 重新录入同题名同分馆 -> 修订号仍递增，旧回报继续退回
    upload(client, {"A": [("B001", "题名"), ("B002", "留存")]})
    item = {i["bib_no"]: i for i in selection(client)["items"]}["B001"]
    assert item["revision"] == rev1 + 1
    assert item["status"] == "pending"
    resp = report(client, "B001", rev1)
    assert resp.status_code == 409
    # 按领单所见的新修订号重报 -> 受理（该条目曾发过，仍在共享发放记录中）
    resp = report(client, "B001", item["revision"], match=False)
    assert resp.status_code == 201
    assert {i["bib_no"]: i for i in selection(client)["items"]}["B001"]["status"] == "mismatch"


# 5. 已发条目落选后未改动仍可回报；再入选可沿用结论
def test_fallout_still_reportable_and_reuse(client):
    seed = 7
    client.post("/orders", json={"seed": seed, "quota": 1})
    upload(client, {"A": [("B001", "题名一")]})
    assert selected_bibs(selection(client)) == {"B001"}

    # 换版加入优先级更高的 B 条目 -> B001 落选
    rival = lower_priority_bib(seed, "B001")
    upload(client, {"A": [("B001", "题名一"), (rival, "题名二")]})
    sel = selection(client)
    assert selected_bibs(sel) == {rival}
    assert "B001" not in selected_bibs(sel)

    # 落选但未改动 -> 仍可回报
    resp = report(client, "B001", 1)
    assert resp.status_code == 201

    # 再换版移除对手 -> B001 重新入选，沿用此前结论
    upload(client, {"A": [("B001", "题名一")]})
    item = selection(client)["items"][0]
    assert item["bib_no"] == "B001"
    assert item["status"] == "match"


# 6. 换版失败（重复书目号 / 字段非法）保留原目录
def test_failed_upload_keeps_original(client):
    client.post("/orders", json={"seed": 7, "quota": 2})
    upload(client, {"A": [("B001", "书名一")]})
    before = client.get("/catalog/current").json()

    resp = client.post("/catalog/versions", json={"entries": [
        {"bib_no": "B002", "branch": "A", "title": "x"},
        {"bib_no": "B002", "branch": "A", "title": "y"},
    ]})
    assert resp.status_code == 400
    resp = client.post("/catalog/versions", json={"entries": [
        {"bib_no": "B003", "branch": "", "title": "x"},
    ]})
    assert resp.status_code == 422

    after = client.get("/catalog/current").json()
    assert after == before
    assert after["version_id"] == 1


# 7. 重启沿用最近生效的名单和回报
def test_restart_persistence(tmp_path):
    db = str(tmp_path / "restart.db")
    with TestClient(create_app(db)) as c1:
        c1.post("/orders", json={"seed": 7, "quota": 2})
        upload(c1, {"A": [("B001", "书名一"), ("B002", "书名二"), ("B003", "书名三")]})
        item = selection(c1)["items"][0]
        report(c1, item["bib_no"], item["revision"])
        sel_before = selection(c1)

    # 模拟重启：同一数据库文件新建应用实例
    with TestClient(create_app(db)) as c2:
        sel_after = selection(c2)
        assert sel_after == sel_before
        statuses = {i["bib_no"]: i["status"] for i in sel_after["items"]}
        assert statuses[item["bib_no"]] == "match"
        assert sel_after["pending"] == 1


# 8. 换版与回报交错：以同一份已生效目录和回报为准
def test_interleaved_version_and_report(client):
    client.post("/orders", json={"seed": 7, "quota": 1})
    upload(client, {
        "A": [("A001", "甲题名")],
        "B": [("B001", "乙题名")],
    })
    sel = selection(client)
    a_item = {i["bib_no"]: i for i in sel["items"]}["A001"]
    report(client, "A001", a_item["revision"])

    # 换版只动 B 馆条目；A 馆条目修订号不变
    upload(client, {
        "A": [("A001", "甲题名")],
        "B": [("B001", "乙题名改")],
    })
    sel = selection(client)
    items = {i["bib_no"]: i for i in sel["items"]}
    assert items["A001"]["status"] == "match"      # 旧回报对未改动条目仍有效
    assert items["A001"]["revision"] == 1
    assert items["B001"]["status"] == "pending"    # 改动条目回到待查
    assert items["B001"]["revision"] == 2
    # B001 旧修订号回报退回，新修订号受理
    assert report(client, "B001", 1).status_code == 409
    assert report(client, "B001", 2, match=False).status_code == 201
    assert {i["bib_no"]: i for i in selection(client)["items"]}["B001"]["status"] == "mismatch"


# 9. 分馆改动同样使旧回报失效
def test_branch_change_invalidates(client):
    client.post("/orders", json={"seed": 7, "quota": 5})
    upload(client, {"A": [("B001", "题名")]})
    rev1 = selection(client)["items"][0]["revision"]
    report(client, "B001", rev1)
    upload(client, {"B": [("B001", "题名")]})  # 同题名但换分馆
    item = selection(client)["items"][0]
    assert item["branch"] == "B"
    assert item["revision"] == rev1 + 1
    assert item["status"] == "pending"
    assert report(client, "B001", rev1).status_code == 409


# 10. 同规则两张单：名单共享，回报只认本单开出时或此后换版入选过的书目
def test_same_rule_two_orders_report_difference(client):
    seed, quota = 42, 1
    upload(client, {"A": [("B001", "书名一"), ("B002", "书名二"), ("B003", "书名三")]})
    pick = min(["B001", "B002", "B003"], key=lambda b: priority(seed, b))
    o1 = client.post("/orders", json={"seed": seed, "quota": quota}).json()["order_id"]
    assert selected_bibs(selection(client, o1)) == {pick}

    # 换版加入优先级更高的书目 -> 原入选书目落选但仍在目录中
    rival = lower_priority_bib(seed, pick, prefix="N")
    upload(client, {"A": [("B001", "书名一"), ("B002", "书名二"), ("B003", "书名三"),
                          (rival, "新书")]})
    o2 = client.post("/orders", json={"seed": seed, "quota": quota}).json()["order_id"]
    # 两张单领到的当前名单相同（都是现行名单），差异在回报受理范围
    assert selected_bibs(selection(client, o1)) == {rival}
    assert selected_bibs(selection(client, o2)) == {rival}

    # 单 1：pick 开出时入选过 -> 受理；rival 此后换版入选 -> 受理
    assert report(client, pick, 1, order_id=o1).status_code == 201
    assert report(client, rival, 1, match=False, order_id=o1).status_code == 201
    # 单 2：同规则，但开出时 pick 已落选 -> 退回；rival 在现行名单 -> 受理
    assert report(client, pick, 1, order_id=o2).status_code == 409
    assert report(client, rival, 1, order_id=o2).status_code == 201


# 11. 共享存储形态：按曾入选书目数增长，每单只加固定大小记录，不再逐单复制名单
def test_shared_storage_growth(client, tmp_path):
    db = sqlite3.connect(str(tmp_path / "test.db"))
    db.row_factory = sqlite3.Row

    def counts():
        return {
            t: db.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in ("rules", "rule_bibs", "orders", "issued")
        }

    seed, quota = 42, 1
    upload(client, {"A": [("B001", "书名一"), ("B002", "书名二"), ("B003", "书名三")]})
    pick = min(["B001", "B002", "B003"], key=lambda b: priority(seed, b))
    rival = lower_priority_bib(seed, pick, prefix="N")

    client.post("/orders", json={"seed": seed, "quota": quota})
    assert counts() == {"rules": 1, "rule_bibs": 1, "orders": 1, "issued": 0}
    # 同规则再开两单：共享记录不增长，每单只加一行固定大小的 orders 记录
    client.post("/orders", json={"seed": seed, "quota": quota})
    client.post("/orders", json={"seed": seed, "quota": quota})
    assert counts() == {"rules": 1, "rule_bibs": 1, "orders": 3, "issued": 0}
    rows = db.execute("SELECT rule_id, base_seq FROM orders ORDER BY id").fetchall()
    assert {r["rule_id"] for r in rows} == {1}
    assert {r["base_seq"] for r in rows} == {1}  # 均为开单时的目录版本 1

    # 换版：只为入选状态变化的书目更新；曾入选书目数增为 2
    upload(client, {"A": [("B001", "书名一"), ("B002", "书名二"), ("B003", "书名三"),
                          (rival, "新书")]})
    assert counts()["rule_bibs"] == 2
    state = {r["bib_no"]: r for r in db.execute("SELECT * FROM rule_bibs WHERE rule_id=1")}
    assert state[pick]["exited_seq"] == 2        # 落选：只记落选版本
    assert state[rival]["exited_seq"] is None    # 新入选：在名单中

    # 再换版移除对手：pick 重新入选只更新原行（entered_seq 前移），不新增行
    upload(client, {"A": [("B001", "书名一"), ("B002", "书名二"), ("B003", "书名三")]})
    assert counts()["rule_bibs"] == 2
    state = {r["bib_no"]: r for r in db.execute("SELECT * FROM rule_bibs WHERE rule_id=1")}
    assert state[pick]["exited_seq"] is None and state[pick]["entered_seq"] == 3
    assert state[rival]["exited_seq"] == 3
    # 全程未写旧版逐单发放表
    assert counts()["issued"] == 0


# 12. 落选后再入选：开出时未入选的单，在此后换版中入选即可回报
def test_reentry_after_gap_reportable(client):
    seed = 7
    client.post("/orders", json={"seed": seed, "quota": 1})
    upload(client, {"A": [("B001", "题名一")]})
    rival = lower_priority_bib(seed, "B001")
    upload(client, {"A": [("B001", "题名一"), (rival, "题名二")]})  # B001 落选
    o2 = client.post("/orders", json={"seed": seed, "quota": 1}).json()["order_id"]
    # 开出时 B001 已落选 -> 退回
    assert report(client, "B001", 1, order_id=o2).status_code == 409
    # 此后换版 B001 再入选 -> 受理
    upload(client, {"A": [("B001", "题名一")]})
    assert report(client, "B001", 1, order_id=o2).status_code == 201


# 13. 旧库迁移：逐单保存的发放记录仍只对原单有效，新单走共享记录
OLD_SCHEMA = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed INTEGER NOT NULL,
    quota INTEGER NOT NULL CHECK (quota >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE catalog_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE entries (
    bib_no TEXT PRIMARY KEY,
    branch TEXT NOT NULL,
    title TEXT NOT NULL,
    revision INTEGER NOT NULL,
    version_id INTEGER NOT NULL REFERENCES catalog_versions(id)
);
CREATE TABLE bib_revisions (
    bib_no TEXT PRIMARY KEY,
    last_revision INTEGER NOT NULL
);
CREATE TABLE issued (
    order_id INTEGER NOT NULL REFERENCES orders(id),
    bib_no TEXT NOT NULL,
    issued_revision INTEGER NOT NULL,
    PRIMARY KEY (order_id, bib_no)
);
CREATE TABLE reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    bib_no TEXT NOT NULL,
    revision INTEGER NOT NULL,
    match INTEGER NOT NULL CHECK (match IN (0, 1)),
    created_at TEXT NOT NULL
);
"""


def test_legacy_db_migration(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    seed = 42
    conn = sqlite3.connect(db_path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO catalog_versions(entry_count, created_at) VALUES (3, '2026-01-01T00:00:00+00:00')"
    )
    for bib, branch, title in [("B001", "A", "书名一"), ("B002", "A", "书名二"), ("B101", "B", "书名十")]:
        conn.execute(
            "INSERT INTO entries(bib_no, branch, title, revision, version_id) VALUES (?,?,?,1,1)",
            (bib, branch, title),
        )
        conn.execute("INSERT INTO bib_revisions(bib_no, last_revision) VALUES (?,1)", (bib,))
    conn.execute("INSERT INTO orders(seed, quota, created_at) VALUES (42, 1, '2026-01-01T00:00:00+00:00')")
    # 旧库逐单发放记录：含曾入选后落选的条目与当前入选的条目
    pick = min(["B001", "B002"], key=lambda b: priority(seed, b))
    drop = ({"B001", "B002"} - {pick}).pop()
    for bib in (pick, drop, "B101"):
        conn.execute("INSERT INTO issued(order_id, bib_no, issued_revision) VALUES (1, ?, 1)", (bib,))
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path)) as c:
        # 旧记录只对原单有效：落选的 drop 在原单可凭现行修订号回报
        assert report(c, drop, 1, order_id=1).status_code == 201
        # 同规则新单：旧记录不共享，drop 未在新单开出后入选 -> 退回；现行名单 pick -> 受理
        o2 = c.post("/orders", json={"seed": seed, "quota": 1}).json()["order_id"]
        assert report(c, drop, 1, order_id=o2).status_code == 409
        assert report(c, pick, 1, order_id=o2).status_code == 201
        # 迁移后换版继续按共享记录维护：pick 落选后，新单退回、开出时在名单的单仍受理
        rival = lower_priority_bib(seed, pick, prefix="N")
        upload(c, {
            "A": [("B001", "书名一"), ("B002", "书名二"), (rival, "新书")],
            "B": [("B101", "书名十")],
        })
        o3 = c.post("/orders", json={"seed": seed, "quota": 1}).json()["order_id"]
        assert report(c, pick, 1, order_id=o3).status_code == 409
        assert report(c, pick, 1, order_id=o2).status_code == 201


# 14. 重启后按已提交的共享发放受理回报
def test_restart_shared_issuance(tmp_path):
    db = str(tmp_path / "restart_shared.db")
    seed = 7
    with TestClient(create_app(db)) as c1:
        c1.post("/orders", json={"seed": seed, "quota": 1})
        upload(c1, {"A": [("B001", "题名一")]})
        rival = lower_priority_bib(seed, "B001")
        upload(c1, {"A": [("B001", "题名一"), (rival, "题名二")]})  # B001 落选
        assert report(c1, "B001", 1).status_code == 201

    with TestClient(create_app(db)) as c2:
        # 落选书目仍受理，从未入选的退回，现行名单不变
        assert report(c2, "B001", 1, match=False).status_code == 201
        assert report(c2, "NOPE", 1).status_code == 409
        assert selected_bibs(selection(c2)) == {rival}


# 15. 开单/换版失败保留原发放状态
def test_failed_writes_keep_issuance(client):
    seed = 7
    client.post("/orders", json={"seed": seed, "quota": 1})
    upload(client, {"A": [("B001", "题名一")]})
    # 非法换版（重复书目号）-> 整版拒收，发放状态不变
    resp = client.post("/catalog/versions", json={"entries": [
        {"bib_no": "B002", "branch": "A", "title": "x"},
        {"bib_no": "B002", "branch": "A", "title": "y"},
    ]})
    assert resp.status_code == 400
    assert report(client, "B001", 1).status_code == 201
    # 非法开单（quota 0）-> 拒收，当前生效单不变
    assert client.post("/orders", json={"seed": 9, "quota": 0}).status_code == 422
    assert client.get("/orders/active").json()["seed"] == seed
