# Server

本目录是 WeChat Ingest System 的服务端。

> **文档真相源请看项目根目录 `README.md`**  
> 路径：`/root/.openclaw/workspace/wechat_ingest_system/README.md`

根 README 已统一说明：

- 整体架构
- Server / Worker / 报告脚本之间的关系
- 与 OpenClaw 的关系
- 安装与配置
- 首次启动步骤
- 报告生成与发布链路
- 常见问题排查

本文件只保留 **server 本地快速说明**，避免和根 README 重复后逐渐过时。

---

## 目录职责

Server 负责：

- 任务分发
- Worker 身份校验
- 结果接收
- `article_url` 去重
- 正文抓取
- Markdown 导出
- Web 管理页面
- 报表记录保存

核心入口：

- `app/main.py`
- `app/api/routes.py`
- `app/web/routes.py`
- `app/services/task_service.py`
- `app/db.py`

---

## 本地安装

```bash
cd /root/.openclaw/workspace/wechat_ingest_system/server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 本地配置

### 1. Worker Token

编辑：`app/config.py`

至少确认：

- `WORKER_TOKEN`

它必须与 Worker 侧配置保持一致。

### 2. 管理后台密码

启动前设置环境变量：

```bash
export WECHAT_ADMIN_PASSWORD='your_strong_password'
export WECHAT_ADMIN_SECRET='your_random_secret'
```

如果不设，代码里仍有默认值，但**不建议在线上使用默认值**。

---

## 初始化默认 targets（可选）

```bash
python seed_targets.py
```

---

## 启动

开发模式：

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

常规运行：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

---

## 页面入口

- `/`
- `/targets`
- `/workers`
- `/tasks`
- `/articles`
- `/reports`

---

## 对 Worker 暴露的接口

- `POST /api/v1/worker/poll`
- `POST /api/v1/worker/report`
- `GET /health`

---

## systemd 示例

可参考：

- `wechat-ingest-server.service.example`

正式部署前请至少改掉：

- `WECHAT_ADMIN_PASSWORD`
- 工作目录
- Python 路径
- 运行用户

---

## 备注

如果你要理解完整运行关系，请回到根 README：

- `../README.md`
