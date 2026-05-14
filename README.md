# WeChat Ingest System

一个“Server + Worker + 报告/发布脚本”的微信公众号文章采集与日报系统。

这份文档的目标是：**只看这一份 README，就知道项目由哪些部分组成、它们之间怎么协作、和 OpenClaw 是什么关系、要配哪些配置、以及怎么把整个系统跑起来。**

---

## 1. 项目是做什么的

这个项目主要解决三件事：

1. **采集公众号文章**
   - Worker 运行在有微信公众号后台登录态的机器上
   - 用 Playwright 打开公众号后台，搜索目标号，抓取文章列表

2. **服务端接收、去重、抓正文、落库、导出**
   - Server 负责分发任务、接收结果、去重、抓正文、导出 Markdown、提供管理页面

3. **基于已入库文章生成日报，并可继续分发**
   - 根目录脚本会从数据库里选文章，调用 LLM 生成早报/晚报
   - 可发到 Telegram
   - 可进一步生成微信公众号草稿

---

## 2. 整体架构

```text
微信公众号后台（需要登录态）
        |
        v
Worker（本地/Windows/有登录态的浏览器）
  - poll server 拿任务
  - Playwright 抓文章列表
  - report 回 server
        |
        v
Server（FastAPI + SQLite）
  - 管理 targets / tasks / workers / articles / reports
  - URL 去重
  - 抓正文
  - 导出 Markdown
  - 提供 Web 管理台
        |
        v
根目录报告脚本
  - report_generator.py 生成早报/晚报
  - send_report.py 通过 OpenClaw 发 Telegram
  - wechat_article_generator.py 改写成公众号文章
  - wechat_draft_publisher.py 推送公众号草稿箱
```

---

## 3. 各目录分别负责什么

```text
wechat_ingest_system/
├─ server/                    # FastAPI 服务端
│  ├─ app/
│  │  ├─ api/                 # /api/v1/worker/poll, /report
│  │  ├─ web/                 # 管理页面路由
│  │  ├─ services/            # 任务分发、正文抓取、导出等
│  │  └─ templates/           # dashboard / targets / tasks / articles 页面
│  ├─ data/                   # SQLite、导出 Markdown、日志等
│  ├─ requirements.txt
│  ├─ seed_targets.py         # 初始化几个默认目标
│  └─ wechat-ingest-server.service.example
│
├─ worker/                    # 抓取端
│  ├─ app/
│  │  ├─ api/client.py        # 调 server 的 poll/report
│  │  ├─ collector/           # Playwright 抓公众号后台
│  │  └─ runner.py            # 轮询执行主循环
│  ├─ data/                   # 浏览器 profile、debug 日志
│  ├─ login_and_export_state.js
│  ├─ requirements.txt
│  └─ main.py
│
├─ report_generator.py        # 生成早报/晚报
├─ send_report.py             # 发送 Telegram 简报，必要时建公众号草稿
├─ wechat_article_generator.py# 把日报改写成公众号文章
├─ wechat_draft_publisher.py  # 推送到公众号草稿箱
├─ report_state.json          # 早/晚报窗口状态
├─ wechat_official_account.json
├─ docs/architecture.md
└─ README.md
```

---

## 4. Server、Worker、报告脚本三者关系

### 4.1 Server

Server 是系统中枢，负责：

- 保存 `targets / tasks / workers / articles / reports`
- 决定什么时候给某个 target 下发任务
- 接收 worker 回报的文章列表
- 用 `article_url` 去重
- 按规则过滤发布时间窗口
- 抓文章正文并导出 Markdown
- 提供 Web 管理页面

### 4.2 Worker

Worker 是抓取执行器，负责：

- 定时调用 `POST /api/v1/worker/poll`
- 拿到任务后，用 Playwright 打开公众号后台
- 搜索公众号并抓文章列表
- 调用 `POST /api/v1/worker/report` 回传结果

### 4.3 根目录报告脚本

这些脚本不参与“采集闭环”的实时任务分发，它们是**消费数据库结果的上层能力**：

- `report_generator.py` 从 `server/data/app.db` 读取文章，生成早报/晚报
- `send_report.py` 通过 OpenClaw CLI 把简报发到 Telegram，并维护 `report_state.json`
- `wechat_article_generator.py` 把日报改写成公众号版文章
- `wechat_draft_publisher.py` 可把文章写入微信公众号草稿箱

---

## 5. 和 OpenClaw 的关系

**结论先说：**

- **Server + Worker 本身不依赖 OpenClaw 才能运行采集闭环**
- **报告发送链路明显依赖 OpenClaw**
- **当前项目路径与部分配置默认假设自己运行在 OpenClaw workspace 里**

### 5.1 哪些地方依赖 OpenClaw

1. `send_report.py` / `send_report.sh`
   - 直接调用 `openclaw message send`
   - 用 OpenClaw 给 Telegram 发消息

2. `report_generator.py`
   - 会优先读环境变量里的 `MINIMAX_API_KEY`
   - 如果没有，也会尝试从 `~/.openclaw/openclaw.json` 里读取 provider 配置

3. 当前代码里的很多绝对路径写死成了：
   - `/root/.openclaw/workspace/wechat_ingest_system/...`

所以：

- **如果你只跑采集闭环**，可以不依赖 OpenClaw，但需要自己改掉这些硬编码路径
- **如果你还要发 Telegram 报告**，那就需要本机能用 `openclaw` CLI，并配置好消息渠道

### 5.2 推荐理解方式

- **OpenClaw 不是采集引擎本身**
- **OpenClaw 更像这个项目所在的运行环境 / 编排与消息外壳**
- 这个项目真正的业务核心仍然是：
  - `server/` 的 FastAPI + SQLite
  - `worker/` 的 Playwright 采集
  - 根目录的报告生成与发布脚本

---

## 6. 当前重要运行规则

### 6.1 任务分发

Server 当前会：

- 只对 `enabled=1` 的 target 下发任务
- 若某 target 有未回报 pending task，则不会重复下发
- 超过约 10 分钟未回报的 pending task，会被自动判成 stale failed
- 正常任务的时间窗口：
  - `max(该公众号已入库最新 publish_time, 现在 - 24 小时)`
- 强制重跑任务的时间窗口：
  - `现在 - 24 小时`
- 单个公众号单次任务最多处理 **50 条** 文章

### 6.2 去重与正文抓取

Worker 上报列表后，Server 会：

- 先按 `article_url` 去重
- 再按时间窗口过滤
- 对新文章抓正文
- 把正文导出到 `server/data/exports/`

### 6.3 管理后台

Server 提供管理页面，常用入口：

- `/` 仪表盘
- `/targets`
- `/workers`
- `/tasks`
- `/articles`
- `/reports`

后台默认需要管理员密码。

---

## 7. 运行前你需要准备什么

### 7.1 基础环境

建议：

- Linux 跑 Server
- Windows 或有微信后台登录态的桌面机跑 Worker
- Python 3.11+（当前环境实际是 3.12）
- Node.js（仅当你要用 `login_and_export_state.js` 时）
- Playwright Chromium（Worker 必需）

### 7.2 你至少要决定三件事

1. **Server 部署在哪台机器上**
2. **Worker 跑在哪台有公众号后台登录态的机器上**
3. **是否要启用 OpenClaw 报告发送链路**

---

## 8. 配置总表

这个项目的配置目前分散在三类地方：

- Python 文件里的 `config.py`
- 进程环境变量
- 根目录 JSON 文件

这不是最优设计，但这是当前代码的真实情况。

### 8.1 Server 配置

文件：`server/app/config.py`

主要配置：

- `WORKER_TOKEN`
  - Worker 调 `poll/report` 时的 Bearer Token
  - Server 和 Worker 必须一致
  - 现支持环境变量：`WECHAT_INGEST_WORKER_TOKEN`

- `DB_PATH`
  - SQLite 路径，默认在 `server/data/app.db`

- `EXPORT_DIR`
  - 导出的文章 Markdown 目录，默认在 `server/data/exports`

环境变量：

- `WECHAT_ADMIN_PASSWORD`
  - 管理后台密码
  - **强烈建议设置，不要用默认值**

- `WECHAT_ADMIN_SECRET`
  - 用于管理员 cookie token 派生
  - 建议自己设置

### 8.2 Worker 配置

文件：`worker/app/config.py`

主要配置：

- `SERVER_BASE_URL`
  - 例如：`http://your-server:8000/api/v1`
  - 现支持环境变量：`WECHAT_INGEST_SERVER_BASE_URL`

- `WORKER_ID`
  - Worker 唯一标识
  - 现支持环境变量：`WECHAT_INGEST_WORKER_ID`

- `WORKER_TOKEN`
  - 必须与 Server 一致
  - 现支持环境变量：`WECHAT_INGEST_WORKER_TOKEN`

- `PERSISTENT_PROFILE_DIR`
  - 浏览器持久 profile 目录
  - 当前采集逻辑实际主要依赖这个目录保存登录态
  - 现支持环境变量：`WECHAT_INGEST_PERSISTENT_PROFILE_DIR`

- `HEADLESS`
  - 是否无头运行
  - 调试期建议 `False`
  - 现支持环境变量：`WECHAT_INGEST_HEADLESS`

- `POLL_INTERVAL_SECONDS`
  - 空闲轮询间隔
  - 现支持环境变量：`WECHAT_INGEST_POLL_INTERVAL_SECONDS`

- `HOMEPAGE_REFRESH_INTERVAL_SECONDS`
  - 公众号后台首页保活刷新间隔，默认 `1800` 秒
  - Worker 到达该间隔时会先暂停 poll server，刷新 `https://mp.weixin.qq.com/` 成功后再继续获取任务
  - 设为 `0` 可关闭
  - 现支持环境变量：`WECHAT_INGEST_HOMEPAGE_REFRESH_INTERVAL_SECONDS`

- `PARAM_CACHE_TTL_SECONDS`
  - token / fingerprint / lang 缓存 TTL
  - 现支持环境变量：`WECHAT_INGEST_PARAM_CACHE_TTL_SECONDS`

> 注意：`BROWSER_STATE_PATH` 虽然还在 config 里，但当前 `wechat_collector.py` 主要使用的是 **persistent profile**，不是这个 storage state 文件。这说明 `login_and_export_state.js` 更偏历史/辅助脚本，而不是当前主链路必需项。

### 8.3 报告生成配置

根目录环境变量：

- `MINIMAX_API_KEY`
- `MINIMAX_BASE_URL`
- `MINIMAX_MODEL`
- `MINIMAX_CALL_TIMEOUT`
- `DEEPSEEK_MODEL`（部分 fallback 逻辑）

说明：

- `.env.example` 已补充为更完整示例
- 如果没显式设置 `MINIMAX_API_KEY`，`report_generator.py` 会尝试从 `~/.openclaw/openclaw.json` 的 provider 配置读取

### 8.4 公众号草稿箱配置

二选一：

1. 环境变量
   - `WECHAT_OFFICIAL_APPID`
   - `WECHAT_OFFICIAL_APPSECRET`
   - `WECHAT_OFFICIAL_ACCOUNT_NAME`

2. 根目录文件
   - `wechat_official_account.json`

### 8.5 报告发送配置

文件：`send_report.py`

当前这些关键参数已经支持环境变量覆盖：

- `WECHAT_INGEST_REPORT_TARGET_CHAT`
- `WECHAT_INGEST_WEB_BASE_URL`
- `WECHAT_INGEST_AUTO_CREATE_WECHAT_DRAFT`
- `WECHAT_INGEST_GENERATOR_TIMEOUT`
- `WECHAT_INGEST_GENERATOR_RETRY_DELAYS`

如果你换环境部署，优先改环境变量，不必直接改脚本。

---

## 9. 安装步骤

## 9.1 推荐部署路径

当前很多脚本写死了绝对路径，**推荐直接部署到：**

```bash
/root/.openclaw/workspace/wechat_ingest_system
```

如果你放到别的目录，根目录报告脚本和部分服务路径大概率需要一起改。

---

### 9.2 安装 Server

```bash
cd /root/.openclaw/workspace/wechat_ingest_system/server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

如需初始化默认 targets：

```bash
python seed_targets.py
```

---

### 9.3 安装 Worker

```bash
cd /root/.openclaw/workspace/wechat_ingest_system/worker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

如果是 Windows，把命令换成对应的 PowerShell/venv 激活方式即可。

---

### 9.4 安装根目录报告脚本依赖

根目录现在补了一个：

- `requirements-root.txt`

推荐这样安装：

```bash
cd /root/.openclaw/workspace/wechat_ingest_system
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-root.txt
```

如果你要让根目录脚本顺手复用 server 里的依赖，最稳妥的做法是再补装：

```bash
pip install -r server/requirements.txt
```

---

### 9.5 如果要发 Telegram，安装并配置 OpenClaw

你需要确保：

- `openclaw` CLI 可执行
- 已配置 Telegram channel
- `openclaw message send` 可正常发消息

如果只跑采集闭环，这一步不是必需。

---

## 10. 首次配置清单

建议按下面顺序配置。

### 10.1 配 Server

修改或确认：

- `server/app/config.py`
  - 改 `WORKER_TOKEN`

启动时设置环境变量：

```bash
export WECHAT_ADMIN_PASSWORD='改成你自己的强密码'
export WECHAT_ADMIN_SECRET='改成你自己的随机字符串'
```

### 10.2 配 Worker

编辑：`worker/app/config.py`

至少确认：

- `SERVER_BASE_URL`
- `WORKER_ID`
- `WORKER_TOKEN`
- `PERSISTENT_PROFILE_DIR`
- `HEADLESS`

### 10.3 配报告生成

根目录现在有一个更完整的示例文件：

- `.env.example`

二选一：

1. 显式设置环境变量

```bash
export MINIMAX_API_KEY='your_key'
export MINIMAX_BASE_URL='https://api.minimaxi.com/anthropic'
export MINIMAX_MODEL='MiniMax-M2.7'
```

2. 或者让它从 `~/.openclaw/openclaw.json` 里读取 minimax provider 配置

### 10.4 配公众号草稿箱（如果要用）

方式一：创建 `wechat_official_account.json`

方式二：设置环境变量：

```bash
export WECHAT_OFFICIAL_APPID='your_appid'
export WECHAT_OFFICIAL_APPSECRET='your_appsecret'
export WECHAT_OFFICIAL_ACCOUNT_NAME='你的公众号名'
```

---

## 11. 怎么启动整个系统

## 11.1 先启动 Server

```bash
cd /root/.openclaw/workspace/wechat_ingest_system/server
source .venv/bin/activate
export WECHAT_ADMIN_PASSWORD='你的后台密码'
export WECHAT_ADMIN_SECRET='你的后台secret'
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

开发调试可用：

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

如果想常驻运行，可参考：

- `server/wechat-ingest-server.service.example`

---

## 11.2 准备 Worker 登录态

当前主逻辑使用 **persistent browser profile**，所以最简单的做法是：

1. 把 `worker/app/config.py` 里的 `HEADLESS=False`
2. 启动 Worker 一次
3. 在弹出的 Chromium 中登录公众号后台
4. 之后登录态会保存在 `worker/data/chrome_profile/`

也可以尝试历史脚本：

```bash
cd /root/.openclaw/workspace/wechat_ingest_system/worker
npm i playwright
node login_and_export_state.js
```

但请注意，**当前 collector 主逻辑并不主要依赖导出的 `storage_state.json`，而是 persistent profile 目录。**

---

## 11.3 启动 Worker

```bash
cd /root/.openclaw/workspace/wechat_ingest_system/worker
source .venv/bin/activate
python main.py
```

启动后它会不断：

- poll server
- 拿任务
- 抓取
- report 回 server

---

## 11.4 验证采集链路

打开 Server 管理页：

- `http://<server>:8000/`
- `http://<server>:8000/targets`
- `http://<server>:8000/workers`
- `http://<server>:8000/tasks`
- `http://<server>:8000/articles`

推荐检查：

1. `workers` 页面是否出现你的 `WORKER_ID`
2. `tasks` 页面是否有 pending → done/failed 流转
3. `articles` 页面是否开始出现新文章
4. `server/data/exports/` 是否生成 Markdown 导出

---

## 12. 怎么生成日报

### 12.1 先确保数据库里已经有文章

日报脚本直接读：

```text
server/data/app.db
```

没有文章就没有可生成内容。

### 12.2 生成早报/晚报

```bash
cd /root/.openclaw/workspace/wechat_ingest_system
source .venv/bin/activate
python report_generator.py morning
python report_generator.py evening
```

它会：

- 从数据库选取时间窗内文章
- 做去重、聚类、偏好过滤等处理
- 调 LLM 生成 Markdown 日报

### 12.3 发送日报到 Telegram

```bash
python send_report.py morning
python send_report.py evening
```

这一步要求：

- `openclaw` CLI 可用
- Telegram 目标 chat 配置正确
- MiniMax 配置可用

脚本会维护：

- `report_state.json`
- `reports` 表

---

## 13. 怎么生成公众号版文章 / 草稿

### 13.1 基于最新日报生成公众号文章

```bash
cd /root/.openclaw/workspace/wechat_ingest_system
source .venv/bin/activate
python wechat_article_generator.py
```

输出会保存到：

```text
wechat_articles/
```

### 13.2 生成并推送到公众号草稿箱

默认先本地生成预览：

```bash
python wechat_draft_publisher.py
```

真正写入草稿箱：

```bash
python wechat_draft_publisher.py --create-draft
```

前提：

- 已配置公众号 AppID / AppSecret
- 对应接口权限可用

---

## 14. 一套最小可运行流程

如果你只想最快跑通采集主链路：

### A. Server 机

```bash
cd /root/.openclaw/workspace/wechat_ingest_system/server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export WECHAT_ADMIN_PASSWORD='your_password'
export WECHAT_ADMIN_SECRET='your_secret'
python seed_targets.py
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### B. Worker 机

先改好 `worker/app/config.py`：

- `SERVER_BASE_URL`
- `WORKER_ID`
- `WORKER_TOKEN`

然后：

```bash
cd /root/.openclaw/workspace/wechat_ingest_system/worker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python main.py
```

浏览器弹出后登录公众号后台。

### C. 验证

看 Server 页面：

- `/workers`
- `/tasks`
- `/articles`

只要这里开始有数据，主链路就通了。

---

## 15. 关键文件与数据落点

### 15.1 数据库

```text
server/data/app.db
```

核心表：

- `targets`
- `tasks`
- `articles`
- `workers`
- `reports`

### 15.2 导出文章 Markdown

```text
server/data/exports/
```

### 15.3 Server 日志

通常在：

```text
server/data/server.log
```

### 15.4 Worker 日志 / 调试

```text
worker/data/debug/
```

### 15.5 浏览器持久登录态

```text
worker/data/chrome_profile/
```

### 15.6 日报状态

```text
report_state.json
```

---

## 16. 常见问题

### Q1. 为什么 Worker 一直拿不到任务？

检查：

- `worker/app/config.py` 的 `SERVER_BASE_URL` 是否正确
- `WORKER_TOKEN` 是否与 Server 一致
- Server 的 `targets` 是否存在且 `enabled=1`
- 该 target 是否有未完成 pending task
- `last_dispatched_at` 是否还没到调度时间

### Q2. 为什么登录过了，Worker 还是抓不到？

优先检查：

- `worker/data/chrome_profile/` 是否真的保留了登录态
- 公众号后台是否过期，需要重新登录
- `HEADLESS` 是否设为 `False` 方便观察
- `worker/data/debug/worker.log` 是否有 token/fingerprint 刷新失败

### Q3. 为什么有 task，但 articles 没增长？

可能原因：

- 该公众号最近 24 小时内没有新文章
- 结果都被 `article_url` 去重了
- 结果都被 `min_publish_time` 过滤了
- 正文抓取失败，可看 `articles.fetch_status` / `fetch_error`

### Q4. 为什么日报发不出去？

检查：

- `MINIMAX_API_KEY` 或 OpenClaw provider 配置
- `openclaw message send` 是否能正常发 Telegram
- `WECHAT_INGEST_REPORT_TARGET_CHAT` / `WECHAT_INGEST_WEB_BASE_URL` 是否正确

### Q5. 为什么换目录后脚本坏了？

因为当前项目很多地方写死了绝对路径：

```text
/root/.openclaw/workspace/wechat_ingest_system
```

如果换部署目录，需要同步修改这些脚本中的路径。

---

## 17. 当前实现上的几个现实注意点

这部分很重要，避免你按 README 期待“它已经完全工程化”。

1. **配置仍未完全环境变量化**
   - 这一轮已经把 Server/Worker 通信参数、Worker 运行参数、报告目标 chat、Web 基础地址、自动建草稿开关、generator 超时/重试提到了环境变量
   - 但项目路径等配置仍有硬编码

2. **根目录脚本路径强依赖当前 workspace**
   - 更适合当前 OpenClaw 工作目录运行

3. **Worker 登录态管理以 persistent profile 为主**
   - `storage_state.json` 不是当前主链路核心

4. **报告链路和采集链路是两个层次**
   - 不要把“采集正常”与“Telegram/公众号发布正常”混为一谈
   - 前者主要是 Server + Worker
   - 后者还叠加了 LLM、OpenClaw、公众号接口

---

## 18. 推荐启动顺序

每次从零拉起时，推荐顺序：

1. 启动 Server
2. 检查 `/health`
3. 配置并启动 Worker
4. 登录公众号后台
5. 在 `/workers` / `/tasks` / `/articles` 验证采集闭环
6. 再测试 `report_generator.py`
7. 最后测试 `send_report.py` 和 `wechat_draft_publisher.py`

---

## 19. 一句话总结

这个项目本质上是一个：

> **“用本地登录态 Worker 抓微信公众号后台列表，用 Server 做任务编排与落库，再用根目录脚本把采集结果加工成日报和公众号稿件”的系统。**

其中：

- **Server + Worker** 是核心采集闭环
- **OpenClaw** 主要承担消息发送、配置复用和当前工作目录环境角色
- **根目录脚本** 是面向日报与发布的上层能力

如果你准备长期维护这个项目，下一步最值得做的是：

- 把硬编码路径和密钥移到统一配置层
- 给根目录脚本补一个正式 `requirements.txt`
- 补全 systemd / Windows service 级部署说明
- 把 OpenClaw 依赖和非 OpenClaw 依赖拆清楚
