# 谱图匹配放行台

面向蛋白鉴定的谱图匹配放行台：上传带递增修订号的**完整待交付谱图清单**与**候选清单**，
设置 FDR 阈值，核对问题，预览并下载带 q 值的**现行靶标 CSV**。

- Web 框架：FastAPI
- 存储：SQLite（WAL，单文件，挂在 `/data`）
- 前端：Jinja2 模板 + 静态 CSS，无前端构建步骤

## 快速启动（Docker Compose）

```bash
docker compose up --build -d
```

打开 <http://localhost:8000>。数据写入命名卷 `release-data`（容器内 `/data/release.db`）。

停止并保留数据：

```bash
docker compose down
```

彻底清空（含已上传来源、任务与放行记录）：

```bash
docker compose down -v
```

## 本机运行（不使用 Docker）

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库路径由环境变量 `RELEASE_DB` 指定，默认 `./data/release.db`。

## 输入文件格式

两个 CSV 都必须带表头，UTF-8（也接受带 BOM 或 GB18030），逗号分隔。

**待交付谱图清单** `manifest`：

| 列 | 别名（大小写不敏感） |
| --- | --- |
| 样本号 | `sample` / `sample_id` / `样本号` |
| 谱图号 | `spectrum` / `spectrum_id` / `spectrum_no` / `谱图号` |

**候选清单** `candidates`：

| 列 | 别名 |
| --- | --- |
| 样本号 | 同上 |
| 谱图号 | 同上 |
| 肽段 | `peptide` / `sequence` / `肽段` |
| 靶标/诱饵 | `label` / `target_decoy` / `靶标/诱饵` / `类型`，取值 `target`/`decoy` 或 `靶标`/`诱饵` |
| 分数 | `score` / `confidence` / `分数` |

示例见 `samples/`。

## 规则与语义

### 上传与修订号

- 修订号必须是正整数且**严格递增**；小于等于当前修订号的请求被拒绝（HTTP 400）。
- 同一修订号、**字节内容相同**的重送：不写库、不撤销放行、不触发重算，页面提示“未变化”
  （若该修订号已是历史版本，则提示“历史版本”，同样不改变现行状态）。
- 同一修订号、**内容不同**：拒绝，不覆盖历史（HTTP 400）。
- 只有通过整份解析校验的文件才会落库；解析失败时数据库状态完全不变。

### 放行前必须全部通过的问题检查

重算时按下列顺序检查，任一类问题存在即**列出问题并暂停放行**（任务状态 `blocked`，
不产生放行表，也不生成下载文件）：

1. `清单重复`：清单中同一（样本号，谱图号）出现多次。
2. `候选不在清单`：候选里的谱图没有出现在清单中。
3. `清单谱图缺候选`：清单中的谱图没有任何候选。
4. `单谱最高分并列`：某谱图最高分候选多于一个，无法确定唯一最高分候选。

每类问题给出数量与前 100 条明细，页面同时给出处理提示。

### q 值计算

- 每个谱图取其**最高分候选**；候选不足或并列时已在上一步暂停。
- 全部最高分候选按分数**降序**排列；**相同分数的候选整组计数**。
- 组内累计后计算 `FDR =（累计诱饵数 + 1）/ max（累计靶标数, 1）`。
- 某分数组的 `q = min(该组 FDR, 所有更低分数组的 FDR)`（从低分向上取运行最小值）。
- 现行靶标 CSV 只含 `q ≤ 阈值` 的**靶标**行，按全实验分数排名排序。
- 使用 `decimal.Decimal` 计算；同分判定为十进制精确相等。

### 换版与放行状态

- 任一来源换版（或阈值变更）时，在**同一事务**内立即撤销现行放行表并排队整份重算；
  撤销后到新结果发布前，`/release.csv` 返回 404，避免交付过期数据。
- 计算在写事务之外完成，发布时再次校验「任务的两份来源 + 阈值」是否仍与当前一致；
  不一致则把该任务标记为 `superseded` 并丢弃整份结果（不会发布半份或混合版本）。
- 因此交错上传只会发布对应**两份现行来源**的整份结果。

### 任务、失败与重启

- 任务状态：`pending` → `running` → `done` / `blocked` / `failed` / `superseded`。
- 计算异常会记录到任务 `error`，状态 `failed`，可点击「重试」重新排队。
- 进程重启时，处于 `running` 的任务会回到 `pending`（`recover_interrupted_jobs`），
  因此未完成的重算不会丢失，可继续重试。
- 旧来源对（已非现行）的失败任务不允许重试，页面会提示按现行来源重算。

### 并发

- 上传、撤销、发布均使用 `BEGIN IMMEDIATE` 写事务；读计算使用一致性快照事务。
- 内置单后台线程 worker，Docker Compose 固定单副本 / 单 uvicorn worker。

## 页面

- `/` 工作台：当前来源与阈值、放行状态、待处理问题、最近任务、任务历史。
- `/release`：现行放行表分页预览。
- `/release.csv`：下载现行靶标 CSV（UTF-8 BOM，Excel 可直接打开）。
- `/api/status`：状态 JSON，便于监控。
- `/docs`：FastAPI 自动生成的接口文档。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

覆盖（36 个用例）：

- 四类放行问题各自单独触发，以及同时上报；
- 同分整组计数、`+1` 公式、q 取后续分数组最小值、q 单调性、仅取每谱最高分；
- 阈值边界（q 恰好等于阈值时放行，低于时不放行，Decimal 精确比较）；
- 同版同内容无变化（现行与历史版本）、同版异内容拒绝、修订号不递增拒绝；
- 换版立即撤销放行，随后自动重算并重新发布；
- 重算与上传交错：先认领任务的旧版本计算完成时整份丢弃，只发布现行两份来源；
- 失败任务重试、旧来源失败任务禁止重试、进程重启后 running 任务回到 pending；
- 后台 worker 线程自动完成计算、CSV 下载内容与表头。

## 目录

```
app/
  compute.py     纯校验与 q 值计算
  csvio.py       CSV 严格解析与表头别名
  db.py          SQLite schema 与事务辅助
  service.py     上传、任务队列、发布、后台 worker
  main.py        FastAPI 路由与页面
  templates/     Jinja2 页面
  static/        样式
samples/         示例清单与候选
tests/           pytest 用例
```
