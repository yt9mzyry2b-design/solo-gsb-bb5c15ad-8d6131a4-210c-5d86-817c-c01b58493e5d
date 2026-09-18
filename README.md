# Feature Flag Decision API

供本地应用调用的功能开关判定服务（Python + FastAPI + SQLite）。

每个开关包含：

- **有序属性规则**：按 `priority` 升序判定，返回**首个命中**规则指定的变体；
- **多个加权变体**：无规则命中时，以 `flag_key` 与 `user_id` 做稳定哈希，映射到 `0-99` 桶，按权重区间选择变体；
- 变体**权重总和必须为 100**；
- 规则所需属性**缺失（或值为 `null`）一律视为未命中**，继续判定下一条；
- 同一用户在配置不变时判定结果**稳定**（哈希输入只有 `flag_key:user_id`）；
- 每次创建 / 更新在**同一事务**中保存一份**不可变配置版本**，历史版本可查询、可指定用于判定；
- 判定响应包含**命中规则、分桶值、最终变体的完整解释链**；
- 每次判定落库为快照，可按 `request_id` 回查；支持 `request_id` **幂等重放**。

## 一条命令启动

```bash
docker compose up --build
```

服务在 http://localhost:8000 启动，SQLite 数据保存在 Docker 卷 `flag-data` 中。

- Swagger 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/healthz
- 首次启动自动写入演示开关 `new_checkout`（员工 / 白名单地区走规则，其余 90/10 分桶）。

不使用容器时（需 Python 3.11+）：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

可通过环境变量 `DATABASE_URL` 覆盖数据库位置，默认 `./data/flags.db`。

## 快速体验

```bash
# 1) 无规则命中 -> 稳定哈希分桶（同一 user_id 多次调用结果一致）
curl -sS -X POST http://localhost:8000/api/v1/flags/new_checkout/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-1001","context":{}}'

# 2) priority=1 的 employee 规则命中（属性缺失时该规则视为未命中）
curl -sS -X POST http://localhost:8000/api/v1/flags/new_checkout/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-1001","context":{"employee":true,"region":"us"}}'

# 3) 用返回的 request_id 查询判定快照
curl -sS http://localhost:8000/api/v1/decisions/<request_id>
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/flags` | 创建 / 更新开关（已存在则 `version + 1`，同事务保存不可变版本） |
| `GET` | `/api/v1/flags` | 列出全部开关 |
| `GET` | `/api/v1/flags/{flag_key}` | 查看单个开关 |
| `DELETE` | `/api/v1/flags/{flag_key}` | 删除开关（版本历史一并删除） |
| `GET` | `/api/v1/flags/{flag_key}/versions` | 列出全部配置版本（按版本号升序） |
| `GET` | `/api/v1/flags/{flag_key}/versions/{version}` | 查看指定版本的完整配置 |
| `POST` | `/api/v1/flags/{flag_key}/evaluate` | 判定（推荐，flag_key 在路径中，可指定 `config_version`） |
| `POST` | `/api/v1/flags/{flag_key}/preview-impact` | 发布前候选配置影响预览（只读，不写版本 / 快照） |
| `POST` | `/api/v1/evaluate` | 判定（flag_key 在请求体中） |
| `GET` | `/api/v1/decisions` | 判定快照审计列表（组合过滤 + 不透明游标分页） |
| `GET` | `/api/v1/decisions/{request_id}` | 按 request_id 查询单条判定快照 |
| `GET` | `/healthz` | 健康检查 |

### 创建开关

```bash
curl -sS -X POST http://localhost:8000/api/v1/flags \
  -H 'Content-Type: application/json' \
  -d '{
    "flag_key": "btn_color",
    "description": "按钮颜色实验",
    "variants": [
      {"key": "red",  "weight": 20},
      {"key": "blue", "weight": 80}
    ],
    "rules": [
      {"priority": 1, "attribute": "region",   "operator": "eq", "values": ["cn"], "variant_key": "red"},
      {"priority": 2, "attribute": "age",      "operator": "gte", "values": [65],  "variant_key": "red"},
      {"priority": 3, "attribute": "user_tier","operator": "in",  "values": ["vip","svip"], "variant_key": "blue"}
    ]
  }'
```

校验规则（不满足返回 `422`）：

- 变体权重之和必须恰好为 `100`，权重允许为 `0`（该变体永不被分桶选中）；变体 key 不可重复；
- 规则 `priority` 不可重复（数字越小越先判定）；
- 规则的 `variant_key` 必须是已配置的变体。

### 配置版本（不可变历史）

每次创建 / 更新都会在**同一事务**中把完整配置追加为一条不可变版本记录（`flag_config_versions` 表），历史版本**不随后续更新改变**。

```bash
# 列出全部版本（按版本号升序）
curl -sS http://localhost:8000/api/v1/flags/btn_color/versions

# 查看某个历史版本的完整配置（版本不存在返回 404）
curl -sS http://localhost:8000/api/v1/flags/btn_color/versions/1
```

响应示例：

```json
{
  "flag_key": "btn_color",
  "version": 1,
  "description": "按钮颜色实验",
  "variants": [{"key": "red", "weight": 20, "description": ""}, {"key": "blue", "weight": 80, "description": ""}],
  "rules": [{"priority": 1, "attribute": "region", "operator": "eq", "values": ["cn"], "variant_key": "red"}],
  "created_at": "2026-09-17T08:00:00Z"
}
```

判定时可通过可选字段 `config_version` 钉住某个历史版本：使用该版本的**规则与权重**计算，并把该版本号写入判定快照；缺省使用最新版；版本不存在返回 `404`。

```bash
# 用 v1 的规则与权重判定（即使当前已发布到 v3）
curl -sS -X POST http://localhost:8000/api/v1/flags/btn_color/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-1001","context":{"region":"cn"},"config_version":1}'
```

删除开关会一并删除其版本历史；同名开关重建后版本号从 `1` 重新开始。

### 规则操作符

| operator | 语义 | values 用法 |
| --- | --- | --- |
| `eq` / `ne` | 等于 / 不等于 | 取 `values[0]` |
| `in` / `not_in` | 属于 / 不属于集合 | 整个 `values` |
| `gt` / `gte` / `lt` / `lte` | 数值比较 | 取 `values[0]`，两侧非数字视为未命中 |
| `prefix` / `suffix` / `contains` | 字符串前缀 / 后缀 / 包含 | 取 `values[0]`，两侧非字符串视为未命中 |

属性不存在、值为 `null`、或类型不兼容时，该规则**记为未命中并附原因**，继续判定后续规则。

### 判定响应与解释链

```json
{
  "request_id": "7f7c0807-...",
  "flag_key": "btn_color",
  "user_id": "user-1001",
  "config_version": 1,
  "matched_rule": null,
  "bucket_value": 42,
  "variant_key": "blue",
  "decision_path": "bucket",
  "explanation": [
    {"step": "rules_loaded", "detail": {"rule_count": 3, "evaluation_order": ["..."]}},
    {"step": "rule_evaluated", "detail": {"rule_index": 0, "result": "miss", "reason": "缺少属性 'region'，视为未命中"}},
    {"step": "no_rule_matched", "detail": {"reason": "所有规则均未命中，进入哈希分桶"}},
    {"step": "bucket_computed", "detail": {"hash_input": "btn_color:user-1001", "algorithm": "sha256[:8] % 100", "bucket_value": 42}},
    {"step": "variant_selected", "detail": {"selected_variant": "blue", "selected_range": [20, 100], "variant_ranges": ["..."]}}
  ]
}
```

- `decision_path = "rule"` 时 `matched_rule` 为命中规则、`bucket_value` 为 `null`；
- `decision_path = "bucket"` 时 `matched_rule` 为 `null`，`bucket_value` 为 `0-99`；
- 配置更新会使 `config_version` 自增；**同一版本配置下，同一 `flag_key + user_id` 永远落入同一桶**。

### 幂等

判定请求可携带自定义 `request_id`。同一 `request_id` 的重复请求直接返回**首次判定快照**（即使第二次 context 或 `config_version` 不同），适合客户端超时重试。若同一 `request_id` 搭配了不同的 `flag_key/user_id`，返回 `409`。不携带时服务自动生成 UUID。

### 发布前影响预览（只读）

发布候选配置前，可用 `POST /api/v1/flags/{flag_key}/preview-impact` 评估影响：服务对请求中的每个用户分别用**当前线上版本**和**候选配置**做只读判定，仅返回结果发生变化的用户。

- 请求体的 `variants` / `rules` 与[创建开关](#创建开关)**同结构、同校验**（权重和为 100、`priority` 唯一、规则引用的变体必须存在），不满足返回 `422`；
- `users` 为 1～500 个评估对象，每个包含 `user_id` 与 `context`，`user_id` **不可重复**，为空、重复或超过 500 返回 `422`；
- 开关不存在返回 `404`；
- 预览**不会**创建配置版本、**不会**写入 `DecisionSnapshot`（可通过版本列表与 `/healthz` 的快照计数验证）；
- 候选与当前判定复用同一套稳定哈希分桶与“首条命中规则”语义。

```bash
curl -sS -X POST http://localhost:8000/api/v1/flags/btn_color/preview-impact \
  -H 'Content-Type: application/json' \
  -d '{
    "variants": [
      {"key": "red",  "weight": 0},
      {"key": "blue", "weight": 100}
    ],
    "rules": [
      {"priority": 1, "attribute": "region", "operator": "eq", "values": ["cn"], "variant_key": "red"}
    ],
    "users": [
      {"user_id": "user-1001", "context": {}},
      {"user_id": "user-1002", "context": {"region": "cn"}}
    ]
  }'
```

响应（`changes` 按请求顺序排列，仅包含 `variant_key`、`decision_path`、`matched_rule_index` 任一发生变化的用户）：

```json
{
  "flag_key": "btn_color",
  "current_version": 1,
  "total_users": 2,
  "changed_users": 1,
  "changes": [
    {
      "user_id": "user-1001",
      "current":   {"variant_key": "red",  "decision_path": "bucket", "matched_rule_index": null},
      "candidate": {"variant_key": "blue", "decision_path": "bucket", "matched_rule_index": null}
    }
  ],
  "migration_counts": {"red": {"blue": 1}}
}
```

- `current_version` 为当前线上版本，预览不会使版本号自增；
- `migration_counts` 按“当前变体 → 候选变体”汇总迁移人数（仅统计变体发生变化的用户；变体不变、仅判定路径或命中规则变化的用户计入 `changed_users`，但不进入迁移计数）。

### 判定快照审计列表（游标分页）

`GET /api/v1/decisions` 与按 `request_id` 的单条查询并存，用于审计 / 回溯历史判定，支持以下查询参数组合过滤：

| 参数 | 说明 |
| --- | --- |
| `flag_key` | 仅返回指定开关的快照 |
| `user_id` | 仅返回指定用户的快照 |
| `variant_key` | 仅返回命中指定变体的快照 |
| `decided_from` | 判定时间下界（含），ISO 8601，如 `2026-09-17T00:00:00Z` |
| `decided_to` | 判定时间上界（含），ISO 8601，支持 `+08:00` 偏移 |
| `limit` | 每页条数，`1～100`，默认 `20` |
| `cursor` | 上一页返回的不透明 `next_cursor` |

结果按 `decided_at`、`request_id` 双列倒序返回（同一时间戳由 `request_id` 裁决，保证不重不漏）；响应项结构与单条查询的 `SnapshotResponse` 完全一致，无更多数据时 `next_cursor` 为 `null`。

```bash
# 首页：查 btn_color 在 9 月对 alice 的判定，每页 50 条
curl -sS 'http://localhost:8000/api/v1/decisions?flag_key=btn_color&user_id=alice&decided_from=2026-09-01T00:00:00Z&decided_to=2026-10-01T00:00:00Z&limit=50'

# 翻页：原样回传 next_cursor（过滤条件必须与首页完全一致，limit 可调整）
curl -sS 'http://localhost:8000/api/v1/decisions?flag_key=btn_color&user_id=alice&decided_from=2026-09-01T00:00:00Z&decided_to=2026-10-01T00:00:00Z&limit=50&cursor=<next_cursor>'
```

响应示例：

```json
{
  "items": [
    {
      "request_id": "7f7c0807-...",
      "flag_key": "btn_color",
      "user_id": "alice",
      "request_context": {"region": "cn"},
      "config_version": 1,
      "bucket_value": null,
      "matched_rule_index": 0,
      "variant_key": "red",
      "explanation": [ {"step": "rules_loaded", "detail": {}} ],
      "decided_at": "2026-09-17T08:00:00.123456"
    }
  ],
  "next_cursor": "eyJ2IjoxLi4ufQ.<hmac签名>"
}
```

游标规则：

- 游标**不透明且带 HMAC 签名**，只记录上一页边界（`decided_at` + `request_id`）、过滤条件与上界高水位，客户端不应解析或构造；
- 翻页时过滤条件（`flag_key` / `user_id` / `variant_key` / `decided_from` / `decided_to`）必须与首页一致；**条件不一致、游标损坏或被篡改、时间区间倒置（from 晚于 to）一律返回 `422`**；
- 首页请求发起时冻结高水位（最大快照 id），**翻页期间新写入的快照不会进入后续页**，页与页之间是一个稳定快照视图；重新从首页开始则能看到最新数据；
- 服务重启后旧游标失效（按损坏处理，返回 `422`），重新取首页即可。

## 分桶算法

```
bucket = int.from_bytes(sha256(f"{flag_key}:{user_id}")[:8], "big") % 100
```

随后按变体配置顺序累加权重切分 `[0, 100)`：上例中桶 `0-19` → `red`（20%），`20-99` → `blue`（80%）。变体顺序或权重变化即视为配置发布（`version` 自增），此前的稳定性不再保证——这是刻意的设计：发布即重新分桶。

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由、错误处理、幂等、种子数据
│   ├── engine.py      # 规则判定 + 稳定哈希分桶 + 解释链（纯函数，易测）
│   ├── schemas.py     # Pydantic 请求/响应模型与配置校验
│   ├── cursor.py      # 审计列表的不透明签名游标（边界 + 过滤绑定 + 高水位）
│   ├── crud.py        # 配置、版本与快照持久化（含分页查询与索引补齐）
│   ├── models.py      # SQLAlchemy ORM（flag_configs / flag_config_versions / decision_snapshots）
│   └── database.py    # 引擎与会话
├── tests/             # pytest：引擎单测 + API 集成测试（64 项）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 运行测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
```

## 说明与边界

- 服务定位为**本地 / 单机**使用：SQLite + 单实例，未做多副本一致性；
- 判定快照只增不删（删除开关不影响历史快照回查）；配置版本随开关一并删除；
- 快照保存判定时的 `config_version`；幂等重放重建 `matched_rule` 详情时优先取该版本的历史配置，版本记录缺失（如开关被删除重建）时回退到当前版本配置；
- 本功能上线前已存在的开关，启动时会自动为当前版本补齐一条版本记录，此后每次更新正常追加。
