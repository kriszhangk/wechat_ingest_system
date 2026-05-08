# Architecture

## 目标

本地 Worker 持有微信公众号后台登录态，负责抓取公众号文章列表；服务器负责：
- 任务管理
- 结果接收
- 去重
- 正文抓取
- Markdown 导出
- 后续通知扩展

## 角色

### Server
- FastAPI HTTP API
- SQLite 数据库
- 接收 worker 结果
- 处理新增文章

### Worker
- 定时轮询 server 任务
- 使用 Playwright 登录后台抓取公众号文章列表
- 将结果 POST 回 server

## 数据流
1. Worker 调用 `/api/v1/worker/poll`
2. Server 返回待抓取的公众号任务
3. Worker 执行抓取，得到文章列表
4. Worker 调用 `/api/v1/worker/report`
5. Server 去重并抓取正文，导出 Markdown
