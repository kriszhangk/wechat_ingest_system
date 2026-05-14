# Worker

本目录是 WeChat Ingest System 的抓取端。

> **文档真相源请看项目根目录 `README.md`**  
> 路径：`/root/.openclaw/workspace/wechat_ingest_system/README.md`

根 README 已统一说明：

- 整体架构
- Worker 与 Server 的配合关系
- 与 OpenClaw 的关系
- 安装与配置
- 首次启动步骤
- 运行与排障

本文件只保留 **worker 本地快速说明**，避免与根 README 长期分叉。

---

## Worker 负责什么

Worker 运行在有微信公众号后台登录态的机器上，负责：

- 定时向 Server 轮询任务
- 使用 Playwright 打开公众号后台
- 搜索目标公众号并抓文章列表
- 将结果回报给 Server

核心入口：

- `main.py`
- `app/runner.py`
- `app/api/client.py`
- `app/collector/wechat_collector.py`

---

## 本地安装

```bash
cd /root/.openclaw/workspace/wechat_ingest_system/worker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

如果是 Windows，请改用对应的 venv 激活方式。

---

## 本地配置

编辑：`app/config.py`

至少确认：

- `SERVER_BASE_URL`
- `WORKER_ID`
- `WORKER_TOKEN`
- `PERSISTENT_PROFILE_DIR`
- `HEADLESS`
- `POLL_INTERVAL_SECONDS`
- `HOMEPAGE_REFRESH_INTERVAL_SECONDS`：默认 `1800`，Worker 每半小时先暂停 poll，刷新一次 `https://mp.weixin.qq.com/`，成功后再继续获取任务；设为 `0` 可关闭

其中：

- `WORKER_TOKEN` 必须和 Server 一致
- `SERVER_BASE_URL` 应指向 `http://<server>:8000/api/v1`
- 调试期建议 `HEADLESS=False`

---

## 登录态说明

当前采集主链路实际主要依赖：

- `PERSISTENT_PROFILE_DIR`

也就是浏览器持久 profile 目录，而不是只依赖 `storage_state.json`。

最实用的首次登录方式通常是：

1. 保持 `HEADLESS=False`
2. 直接启动 Worker
3. 在弹出的 Chromium 中登录公众号后台
4. 登录态会保存在 `worker/data/chrome_profile/`

历史辅助脚本：

```bash
npm i playwright
node login_and_export_state.js
```

但它不是当前主链路最关键的登录态来源。

---

## 启动

```bash
python main.py
```

启动后 Worker 会持续：

1. 到达 `HOMEPAGE_REFRESH_INTERVAL_SECONDS` 时，先刷新 `https://mp.weixin.qq.com/` 保持 cookie 活跃
2. 刷新成功后 poll server
3. 拿任务
4. 抓取文章列表
5. report 回 server
6. 休眠后继续下一轮

---

## 常看目录

- `data/chrome_profile/`：浏览器持久 profile
- `data/debug/`：调试日志与抓取痕迹

---

## 排查建议

如果 Worker 拿不到任务或抓不到数据，优先检查：

1. `SERVER_BASE_URL` 是否正确
2. `WORKER_TOKEN` 是否与 Server 一致
3. Server 是否已启动
4. `targets` 是否启用
5. 浏览器登录态是否仍有效
6. `data/debug/worker.log` 是否有 token / fingerprint 刷新异常

---

## 备注

如果你要看完整运行说明，请回到根 README：

- `../README.md`
