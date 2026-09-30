# 场馆失物认领系统

线下场馆失物认领网页应用：Python (Flask) + SQLite + 浏览器页面。

- **工作人员**凭配置口令登记物品：公开描述、私密辨认特征、保管状态；核对认领申请后**选定唯一一份**认领（并发确认同一物品最多成功一次，其余申请自动标记为未获选）；核销一次性交还码并记录交还。
- **访客**只能看到物品公开描述；可提交辨认答案与联系方式，获得**不可猜测的查询凭证**（URL 随机 token），凭证仅能查询本人认领进度。
- 私密辨认特征、他人认领答案与联系方式**仅工作人员可见**。
- 已撤下或已交还（含已选定待交还）的物品**拒绝新申请**。
- 获选者凭查询凭证取得**一次性交还码**；过期可重发，**新码使旧码失效**；**重复核码不得再次交还**。

## 所需配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `STAFF_TOKEN` | `staff-demo-token` | 工作人员登录口令，**生产环境必须修改** |
| `SECRET_KEY` | `dev-secret-change-me` | 会话签名密钥，**生产环境必须修改** |
| `DATABASE` | 容器内 `/data/lostfound.db`；本地 `./lostfound.db` | SQLite 数据库文件路径 |
| `PORT` | `8000` | 服务监听端口 |
| `CODE_TTL_MINUTES` | `30` | 交还码有效期（分钟） |
| `SEED_DEMO` | Compose 中默认 `1` | 首次启动写入演示物品数据（`1` 开 / `0` 关） |

数据库表结构在首次启动时自动创建，无需手动初始化。

## 启动命令

### 方式一：Docker Compose（推荐，一条命令）

```bash
docker compose up --build
```

自定义口令（可选）：

```bash
STAFF_TOKEN=your-strong-token SECRET_KEY=$(openssl rand -hex 32) docker compose up --build
```

### 方式二：本地 Python 运行

```bash
pip install -r requirements.txt
STAFF_TOKEN=your-strong-token python app.py
```

## 网页地址与端口

| 页面 | 地址 |
|---|---|
| 访客失物列表（首页） | http://localhost:8000/ |
| 工作人员入口 | http://localhost:8000/staff |
| 核销交还码 | http://localhost:8000/staff/verify |

默认端口 **8000**（Compose 映射 `8000:8000`，可在 `docker-compose.yml` 中调整）。

## 使用流程

1. **工作人员登记**：`/staff` 输入口令 → “登记新物品”，填写公开描述与私密辨认特征。
2. **访客认领**：首页选择物品 → 提交辨认答案与联系方式 → 保存系统发放的查询凭证及查询链接。
3. **工作人员核对**：进入物品详情，对照私密特征审核各申请的答案 → “选定此认领”（其余申请自动标记为未获选；已撤下/已交还物品拒绝新申请）。
4. **获选者取码**：凭查询凭证打开查询页，获得一次性交还码（默认 30 分钟有效，过期可重发，新码使旧码失效）。
5. **现场交还**：工作人员在 `/staff/verify` 输入交还码核销，系统记录交还；同一码重复核销会被拒绝。

## 数据持久化

Compose 使用命名卷 `lostfound-data` 挂载容器 `/data` 目录，SQLite 数据在容器重启后保留。备份只需复制卷内的 `lostfound.db` 文件。
