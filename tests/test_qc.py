"""关键流程自测：开单、换版、领单、回报、失败保留、重启沿用。"""

import os
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


def selection(client):
    resp = client.get("/selection")
    assert resp.status_code == 200, resp.text
    return resp.json()


def selected_bibs(sel):
    return {item["bib_no"] for item in sel["items"]}


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
    resp = client.post("/reports", json={
        "bib_no": item["bib_no"], "revision": item["revision"] + 1, "match": True})
    assert resp.status_code == 409
    # 未发条目 -> 退回
    resp = client.post("/reports", json={"bib_no": "NOPE", "revision": 1, "match": True})
    assert resp.status_code == 409
    # 正确回报 -> 受理
    resp = client.post("/reports", json={
        "bib_no": item["bib_no"], "revision": item["revision"], "match": True})
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
    client.post("/reports", json={"bib_no": "B001", "revision": rev1, "match": True})

    # 换版改题名 -> 修订号 +1，旧结论失效，回到待查
    upload(client, {"A": [("B001", "新题名")]})
    item = selection(client)["items"][0]
    assert item["revision"] == rev1 + 1
    assert item["status"] == "pending"
    resp = client.post("/reports", json={"bib_no": "B001", "revision": rev1, "match": True})
    assert resp.status_code == 409

    reports = client.get("/reports").json()["reports"]
    assert reports[0]["effective"] is False

    # 改回原名仍是新修订号，旧回报依旧退回
    upload(client, {"A": [("B001", "原题名")]})
    item = selection(client)["items"][0]
    assert item["revision"] == rev1 + 2
    resp = client.post("/reports", json={"bib_no": "B001", "revision": rev1, "match": True})
    assert resp.status_code == 409


# 4. 条目曾删除 -> 旧回报退回；重新录入后旧回报仍退回，须按新修订号重报
def test_delete_and_reentry_invalidates(client):
    client.post("/orders", json={"seed": 7, "quota": 5})
    upload(client, {"A": [("B001", "题名"), ("B002", "留存")]})
    rev1 = selection(client)["items"][0]["revision"]
    client.post("/reports", json={"bib_no": "B001", "revision": rev1, "match": True})

    # 换版删除 B001 -> 旧回报退回（已删除）
    res = upload(client, {"A": [("B002", "留存")]})
    assert res["removed"] == 1
    resp = client.post("/reports", json={"bib_no": "B001", "revision": rev1, "match": True})
    assert resp.status_code == 409

    # 重新录入同题名同分馆 -> 修订号仍递增，旧回报继续退回
    upload(client, {"A": [("B001", "题名"), ("B002", "留存")]})
    item = {i["bib_no"]: i for i in selection(client)["items"]}["B001"]
    assert item["revision"] == rev1 + 1
    assert item["status"] == "pending"
    resp = client.post("/reports", json={"bib_no": "B001", "revision": rev1, "match": True})
    assert resp.status_code == 409
    # 按领单所见的新修订号重报 -> 受理（该条目曾发过，仍在已发集合）
    resp = client.post("/reports", json={"bib_no": "B001", "revision": item["revision"], "match": False})
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
    resp = client.post("/reports", json={"bib_no": "B001", "revision": 1, "match": True})
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
        c1.post("/reports", json={
            "bib_no": item["bib_no"], "revision": item["revision"], "match": True})
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
    client.post("/reports", json={
        "bib_no": "A001", "revision": a_item["revision"], "match": True})

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
    assert client.post("/reports", json={
        "bib_no": "B001", "revision": 1, "match": True}).status_code == 409
    assert client.post("/reports", json={
        "bib_no": "B001", "revision": 2, "match": False}).status_code == 201
    assert {i["bib_no"]: i for i in selection(client)["items"]}["B001"]["status"] == "mismatch"


# 9. 分馆改动同样使旧回报失效
def test_branch_change_invalidates(client):
    client.post("/orders", json={"seed": 7, "quota": 5})
    upload(client, {"A": [("B001", "题名")]})
    rev1 = selection(client)["items"][0]["revision"]
    client.post("/reports", json={"bib_no": "B001", "revision": rev1, "match": True})
    upload(client, {"B": [("B001", "题名")]})  # 同题名但换分馆
    item = selection(client)["items"][0]
    assert item["branch"] == "B"
    assert item["revision"] == rev1 + 1
    assert item["status"] == "pending"
    assert client.post("/reports", json={
        "bib_no": "B001", "revision": rev1, "match": True}).status_code == 409
