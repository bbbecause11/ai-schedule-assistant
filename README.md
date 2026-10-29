# 🗂️ AI 智能日程收纳 & 日历导入工具

> 把散落在聊天记录、课表截图、备忘录里的碎片化日程，一键整理成可视化日程表，
> 并导出为标准 `.ics` 日历文件，直接导入手机、电脑、Outlook、Google 日历。

面向**大学生 AI 创新比赛**的参赛作品，基于 Streamlit 构建，单文件应用，可本地一键运行。

**仓库地址**：<https://github.com/bbbecause11/ai-schedule-assistant>（公开，可直接访问）

| 访问方式 | 地址 | 说明 |
| --- | --- | --- |
| 本地运行 | <http://localhost:8502> | `streamlit run app.py --server.port 8502`（8501 已被本机其他项目占用） |
| 云端部署 | 见「部署 → 方案 B」 | Streamlit Community Cloud，可生成公网分享链接 |

---

## ✨ 核心能力

| 模块 | 能力 |
| --- | --- |
| **输入** | 纯文本粘贴、截图上传 OCR、**多次追加不覆盖**已有日程 |
| **AI 解析** | 自动提取标题 / 开始结束时间 / 地点 / 备注 / 优先级 / 循环规则 |
| **场景覆盖** | 普通活动、DDL 截止、会议、每周课表（支持「第 1-2 节」自动换算时间） |
| **智能校验** | 模糊时间自动推测并标注 ⚠️、自动去重、过滤过期日程、**时间冲突标红** |
| **交互** | 可视化表格、**在线编辑**、单条删除、一键加载示例、自定义提醒 |
| **导出** | 标准 `.ics`（含 VALARM 提醒）、CSV（Excel 友好）、Markdown |
| **容错** | 空输入、损坏图、大图、模糊图、OCR 失败、API 超时、解析失败均有友好提示 |
| **隐私** | 全程内存运算，**不使用数据库**，不存储图片与日程，页面底部含免责声明 |

### 双引擎解析设计

这是本项目与普通"调一次大模型"的方案最大的区别：

```
              ┌──────────────────┐
   文本/图片 ─→│  OCR（本地内存）  │─┐
              └──────────────────┘ │
                                   ├─→ ┌──────────────┐   融合   ┌────────────┐
              ┌──────────────────┐ │   │ 本地规则引擎  │───┬───→│ 去重/冲突  │─→ 日程表
   纯文本 ───→│  清洗 & 分段      │─┘   └──────────────┘   │    │ 过期过滤   │
              └──────────────────┘     ┌──────────────┐   │    └────────────┘
                                       │ 大模型解析(可选)│───┘
                                       └──────────────┘
```

- **有 API Key**：大模型负责理解口语化、上下文依赖强的文本（如群聊记录），规则引擎负责补全字段与校验。
- **没有 API Key / 网络异常**：自动降级为纯本地规则引擎，**完全离线可用**，比赛现场不会"空场"。
- **大模型超时或报错**：捕获异常 → 展示原因 → 自动回退规则引擎，页面不崩溃。

---

## 🚀 快速开始

### 环境要求

- Python **3.10+**（推荐 3.11 / 3.12 / 3.13）
- 可联网（仅安装依赖时需要；运行时可完全离线）

> **关于 OCR 依赖的版本差异**（这点很容易踩坑，已在 `requirements.txt` 中自动处理）：
> - Python **< 3.13**：安装 `rapidocr-onnxruntime`（自带 onnxruntime）
> - Python **≥ 3.13**：安装 `rapidocr` + **额外显式安装 `onnxruntime`**
>
> 如果只装 `rapidocr` 而漏了 `onnxruntime`，运行时才会报 `onnxruntime is not installed`。
> 侧边栏会实时显示 OCR 是否就绪；即使完全没装，文本解析功能也不受影响。

### 项目文件

```
.
├── app.py                       主应用（单文件，约 2700 行，19 个逻辑区块）
├── requirements.txt             依赖清单（含 Python 版本分流）
├── README.md                    本文档
├── SELF_CHECK_REPORT.md         自查报告（79 项用例 + 风险点 + 部署清单）
├── selfcheck.py                 自动化自查脚本，可随时复跑
├── run.bat                      Windows 一键启动
├── run.sh                       macOS / Linux 一键启动
├── .gitignore
└── .streamlit/
    └── secrets.toml.example     密钥配置模板（复制为 secrets.toml 使用）
```

### 第 1 步：安装依赖

```bash
# 进入项目目录
cd ai-schedule-assistant

# 创建虚拟环境（推荐，避免污染全局环境）
python -m venv .venv

# 激活虚拟环境
# Windows (CMD):
.venv\Scripts\activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 第 2 步：启动

```bash
streamlit run app.py
```

浏览器会自动打开 `http://localhost:8501`。若未自动打开，手动访问该地址即可。

> Windows 用户也可直接双击项目根目录的 `run.bat`（首次会自动创建虚拟环境并安装依赖）。

### 第 3 步：试试看

1. 点击 **「📚 加载示例」** → **「✨ 开始解析」**，立即看到 15 条混合格式的日程被整理成表格。
2. 切到 **「📅 日程表」**，观察标红（时间冲突）与标黄（时间待核对）的行，直接在表格里改。
3. 切到 **「📤 导出日历」**，下载 `.ics` 并双击导入系统日历。

---

## 🔑 配置大模型（可选，但推荐比赛时配置）

程序通过 **环境变量** 读取密钥，**代码中不存在任何硬编码密钥**。
支持任意 **OpenAI 兼容接口**：DeepSeek、通义千问、Kimi、智谱 GLM、OpenAI 等。

### 环境变量清单

| 变量名 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `AI_API_KEY` | 是 | 无 | 大模型 API Key（也兼容 `OPENAI_API_KEY`） |
| `AI_BASE_URL` | 否 | `https://api.deepseek.com/v1` | 接口地址，需包含 `/v1` |
| `AI_MODEL` | 否 | `deepseek-chat` | 模型名称 |
| `AI_TIMEOUT` | 否 | `60` | 单次请求超时秒数（10~180） |

### 配置方式（任选其一）

**Windows（CMD，当前窗口有效）**
```cmd
set AI_API_KEY=sk-你的密钥
set AI_BASE_URL=https://api.deepseek.com/v1
set AI_MODEL=deepseek-chat
streamlit run app.py
```

**Windows（PowerShell，当前窗口有效）**
```powershell
$env:AI_API_KEY="sk-你的密钥"
$env:AI_BASE_URL="https://api.deepseek.com/v1"
streamlit run app.py
```

**macOS / Linux**
```bash
export AI_API_KEY="sk-你的密钥"
export AI_BASE_URL="https://api.deepseek.com/v1"
streamlit run app.py
```

**永久生效（推荐比赛前配好）**
- Windows：`系统属性` → `高级` → `环境变量` → 新建用户变量，配置后**重启终端**。
- macOS / Linux：把 `export` 语句写入 `~/.bashrc` 或 `~/.zshrc`，然后 `source` 一下。

**最省事：用 Streamlit secrets（推荐新手）**

项目已提供模板 `.streamlit/secrets.toml.example`，复制一份改名为 `secrets.toml` 并填入密钥即可，
无需配置系统环境变量：

```toml
AI_API_KEY = "sk-你的密钥"
AI_BASE_URL = "https://api.deepseek.com/v1"
AI_MODEL = "deepseek-chat"
```

该文件已被 `.gitignore` 忽略，不会被提交到仓库。

> ⚠️ **不要把密钥写进 `app.py`、或粘贴到公开的截图里。**
> 代码中没有任何密钥占位符，只从运行时配置读取。

### 各服务商常用参数

| 服务商 | `AI_BASE_URL` | `AI_MODEL` 示例 |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 阿里通义 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 月之暗面 Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |

未配置密钥也能完整使用（走本地规则引擎），侧边栏会显示当前状态。

---

## 📖 使用说明

### 输入格式建议

程序对以下写法识别效果最好（每行一条，或用换行/分号分隔）：

```
明天下午3点 项目组例会 腾讯会议
每周二第3-4节 数据结构与算法 计算机楼C401
11月20日 23:59 前提交课程论文 DDL
下周三下午2点 在图书馆三楼研讨间开班会，务必到场
每天 早上7点 晨跑 操场
12月5日 全天 志愿者活动
22:00 到次日2点 通宵赶项目（跨天）
```

**支持的时间表达**

| 类型 | 示例 |
| --- | --- |
| 相对日期 | 今天、明天、后天、大后天、下周三、本周五、周三 |
| 绝对日期 | `11月20日`、`2026-12-01`、`11/20`、`月底` |
| 时刻 | `14:00`、`下午3点`、`上午9点半`、`晚上8点一刻` |
| 区间 | `3点到5点`、`9:00-11:30`、`14:00~16:00`、`9点至10点` |
| 时长 | `持续2小时`、`90分钟` |
| 课表 | `第1-2节`、`第9-10节`（按大学作息自动换算） |
| 循环 | `每周二`、`每周一三五`、`工作日`、`每天`、`每月` |
| 跨天 | `22:00 到次日2点`（自动识别为次日结束） |

### 关于 ⚠️ 待核对标记

以下情况程序会**推测**一个合理时间，并在「状态」列标记 ⚠️：

- 只有日期没有时刻 → 默认 09:00，持续 1 小时
- 只写「3点」未说明上下午 → 按常见作息推断为 15:00
- 只写「周三」未说明本周/下周 → 取最近的该星期几
- 由本地规则引擎补充的条目（大模型遗漏时）

**推测原因会写在表格的「状态」列与导出的 `.ics` 描述中**，请核对后再使用。

### 关于时间冲突

同一自然日内两个日程的时间区间发生重叠时，两行都会被标红，并在日程表顶部列出冲突明细。
冲突**只做提示，不会自动删除**任何日程。

---

## 🏗️ 技术架构

```
app.py  (单文件，约 2600 行，分 19 个逻辑区块)
├── 0.  全局配置          常量、时区、阈值、大学作息表
├── 1.  轻量日志          仅控制台输出，不落盘
├── 2.  数据模型          Event dataclass（含派生属性 / 双向序列化）
├── 3.  时间解析工具      中文数字、日期、时刻、循环、课节
├── 4.  规则解析引擎      无网络可用的主力解析器
├── 5.  OCR 层            多引擎探测 + 图片校验 + 缩放 + 容错
├── 6.  大模型层          OpenAI 兼容调用 + JSON 提取 + 错误分类
├── 7.  去重/冲突/过期     归一化流水线
├── 8.  导出层            ICS（RFC 5545）/ CSV / Markdown
├── 9.  演示数据          相对日期生成，永不过期
├── 10-13. 样式与侧边栏
├── 14-17. 四个 Tab 页面
├── 18. 页脚免责声明
└── 19. 主入口
```

### 关键技术决策

| 决策 | 原因 |
| --- | --- |
| **固定 UTC+8 偏移**而非 `zoneinfo` | 中国无夏令时，固定偏移精确；避免 Windows 缺 `tzdata` 时报 `ZoneInfoNotFoundError` |
| **ICS 用 UTC 时间戳**（`DTSTART:...Z`） | 绝对时刻，任何日历客户端都能正确换算本地时间，无需 `VTIMEZONE` |
| **ICS 按 UTF-8 字节折行 75 列** | RFC 5545 强制要求；按字符数折行会让中文标题在 Outlook 中解析失败 |
| **规则引擎 + 大模型双路融合** | 保证无网络/无 Key 时仍可用，且能用规则结果校正模型输出 |
| **`st.session_state` 存全部数据** | 满足"不落库、不落盘"的隐私要求，刷新即销毁 |
| **`on_click` 回调修改输入框** | Streamlit 不允许在 widget 实例化后直接改其 `session_state` |
| **内容指纹判断编辑** | 避免 `data_editor` 同步 → `rerun` → 再同步的无限循环 |
| **OCR 前缩放到 2400px** | 大图直接跑 OCR 极慢；等比缩放对识别率几乎无影响 |

---

## ☁️ 部署

### 方案 A：本地运行（比赛现场推荐）

见「快速开始」。建议提前跑一次 `pip install` 并预热 OCR 模型，避免现场等下载。

### 方案 B：Streamlit Community Cloud（免费，可分享链接）

**代码已托管在**：<https://github.com/bbbecause11/ai-schedule-assistant>（公开仓库）

部署步骤：

1. 打开 <https://share.streamlit.io>，用 GitHub 账号 `bbbecause11` 登录。
2. 首次使用需点 **Authorize Streamlit**，授予它读取你仓库的权限
   （仓库已公开，授权后可直接在列表中选到；不授权仍然看不到仓库列表）。
3. 点 `Create app` → 选择 `Deploy a public app from GitHub`，
   然后按下表填写：

   | 字段 | 填写内容 |
   | --- | --- |
   | Repository | `bbbecause11/ai-schedule-assistant` |
   | Branch | `main` |
   | Main file path | `app.py` |
   | App URL | 自定义子域名，如 `ai-schedule-xxxx` |
   | Python version（Advanced settings） | **3.13** |

4. 点 `Deploy`，等待 3~6 分钟构建（首次要装 OCR 依赖，较慢）。
5. 构建完成后在 `Settings` → `Secrets` 粘贴以下内容并保存：

   ```toml
   AI_API_KEY = "sk-你的密钥"
   AI_BASE_URL = "https://api.deepseek.com/v1"
   AI_MODEL = "deepseek-chat"
   AI_TIMEOUT = 60
   ```

6. 保存后应用自动重启，侧边栏显示「已连接」即配置成功。
   **不填 Secrets 也能用**，此时走本地规则引擎，只是不启用大模型增强。

> **几个云端专属注意点**
>
> - `packages.txt` 已声明 `libgl1`、`libglib2.0-0`，解决云端 OpenCV 缺少系统库报错，无需改动。
> - 免费实例内存约 1GB，且**闲置会休眠**，休眠后首次唤醒要 30 秒左右。比赛演示前先访问一次唤醒它。
> - 免费实例首次加载 OCR 模型需 20~40 秒，属正常现象。
> - 云端是**公共实例**，页面内存中的数据不跨用户共享，符合本项目「不落库」的设计。

### 方案 C：Docker（可选）

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
```

```bash
docker build -t ai-schedule .
docker run -p 8501:8501 -e AI_API_KEY=sk-xxx ai-schedule
```

---

## ✅ 部署需手动配置的步骤清单

| # | 事项 | 是否必须 | 说明 |
| --- | --- | --- | --- |
| 1 | 安装 Python 3.10+ | ✅ 必须 | `python --version` 确认 |
| 2 | `pip install -r requirements.txt` | ✅ 必须 | 首次约 100~300MB 下载 |
| 3 | 配置 `AI_API_KEY` 环境变量 | ⬜ 可选 | 不配则走本地规则引擎，功能仍完整 |
| 4 | 配置 `AI_BASE_URL` / `AI_MODEL` | ⬜ 可选 | 换非 DeepSeek 服务商时必须改 |
| 5 | 配置 `AI_TIMEOUT` | ⬜ 可选 | 网络慢时调大到 90~120 |
| 6 | 启动 `streamlit run app.py` | ✅ 必须 | 默认端口 8501，被占用时加 `--server.port 8502` |
| 7 | 允许防火墙访问 8501 | ⬜ 按需 | 仅当需要局域网内他人访问 |
| 8 | 云端部署时在 Secrets 填密钥 | ⬜ 按需 | 仅 Streamlit Cloud 方案需要 |
| 9 | 提前预热 OCR 模型 | ⬜ 建议 | 首次上传图片会下载模型，建议赛前跑一次 |
| 10 | 准备示例截图 | ⬜ 建议 | 现场演示 OCR 建议用清晰、字号大的课表截图 |

**无需手动配置**：数据库、用户系统、文件存储、定时任务 —— 本项目全部不需要。

---

## 🔒 隐私与合规

- **不落库**：无数据库、无 ORM、无 SQL，所有数据仅存于 `st.session_state`（浏览器会话内存）。
- **不落盘**：不上传文件到磁盘，不写日志文件，不缓存图片。
- **图片不出本机**：OCR 使用本地 `rapidocr` 模型推理，原图不发送到任何服务器。
- **仅文本出网**：仅当配置了大模型时，**识别出的文字**会发送至该模型服务用于解析。
- **密钥安全**：只从环境变量 / Streamlit Secrets 读取，不写入代码、不写入日志、不回显到页面。
- **免责声明**：页面底部常驻，明确解析结果仅供参考、使用风险自负。

---

## ⚠️ 已知限制

1. **手写体与艺术字识别率低**：OCR 针对印刷体优化，手写课表建议手动录入。
2. **复杂表格截图**：多列课表（周一~周日横向排布）可能被解析为单列，建议按行截图。
3. **极度口语化的表达**：如「下周找个时间聚一下」，规则引擎无法解析，需配置大模型。
4. **农历日期不支持**：仅识别公历。
5. **循环日程的冲突检测**：只检查已录入的首次实例，不会展开未来所有实例做冲突检查。
6. **单次上限**：文本 20000 字、图片 3 张 × 10MB、单次解析最多 60 条（防止异常输出撑爆页面）。
7. **时区**：固定按 UTC+8 处理，海外用户需自行调整 `CN_TZ` 常量。
8. **首次 OCR 推理较慢**：需要加载约 15MB 模型，首次约 3~5 秒；之后由缓存加速。
   **建议比赛前先上传一张图预热。**
9. **数据不持久化**：刷新页面即清空（这是隐私设计的一部分），演示完请及时导出 `.ics`。

---

## 🧪 自动化自查

项目附带可复跑的测试脚本，无需打开浏览器：

```bash
python selfcheck.py      # 静默模式，仅打印失败项
python selfcheck.py -v   # 打印每条用例详情
```

覆盖 79 项用例：时间解析准确性、跨天与跨年、去重与冲突、ICS RFC 合规性（75 字节折行 /
UTC 转换 / 字符转义）、OCR 各返回格式与异常分支、AI 层错误处理、安全性静态扫描。

详细结果见 **[SELF_CHECK_REPORT.md](SELF_CHECK_REPORT.md)**，其中还包含：
- 25 项需求逐条核对表
- 自查中发现并修复的 14 个缺陷
- 7 项风险点与缓解措施（答辩时可能被问到）
- 10 步部署配置清单

---

## 📄 许可

本项目为比赛演示作品，仅供学习与交流使用。
