---
name: 仪表盘
description: 管理 Stone Plan Dashboard 服务器。当用户说"启动仪表盘"、"打开面板"、"看面板"、"dashboard"、"重启服务"时自动触发。
---

# 仪表盘

## 描述
管理 Stone Plan 的 Web Dashboard 服务器，包括启动、重启、查看状态。

## 使用场景
- 用户说"启动仪表盘"、"打开面板"、"看面板"
- 用户说"dashboard"、"重启服务"
- 需要在浏览器中查看任务进度

## 指令

### 启动服务器
```bash
cd /workspace/stone-plan && python3 generate-dashboard.py --serve
```
- 默认端口：8765
- 访问地址：http://localhost:8765
- 自动生成 dashboard.html 并启动 HTTP 服务

### API 端点
| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | Dashboard 页面 |
| `/api/data` | GET | 获取所有数据（JSON） |
| `/api/toggle-task` | POST | 切换今日任务状态 |
| `/api/toggle-milestone` | POST | 切换目标里程碑状态 |
| `/api/upload` | POST | 上传文件（multipart） |
| `/api/files` | GET | 列出已上传文件 |
| `/api/delete-file` | POST | 删除文件 |

### 重启服务器
如果修改了 md 文件但页面没更新：
1. 杀掉旧进程：`kill -9 $(lsof -t -i:8765)`
2. 重新启动：`python3 generate-dashboard.py --serve`

### 注意事项
- 服务器时区为 UTC，比北京时间慢8小时
- Dashboard 每10秒自动刷新数据
- 点击任务/里程碑可直接修改 md 文件（双向同步）
- 手机访问已适配（3个响应式断点：900/640/380px）

## 文件位置
- `generate-dashboard.py` — Dashboard 生成器和服务器
- `dashboard.html` — 生成的页面（自动生成，不要手动编辑）
- `uploads/` — 上传文件目录
