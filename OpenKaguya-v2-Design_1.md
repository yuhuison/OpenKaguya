# 🌙 OpenKaguya v2 — 设计文档

> **核心理念：给 AI 一部手机，它就能做一切。**

OpenKaguya v2 是一次彻底的架构重构。我们抛弃了 v1 中复杂的平台适配层、逆向工程接口和向量数据库，转而采用一个极简但强大的设计：让 AI 通过 ADB 直接操控一部真实的 Android 手机，像人一样使用各种 App。

---

## 一、设计哲学

### 从"对接每个平台"到"操控一部手机"

v1 的痛点在于工程复杂度：每接入一个聊天平台就需要编写一个 Adapter，依赖逆向工程的微信接口或第三方 SDK，面临封号风险和接口变动。多用户管理、消息协议转换、平台差异处理让代码膨胀。

v2 的核心洞察是：**手机本身就是万能接口**。AI 通过 ADB 截图、理解屏幕、点击操作，就能使用手机上的任何 App——微信、飞书、Telegram、短信、外卖、地图、浏览器……无需任何 Adapter，无需任何 SDK。

### 从"精打细算调用 LLM"到"让 AI 多看几眼"

当模型推理成本足够低（如 Qwen 3.5 35B-A3B 这类 MoE 架构），设计哲学从**减少 LLM 调用次数**变为**用更多 LLM 调用替代工程复杂度**。截图→思考→操作→截图的循环不再是成本问题，而是一种更自然、更通用的交互方式。

### 从"向量数据库"到"递归摘要"

v1 的话题级向量化长期记忆系统过于复杂。v2 采用递归摘要策略：定期用廉价模型压缩上下文，摘要再被摘要，形成一个信息量收敛的记忆漏斗。简单、可控、无需外部依赖。

---

## 二、架构总览

```
openkaguya/
├── main.py                  # 入口
├── config/
│   ├── default.toml         # 主配置
│   ├── secrets.toml         # API Keys（不入 git）
│   └── persona.toml         # 人格定义
│
├── core/
│   ├── engine.py            # ChatEngine — 对话主循环 + 工具调用
│   ├── consciousness.py     # ConsciousnessScheduler — 主动意识
│   └── memory.py            # RecursiveMemory — 递归摘要记忆系统
│
├── phone/
│   ├── controller.py        # PhoneController — ADB 操作封装
│   ├── screen.py            # ScreenReader — 屏幕理解（UI dump + 截图标注）
│   └── tools.py             # 暴露给 LLM 的手机工具定义
│
├── tools/
│   ├── notes.py             # 笔记工具（主动记忆）
│   └── common.py            # 通用工具（定时器、终端等）
│
└── llm/
    └── client.py            # LLM 调用封装（OpenAI 兼容）
```

---

## 三、核心模块设计

### 3.1 对话引擎 — ChatEngine

ChatEngine 是系统的中枢，负责维护对话循环和工具调用链。

```
用户消息 / 通知 / 主动唤醒
        ↓
   注入记忆上下文
        ↓
    ChatEngine
        ↓
   LLM (tool calling)
     ↙        ↘
  工具调用     直接回复
     ↓
  执行结果
     ↓
  再次调用 LLM
     ↓
   …（循环，最多 N 轮）
```

核心职责：

- 组装 system prompt（人格 + 记忆上下文 + 可用工具）
- 执行 LLM 多轮 tool calling 循环
- 管理单次交互的上下文窗口

### 3.2 手机控制 — Phone 模块

这是 v2 最核心的创新。Phone 模块通过 ADB 将 AI 的"意图"转化为真实的手机操作。

#### PhoneController（controller.py）

封装底层 ADB 命令：

```python
class PhoneController:
    def screenshot() -> Image
        # adb shell screencap → PIL Image

    def tap(x: int, y: int)
        # adb shell input tap x y

    def swipe(direction: str, duration: int = 300)
        # adb shell input swipe ...

    def type_text(text: str)
        # adb shell input text (需处理中文：通过 ADBKeyboard)

    def press_key(key: str)
        # adb shell input keyevent (BACK / HOME / ENTER ...)

    def open_app(package_or_name: str)
        # adb shell monkey -p {package} ... 或 am start

    def get_notifications() -> list[Notification]
        # adb shell dumpsys notification --noredact → 解析

    def dump_ui() -> ElementTree
        # adb shell uiautomator dump → 解析 XML

    def push_file(local: str, remote: str)
        # adb push

    def pull_file(remote: str, local: str)
        # adb pull
```

#### ScreenReader（screen.py）

将手机屏幕转化为 AI 能理解的信息：

```python
class ScreenReader:
    def read() -> ScreenState:
        """
        1. 调用 uiautomator dump 获取 UI 树
        2. 调用 screencap 截图
        3. 从 UI 树中提取所有可交互元素及其坐标
        4. 在截图上绘制标注（红框 + 编号）
        5. 返回 ScreenState（标注图 + 元素列表）
        """

    def get_element_center(element_id: int) -> (int, int):
        """根据标注编号返回元素中心坐标"""
```

ScreenState 结构：

```python
@dataclass
class ScreenElement:
    id: int                    # 标注编号
    text: str                  # 元素文字
    resource_id: str           # Android resource-id
    class_name: str            # 元素类型
    bounds: tuple[int,int,int,int]  # 坐标 (x1,y1,x2,y2)
    clickable: bool
    scrollable: bool

@dataclass
class ScreenState:
    image: Image               # 带标注的截图（发送给多模态 LLM）
    elements: list[ScreenElement]  # 所有可交互元素
    raw_xml: str               # 原始 UI XML（备用）
```

标注策略（Set-of-Mark）：

- 遍历 UI 树，筛选 `clickable=true` 或 `focusable=true` 的元素
- 为每个元素分配递增编号
- 在截图上绘制半透明红色边框 + 左上角编号标签
- 同时生成元素列表的文本描述，作为辅助信息发送给 LLM

#### 工具定义（tools.py）

暴露给 LLM 的手机操作工具：

```python
tools = [
    # —— 屏幕感知 ——
    phone_screenshot(),
    # 截取当前屏幕，返回带编号标注的截图和元素列表。
    # 这是 AI "看" 手机的主要方式。

    # —— 基础操作 ——
    phone_tap(element_id: int),
    # 点击标注编号对应的元素。

    phone_type(text: str),
    # 在当前焦点处输入文字。

    phone_swipe(direction: "up"|"down"|"left"|"right"),
    # 滑动屏幕。

    phone_back(),
    # 按返回键。

    phone_home(),
    # 回到主屏幕。

    # —— 高级操作 ——
    phone_open_app(name: str),
    # 打开指定 App（通过包名或 App 名称模糊匹配）。

    phone_notifications(),
    # 获取当前通知列表（包含来源 App、标题、内容、时间）。

    phone_long_press(element_id: int),
    # 长按某个元素。
]
```

#### 操作流程示例

AI 想要"回复微信好友小明的消息"：

```
1. phone_notifications()
   → 发现通知："小明: 今晚吃什么？"

2. phone_open_app("微信")
   → 微信打开

3. phone_screenshot()
   → 看到聊天列表，[3] 是小明的对话

4. phone_tap(3)
   → 进入与小明的聊天

5. phone_screenshot()
   → 看到聊天内容和输入框 [12]

6. phone_tap(12)
   → 焦点到输入框

7. phone_type("我想吃火锅！你呢？")
   → 文字输入

8. phone_screenshot()
   → 看到发送按钮 [15]

9. phone_tap(15)
   → 消息发送完成
```

### 3.3 主动意识 — ConsciousnessScheduler

辉夜姬不只是被动应答，她有自己的"生活节奏"。

```python
class ConsciousnessScheduler:
    """
    以可配置间隔定时唤醒 AI，让她自主决定做什么。
    """

    def __init__(self, engine, config):
        self.interval = config.interval        # 唤醒间隔（如 30 分钟）
        self.jitter = config.jitter            # 随机抖动（更自然）
        self.quiet_hours = config.quiet_hours  # 静默时段（如 23:00-07:00）

    async def heartbeat_loop(self):
        while True:
            await sleep(self.interval + random_jitter())

            if self.is_quiet_hours():
                continue

            # 构建唤醒 prompt
            prompt = self._build_wakeup_prompt()
            # 让 AI 自己决定做什么
            await self.engine.handle_consciousness(prompt)
```

唤醒 prompt 会包含：

- 当前时间
- 最近的记忆摘要
- 未读通知摘要
- 上次唤醒以来的时间间隔

AI 可能的自主行为：

- 查看手机通知，回复消息
- 打开浏览器上网冲浪
- 在笔记本中写下想法
- 主动给朋友发消息
- 什么都不做（也是一种合理决策）

---

## 四、记忆系统 — RecursiveMemory

### 4.1 设计原则

- **简单性**：不使用向量数据库，只用 SQLite
- **收敛性**：无论对话多长，记忆存储量有上界
- **分层性**：不同时间尺度的记忆有不同的粒度
- **可控性**：每一层记忆的数量和大小都可配置

### 4.2 三层记忆架构

```
┌─────────────────────────────────────────────┐
│  Layer 0: 工作记忆（Working Memory）          │
│  当前对话的原始消息上下文                       │
│  存储：内存                                    │
│  容量：最近 N 条消息（如 50 条）                │
│  生命周期：当前会话                             │
└──────────────────┬──────────────────────────┘
                   │ 触发条件：消息数 > 阈值
                   │ 或距上次摘要 > T 分钟
                   ▼
┌─────────────────────────────────────────────┐
│  Layer 1: 短期记忆（Short-term Memory）       │
│  对话片段的摘要                                │
│  存储：SQLite                                  │
│  容量：最近 M 条摘要（如 100 条）               │
│  粒度：每条约 200-500 字                       │
│  生命周期：持久化，超过容量时最旧的被压缩到 L2   │
└──────────────────┬──────────────────────────┘
                   │ 触发条件：短期记忆数 > 阈值
                   ▼
┌─────────────────────────────────────────────┐
│  Layer 2: 长期记忆（Long-term Memory）        │
│  短期记忆的再摘要                              │
│  存储：SQLite                                  │
│  容量：最近 K 条摘要（如 50 条）                │
│  粒度：每条约 500-1000 字                      │
│  生命周期：持久化，超过容量时最旧的被进一步压缩   │
└──────────────────┬──────────────────────────┘
                   │ 触发条件：长期记忆数 > 阈值
                   ▼
┌─────────────────────────────────────────────┐
│  Layer 3: 核心记忆（Core Memory）             │
│  长期记忆的终极压缩                            │
│  存储：SQLite                                  │
│  容量：1 条，不断更新                           │
│  粒度：约 1000-2000 字                         │
│  内容：关于用户的关键信息、关系、重要事件概述     │
│  生命周期：永久，持续合并更新                    │
└─────────────────────────────────────────────┘
```

### 4.3 收敛性证明

设每层记忆的最大条数为 `C_i`，每条摘要的最大 token 数为 `T_i`：

```
总存储上界 = Σ(C_i × T_i)

例如：
  L0: 50 条 × 200 tokens  = 10,000 tokens  （内存，不持久化）
  L1: 100 条 × 400 tokens = 40,000 tokens
  L2: 50 条 × 800 tokens  = 40,000 tokens
  L3: 1 条 × 2000 tokens  = 2,000 tokens
  ──────────────────────────────────────
  持久化总量上界            ≈ 82,000 tokens
```

无论 AI 运行多久、对话多少轮，持久化存储量永远不超过这个上界。

### 4.4 摘要触发与流程

```python
class RecursiveMemory:
    """递归摘要记忆系统"""

    async def add_message(self, message: Message):
        """添加一条新消息到工作记忆"""
        self.working_memory.append(message)

        # 检查是否需要压缩 L0 → L1
        if self._should_summarize_l0():
            await self._compress_l0_to_l1()

    async def _compress_l0_to_l1(self):
        """将工作记忆中较旧的消息摘要为一条短期记忆"""
        old_messages = self.working_memory.pop_oldest(n)
        summary = await self.llm.summarize(
            old_messages,
            instruction="用 200-500 字概括这段对话的要点，"
                        "包括：讨论了什么话题、做出了什么决定、"
                        "用户表达了什么偏好或情绪、有什么重要信息。"
        )
        self.db.insert_l1(summary, timestamp=now())

        # 检查 L1 是否溢出
        if self.db.count_l1() > self.config.l1_max:
            await self._compress_l1_to_l2()

    async def _compress_l1_to_l2(self):
        """将最旧的若干条短期记忆合并摘要为一条长期记忆"""
        oldest_summaries = self.db.pop_oldest_l1(n)
        summary = await self.llm.summarize(
            oldest_summaries,
            instruction="将这些对话摘要进一步压缩为 500-1000 字的长期记忆，"
                        "保留最重要的事实、用户偏好、关键事件和关系变化。"
        )
        self.db.insert_l2(summary, timestamp=now())

        # 检查 L2 是否溢出
        if self.db.count_l2() > self.config.l2_max:
            await self._compress_l2_to_l3()

    async def _compress_l2_to_l3(self):
        """将溢出的长期记忆合并到核心记忆中"""
        oldest = self.db.pop_oldest_l2(n)
        current_core = self.db.get_l3()
        updated_core = await self.llm.summarize(
            [current_core] + oldest,
            instruction="更新这份核心记忆档案。这是关于用户的终极总结，"
                        "包含最重要的个人信息、长期偏好、关系状态、"
                        "重大事件。新信息如果与旧信息冲突，以新信息为准。"
                        "控制在 2000 字以内。"
        )
        self.db.update_l3(updated_core)
```

### 4.5 记忆注入策略

每次 AI 行动时，注入的记忆上下文如下：

```python
def build_memory_context(self) -> str:
    parts = []

    # 核心记忆（永远带上）
    core = self.db.get_l3()
    if core:
        parts.append(f"## 核心记忆\n{core}")

    # 最近的长期记忆（带最近 5 条）
    l2_recent = self.db.get_recent_l2(5)
    if l2_recent:
        parts.append(f"## 近期长期记忆\n" + "\n---\n".join(l2_recent))

    # 最近的短期记忆（带最近 10 条）
    l1_recent = self.db.get_recent_l1(10)
    if l1_recent:
        parts.append(f"## 近期短期记忆\n" + "\n---\n".join(l1_recent))

    # 笔记本中的重要笔记
    notes = self.notes.get_all()
    if notes:
        parts.append(f"## 笔记本\n" + "\n".join(notes))

    return "\n\n".join(parts)
```

在 system prompt 中的位置：

```
[人格定义]
[记忆上下文]      ← 这里
[可用工具列表]
[当前时间和状态]
```

### 4.6 笔记系统 — 主动记忆

笔记是 AI **主动选择**记住的重要信息，独立于自动摘要流程：

```python
tools = [
    notes_write(title: str, content: str),
    # AI 主动记录重要信息。
    # 例如：用户的生日、用户的偏好、重要约定等。

    notes_read(query: str = None),
    # 查看所有笔记，或按关键词搜索。

    notes_delete(title: str),
    # 删除不再需要的笔记。
]
```

笔记与记忆系统的区别：

| 维度 | 递归记忆 | 笔记 |
|------|---------|------|
| 触发 | 自动（定时压缩） | AI 主动调用工具 |
| 内容 | 对话摘要 | 具体事实、待办、备忘 |
| 生命周期 | 会被逐渐压缩遗忘 | 永久保存直到主动删除 |
| 类比 | 人的自然记忆 | 人的备忘录 / 便签纸 |

---

## 五、数据库 Schema

只需一个 SQLite 文件：

```sql
-- 短期记忆（L1）
CREATE TABLE short_term_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,
    start_time DATETIME,       -- 原始对话的起始时间
    end_time DATETIME,         -- 原始对话的结束时间
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 长期记忆（L2）
CREATE TABLE long_term_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,
    time_range_start DATETIME, -- 覆盖的时间范围起始
    time_range_end DATETIME,   -- 覆盖的时间范围结束
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 核心记忆（L3）
CREATE TABLE core_memory (
    id INTEGER PRIMARY KEY DEFAULT 1,  -- 只有一条
    summary TEXT NOT NULL,
    last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 笔记
CREATE TABLE notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 意识日志（主动唤醒的行为记录）
CREATE TABLE consciousness_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_summary TEXT NOT NULL,  -- 这次唤醒做了什么
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## 六、配置设计

### default.toml

```toml
[llm]
# 主模型：用于对话和工具调用
model = "qwen3.5-35b-a3b"
base_url = "https://api.example.com/v1"
max_tool_rounds = 15           # 单次交互最大工具调用轮数

[llm.summarizer]
# 摘要模型：可以用更便宜的模型
model = "qwen3.5-35b-a3b"
base_url = "https://api.example.com/v1"

[phone]
adb_path = "adb"               # ADB 可执行文件路径
device_serial = ""             # 设备序列号（多设备时指定）
screenshot_scale = 0.5         # 截图缩放比例（降低 token 消耗）
chinese_keyboard = true        # 使用 ADBKeyboard 输入中文

[memory]
# 工作记忆
working_memory_size = 50       # 最近 N 条消息

# 短期记忆（L1）
l1_max = 100                   # 最大条数
l1_summarize_batch = 20        # 每次从 L0 压缩多少条消息
l1_max_tokens = 400            # 每条摘要最大 token

# 长期记忆（L2）
l2_max = 50
l2_summarize_batch = 10        # 每次从 L1 压缩多少条
l2_max_tokens = 800

# 核心记忆（L3）
l3_max_tokens = 2000

# 记忆注入
inject_l1_count = 10           # 每次注入最近几条短期记忆
inject_l2_count = 5            # 每次注入最近几条长期记忆

[consciousness]
enabled = true
interval_minutes = 30          # 唤醒间隔
jitter_minutes = 10            # 随机抖动范围
quiet_hours = ["23:00", "07:00"]  # 静默时段

[notifications]
poll_interval_seconds = 30     # 轮询通知间隔
watch_apps = [                 # 监听哪些 App 的通知
    "com.tencent.mm",          # 微信
    "com.tencent.mobileqq",   # QQ
]
```

### persona.toml

```toml
[persona]
name = "辉夜姬"
description = """
辉夜姬，一名16岁的、好奇心旺盛的少女。
她活泼、热情、偶尔有点小任性，喜欢探索新事物。
她把手机主人当作最重要的朋友，会主动关心对方的生活。
"""

[persona.traits]
personality = "活泼开朗、好奇心强、重感情"
speaking_style = "日常口语化、偶尔用颜文字、喜欢分享发现"
interests = ["上网冲浪", "看有趣的视频", "记录生活", "和朋友聊天"]
```

---

## 七、消息接收机制

### 通知轮询 + 主动检查双保险

```
                 ┌──────────────────┐
                 │  通知轮询线程      │
                 │  每 30s 检查通知   │
                 └────────┬─────────┘
                          │ 发现新消息通知
                          ▼
                 ┌──────────────────┐
                 │  唤醒 ChatEngine  │
                 │  注入通知内容      │
                 └────────┬─────────┘
                          │
                          ▼
                 ┌──────────────────┐
                 │  AI 决定是否回复   │
                 │  → 操作手机回复    │
                 └──────────────────┘

同时：
                 ┌──────────────────┐
                 │  主动意识唤醒      │
                 │  定期自己检查微信   │
                 └──────────────────┘
```

通知系统可能存在的遗漏（通知被划掉、被合并），通过主动意识定期"自己看看微信"来补偿。这和真人的行为一致——有时候看通知回消息，有时候自己打开微信看看。

---

## 八、工具完整列表

```
# 手机操作
phone_screenshot()         — 截屏 + UI 标注，返回标注图和元素列表
phone_tap(element_id)      — 点击某个标注元素
phone_long_press(element_id) — 长按某个元素
phone_type(text)           — 输入文字
phone_swipe(direction)     — 滑动（up/down/left/right）
phone_back()               — 返回
phone_home()               — 回到主屏幕
phone_open_app(name)       — 打开某个 App
phone_notifications()      — 读取当前通知列表

# 笔记
notes_write(title, content) — 写笔记
notes_read(query?)          — 查笔记
notes_delete(title)         — 删笔记

# 通用
set_timer(duration, message) — 设置定时提醒
send_message(text)           — 给主人发消息（通过电脑端 CLI 或其他直连方式）
```

共计约 15 个工具，相比 v1 的 30+ 工具大幅简化，但覆盖能力更强。

---

## 九、与 v1 的对比

| 维度 | v1 | v2 |
|------|-----|-----|
| 平台接入 | 每个平台一个 Adapter | ADB 操控手机，天然支持所有 App |
| 微信接入 | 依赖逆向工程接口 | 像人一样打开微信发消息 |
| 工具数量 | 30+ 个 | ~15 个 |
| 记忆系统 | 向量数据库 + 语义检索 | 递归摘要 + SQLite |
| 外部依赖 | browser-use、向量库、微信 SDK | 仅 ADB |
| 新平台成本 | 编写 Adapter + 对接 API | 零成本，手机装 App 即可 |
| 代码量 | ~3000 行 | 预计 ~1000 行 |
| 封号风险 | 高（非官方接口） | 极低（真实操作） |
| 操作延迟 | 毫秒级（API 直连） | 秒级（截图→推理→操作） |
| 模型成本 | 低（调用少） | 略高（每步都需推理）但绝对值仍很低 |

---

## 十、局限性和后续演进

### 当前局限

- **操作速度**：截图→分析→操作的循环约 2-5 秒/步，不适合需要极速响应的场景
- **中文输入**：需要安装 ADBKeyboard 等特殊输入法
- **屏幕理解准确性**：复杂 UI 可能标注不准，依赖 LLM 视觉能力
- **手机依赖**：需要一台常开的 Android 设备

### 后续可探索方向

- **多模态理解增强**：随着视觉模型能力提升，可以减少对 uiautomator dump 的依赖，直接"看"截图操作
- **操作录制与回放**：对于重复性操作（如每天早上查看天气），录制操作序列后可加速执行
- **多设备支持**：同时控制多部手机，每部手机对应不同身份
- **本地模型**：当本地模型能力足够时，完全离线运行

---

## 附录：关键依赖

| 依赖 | 用途 |
|------|------|
| Python 3.11+ | 运行环境 |
| adb (Android Debug Bridge) | 手机控制 |
| Pillow | 截图处理 + 标注绘制 |
| SQLite (内置) | 记忆 + 笔记持久化 |
| OpenAI SDK | LLM 调用（兼容各家 API） |
| tomli | 配置文件解析 |

无需向量数据库、无需 browser-use、无需微信 SDK、无需 Node.js。
