# Rust SDK实现完整流程图

## 完整订阅流程（从OracleMap到PubsubClient）

### 阶段1: OracleMap并发订阅多个Oracle

```
OracleMap::subscribe(markets)
│
├─ 步骤1: 去重和构造订阅器
│   ├─ 遍历markets
│   ├─ 检查是否已订阅（self.subscriptions.contains_key(oracle_pubkey)）
│   ├─ 为每个oracle创建WebsocketAccountSubscriber
│   └─ 所有订阅器共享同一个Arc<PubsubClient> ⭐
│
├─ 步骤2: 构造订阅future
│   ├─ 为每个订阅器构造async future
│   └─ future内部调用: sub_fut.subscribe(...).await
│       └─ 内部调用: pubsub.account_subscribe(...) ⭐
│
├─ 步骤3: 并发执行
│   ├─ FuturesUnordered::from_iter(futs_iter) ⭐
│   └─ while let Some(result) = subscription_futs.next().await
│       └─ 谁先完成先返回（真正并发）
│
└─ 结果: 所有订阅并发完成，共享单条WS连接
```

### 阶段2: WebsocketAccountSubscriber订阅单个账户

```
WebsocketAccountSubscriber::subscribe()
│
├─ 步骤1: 获取初始数据（可选）
│   └─ RPC.get_account_with_commitment() ⭐
│
├─ 步骤2: 启动独立任务
│   └─ tokio::spawn(async move { ⭐
│       │
│       ├─ loop {  // 重连循环
│       │   ├─ 调用pubsub.account_subscribe() ⭐
│       │   │   └─ 返回: (account_updates_stream, unsubscribe_fn)
│       │   │
│       │   └─ tokio::select! { ⭐
│       │       ├─ account_updates.next() => 处理更新
│       │       └─ unsub_rx => 取消订阅
│       │   }
│       │ }
│       │
│       └─ })
│
└─ 返回: unsub_tx (取消handle)
```

### 阶段3: PubsubClient的请求-响应匹配（核心）

```
PubsubClient::run_ws()  // 单条WS连接管理任务
│
├─ 初始化
│   ├─ request_id: u64 = 0  ⭐ 自己维护计数器
│   ├─ inflight_subscribes: BTreeMap<u64, (operation, payload, sender)>  ⭐
│   ├─ subscriptions: BTreeMap<u64, SubscriptionInfo>  // sid -> SubscriptionInfo
│   └─ request_id_to_sid: BTreeMap<u64, u64>  // request_id -> sid
│
├─ 重连循环 ('reconnect: loop)
│   ├─ connect_async()  // 建立WS连接
│   ├─ 重连后重新发送所有订阅（使用保存的payload）
│   └─ 进入消息处理循环
│
└─ 消息处理循环 ('manager: loop)
    │
    ├─ tokio::select! {  ⭐ 并发处理多个事件
    │   │
    │   ├─ 事件1: 接收订阅请求 (subscribe_receiver.recv())
    │   │   ├─ request_id += 1  ⭐ 自增
    │   │   ├─ 构造JSON-RPC消息: {"jsonrpc":"2.0","id":request_id,"method":"accountSubscribe","params":...}  ⭐
    │   │   ├─ ws.send(Message::Text(text))  ⭐ 发送
    │   │   └─ inflight_subscribes.insert(request_id, (operation, text, response_sender))  ⭐ 记录映射
    │   │
    │   ├─ 事件2: 接收WS消息 (ws.next())
    │   │   │
    │   │   ├─ 情况A: 通知消息 (有params.subscription)
    │   │   │   ├─ sid = params.get("subscription").u64()  ⭐
    │   │   │   ├─ subscriptions.get(&sid)  ⭐ 查找映射
    │   │   │   └─ sub.sender.send(result)  ⭐ 路由到对应channel
    │   │   │
    │   │   └─ 情况B: 响应消息 (有id字段)
    │   │       ├─ id = gjson::get(text, "id").u64()  ⭐ 提取id
    │   │       ├─ inflight_subscribes.remove(&id)  ⭐ 查找映射
    │   │       ├─ sid = gjson::get(text, "result").u64()  ⭐ 提取subscription_id
    │   │       ├─ request_id_to_sid.insert(id, sid)  ⭐ 记录映射
    │   │       ├─ subscriptions.insert(sid, SubscriptionInfo {...})  ⭐
    │   │       └─ response_sender.send(Ok((notifications_receiver, unsubscribe)))  ⭐ 通知调用者
    │   │
    │   ├─ 事件3: 取消订阅 (unsubscribe_receiver.recv())
    │   │   └─ 发送accountUnsubscribe消息
    │   │
    │   └─ 事件4: 心跳/超时检查
    │       └─ 发送Ping或检测超时
    │
    └─ }
```

## 关键数据流

### 订阅请求流程

```
调用者
  │
  ├─> pubsub.account_subscribe(pubkey, config)
  │     │
  │     ├─> subscribe("account", params)
  │     │     │
  │     │     ├─> subscribe_sender.send((operation, params, response_sender))
  │     │     │     │
  │     │     │     └─> run_ws()的subscribe_receiver接收
  │     │     │           │
  │     │     │           ├─> request_id += 1  ⭐
  │     │     │           ├─> 构造JSON: {"id":request_id,"method":"accountSubscribe",...}  ⭐
  │     │     │           ├─> ws.send(Message::Text(json))  ⭐
  │     │     │           └─> inflight_subscribes.insert(request_id, ...)  ⭐
  │     │     │
  │     │     └─> response_receiver.await  // 等待响应
  │     │
  │     └─> 返回: (notifications_stream, unsubscribe_fn)
  │
  └─> 使用notifications_stream接收更新
```

### 订阅确认流程

```
RPC服务器
  │
  └─> 发送响应: {"jsonrpc":"2.0","result":5308752,"id":1}
        │
        └─> run_ws()接收
              │
              ├─> id = gjson::get(text, "id").u64()  ⭐ 提取id=1
              ├─> inflight_subscribes.remove(&1)  ⭐ 查找映射
              │     └─> 找到: (operation="account", payload="...", response_sender)
              │
              ├─> sid = gjson::get(text, "result").u64()  ⭐ 提取sid=5308752
              ├─> request_id_to_sid.insert(1, 5308752)  ⭐
              ├─> subscriptions.insert(5308752, SubscriptionInfo {...})  ⭐
              │
              └─> response_sender.send(Ok((notifications_receiver, unsubscribe)))  ⭐
                    │
                    └─> account_subscribe()的response_receiver收到
                          │
                          └─> 返回给调用者
```

### 数据更新流程

```
RPC服务器
  │
  └─> 发送通知: {"method":"accountNotification","params":{"result":{...},"subscription":5308752}}
        │
        └─> run_ws()接收
              │
              ├─> sid = params.get("subscription").u64()  ⭐ 提取sid=5308752
              ├─> subscriptions.get(&5308752)  ⭐ 查找映射
              │     └─> 找到: SubscriptionInfo { sender, payload }
              │
              └─> sub.sender.send(result)  ⭐ 发送到channel
                    │
                    └─> notifications_stream接收
                          │
                          └─> 调用者的回调函数处理
```

## Python SDK的对应流程（当前实现）

### 阶段1: 发送订阅请求

```
WebsocketMultiAccountSubscriber::_subscribe_ws()
│
├─ for pubkey in initial_accounts:  ⚠️ 快速循环（伪并发）
│   ├─ await ws.account_subscribe(pubkey, ...)  ⚠️ 不返回request_id
│   └─ pending_subscriptions.append(pubkey)  ⚠️ 只有顺序列表
│
└─ 问题: 无法记录request_id -> pubkey映射
```

### 阶段2: 接收订阅确认

```
async for msg in ws:
  │
  └─> if isinstance(result, int):  // 订阅确认
        │
        ├─> pubkey = pending_subscriptions.pop(0)  ⚠️ 假设顺序
        ├─> subscription_id = result
        └─> subscription_map[subscription_id] = pubkey  ⚠️ 可能错配
```

### 阶段3: 接收数据更新

```
async for msg in ws:
  │
  └─> if hasattr(result, "value"):  // 数据更新
        │
        ├─> subscription_id = msg[0].subscription  ✅ 正确
        ├─> pubkey = subscription_map[subscription_id]  ⚠️ 但映射可能错误
        ├─> decode_fn = decode_map.get(pubkey)  ⚠️ 使用错误的解码器
        └─> decoded_data = decode_fn(account_bytes)  ❌ 解码错误
```

## 关键差异总结

| 步骤 | Rust SDK | Python SDK |
|------|----------|------------|
| **1. 并发订阅** | `FuturesUnordered`真正并发 | `for`循环快速发送（伪并发） |
| **2. request_id管理** | 自己维护计数器 | 无法获取 |
| **3. 发送请求** | 直接构造JSON-RPC消息 | 使用`account_subscribe`方法 |
| **4. 记录映射** | `inflight_subscribes.insert(request_id, ...)` | 只有`pending_subscriptions`列表 |
| **5. 收到确认** | 使用`id`字段查找映射 | 使用`pop(0)`假设顺序 |
| **6. 匹配准确性** | ✅ 100%准确 | ❌ 可能错配 |

## Python修改方案的关键点

### 必须实现的功能

1. **维护`request_id`计数器**
   - 类似Rust的`request_id += 1`
   - 但需要检查`solana-py`是否支持

2. **记录`inflight_subscribes`映射**
   - `request_id -> (pubkey, oracle_id)`
   - 但需要能在发送时知道`request_id`

3. **使用响应消息的`id`字段匹配**
   - 检查`msg[0].id`
   - 使用`id`查找`inflight_subscribes`

### 关键挑战

- ❓ `solana-py`的`account_subscribe`是否返回`request_id`？
- ❓ `ws`对象是否支持直接发送JSON-RPC消息？
- ❓ 响应消息的`id`字段是否可以访问？

### 如果都不支持

使用备选方案：
- 为每个`pubkey`创建锁
- 同一`pubkey`的订阅串行化
- 不同`pubkey`仍可并发（但需要等待确认）
