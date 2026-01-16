# Rust SDK vs Python SDK 对比和建议

## 核心差异对比

### 1. 并行订阅架构

#### Rust SDK (`drift-rs`)
```
每个Oracle → 独立的WebSocket连接 → 独立的tokio任务
├── Oracle A → WS连接1 → Task 1
├── Oracle B → WS连接2 → Task 2
└── Oracle C → WS连接3 → Task 3

使用 FuturesUnordered 并发发起订阅
每个订阅是独立的异步任务，真正并行运行
```

**优点**：
- ✅ 真正的并行，无匹配问题
- ✅ 每个oracle独立重连，互不影响
- ✅ 天然支持不同oracle使用不同解码器

**缺点**：
- ❌ 连接数多（每个oracle一个连接）
- ❌ 资源消耗较大

#### Python SDK (当前实现)
```
所有Oracle → 单个WebSocket连接 → 单个任务
├── Oracle A ┐
├── Oracle B ├→ WS连接1 → Task 1
└── Oracle C ┘

使用 for 循环快速发送多个订阅请求
在一个循环中接收所有消息
```

**优点**：
- ✅ 连接数少（只有一个连接）
- ✅ 资源消耗小

**缺点**：
- ❌ 需要处理请求-响应匹配问题
- ❌ 当前实现有BUG（假设顺序匹配）
- ❌ 一个连接断开影响所有oracle

## 2. 请求-响应匹配机制

### Rust SDK

```rust
// 维护映射
inflight_subscribes: HashMap<request_id, (operation, payload, pubkey, sender)>
request_id_to_sid: HashMap<request_id, sid>
subscriptions: HashMap<sid, SubscriptionInfo>

// 发送请求
let request_id = next_id();
inflight_subscribes.insert(request_id, (operation, pubkey, ...));
send_json({"id": request_id, "method": "accountSubscribe", ...});

// 收到响应
let response_id = msg.id;  // 从响应中获取id
let (operation, pubkey, ...) = inflight_subscribes.remove(response_id);
// ✅ 准确匹配！
```

### Python SDK (当前实现)

```python
# 维护映射（有问题）
pending_subscriptions: List[Pubkey]  # 只有顺序列表

# 发送请求
self.pending_subscriptions.append(pubkey)
await self.ws.account_subscribe(pubkey, ...)
# ⚠️ 无法获取request_id

# 收到响应
pubkey = self.pending_subscriptions.pop(0)  # ⚠️ 假设顺序
# ❌ 如果RPC乱序返回，匹配错误
```

## 建议方案

### 方案A: 学习Rust，改为每个Oracle一个连接（推荐）

**实现方式**：

```python
class WebsocketDriftClientAccountSubscriber:
    def __init__(self, ...):
        # 改为：每个oracle一个独立的订阅器
        self.oracle_subscribers: Dict[str, WebsocketAccountSubscriber] = {}
        # 不再使用 WebsocketMultiAccountSubscriber
    
    async def subscribe_to_oracle(self, full_oracle_wrapper):
        oracle_id = get_oracle_id(full_oracle_wrapper.pubkey, ...)
        
        if oracle_id in self.oracle_subscribers:
            return
        
        # 为每个oracle创建独立的订阅器
        subscriber = WebsocketAccountSubscriber(
            full_oracle_wrapper.pubkey,
            self.program,
            self.commitment,
            decode=get_oracle_decode_fn(full_oracle_wrapper.oracle_source),
            initial_data=full_oracle_wrapper.oracle_price_data_and_slot,
        )
        
        self.oracle_subscribers[oracle_id] = subscriber
        await subscriber.subscribe()  # 每个oracle独立连接和任务
```

**优点**：
- ✅ 完全解决匹配问题（每个连接只订阅一个账户）
- ✅ 解决BUG1（同一pubkey不同解码器，用不同的订阅器）
- ✅ 解决BUG2（不需要匹配，每个连接独立）
- ✅ 每个oracle独立重连，互不影响
- ✅ 代码简单，逻辑清晰

**缺点**：
- ❌ 连接数多（如果有50个oracle，就有50个连接）
- ❌ 资源消耗较大

**适用场景**：
- Oracle数量不是特别多（<100个）
- 需要高可靠性
- 可以接受更多连接数

### 方案B: 保持单连接，但实现正确的request_id匹配

**实现方式**：

```python
import itertools

class WebsocketMultiAccountSubscriber:
    def __init__(self, ...):
        self.request_id_counter = itertools.count(1)
        self.pending_requests: Dict[int, Pubkey] = {}  # request_id -> pubkey
        self.pending_requests_by_pubkey: Dict[Pubkey, int] = {}  # pubkey -> request_id
    
    async def add_account(self, pubkey, decode_fn, oracle_id=None):
        # 生成request_id
        request_id = next(self.request_id_counter)
        
        # 记录映射
        self.pending_requests[request_id] = pubkey
        if oracle_id:
            self.pending_requests_by_pubkey[pubkey] = request_id
        
        # 发送请求（需要检查solana-py是否支持）
        # 方案B1: 如果account_subscribe支持自定义id
        await self.ws.account_subscribe(
            pubkey,
            commitment=self.commitment,
            encoding="base64",
            request_id=request_id  # 如果支持
        )
        
        # 方案B2: 如果支持直接发送JSON
        # await self._send_json_rpc("accountSubscribe", [str(pubkey), ...], request_id)
    
    async def _subscribe_ws(self):
        async for msg in ws:
            if isinstance(result, int):  # 订阅确认
                if hasattr(msg[0], 'id') and msg[0].id is not None:
                    request_id = msg[0].id
                    pubkey = self.pending_requests.get(request_id)
                    if pubkey:
                        # ✅ 准确匹配！
                        subscription_id = result
                        self.subscription_map[subscription_id] = pubkey
                        del self.pending_requests[request_id]
```

**关键挑战**：
- ❓ `solana-py`是否支持自定义`request_id`？
- ❓ 是否可以直接发送JSON-RPC消息？
- ❓ 响应消息是否包含`id`字段？

**优点**：
- ✅ 保持单连接，资源消耗小
- ✅ 如果实现正确，可以准确匹配

**缺点**：
- ❌ 需要修改`solana-py`的调用方式
- ❌ 实现复杂
- ❌ 如果`solana-py`不支持，无法实现

### 方案C: 混合方案 - 按pubkey分组，同pubkey串行，不同pubkey并行

**实现方式**：

```python
class WebsocketMultiAccountSubscriber:
    def __init__(self, ...):
        self.pubkey_locks: Dict[Pubkey, asyncio.Lock] = {}
    
    async def add_account(self, pubkey, decode_fn, oracle_id=None):
        # 为每个pubkey创建锁
        if pubkey not in self.pubkey_locks:
            self.pubkey_locks[pubkey] = asyncio.Lock()
        
        async with self.pubkey_locks[pubkey]:
            # 同一pubkey的订阅串行化
            if pubkey in self.pubkey_to_subscription:
                # 已订阅，复用subscription_id
                subscription_id = self.pubkey_to_subscription[pubkey]
                if oracle_id:
                    self.oracle_id_to_subscription[oracle_id] = subscription_id
                return
            
            # 发送订阅请求
            await self.ws.account_subscribe(pubkey, ...)
            # 等待确认（在_subscribe_ws中处理）
            self.pending_subscriptions.append((pubkey, oracle_id))
```

**优点**：
- ✅ 不同pubkey可以并行订阅
- ✅ 同一pubkey的多个订阅串行化，避免匹配问题
- ✅ 可以复用subscription_id（同一pubkey的多个订阅共享一个连接）

**缺点**：
- ❌ 同一pubkey的不同oracle_id需要串行订阅，有延迟
- ❌ 仍然需要处理不同pubkey的匹配问题

## 最终建议

### 推荐方案：方案A（每个Oracle一个连接）

**理由**：

1. **完全解决两个BUG**：
   - BUG1：每个oracle独立的订阅器，自然支持不同解码器
   - BUG2：每个连接只订阅一个账户，不需要匹配

2. **实现简单**：
   - 不需要修改`solana-py`的调用方式
   - 不需要处理复杂的匹配逻辑
   - 代码清晰，易于维护

3. **可靠性高**：
   - 每个oracle独立重连，互不影响
   - 一个oracle出问题不影响其他

4. **性能可接受**：
   - 即使有50个oracle，50个连接也是可接受的
   - 现代系统可以轻松处理
   - 相比匹配错误的代价，这点资源消耗是值得的

### 如果Oracle数量特别多（>100），考虑方案C

如果Oracle数量特别多，可以：
- 使用方案A，但限制并发连接数（如最多50个并发）
- 或者使用方案C，按pubkey分组

### 实施步骤

1. **修改`WebsocketDriftClientAccountSubscriber`**：
   - 移除`WebsocketMultiAccountSubscriber`
   - 改为使用`Dict[str, WebsocketAccountSubscriber]`存储每个oracle的订阅器

2. **修改`subscribe_to_oracle`方法**：
   - 为每个oracle创建独立的`WebsocketAccountSubscriber`
   - 使用`oracle_id`作为key

3. **修改`get_oracle_price_data_and_slot`方法**：
   - 直接从对应的订阅器获取数据

4. **测试**：
   - 测试同一pubkey不同oracle_source的订阅
   - 测试并发订阅
   - 测试重连恢复

## 总结

**Rust SDK的优势**：
- 每个oracle独立连接和任务，天然并行
- 使用`inflight_subscribes`映射，准确匹配请求-响应

**Python SDK的困境**：
- 单连接多订阅，需要处理匹配问题
- 当前实现有BUG，假设顺序匹配

**最佳解决方案**：
- 学习Rust SDK的架构，改为每个Oracle一个连接
- 简单、可靠、完全解决两个BUG
