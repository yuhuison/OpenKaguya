# wechat-v864 Adapter 接入协议文档

本文档描述如何通过 wechat-v864 代理服务的 HTTP / WebSocket 接口收发微信消息。

Adapter 需要管理以下配置:
- `BASE_URL` — 代理服务地址 (如 `http://127.0.0.1:8099`)
- `API_KEY` — 授权令牌 (通过管理接口生成)

所有 HTTP 接口均以 `?key={API_KEY}` 传递认证。

---

## 一、接收消息 (三种方式)

### 方式 1: WebSocket (推荐，跨机器友好)

连接地址:
```
ws://{BASE_URL}/ws/GetSyncMsg?key={API_KEY}
```

连接后服务端会实时推送 JSON 文本帧，每条消息一帧。需要 `setting.json` 中 `wsSyncMsg: true`。

**Python 示例:**
```python
import websocket
import json

def on_message(ws, message):
    msg = json.loads(message)
    print(f"收到消息: {msg}")

ws = websocket.WebSocketApp(
    f"ws://{BASE_URL}/ws/GetSyncMsg?key={API_KEY}",
    on_message=on_message
)
ws.run_forever(reconnect=5)
```

收到的消息为 protobuf `AddMsg` 的 JSON 序列化，关键字段:

```json
{
  "fromUserName": {"str": "wxid_发送者"},
  "toUserName":   {"str": "wxid_接收者"},
  "msgType":      1,
  "content":      {"str": "消息内容"},
  "pushContent":  "昵称: 消息摘要",
  "newMsgId":     7755184359338209095,
  "msgId":        1217115945,
  "createTime":   1771750000
}
```

> **注意**: WebSocket 推送的格式是原始 protobuf 结构 (camelCase 字段名，嵌套 `{str: ""}` 对象)，与 HTTP 回调格式不同。

---

### 方式 2: HTTP 回调

代理主动 POST 消息到你指定的 URL。需要 `setting.json` 中 `httpSyncMsg: true`。

**注册回调地址:**
```
POST {BASE_URL}/forward/SetForward?key={API_KEY}
Content-Type: application/json

{"url": "http://你的机器人地址:端口/路径"}
```

**回调数据格式 (代理 POST 到你的 URL):**
```json
{
  "msgType":      1,
  "msgContent":   "你好",
  "FromUserName": "wxid_发送者",
  "ToUserName":   "wxid_接收者",
  "pushContent":  "昵称: 你好",
  "beAtUser":     "",
  "msg_id":       1217115945,
  "new_msg_id":   7755184359338209095
}
```

你的回调接口必须返回 `HTTP 200`，连续 3 次失败会自动停止转发。

**查询/删除回调地址:**
```
GET  {BASE_URL}/forward/GetForward?key={API_KEY}
POST {BASE_URL}/forward/SetForward?key={API_KEY}  body: {"url": ""}  # 删除
```

---

### 方式 3: HTTP 轮询

主动拉取待消费消息。适合简单场景或调试。

```
POST {BASE_URL}/message/HttpSyncMsg?key={API_KEY}
Content-Type: application/json

{"Count": 0}
```

`Count: 0` 表示拉取所有待消费消息。

---

## 二、消息类型 (msgType)

| 值 | 类型 | 说明 |
|---|---|---|
| 1 | 文本 | |
| 3 | 图片 | |
| 34 | 语音 | |
| 37 | 好友请求 | 需解析 XML 获取 v3/v4 |
| 42 | 名片 | |
| 43 | 视频 | |
| 47 | 表情包 | |
| 48 | 位置 | |
| 49 | 链接/文件/引用/小程序 | 需解析 XML 中的 `<type>` 子标签 |
| 51 | 状态通知 | 一般忽略 |
| 10000 | 系统消息 | 入群/红包领取提示等 |
| 10002 | 撤回消息 | |

---

## 三、群消息判断

```
FromUserName 以 "@chatroom" 结尾 → 群里别人发的
ToUserName   以 "@chatroom" 结尾 → 你在群里发的

群消息的 msgContent 格式: "发送者wxid:\n实际内容"
需要自行分割第一个 ":\n" 来提取真实发送者和内容
```

---

## 四、发送消息

### 发送文本
```
POST {BASE_URL}/message/SendTextMessage?key={API_KEY}
Content-Type: application/json
```
```json
{
  "MsgItem": [{
    "ToUserName":  "wxid_xxx 或 xxx@chatroom",
    "TextContent": "消息内容",
    "MsgType":     1,
    "AtWxIDList":  []
  }]
}
```

群里 @ 某人时，`TextContent` 中写 `@昵称`，`AtWxIDList` 填对应 wxid。

### 发送图片
```
POST {BASE_URL}/message/SendImageMessage?key={API_KEY}
```
```json
{
  "MsgItem": [{
    "ToUserName":   "wxid_xxx",
    "ImageContent": "图片base64",
    "MsgType":      2,
    "AtWxIDList":   []
  }]
}
```

### 发送语音
```
POST {BASE_URL}/message/SendVoice?key={API_KEY}
```
```json
{
  "ToUserName":  "wxid_xxx",
  "VoiceData":   "语音base64",
  "VoiceSecond": 5,
  "VoiceFormat": 0
}
```

### 发送名片
```
POST {BASE_URL}/message/ShareCardMessage?key={API_KEY}
```
```json
{
  "ToUserName":   "wxid_接收者",
  "CardWxId":     "wxid_名片用户",
  "CardNickName": "昵称",
  "CardAlias":    "",
  "CardFlag":     0
}
```

### 发送链接/卡片消息
```
POST {BASE_URL}/message/SendAppMessage?key={API_KEY}
```
```json
{
  "AppList": [{
    "ToUserName":  "wxid_接收者",
    "ContentXML":  "<appmsg>...</appmsg>",
    "ContentType": 0
  }]
}
```

### 撤回消息
```
POST {BASE_URL}/message/RevokeMsg?key={API_KEY}
```
```json
{
  "NewMsgId":    "消息的 new_msg_id (字符串)",
  "ClientMsgId": 0,
  "CreateTime":  0,
  "ToUserName":  "wxid_接收者"
}
```

### 群发文本
```
POST {BASE_URL}/message/GroupMassMsgText?key={API_KEY}
```
```json
{
  "ToUserName": ["wxid_1", "wxid_2", "xxx@chatroom"],
  "Content": "群发内容"
}
```

---

## 五、联系人与群操作

### 获取联系人列表
```
POST {BASE_URL}/friend/GetContactList?key={API_KEY}
body: {"CurrentWxcontactSeq": 0, "CurrentChatRoomContactSeq": 0}
```

### 获取联系人/群详情
```
POST {BASE_URL}/friend/GetContactDetailsList?key={API_KEY}
body: {"UserNames": ["wxid_xxx", "xxx@chatroom"], "RoomWxIDList": []}
```

### 获取群列表
```
GET {BASE_URL}/friend/GroupList?key={API_KEY}
```

### 获取群成员
```
POST {BASE_URL}/group/GetChatroomMemberDetail?key={API_KEY}
body: {"ChatRoomName": "xxx@chatroom"}
```

### 同意好友请求
收到 `msgType: 37` 时，解析 XML 获取 `v3`、`v4`、`scene`:
```
POST {BASE_URL}/friend/VerifyUser?key={API_KEY}
body: {"OpCode": 3, "V3": "...", "V4": "...", "Scene": 30, "VerifyContent": ""}
```

### 搜索联系人
```
POST {BASE_URL}/friend/SearchContact?key={API_KEY}
body: {"OpCode": 0, "UserName": "微信号或手机号", "FromScene": 3, "SearchScene": 1}
```

---

## 六、状态检查

```
GET {BASE_URL}/login/CheckLoginStatus?key={API_KEY}   # 登录状态
GET {BASE_URL}/login/GetLoginStatus?key={API_KEY}      # 详细在线信息
GET {BASE_URL}/forward/GetForward?key={API_KEY}        # 当前回调地址
```

---

## 七、通用返回格式

```json
{"Code": 200, "Data": {}, "Text": "描述", "Data62": ""}
```

| Code | 含义 |
|---|---|
| 200 | 成功 |
| 300 | 失败 |

---

## 八、完整 Swagger 文档

```
{BASE_URL}/docs
```
