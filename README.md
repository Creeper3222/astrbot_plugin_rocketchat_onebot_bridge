# AstrBot Rocket.Chat OneBot Bridge

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D3.4-blue)](https://github.com/AstrBotDevs/AstrBot)
[![Platform](https://img.shields.io/badge/Platform-aiocqhttp%20%2F%20OneBot%20v11-pink)](https://github.com/AstrBotDevs/AstrBot)

将 [Rocket.Chat](https://rocket.chat) 通过桥接方式接入 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的 Star 插件。

本项目参考了 [NET-Homeless/astrbot_plugin_rocket_chat_adapter](https://github.com/NET-Homeless/astrbot_plugin_rocket_chat_adapter) 的 `v0.5.3` 基础实现，但当前形态已经不再是一个独立的 Rocket.Chat 平台适配器，而是完整构建了 `AstrBot <-> OneBot v11 <-> Rocket.Chat` 的桥接连接器。

这意味着：

- 现在可以直接使用 AstrBot 自带的 `aiocqhttp` / `OneBot v11` 平台来配置机器人。
- 本插件负责把 Rocket.Chat 的消息、媒体、引用、成员信息和反应能力桥接成 OneBot v11 语义。
- 对 AstrBot 来说，Rocket.Chat 侧会表现为一个可直接参与现有插件生态的 OneBot v11 机器人。

---

## 架构说明

```text
Rocket.Chat Server
    ^
    |  REST API + DDP/WebSocket
    v
astrbot_plugin_rocketchat_onebot_bridge
    ^
    |  OneBot v11 Reverse WebSocket Client
    v
AstrBot built-in aiocqhttp adapter
    ^
    |  AstrBot event pipeline / plugins / providers
    v
AstrBot Core
```

声明：

- 当前桥接器是在 AstrBot 内部同时连接 Rocket.Chat 和 AstrBot 自带的 OneBot v11 反向 WebSocket 服务。
- 因此不需要再额外维护一套 Rocket.Chat 平台适配器入口，直接复用 AstrBot 已有的 `aiocqhttp` 生态。

---

## 功能特性

- 实时接收 Rocket.Chat 频道、私有群组、私聊消息。
- 通过 AstrBot 自带 `aiocqhttp` 平台接入，形成 OneBot v11 语义闭环。
- 支持主 bot + 多副 bot 架构。
- 内置独立 WebUI，可管理网络配置、副 bot、基础信息和运行日志。
- 独立 WebUI 支持可选访问密码；密码留空时不启用认证。
- 基础信息页会展示每个 bot 对应 Rocket.Chat 服务器的品牌头像和服务器名称。
- 支持自动重连；当超过最大重连次数时，可自动关闭对应 bot，避免反复空转。
- 支持动态订阅新房间，机器人被拉入新房间后无需重启。
- 支持 OneBot 风格的群聊、私聊、消息查询、群成员查询、登录信息查询。
- 支持文本、`at`、引用回复、图片、文件、语音、视频、Markdown 出站发送。
- 支持引用链提取、回复来源识别、提及用户映射、群聊/私聊上下文映射。
- 支持远端媒体下载、大小限制控制、本地临时文件落地和 Base64 媒体上传。
- 支持 Rocket.Chat 官方 E2EE 私聊/私有群组文本与媒体收发。
- 支持 `astrbot_plugin_iamthinking` 的数字表情反应映射，可配置思考中/完成后的 Rocket.Chat reaction shortcode。
- 使用 `plugin_data` 持久化保存消息映射、上下文房间映射、副 bot 配置和运行状态。

---

## 当前实现范围

### 已实现的 OneBot 动作

- `send_group_msg`
- `send_private_msg`
- `send_msg`
- `get_msg`
- `get_group_info`
- `get_group_member_info`
- `get_group_member_list`
- `get_stranger_info`
- `set_msg_emoji_like`
- `get_login_info`

### 当前不支持的 OneBot 动作

- `send_group_forward_msg`
- `send_private_forward_msg`

桥接器在这一版里明确不承诺合并转发消息语义。

---

## 消息与媒体能力

### 入站能力

- Rocket.Chat 文本消息会被转换为 OneBot `message` 事件。
- 私聊会映射为 OneBot `private` 消息。
- 频道和私有群组会映射为 OneBot `group` 消息。
- Rocket.Chat `mentions` 会转换为 OneBot `at` 段。
- Rocket.Chat 引用、消息链接、线程回复会转换为 OneBot `reply` 语义，并补充引用上下文文本。
- 图片、普通文件、音频、视频附件会被识别并转换成对应的 OneBot 媒体段。
- 不支持直接桥接的媒体会降级为可读文本占位，避免整条消息消失。

### 出站能力

- OneBot `text` 直接发送为 Rocket.Chat 文本。
- OneBot `at` 会转换为 Rocket.Chat `@username` 或 `@all`。
- OneBot `reply` 会转换为 Rocket.Chat 消息链接引用格式。
- OneBot `image` 支持 HTTP(S) 链接、本地文件和 Base64 数据。
- OneBot `file`、`record`、`video` 支持本地文件；远端媒体会先尝试下载再上传。
- OneBot `markdown` 会按文本内容发往 Rocket.Chat。

### 上下文与映射

- Rocket.Chat 的房间 ID、用户 ID、消息 ID 会被桥接器映射为可持久化的 OneBot 数字 surrogate ID。
- 群聊上下文使用 `context_room_registry.json` 维持群上下文到真实房间的绑定关系。
- 私聊上下文使用 `PrivateRoomStore` 维护用户与私聊房间的绑定关系。
- 可选开启“子频道会话隔离”，把不同子房间拆成不同会话上下文。

---

## E2EE 支持

当前实现支持 Rocket.Chat 官方 E2EE 链路，覆盖：

- 加密私聊房间 `d`
- 加密私有群组 `p`
- 加密文本消息
- 加密图片、语音、视频、普通文件上传和下载

实现特征：

- 启用了 `e2ee_password` 后，桥接器会初始化本机密钥对并请求/同步房间密钥。
- 接收入站加密消息时，会自动解密再注入 AstrBot 事件流。
- 发送到加密房间时，会自动走加密消息体和加密媒体上传确认流程。
- 如果 E2EE 初始化失败，不会影响未加密房间的正常收发。

---

## 独立 WebUI

启用后会在本地启动一个独立 WebUI，默认监听 `127.0.0.1`，默认端口 `5751`。

### 页面能力

- `网络配置`：查看主 bot 状态、创建/编辑/删除副 bot。
- `基础信息`：查看每个 bot 的账号信息、OneBot self ID、Rocket.Chat 服务器品牌头像和服务器名称。
- `猫猫日志`：查看RocketCat运行日志，并支持清空日志。

### WebUI 认证

- 配置项 `webui_access_password` 留空时，不启用密码访问。
- 非空时，WebUI 会启用登录页与 Cookie 鉴权。
- `/api/status`、`/api/basic-info`、`/api/logs`、`/api/bots` 等接口都会受认证保护。

### WebUI 与副 bot 的关系

- 主 bot 不依赖独立 WebUI，也可以单独运行。
- 副 bot 仅在启用独立 WebUI 时运行。
- 副 bot 配置保存在 `data/plugin_data/astrbot_plugin_rocketchat_onebot_bridge/sub_bots.json`。
- 每个副 bot 都有自己的独立持久化状态目录。

---

## 环境要求

| 项目 | 要求 |
|------|------|
| AstrBot | `>= 3.4` |
| 支持平台声明 | `aiocqhttp` |
| 运行依赖 | `aiohttp`, `cryptography`, `fastapi`, `uvicorn` |
| Rocket.Chat | 需要可用的 REST API、DDP/WebSocket 和 E2EE 接口（如使用加密功能） |

---

## 安装

### 方式一：手动安装

将本仓库放入 AstrBot 插件目录：

```bash
cd data/plugins
git clone https://github.com/Creeper3222/astrbot_plugin_rocketchat_onebot_bridge
```

安装依赖：

```bash
pip install -r data/plugins/astrbot_plugin_rocketchat_onebot_bridge/requirements.txt
```

### 方式二：通过 AstrBot WebUI 安装

如果后续已经接入插件市场，也可以直接通过 AstrBot WebUI 安装本插件。

---

## 快速开始

首次安装后的初始状态是：桥接总开关关闭、主 bot 关闭、独立 WebUI 关闭、`plugin_data` 目录为空。也就是说，只有在你显式填写配置并开启相关开关后，桥接器才会真正开始连接 Rocket.Chat 和 AstrBot OneBot reverse WebSocket。

### 1. 先在 AstrBot 中创建内置 OneBot v11 平台

进入 AstrBot WebUI：

1. 打开 `机器人`
2. 点击 `+ 创建机器人`
3. 选择 `OneBot v11`
4. 填写以下关键项：

- `ID(id)`：任意，用于区分平台实例
- `启用(enable)`：勾选
- `反向 WebSocket 主机地址`：通常为 `0.0.0.0`
- `反向 WebSocket 端口`：默认 `6199`
- `反向 WebSocket Token`：如需鉴权则填写，桥接器侧要保持一致

AstrBot 官方文档中的默认反向 WS 入口为：

```text
ws://<your-host>:6199/ws
```

本地部署最常见的就是：

```text
ws://127.0.0.1:6199/ws
```

### 2. 配置桥接器主 bot

在插件配置页中，主配置的初始值和作用如下：

| 设置项 | 初始值 | 作用 |
|--------|--------|------|
| `启用桥接总开关` | `关闭` | 整个桥接器的总开关。关闭时，主 bot 和所有副 bot 都不会运行。 |
| `主bot启用` | `关闭` | 主 bot 的独立开关。只有总开关开启后它才有机会启动。 |
| `启用独立WebUI` | `关闭` | 控制独立管理界面是否启动。副 bot 的管理和运行依赖这个开关。 |
| `webui访问密码` | `空` | 给独立 WebUI 增加登录密码；留空表示不启用认证。 |
| `独立WebUI端口` | `5751` | 独立 WebUI 默认监听端口。 |
| `主bot Rocket.Chat 服务器地址` | `http://127.0.0.1:3000` | 主 bot 要连接的 Rocket.Chat 服务器地址。 |
| `主bot Rocket.Chat 用户名` | `空` | 主 bot 登录 Rocket.Chat 使用的用户名。 |
| `主bot Rocket.Chat 密码` | `空` | 主 bot 登录 Rocket.Chat 使用的密码。 |
| `主bot E2EE 密钥密码` | `空` | 加密私聊和加密私有群组要用到的 E2EE 私钥密码；留空表示不启用 E2EE。 |
| `主bot AstrBot OneBot reverse WS 地址` | `ws://127.0.0.1:6199/ws/` | 主 bot 作为 OneBot 客户端要主动连接的 AstrBot 反向 WebSocket 地址。 |
| `主bot OneBot Access Token` | `空` | 与 AstrBot OneBot v11 平台的 `ws_reverse_token` 保持一致。 |
| `主bot OneBot self_id` | `910001` | 主 bot 对外暴露给 AstrBot / OneBot 语义层使用的机器人 ID。 |
| `重连延迟(秒)` | `5` | Rocket.Chat 或 OneBot reverse WS 断开后，下一次重连前的等待时间。 |
| `最大连续重连次数` | `10` | 连续失败达到该次数后自动停用对应 bot；如果你想无限重连，可以改成 `0`。 |
| `启用子频道会话隔离` | `开启` | 让不同子频道或子房间使用各自独立的会话上下文，减少串话。 |
| `远程媒体大小上限(字节)` | `20971520` | 限制桥接器从远端拉取媒体时允许下载的最大文件大小。 |
| `忽略机器人自己的消息` | `开启` | 跳过当前 Rocket.Chat 机器人账号自己发出的消息，避免桥接回环。 |
| `调试日志` | `关闭` | 开启后输出更详细的桥接内部日志，适合排障，不建议长期常开。 |
| `LLM思考时贴表情ID` | `:heart:` | 配合 `astrbot_plugin_iamthinking`，在 LLM 处理中给消息贴上的 Rocket.Chat 表情短码。 |
| `LLM应答完成时贴表情ID` | `:sunny:` | 配合 `astrbot_plugin_iamthinking`，在 LLM 完成应答后给消息贴上的 Rocket.Chat 表情短码。 |

建议的启用顺序是：

1. 先填好 `主bot Rocket.Chat 服务器地址`、`用户名`、`密码`。
2. 再把 `主bot AstrBot OneBot reverse WS 地址`、`OneBot Access Token`、`OneBot self_id` 与 AstrBot 自带 OneBot v11 平台对齐。
3. 如果需要加密私聊或加密私有群组，再填写 `主bot E2EE 密钥密码`。
4. 最后再打开 `启用桥接总开关` 和 `主bot启用`。

### 3. 如需多 bot，再启用独立 WebUI

如果你只需要一个主 bot，这一步可以跳过。

如果你要在 WebUI 中创建副 bot，请先启用：

- `enable_independent_webui`
- 按需设置 `webui_access_password`
- 设置 `independent_webui_port`

然后打开：

```text
http://127.0.0.1:5751/
```

在 WebUI 中创建副 bot，每个副 bot 都需要配置独立的：

| 设置项 | 初始值 | 作用 |
|--------|--------|------|
| `副bot名称` | `sub_bot` | 这个副 bot 在 WebUI 和运行日志中显示的名称。 |
| `启用该副bot` | `关闭` | 控制这只副 bot 是否真正参与桥接。 |
| `Rocket.Chat 服务器地址` | `空` | 这只副 bot 要连接的 Rocket.Chat 服务器地址。 |
| `Rocket.Chat 用户名` | `空` | 这只副 bot 登录 Rocket.Chat 的用户名。 |
| `Rocket.Chat 密码` | `空` | 这只副 bot 登录 Rocket.Chat 的密码。 |
| `E2EE 密钥密码` | `空` | 这只副 bot 的加密房间私钥密码；不需要 E2EE 时保持为空。 |
| `AstrBot OneBot reverse WS 地址` | `ws://127.0.0.1:6200/ws/` | 这只副 bot 要主动连接的 AstrBot OneBot 反向 WebSocket 地址。 |
| `OneBot Access Token` | `空` | 与对应 AstrBot OneBot v11 平台实例的 `ws_reverse_token` 保持一致。 |
| `OneBot self_id` | `实时获取下一个可用 ID` | WebUI 会根据当前主 bot 和已有副 bot 的占用情况自动建议一个不重复的 self ID。 |
| `重连延迟(秒)` | `5` | 这只副 bot 的断线重连等待时间。 |
| `最大连续重连次数` | `10` | 这只副 bot 连续失败达到该次数后会被自动停用；设置为 `0` 表示不限次数。 |
| `远程媒体大小上限(字节)` | `20971520` | 这只副 bot 从远端下载媒体时允许的最大文件大小。 |
| `子频道会话隔离` | `开启` | 让这只副 bot 的不同子频道使用独立上下文。 |
| `忽略机器人自己的消息` | `开启` | 避免这只副 bot 处理自己刚发出的 Rocket.Chat 消息。 |
| `调试日志` | `关闭` | 控制这只副 bot 是否输出更详细的调试日志。 |

“猫猫日志”页的 `自动滚动已开启` 也是默认开启的，适合在调试连接阶段持续盯住最新日志输出。

注意：所有 bot 的 `onebot_self_id` 必须唯一，桥接器会自动校验冲突。

### 4. 验证连接

当两端都配置正确时，你应能在日志中看到：

- Rocket.Chat 登录成功
- Rocket.Chat WebSocket 就绪
- OneBot reverse WebSocket 已连接 AstrBot

如果配置了最大重连次数且持续失败，对应 bot 会被自动关闭，需要在配置页重新启用。

---

## 配置项说明

### 全局控制配置

| 配置项 | 说明 |
|--------|------|
| `enabled` | 桥接总开关。关闭后所有 bot 都不会运行。 |
| `main_bot_enabled` | 是否启用主 bot。 |
| `enable_independent_webui` | 是否启用独立 WebUI。副 bot 依赖这个开关。 |
| `webui_access_password` | 独立 WebUI 访问密码。留空表示不启用认证。 |
| `independent_webui_port` | 独立 WebUI 请求端口。端口被占用时会自动回退到可用端口。 |
| `llm_thinking_reaction` | LLM 思考中反应的 Rocket.Chat shortcode，默认 `:heart:`。 |
| `llm_done_reaction` | LLM 完成后反应的 Rocket.Chat shortcode，默认 `:sunny:`。 |

### 单个 bot 运行配置

| 配置项 | 说明 |
|--------|------|
| `server_url` | Rocket.Chat 服务器地址，需带 `http://` 或 `https://`。 |
| `username` | Rocket.Chat 机器人用户名。 |
| `password` | Rocket.Chat 机器人密码。 |
| `e2ee_password` | E2EE 私钥密码；只有需要加密房间时才填写。 |
| `onebot_ws_url` | AstrBot OneBot v11 反向 WebSocket 地址。 |
| `onebot_access_token` | AstrBot OneBot v11 反向 WebSocket Token。 |
| `onebot_self_id` | 当前 bot 对应的 OneBot 机器人 ID，必须为正整数且不能与其他 bot 冲突。 |
| `reconnect_delay` | 断线重连等待秒数。 |
| `max_reconnect_attempts` | 最大重连次数，默认 `10`；设置为 `0` 表示不限次数。 |
| `enable_subchannel_session_isolation` | 是否按子频道/子房间隔离会话上下文，默认开启。 |
| `remote_media_max_size` | 远端媒体下载大小上限，默认 20 MiB。 |
| `skip_own_messages` | 是否跳过自己发出的 Rocket.Chat 消息，避免回环。 |
| `debug` | 是否启用调试模式。 |

---

## 与 `astrbot_plugin_iamthinking` 的协同

本桥接器已经对 `set_msg_emoji_like` 做了适配，专门处理 `astrbot_plugin_iamthinking` 常见的数字 QQ 表情 ID。

当前策略是：

- 不维护完整 QQ 表情到 Rocket.Chat 表情的全量映射表。
- 将“思考中”和“完成后”两个阶段归一为可配置的 Rocket.Chat reaction shortcode。
- 默认值分别为：
  - 思考中：`:heart:`
  - 完成后：`:sunny:`

如果你有自己的 Rocket.Chat 表情体系，可以直接改成任意合法 shortcode，例如 `:thinking_face:`、`:eyes:`、`:white_check_mark:`。

---

## 持久化目录

本插件使用 AstrBot 标准 `plugin_data` 目录作为持久化数据存储目录。

默认持久化数据保存目录：

```text
data/plugin_data/astrbot_plugin_rocketchat_onebot_bridge/
```

其中典型文件包括：

- `id_map.json`：Rocket.Chat ID 和 OneBot surrogate ID 映射
- `message_registry.json`：消息记录和消息来源映射
- `context_room_registry.json`：群上下文到真实 Rocket.Chat 房间的绑定
- `runtime_state.json`：运行态持久化信息
- `sub_bots.json`：副 bot 配置清单
- `sub_bots/<bot_id>/`：副 bot 各自独立的数据目录

这保证了重启后消息 ID、群上下文和副 bot 配置不会丢失。

---

## 已知限制

- 当前不是一个独立 Rocket.Chat 平台适配器，而是依赖 AstrBot 自带 `aiocqhttp` 的桥接器。
- 合并转发消息当前未实现。
- 系统事件、审计事件、编辑/撤回/已读等非消息类事件不在这一版的桥接承诺范围内。
- E2EE 仅覆盖 Rocket.Chat 加密私聊和加密私有群组。
- 远端媒体如果下载失败、超出大小限制或源地址不可用，相关媒体发送会失败或降级。
- 副 bot 依赖独立 WebUI 开关，关闭 WebUI 时不会启动副 bot。

---

## 致谢

- 基础实现参考：[NET-Homeless/astrbot_plugin_rocket_chat_adapter](https://github.com/NET-Homeless/astrbot_plugin_rocket_chat_adapter) `v0.5.3`
- OneBot v11 接入能力由 AstrBot 内置 `aiocqhttp` 平台提供
- Rocket.Chat 桥接、E2EE、独立 WebUI、多 bot 管理由当前项目在参考实现基础上继续重构和扩展完成
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 强大的 AI 聊天机器人框架
- [Rocket.Chat](https://rocket.chat) — 开源团队协作平台
- [aiohttp](https://github.com/aio-libs/aiohttp) — Python 异步 HTTP 客户端