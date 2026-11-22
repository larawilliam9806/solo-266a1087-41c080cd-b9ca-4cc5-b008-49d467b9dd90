# 噪声小时等效声级监测 API

监测员按测点登记仪器读数与校准单，服务按 **UTC 小时**计算等效声级 Leq，并给出主表 / 备表各计入的秒数。
技术栈：FastAPI + SQLite（WAL），支持 `docker compose` 一键启动。

## 1. 业务规则

1. **读数登记**：按编号登记，填写测点、主备角色（`primary` / `backup`）、左闭右开的 UTC 整秒区间 `[start, end)` 和段内恒定分贝值。
2. **区间互斥**：同一仪器（测点 + 角色）的读数区间互不重叠；校准单的有效区间同样互不重叠（相邻允许，因为区间左闭右开）。
3. **校准叠加**：取读数与**有效校准单**重叠的时段，把段内恒定分贝值加上校准偏移；校准单同仪器互不重叠，任一秒最多命中一个偏移。
4. **主备合并**：逐秒选择数据源——主表优先，主表未覆盖的秒由有效备表补；未被任何仪器覆盖的秒不参与平均。
5. **等效声级**：按计入时长对声能平均后换回分贝：
   `Leq = 10·lg( Σ(10^(0.1·Li)·ti) / Σti )`，无任何覆盖时报无数据（单小时查询返回 404）。
6. **读数补交 / 重传**：读数可补交任意历史时刻；同号只接受原样重传（幂等 200），同号内容不同返回 409。
7. **校准单变更**：可修改起止时刻与偏移（测点、角色不可变），可撤回（软删除，撤回幂等）；已撤回编号不可复活。
8. **增量重算**：每次写入只重算受影响的小时；原始数据与小时结果在**同一事务**内提交，失败则一起回滚。
9. **并发写入**：写事务以 `BEGIN IMMEDIATE` 串行化，配合 `busy_timeout` 等待锁；成功的变更全部计入，失败时原数据与小时结果保持不变。
10. **换档**：报送平台更换后，可按「测点 + 主/备角色」提交一个最长 24 小时的 UTC 整秒窗口，把窗口内的历史读数整批换成新编号与新分段。
    - 新档每段填写编号、`[start, end)` 起止时刻和原始分贝；各段必须落在窗口内且互不重叠。
    - 已有读数只要跨越窗口边界（部分在窗口外）即拒绝整批切换。
    - 新旧档覆盖的秒必须完全相同、每秒原始分贝必须相同；新编号在批内互不重复，且从未用于任何读数（包括换档后保留的原读数）。
    - 窗口内的普通补报在切换前已入库的，同样纳入核对并被整批替换。
    - 切换后明细查询在该窗口只列新档；原读数仍保留用于原号重传：旧设备按原编号原样重传仍返回原登记内容，同号改报返回 409。
    - 换档时小时结果沿用原值，不重算；后续新增、修改或撤回校准单时按新档重新核算。
    - 所有校验与写入在同一事务内完成；任何失败都会保留原明细与重传记录。

## 2. 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与异常映射
│   ├── services.py    # 领域逻辑：登记、重叠校验、校准叠加、主备合并、换档、小时重算
│   ├── database.py    # SQLite 连接、建表、写事务
│   ├── schemas.py     # Pydantic 请求 / 响应模型
│   ├── config.py      # 环境变量配置
│   └── time_utils.py  # UTC 整秒解析与受影响小时计算
├── tests/             # pytest 用例（含并发、换档场景）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 3. 启动

### docker compose（推荐）

```bash
docker compose up -d --build
# 服务监听 http://localhost:8000，SQLite 文件存放在 noise-data 卷的 /data/noise.db
curl http://localhost:8000/health
```

交互式文档：`http://localhost:8000/docs`（Swagger UI）。

### 本地运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
NOISE_DB_PATH=./data/noise.db uvicorn app.main:app --reload
```

### 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NOISE_DB_PATH` | `/data/noise.db` | SQLite 数据库文件路径 |
| `NOISE_BUSY_TIMEOUT_MS` | `10000` | 写锁等待上限（毫秒） |
| `NOISE_MAX_HOUR_RANGE` | `744` | 区间查询最多返回的小时数 |

## 4. API

所有时间均为带 `Z` 后缀的 UTC ISO-8601 整秒（也接受纪元秒整数），区间一律左闭右开。

### 4.1 读数

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `PUT` | `/readings/{number}` | 登记读数；同号原样重传幂等，内容不同 409，同仪器区间重叠 409 |
| `GET` | `/readings?point=` | 列表，可按测点过滤 |

```bash
curl -X PUT http://localhost:8000/readings/R-0001 \
  -H 'Content-Type: application/json' \
  -d '{"point":"P-101","role":"primary",
       "start":"2026-11-21T08:00:00Z","end":"2026-11-21T08:30:00Z","db":60.0}'
```

### 4.2 换档

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/readings/switch` | 按测点 + 主/备角色整批换档（窗口最长 24 小时） |

请求体：

```json
{
  "point": "P-101",
  "role": "primary",
  "start": "2026-11-21T08:00:00Z",
  "end": "2026-11-21T09:00:00Z",
  "segments": [
    {"number": "N-0001", "start": "2026-11-21T08:00:00Z",
     "end": "2026-11-21T08:30:00Z", "db": 60.0},
    {"number": "N-0002", "start": "2026-11-21T08:30:00Z",
     "end": "2026-11-21T09:00:00Z", "db": 60.0}
  ]
}
```

窗口也可写成 `"window": {"start": ..., "end": ...}`，新档列表也可写作 `readings`；
测点也可放在路径上（`POST /points/{point}/readings/switch`，此时请求体可省略 `point`）。
响应给出 `superseded`（被替换的原读数条数）、`inserted`（新档条数）以及新档明细：

```json
{
  "point": "P-101",
  "role": "primary",
  "start": "2026-11-21T08:00:00Z",
  "end": "2026-11-21T09:00:00Z",
  "superseded": 1,
  "inserted": 2,
  "readings": [
    {"number": "N-0001", "point": "P-101", "role": "primary",
     "start": "2026-11-21T08:00:00Z", "end": "2026-11-21T08:30:00Z", "db": 60.0}
  ]
}
```

换档只替换窗口内该「测点 + 角色」的读数；其他测点、其他角色不受影响。

### 4.3 校准单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `PUT` | `/calibrations/{number}` | 登记或修改（仅起止时刻、偏移可改；测点/角色变更 409） |
| `POST` | `/calibrations/{number}/withdraw` | 撤回（幂等） |
| `GET` | `/calibrations?point=&include_withdrawn=` | 列表 |

```bash
curl -X PUT http://localhost:8000/calibrations/C-0001 \
  -H 'Content-Type: application/json' \
  -d '{"point":"P-101","role":"primary",
       "start":"2026-11-21T08:00:00Z","end":"2026-11-21T08:15:00Z","offset":3.0}'

curl -X POST http://localhost:8000/calibrations/C-0001/withdraw
```

### 4.4 小时结果

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/points/{point}/hours/{hour}` | 单小时；`hour` 必须 UTC 整点，无覆盖返回 404 |
| `GET` | `/points/{point}/hours?start=&end=` | 整点区间查询（左闭右开），只返回有覆盖的小时 |

响应字段：

```json
{
  "point": "P-101",
  "hour": "2026-11-21T08:00:00Z",
  "leq_db": 57.40362689494244,
  "covered_seconds": 3600,
  "primary_seconds": 1800,
  "backup_seconds": 1800
}
```

### 4.5 错误码

| 状态码 | 含义 |
| --- | --- |
| 400 | 区间 `start >= end`、hour 非整点等语义错误 |
| 404 | 编号不存在 / 该小时无覆盖（无数据） |
| 409 | 同号内容不同、同仪器区间重叠、校准单已撤回或试图改测点/角色；换档编号重复/已用、新档重叠、已有读数跨界或新旧档不一致 |
| 422 | 请求体字段缺失或时间格式不合法 |
| 503 | 写锁等待超时，事务已回滚，可安全重试 |

## 5. 关键设计

- **存储**：读数、校准单、小时结果三张表（另有换档批次审计表）；时间统一存 UTC 纪元秒整数，区间重叠用半开判定 `existing.start < new.end AND existing.end > new.start`。
- **换档存储**：原读数不物理删除，而是标记 `superseded = 1`，因此既能从明细/计算中隐去，又能继续承接原号重传并永久占用原编号；新档以普通读数行插入，明细与后续校准重算天然只看到新档。
- **增量重算**：写入时按区间求出所有触及的 UTC 小时（校准修改取新旧窗口并集），逐小时删除旧结果后基于当前全量原始数据重算，无覆盖则不写结果行。
- **逐秒合并**：小时内 3600 个秒槽，先到的主表数据占位，备表只填空槽；校准在切片阶段完成偏移叠加。
- **事务与并发**：WAL 模式 + `BEGIN IMMEDIATE` + `busy_timeout`，每个请求独立连接；原始数据变更与小时重算同事务提交或回滚。

## 6. 测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

覆盖：Leq 声能平均、主备优先与补缺口、校准叠加/修改/撤回、区间互斥、原样重传、补交、无数据 404、
测点隔离、失败回滚，多线程并发写（互不重叠全部成功、互斥区间恰有一方成功且旧结果不被破坏），
以及换档（重新分段、窗口/边界校验、补报纳入核对、原号重传、小时结果沿用与后续校准重算、失败回滚）。
