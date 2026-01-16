# 正确的Rust SDK架构理解

## 核心架构（已纠正）

### 1. 单条WS连接 + 多路复用

```
所有Oracle → 单条WS连接 → 多路复用
├── Oracle A → accountSubscribe → subscription_id_1
├── Oracle B → accountSubscribe → subscription_id_2
└── Oracle C → accountSubscribe → subscription_id_3

所有订阅共享同一个 PubsubClient（单条WS连接）
每个oracle对应一个 subscription_id (sid)
```

**关键点**：
- ✅ 不是每个oracle一个连接
- ✅ 所有oracle共享单条WS连接
- ✅ 通过`subscription_id` (sid)区分不同的订阅

### 2. 并发订阅机制

```rust
// OracleMap::subscribe()
// 1. 去重（共享oracle只订阅一次）
// 2. 为每个oracle构造订阅future
// 3. 使用FuturesUnordered并发执行

FuturesUnordered::from_iter(subscribe_futures)
// 并发推进所有订阅请求
```

**关键点**：
- ✅ 逻辑上并行发起订阅（并发执行）
- ✅ 物理上复用同一条WS连接
- ✅ 使用`FuturesUnordered`并发drive订阅future

### 3. 请求-响应匹配

```rust
// PubsubClient::run_ws()

// 维护映射
inflight_subscribes: HashMap<request_id, (operation, payload, pubkey, sender)>

// 发送请求
let request_id = next_id();
inflight_subscribes.insert(request_id, (operation, pubkey, ...));
send_json({"id": request_id, "method": "accountSubscribe", ...});

// 收到响应
let response_id = msg.id;  // 从响应中获取id
let (operation, pubkey, ...) = inflight_subscribes.remove(response_id);
// ✅ 准确匹配！
```

**关键点**：
- ✅ 维护`inflight_subscribes`映射
- ✅ 使用JSON-RPC的`id`字段匹配请求-响应
- ✅ 完全控制`request_id`的生成和匹配

### 4. 数据更新路由

```rust
// 收到通知
{"method": "accountNotification", "params": {"result": ..., "subscription": 3114862}}

// 使用sid路由
let sid = msg.params.subscription;
let subscription_info = subscriptions.get(sid);
// 发送到对应的channel
```

**关键点**：
- ✅ 使用`params.subscription` (sid)路由数据更新
- ✅ 维护`subscriptions: HashMap<sid, SubscriptionInfo>`映射

## Python SDK的对应实现

### 当前架构（正确）

```python
# WebsocketMultiAccountSubscriber
# 单条WS连接，订阅多个账户
# ✅ 架构与Rust SDK一致
```

### 当前问题

1. **BUG1**: 使用`pubkey`作为key，不支持同一pubkey不同解码器
2. **BUG2**: 没有使用`request_id`匹配，假设顺序匹配

### 需要实现

1. **添加`inflight_subscribes`映射**:
```python
self.inflight_subscribes: Dict[int, Tuple[Pubkey, Optional[str]]] = {}
# request_id -> (pubkey, oracle_id)
```

2. **实现`request_id`匹配**:
```python
# 发送请求时
request_id = next(self.request_id_counter)
self.inflight_subscribes[request_id] = (pubkey, oracle_id)

# 收到响应时
if hasattr(msg[0], 'id'):
    request_id = msg[0].id
    pubkey, oracle_id = self.inflight_subscribes.pop(request_id)
    # ✅ 准确匹配！
```

3. **关键挑战**:
- ❓ `solana-py`是否支持获取或传入`request_id`？
- ❓ 是否可以直接构造JSON-RPC消息？

## 总结

**Rust SDK的正确架构**：
- ✅ 单条WS连接多路复用
- ✅ 并发发起订阅请求
- ✅ 使用`inflight_subscribes`匹配请求-响应
- ✅ 使用`subscription_id`路由数据更新

**Python SDK需要做的**：
- ✅ 保持单连接架构（已正确）
- ❌ 实现`inflight_subscribes`映射机制
- ❌ 解决如何获取或传入`request_id`的问题
