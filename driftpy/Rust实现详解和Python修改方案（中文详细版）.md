# Rust SDK实现详解和Python修改方案（中文详细版）

## 第一部分：Rust SDK的完整实现机制详解

### 1. 整体架构

Rust SDK使用**单条WebSocket连接 + 多路复用**的架构：

```
所有Oracle订阅
    ↓
共享同一个 PubsubClient (单条WS连接)
    ↓
每个Oracle对应一个 subscription_id (sid)
    ↓
通过 sid 路由数据更新
```

### 2. 并发订阅机制详解

#### 2.1 OracleMap::subscribe() 的并发订阅流程

**代码位置**: `oraclemap.rs` L147-224

**步骤详解**:

**步骤1: 去重和构造订阅器** (L154-181)

```rust
// 遍历要订阅的市场
for market in markets {
    let (oracle_pubkey, _oracle_source) = self.oracle_by_market.get(market).expect("oracle exists");
    
    // ⭐ 关键：去重 - 共享oracle只订阅一次
    if self.subscriptions.contains_key(oracle_pubkey) {
        continue;  // 已订阅，跳过
    }
    
    // ⭐ 关键：所有订阅器共享同一个pubsub（单条WS连接）
    let oracle_subscriber = WebsocketAccountSubscriber::new(
        Arc::clone(&self.pubsub),  // 共享同一个PubsubClient
        *oracle_pubkey,
        self.commitment,
    );
    
    pending_subscriptions.push(oracle_subscriber);
}
```

**关键点**：
- ✅ 共享oracle只订阅一次（去重）
- ✅ 所有订阅器共享同一个`Arc<PubsubClient>`（单条WS连接）

**步骤2: 构造并发future** (L183-210)

```rust
// 为每个订阅器构造async future
let futs_iter = pending_subscriptions.into_iter().map(|sub_fut| {
    // 捕获需要的上下文
    let oraclemap = Arc::clone(&self.oraclemap);
    let oracle_shared_mode = self.shared_oracles.get(&sub_fut.pubkey).expect("oracle exists").clone();
    
    // ⭐ 返回async future
    async move {
        // ⭐ 调用subscribe，内部会调用pubsub.account_subscribe()
        let unsub = sub_fut.subscribe(Self::SUBSCRIPTION_ID, true, move |update| {
            // 处理更新...
        }).await;
        
        ((sub_fut.pubkey, oracle_share_mode), unsub)
    }
});
```

**关键点**：
- ✅ 每个future内部调用`pubsub.account_subscribe()`（共享同一个pubsub）
- ✅ future是异步的，可以并发执行

**步骤3: 并发执行** (L212-220)

```rust
// ⭐ 使用FuturesUnordered并发执行所有future
let mut subscription_futs = FuturesUnordered::from_iter(futs_iter);

// ⭐ 并发等待所有订阅完成（谁先完成先返回）
while let Some(((pubkey, oracle_share_mode), unsub)) = subscription_futs.next().await {
    // 处理完成的订阅
    self.subscriptions.insert(pubkey, unsub?);
}
```

**关键点**：
- ✅ `FuturesUnordered`真正并发执行所有future
- ✅ 谁先完成先返回，不按顺序
- ✅ 所有订阅共享单条WS连接

### 3. PubsubClient的请求-响应匹配机制（核心）

#### 3.1 架构设计

**代码位置**: `pubsub-client/lib.rs` L334-349

```rust
async fn run_ws(...) -> PubsubClientResult {
    // ⭐ 关键1: 自己维护request_id计数器
    let mut request_id: u64 = 0;
    
    // ⭐ 关键2: 维护订阅映射
    let mut subscriptions = BTreeMap::<u64, SubscriptionInfo>::new();  // sid -> SubscriptionInfo
    let mut request_id_to_sid = BTreeMap::<u64, u64>::new();  // request_id -> sid
    
    'reconnect: loop {
        // 建立WS连接
        let mut ws = connect_async(url.as_str()).await?;
        
        // ⭐ 关键3: 每次重连时重新初始化inflight映射
        let mut inflight_subscribes = BTreeMap::<u64, (String, String, oneshot::Sender<SubscribeResponseMsg>)>::new();
        // request_id -> (operation, payload, response_sender)
        
        // 重连后重新发送所有订阅...
```

**关键点**：
- ✅ **完全控制`request_id`的生成**
- ✅ **维护`inflight_subscribes`映射**：`request_id -> (operation, payload, response_sender)`
- ✅ **维护`subscriptions`映射**：`sid -> SubscriptionInfo`
- ✅ **维护`request_id_to_sid`映射**：`request_id -> sid`

#### 3.2 发送订阅请求的完整流程

**代码位置**: `pubsub-client/lib.rs` L153-172, L577-586

**流程1: 调用者调用account_subscribe** (L183-190)

```rust
pub async fn account_subscribe(
    &self,
    pubkey: &Pubkey,
    config: Option<RpcAccountInfoConfig>,
) -> SubscribeResult<'_, RpcResponse<UiAccount>> {
    let params = json!([pubkey.to_string(), config]);
    // ⭐ 调用通用的subscribe方法
    self.subscribe("account", params).await
}
```

**流程2: subscribe方法发送请求到channel** (L153-172)

```rust
async fn subscribe<'a, T>(&self, operation: &str, params: Value) -> SubscribeResult<'a, T> {
    // ⭐ 创建oneshot channel用于接收响应
    let (response_sender, response_receiver) = oneshot::channel();
    
    // ⭐ 发送请求到subscribe_sender channel
    self.subscribe_sender.send((operation.to_string(), params.clone(), response_sender))?;
    
    // ⭐ 等待响应
    let (notifications, unsubscribe) = response_receiver.await??;
    
    Ok((notifications_stream, unsubscribe))
}
```

**流程3: run_ws接收请求并发送** (L577-586)

```rust
// 从channel接收订阅请求
subscribe = subscribe_receiver.recv() => {
    let (operation, params, response_sender) = subscribe.expect("subscribe channel");
    
    // ⭐ 关键1: 自增request_id（完全控制）
    request_id += 1;
    
    // ⭐ 关键2: 构造JSON-RPC消息（使用自己的request_id）
    let method = format!("{operation}Subscribe");
    let text = json!({
        "jsonrpc":"2.0",
        "id":request_id,  // ⭐ 使用自己的request_id
        "method":method,
        "params":params
    }).to_string();
    
    // ⭐ 关键3: 发送消息
    ws.send(Message::Text(text.clone().into())).await;
    
    // ⭐ 关键4: 记录到inflight_subscribes映射
    inflight_subscribes.insert(request_id, (operation, text, response_sender));
}
```

**关键点**：
- ✅ **完全控制`request_id`的生成**（`request_id += 1`）
- ✅ **直接构造JSON-RPC消息**（不依赖外部库）
- ✅ **发送前记录映射**（`inflight_subscribes.insert(request_id, ...)`）

#### 3.3 收到订阅确认的完整流程

**代码位置**: `pubsub-client/lib.rs` L470-543

**流程详解**:

```rust
// 收到响应：{"jsonrpc":"2.0","result":5308752,"id":1}
let id = gjson::get(text, "id");
if id.exists() {
    // ⭐ 关键1: 从响应中提取id字段
    let id = id.u64();  // id = 1
    
    // ⭐ 关键2: 查找inflight_subscribes映射
    if let Some((operation, payload, response_sender)) = inflight_subscribes.remove(&id) {
        // ⭐ 关键3: 准确匹配！
        // 这里找到了request_id=1对应的请求信息
        
        // ⭐ 关键4: 提取subscription_id (sid)
        let sid = gjson::get(text, "result").u64();  // sid = 5308752
        
        // ⭐ 关键5: 创建通知channel
        let (notifications_sender, notifications_receiver) = mpsc::unbounded_channel();
        
        // ⭐ 关键6: 发送响应给调用者
        response_sender.send(Ok((notifications_receiver, unsubscribe))).unwrap();
        
        // ⭐ 关键7: 记录映射
        request_id_to_sid.insert(id, sid);  // 1 -> 5308752
        subscriptions.insert(sid, SubscriptionInfo {
            sender: notifications_sender,
            payload,
        });  // 5308752 -> SubscriptionInfo
    }
}
```

**关键点**：
- ✅ **从响应消息中提取`id`字段**（`gjson::get(text, "id")`）
- ✅ **使用`id`查找`inflight_subscribes`映射**（`inflight_subscribes.remove(&id)`）
- ✅ **准确匹配请求和响应**（100%准确）
- ✅ **建立`request_id -> sid`和`sid -> SubscriptionInfo`映射**

#### 3.4 数据更新路由的完整流程

**代码位置**: `pubsub-client/lib.rs` L443-467

```rust
// 收到通知：{"method":"accountNotification","params":{"result":{...},"subscription":5308752}}
let params = gjson::get(text, "params");
if params.exists() {
    // ⭐ 关键1: 使用sid路由
    let sid = params.get("subscription").u64();  // sid = 5308752
    
    // ⭐ 关键2: 查找subscriptions映射
    if let Some(sub) = subscriptions.get(&sid) {
        // ⭐ 关键3: 发送到对应的channel
        let result = params.get("result");
        sub.sender.send(serde_json::from_str(result.json()).expect("valid json"));
    }
}
```

**关键点**：
- ✅ 使用`params.subscription` (sid)路由数据更新
- ✅ 维护`subscriptions`映射：`sid -> SubscriptionInfo`

### 4. 重连恢复机制

**代码位置**: `pubsub-client/lib.rs` L386-401

```rust
// 重连后重新发送所有订阅
if !subscriptions.is_empty() {
    // ⭐ 关键：保存了每个订阅的payload
    // subscriptions中存储了SubscriptionInfo { sender, payload }
    
    // 重新发送订阅请求
    ws.send_all(&mut stream::iter(
        subscriptions
            .values()
            .cloned()
            .map(|s| Ok(Message::text(s.payload))),  // ⭐ 使用保存的payload
    )).await;
}

// 收到重连后的确认时 (L545-568)
else if let Some(previous_sid) = request_id_to_sid.remove(&id) {
    // ⭐ 关键：找到旧的sid
    let new_sid = sid.u64();  // 新的subscription_id
    
    // ⭐ 更新映射：从old_sid改为new_sid
    let info = subscriptions.remove(&previous_sid).unwrap();
    subscriptions.insert(new_sid, info);
}
```

**关键点**：
- ✅ 保存每个订阅的`payload`（JSON-RPC消息）
- ✅ 重连后重新发送所有订阅
- ✅ 使用`request_id_to_sid`映射更新`sid`

## 第二部分：Python SDK的差异和问题

### 1. 并发订阅机制差异

#### Rust SDK
```rust
// 真正并发执行
let mut subscription_futs = FuturesUnordered::from_iter(futs_iter);
while let Some(result) = subscription_futs.next().await {
    // 处理结果
}
```

#### Python SDK (`multi_account_subscriber.py` L130-136)
```python
# 快速循环发送（伪并发）
for pubkey in initial_accounts:
    await ws.account_subscribe(pubkey, ...)
    # ⚠️ 不等待确认，继续发送下一个
```

**差异**：
- ❌ Python没有使用`asyncio.gather`真正并发
- ❌ Python没有等待确认，假设顺序匹配

### 2. 请求-响应匹配机制差异

#### Rust SDK的完整流程

```
1. 调用者: pubsub.account_subscribe(pubkey, config)
   ↓
2. subscribe方法: 发送请求到channel，等待响应
   ↓
3. run_ws接收: subscribe_receiver.recv()
   ↓
4. 生成request_id: request_id += 1  ⭐
   ↓
5. 构造JSON: {"id":request_id,"method":"accountSubscribe",...}  ⭐
   ↓
6. 发送: ws.send(Message::Text(json))  ⭐
   ↓
7. 记录映射: inflight_subscribes.insert(request_id, ...)  ⭐
   ↓
8. RPC返回: {"result":5308752,"id":1}
   ↓
9. 提取id: id = gjson::get(text, "id").u64()  ⭐
   ↓
10. 查找映射: inflight_subscribes.remove(&id)  ⭐
    ↓
11. 准确匹配！ ✅
```

#### Python SDK的当前流程

```
1. 调用者: await ws.account_subscribe(pubkey, ...)
   ↓
2. ⚠️ 无法获取request_id
   ↓
3. ⚠️ 无法记录映射
   ↓
4. 只能记录: pending_subscriptions.append(pubkey)  ⚠️
   ↓
5. RPC返回: {"result":5308752,"id":1}
   ↓
6. ⚠️ 没有检查id字段
   ↓
7. ⚠️ 假设顺序: pubkey = pending_subscriptions.pop(0)
   ↓
8. ❌ 可能错配
```

**关键差异**：

| 步骤 | Rust SDK | Python SDK |
|------|----------|------------|
| **request_id生成** | ✅ 自己维护计数器 | ❌ 无法获取 |
| **JSON构造** | ✅ 直接构造 | ❌ 使用库方法 |
| **映射记录** | ✅ `inflight_subscribes.insert(request_id, ...)` | ❌ 只有顺序列表 |
| **响应匹配** | ✅ 使用`id`字段查找映射 | ❌ 使用`pop(0)`假设顺序 |
| **匹配准确性** | ✅ 100%准确 | ❌ 可能错配 |

### 3. 数据更新路由差异

#### Rust SDK
```rust
// 使用sid路由
let sid = params.get("subscription").u64();
subscriptions.get(&sid);  // ✅ 准确路由
```

#### Python SDK
```python
# 使用subscription_id路由
subscription_id = msg[0].subscription  # ✅ 正确
pubkey = self.subscription_map[subscription_id]  # ⚠️ 但映射可能错误
decode_fn = self.decode_map.get(pubkey)  # ⚠️ 使用错误的解码器
```

**差异**：
- ✅ Python使用`subscription_id`路由（正确）
- ❌ 但`subscription_map`可能错误（因为确认匹配错误）
- ❌ 导致使用错误的解码器

## 第三部分：Python SDK的具体修改方案

### 修改方案1: BUG1修复 - 使用oracle_id作为内部key

#### 核心思路

将内部存储的key从`Pubkey`改为`oracle_id`（字符串），这样同一pubkey的不同`oracle_source`可以使用不同的key。

#### 具体修改

**文件**: `src/driftpy/accounts/ws/multi_account_subscriber.py`

**修改1: 初始化数据结构** (L26-33)

```python
# 原代码
self.decode_map: Dict[Pubkey, Callable[[bytes], Any]] = {}
self.data_map: Dict[Pubkey, Optional[DataAndSlot]] = {}
self.pending_subscriptions: list[Pubkey] = []

# 改为
self.decode_map: Dict[str, Callable[[bytes], Any]] = {}  # oracle_id -> decode_fn
self.data_map: Dict[str, Optional[DataAndSlot]] = {}  # oracle_id -> data
self.pubkey_to_oracle_ids: Dict[Pubkey, set[str]] = {}  # pubkey -> {oracle_id, ...}
self.oracle_id_to_subscription: Dict[str, int] = {}  # oracle_id -> subscription_id
self.pending_subscriptions: list[Tuple[Pubkey, Optional[str]]] = []  # (pubkey, oracle_id)
```

**修改2: add_account方法** (L35-86)

```python
async def add_account(
    self,
    pubkey: Pubkey,
    decode: Optional[Callable[[bytes], Any]] = None,
    initial_data: Optional[DataAndSlot] = None,
    oracle_id: Optional[str] = None,  # ⭐ 新增参数
):
    decode_fn = decode if decode is not None else self.program.coder.accounts.decode
    
    # ⭐ 确定使用的key
    key = oracle_id if oracle_id is not None else str(pubkey)
    
    async with self._lock:
        # ⭐ 检查是否已订阅（使用oracle_id或pubkey）
        if oracle_id and oracle_id in self.data_map:
            return
        if not oracle_id and pubkey in self.pubkey_to_subscription:
            return
        
        if key in self.data_map and initial_data is None:
            initial_data = self.data_map[key]
    
    if initial_data is None:
        try:
            initial_data = await get_account_data_and_slot(
                pubkey, self.program, self.commitment, decode_fn
            )
        except Exception as e:
            print(f"Error fetching initial data for {pubkey}: {e}")
            return
    
    async with self._lock:
        if oracle_id and oracle_id in self.data_map:
            return
        if not oracle_id and pubkey in self.pubkey_to_subscription:
            return
        
        # ⭐ 使用oracle_id或pubkey作为key
        self.decode_map[key] = decode_fn
        self.initial_data_map[key] = initial_data
        self.data_map[key] = initial_data
        
        # ⭐ 维护pubkey到oracle_id的映射
        if oracle_id:
            if pubkey not in self.pubkey_to_oracle_ids:
                self.pubkey_to_oracle_ids[pubkey] = set()
            self.pubkey_to_oracle_ids[pubkey].add(oracle_id)
    
    if self.ws is not None:
        try:
            async with self._lock:
                # ⭐ 记录(pubkey, oracle_id)元组
                self.pending_subscriptions.append((pubkey, oracle_id))
            
            await self.ws.account_subscribe(
                pubkey,
                commitment=self.commitment,
                encoding="base64",
            )
        except Exception as e:
            print(f"Error subscribing to account {pubkey}: {e}")
            async with self._lock:
                if self.pending_subscriptions and self.pending_subscriptions[-1] == (pubkey, oracle_id):
                    self.pending_subscriptions.pop()
                elif (pubkey, oracle_id) in self.pending_subscriptions:
                    self.pending_subscriptions.remove((pubkey, oracle_id))
```

**修改3: 确认处理逻辑** (L151-165)

```python
if isinstance(result, int):  # 订阅确认
    async with self._lock:
        subscription_id = result
        pubkey = None
        oracle_id = None
        
        # ⭐ 尝试使用id字段匹配（如果实现了inflight_subscribes）
        if hasattr(self, 'inflight_subscribes') and hasattr(msg[0], 'id') and msg[0].id is not None:
            request_id = msg[0].id
            if request_id in self.inflight_subscribes:
                pubkey, oracle_id = self.inflight_subscribes.pop(request_id)
                # ✅ 准确匹配！
        else:
            # 回退到顺序匹配（向后兼容）
            if self.pending_subscriptions:
                pubkey, oracle_id = self.pending_subscriptions.pop(0)
            else:
                print("No pending subscription")
                continue
        
        if pubkey:
            # 建立映射
            self.subscription_map[subscription_id] = pubkey
            self.pubkey_to_subscription[pubkey] = subscription_id
            
            # ⭐ 如果使用oracle_id，建立oracle_id到subscription_id的映射
            if oracle_id:
                self.oracle_id_to_subscription[oracle_id] = subscription_id
```

**修改4: 数据更新处理** (L167-193)

```python
if hasattr(result, "value") and result.value is not None:
    subscription_id = msg[0].subscription
    
    if subscription_id not in self.subscription_map:
        continue
    
    pubkey = self.subscription_map[subscription_id]
    
    # ⭐ 如果该pubkey有多个oracle_id，需要确定使用哪个
    oracle_ids = self.pubkey_to_oracle_ids.get(pubkey, set())
    
    if len(oracle_ids) == 1:
        # 只有一个oracle_id，直接使用
        oracle_id = list(oracle_ids)[0]
        key = oracle_id
    elif len(oracle_ids) > 1:
        # ⭐ 多个oracle_id：尝试所有解码器，选择能成功解码的
        key = None
        for oracle_id in oracle_ids:
            candidate_decode_fn = self.decode_map.get(oracle_id)
            if candidate_decode_fn:
                try:
                    # 尝试解码
                    test_decoded = candidate_decode_fn(account_bytes)
                    key = oracle_id
                    break
                except Exception:
                    continue
        if key is None:
            continue
    else:
        # 没有oracle_id，使用pubkey（向后兼容）
        key = str(pubkey)
    
    decode_fn = self.decode_map.get(key)
    if decode_fn is None:
        continue
    
    try:
        slot = int(result.context.slot)
        decoded_data = decode_fn(account_bytes)
        new_data = DataAndSlot(slot, decoded_data)
        self._update_data(key, new_data)  # ⭐ 使用key更新
    except Exception:
        continue
```

**修改5: get_data方法** (L224-225)

```python
def get_data(self, key: Union[Pubkey, str]) -> Optional[DataAndSlot]:
    """
    获取数据
    key可以是Pubkey（向后兼容）或oracle_id（新方式）
    """
    if isinstance(key, Pubkey):
        # 向后兼容：如果只有一个oracle_id，返回它；否则返回第一个
        oracle_ids = self.pubkey_to_oracle_ids.get(key, set())
        if len(oracle_ids) == 1:
            key = list(oracle_ids)[0]
        else:
            key = str(key)
    
    return self.data_map.get(key)
```

**文件**: `src/driftpy/accounts/ws/drift_client.py`

**修改1: subscribe_to_oracle方法** (L176-192)

```python
async def subscribe_to_oracle(self, full_oracle_wrapper: FullOracleWrapper):
    if full_oracle_wrapper.pubkey == Pubkey.default():
        return
    
    oracle_id = get_oracle_id(
        full_oracle_wrapper.pubkey,
        full_oracle_wrapper.oracle_source,
    )
    
    # ⭐ 使用oracle_id检查是否已订阅
    if oracle_id in self.oracle_subscriber.data_map:
        return
    
    # ⭐ 传递oracle_id参数
    await self.oracle_subscriber.add_account(
        full_oracle_wrapper.pubkey,
        get_oracle_decode_fn(full_oracle_wrapper.oracle_source),
        initial_data=full_oracle_wrapper.oracle_price_data_and_slot,
        oracle_id=oracle_id,  # ⭐ 新增参数
    )
    self.oracle_id_to_pubkey[oracle_id] = full_oracle_wrapper.pubkey
```

**修改2: get_oracle_price_data_and_slot方法** (L267-273)

```python
def get_oracle_price_data_and_slot(
    self, oracle_id: str
) -> Optional[DataAndSlot[OraclePriceData]]:
    # ⭐ 直接使用oracle_id获取数据
    return self.oracle_subscriber.get_data(oracle_id)
```

### 修改方案2: BUG2修复 - 实现request_id匹配机制

#### 核心思路

学习Rust SDK的方式，维护`inflight_subscribes`映射，使用响应消息的`id`字段准确匹配。

#### 实施步骤

**步骤1: 检查`solana-py`的能力**

创建测试脚本：

```python
# test_solana_py.py
import asyncio
from solana.rpc.websocket_api import connect, SolanaWsClientProtocol
from solders.pubkey import Pubkey

async def test():
    ws_url = "wss://api.mainnet-beta.solana.com"
    async for ws in connect(ws_url):
        ws = cast(SolanaWsClientProtocol, ws)
        
        # 测试1: account_subscribe返回值
        print("=== 测试1: account_subscribe返回值 ===")
        result = await ws.account_subscribe(
            Pubkey.default(),
            commitment="confirmed",
            encoding="base64",
        )
        print(f"返回类型: {type(result)}")
        print(f"返回值: {result}")
        print(f"属性: {dir(result)}")
        
        # 测试2: 响应消息结构
        print("\n=== 测试2: 响应消息结构 ===")
        msg = await ws.recv()
        print(f"消息类型: {type(msg)}")
        print(f"消息: {msg}")
        print(f"msg[0]类型: {type(msg[0])}")
        print(f"msg[0]属性: {dir(msg[0])}")
        print(f"是否有id: {hasattr(msg[0], 'id')}")
        if hasattr(msg[0], 'id'):
            print(f"id值: {msg[0].id}")
        
        # 测试3: ws对象方法
        print("\n=== 测试3: ws对象方法 ===")
        print(f"方法: {dir(ws)}")
        print(f"是否有send: {hasattr(ws, 'send')}")
        print(f"是否有_send: {hasattr(ws, '_send')}")
        
        break

asyncio.run(test())
```

**步骤2A: 如果支持获取request_id**

```python
import itertools

class WebsocketMultiAccountSubscriber:
    def __init__(self, ...):
        self.request_id_counter = itertools.count(1)
        self.inflight_subscribes: Dict[int, Tuple[Pubkey, Optional[str]]] = {}
    
    async def add_account(self, pubkey, decode_fn, oracle_id=None):
        # 如果account_subscribe返回request_id
        result = await self.ws.account_subscribe(pubkey, ...)
        if hasattr(result, 'request_id'):
            request_id = result.request_id
            self.inflight_subscribes[request_id] = (pubkey, oracle_id)
        else:
            # 回退到顺序匹配
            self.pending_subscriptions.append((pubkey, oracle_id))
```

**步骤2B: 如果支持直接发送JSON**

```python
import json

async def add_account(self, pubkey, decode_fn, oracle_id=None):
    # 生成request_id
    request_id = next(self.request_id_counter)
    
    # 记录映射
    self.inflight_subscribes[request_id] = (pubkey, oracle_id)
    
    # 直接构造并发送JSON-RPC消息
    message = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "accountSubscribe",
        "params": [
            str(pubkey),
            {
                "encoding": "base64",
                "commitment": str(self.commitment)
            }
        ]
    }
    
    # 发送消息
    if hasattr(self.ws, 'send'):
        await self.ws.send(json.dumps(message))
    elif hasattr(self.ws, '_send'):
        await self.ws._send(message)
    else:
        # 回退到使用account_subscribe
        await self.ws.account_subscribe(pubkey, ...)
        self.pending_subscriptions.append((pubkey, oracle_id))
```

**步骤3: 修改确认处理逻辑**

```python
if isinstance(result, int):  # 订阅确认
    async with self._lock:
        subscription_id = result
        request_id = None
        pubkey = None
        oracle_id = None
        
        # ⭐ 尝试从响应消息中获取id字段
        if hasattr(msg[0], 'id') and msg[0].id is not None:
            request_id = msg[0].id
            
            # ⭐ 从inflight_subscribes中查找
            if hasattr(self, 'inflight_subscribes') and request_id in self.inflight_subscribes:
                pubkey, oracle_id = self.inflight_subscribes.pop(request_id)
                # ✅ 准确匹配！
            else:
                # 回退到顺序匹配
                if self.pending_subscriptions:
                    pubkey, oracle_id = self.pending_subscriptions.pop(0)
        else:
            # 没有id字段，使用顺序匹配（向后兼容）
            if self.pending_subscriptions:
                pubkey, oracle_id = self.pending_subscriptions.pop(0)
            else:
                print("No pending subscription and no request_id")
                continue
        
        if pubkey:
            # 建立映射
            self.subscription_map[subscription_id] = pubkey
            self.pubkey_to_subscription[pubkey] = subscription_id
            
            if oracle_id:
                self.oracle_id_to_subscription[oracle_id] = subscription_id
```

**步骤4: 如果都不支持，使用pubkey锁备选方案**

```python
import asyncio

class WebsocketMultiAccountSubscriber:
    def __init__(self, ...):
        self.pubkey_locks: Dict[Pubkey, asyncio.Lock] = {}
        self.pubkey_subscription_events: Dict[Pubkey, asyncio.Event] = {}
    
    async def add_account(self, pubkey, decode_fn, oracle_id=None):
        # 为每个pubkey创建锁
        if pubkey not in self.pubkey_locks:
            self.pubkey_locks[pubkey] = asyncio.Lock()
            self.pubkey_subscription_events[pubkey] = asyncio.Event()
        
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
            
            # 等待确认（在_subscribe_ws中设置event）
            self.pending_subscriptions.append((pubkey, oracle_id))
            await self.pubkey_subscription_events[pubkey].wait()
```

## 第四部分：实施优先级和总结

### 实施优先级

1. **优先级1: BUG1修复**（必须）
   - ✅ 完全解决同pubkey不同解码器的问题
   - ✅ 实现相对简单
   - ✅ 不依赖`solana-py`的特殊能力

2. **优先级2: 检查`solana-py`的能力**（重要）
   - ⚠️ 需要先检查才能决定BUG2的修复方案
   - ⚠️ 如果支持，实现`inflight_subscribes`机制
   - ⚠️ 如果不支持，使用pubkey锁备选方案

3. **优先级3: BUG2修复**（重要）
   - 根据检查结果选择方案
   - 实现`request_id`匹配机制或pubkey锁

### 总结

**Rust SDK的关键优势**:
1. ✅ 完全控制`request_id`的生成和匹配
2. ✅ 直接构造JSON-RPC消息
3. ✅ 使用`inflight_subscribes`映射准确匹配
4. ✅ 使用`FuturesUnordered`真正并发

**Python SDK需要做的**:
1. ✅ 修复BUG1：使用`oracle_id`作为内部key
2. ⚠️ 修复BUG2：实现`request_id`匹配机制（需要先检查`solana-py`的能力）
3. ⚠️ 如果无法实现`request_id`匹配，使用pubkey锁备选方案
