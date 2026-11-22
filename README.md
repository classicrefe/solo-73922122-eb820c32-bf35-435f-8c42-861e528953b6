# 馆藏目录抽检 API

馆藏目录换版后，质检员接着核对各分馆题名。本服务用 FastAPI + SQLite 实现抽检流程：
开单（固定随机种子 + 每馆名额）→ 整版换版 → 领单（当前名单及待查状态）→ 回报（带条目修订号）。

## 启动

```bash
docker compose up --build
```

服务监听 `8000` 端口，交互式文档在 <http://localhost:8000/docs>（Swagger UI，可直接试走换版与回报）。
SQLite 数据落在命名卷 `qc-data`（容器内 `/data/qc.db`），重启后沿用最近生效的名单和回报。

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
QC_DB_PATH=./qc.db uvicorn app.main:app --port 8000
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/orders` | 开单：`{seed, quota}`，固定随机种子与每馆名额；新开单为当前生效单 |
| GET | `/orders/active` | 查看当前生效抽检单 |
| POST | `/catalog/versions` | 换版：整版目录 `{entries: [{bib_no, branch, title}, ...]}`，原子生效 |
| GET | `/catalog/current` | 当前生效目录（含每条修订号） |
| GET | `/selection` | 领单：当前名单及待查状态（`pending`/`match`/`mismatch`），含条目修订号 |
| POST | `/reports` | 回报：`{bib_no, revision, match}`，`revision` 为领单所见修订号 |
| GET | `/reports` | 当前抽检单的回报记录及是否仍有效 |

`/selection`、`/reports` 支持 `?order_id=` 指定历史抽检单，缺省取当前生效单。

## 规则实现要点

- **固定随机优先级**：每条书目的优先级 = `SHA256(seed:bib_no)` 前 8 字节，只由开单种子和书目号决定，
  换版、重启后不变；换版后各馆按此优先级重选，条目不足名额则全查。
- **修订号单调递增**：`bib_revisions` 表记住每个书目号的历史最大修订号（含已删除条目的墓碑）。
  题名或分馆改动、删除后重新录入，修订号都只会变大、绝不复用——因此旧回报即使遇到"改回原名"
  或"原样重录"也永远对不上，一律退回。
- **回报校验**：条目须已发给质检员（历史入选并集），且回报修订号等于当前生效目录中的修订号；
  条目已删除则退回。已发条目落选但未改动仍可回报；再入选时旧结论（修订号未变）自动沿用。
- **一致性**：换版整体在一个事务内生效，校验失败（如书目号重复）或中途出错即回滚、保留原目录；
  领单查询在同一事务里读取生效目录与回报，换版与回报交错也不会读到拼接状态。
- **持久化**：名单由"当前生效目录 + 当前抽检单"即时算出，两者连同回报全部落库，
  重启后自然沿用最近生效状态。

## 自测

```bash
pip install -r requirements.txt pytest httpx
pytest tests/ -v
```

覆盖：名额与全查、名单可复现、回报受理/退回、题名或分馆改动使旧回报失效、删除重录失效、
落选可回报与再入选沿用、换版失败保留原目录、重启沿用、换版与回报交错。
