# 轻量级万能数据存储后端 (Universal Flask Backend)

这是一个基于 Flask 和 SQLite 的轻量级后端服务，提供通用的数据存储与提取功能。采用“前端驱动”的设计理念，后端只负责提供万能数据接口和静态页面托管，极大提升了小型项目和原型开发的效率。

## 🌟 核心特性
- **静态页面自动托管**：将 HTML 文件放入 `pages/` 目录即可通过路由直接访问（无需加 `.html` 后缀）。
- **万能集合存储 (BaaS)**：无需提前建表，通过 `/api/<collection>` 直接进行 JSON 数据的存取操作。
- **阅后即焚 / 限时提取码 (KV Store)**：内置带有过期时间的键值对存储，适用于提取码、临时数据中转，过期自动销毁。

## 🚀 快速启动
1. 确保安装了 Python 和 Flask：
   ```bash
   pip install flask




   🗂 API 接口文档
1. 系统接口
获取可用页面列表

GET /api/_sys/pages

返回: 自动扫描 pages 目录下的 HTML 文件名列表。

2. 万能数据集合 (Collection)
保存数据

POST /api/<collection>

Body: 任意 JSON 对象。

返回: {"status": "success"}

获取数据

GET /api/<collection>

返回: 该集合下最新的 50 条数据列表，包含 _time 字段。

3. 限时键值存储 (KV Store)
设置临时数据

POST /api/kv/<key>

Body: {"payload": {...}, "expire_at": 1710000000} (expire_at 为 Unix 秒级时间戳，可为空)

返回: {"status": "success"}

提取临时数据

GET /api/kv/<key>

返回: 如果未过期则返回 payload，若超时则返回 404 且数据被永久销毁。


### 第二部分：歌曲 AI 分类页面

为了让你更好地理解 AI 歌曲分类的系统架构逻辑，这里附上一张原理图：


请在项目的 `pages/` 目录下创建一个名为 `song_tags.html` 的文件，并将以下代码粘贴进去。

这个页面实现了：
1. **指定资源库节点**：输入 IP 和端口。
2. **音频获取与播放**：拼接流媒体地址进行播放。
3. **标签管理**：从预设标签池中挑选，支持手动点击添加/删除。
4. **AI 智能标注**：内置一个模拟的 AI 算法，一键随机挑选合适的标签（你可以随时替换为你真实的 AI 接口请求）。
5. **保存同步**：调用你现有的 `/api/song_tags` 接口进行持久化保存。