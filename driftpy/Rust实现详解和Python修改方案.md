# Rust SDK实现详解和Python修改方案

## 第一部分：Rust SDK的完整实现机制

### 1. 并发订阅流程（OracleMap::subscribe）

#### 步骤1: 去重和构造订阅future (`oraclemap.rs` L154-181)

```rust
// 1. 去重：共享oracle只订阅一次
for market in markets {
    let (oracle_pubkey, _oracle_source) = self.oracle_by_market.get(market).expect("oracle exists");
    
    // 跳过已订阅的pubkey
    if self.subscriptions.contains_key(oracle_pubkey) {
        continue;
    }
    
    // 为每个oracle创建WebsocketAccountSubscriber（共享同一个pubsub）
    let oracle_subscriber = WebsocketAccountSubscriber::new(
        Arc::clone(&self.pubsub),  // ⭐ 共享同一个PubsubClient
        *oracle_pubkey,
        self.commitment,
    );
    
    pending_subscriptions.push(oracle_subscriber);
}
```

**关键点**：
- ✅ 共享oracle只订阅一次（去重）
- ✅ 所有订阅器共享同一个`Arc<PubsubClient>`（单条WS连接）
- ✅ 每个订阅器对应一个`pubkey`

#### 步骤2: 并发执行订阅 (`oraclemap.rs` L183-210)

```rust
// 2. 为每个订阅构造future
let futs_iter = pending_subscriptions.into_iter().map(|sub_fut| {
    let oraclemap = Arc::clone(&self.oraclemap);
    let oracle_shared_mode = self.shared_oracles.get(&sub_fut.pubkey).expect("oracle exists").clone();
    let oracle_shared_mode_ref = oracle_shared_mode.clone();
    let on_account = on_account.clone();
    
    async move {
        // ⭐ 调用subscribe，内部会调用pubsub.account_subscribe()
        let unsub = sub_fut.subscribe(Self::SUBSCRIPTION_ID, true, move |update| {
            // 处理更新...
        }).await;
        ((sub_fut.pubkey, oracle_share_mode), unsub)
    }
});

// 3. 使用FuturesUnordered并发执行
let mut subscription_futs = FuturesUnordered::from_iter(futs_iter);

// 4. 并发等待所有订阅完成（谁先完成先返回）
while let Some(((pubkey, oracle_share_mode), unsub)) = subscription_futs.next().await {
    self.subscriptions.insert(pubkey, unsub?);
}
```

**关键点**：
- ✅ 使用`FuturesUnordered`真正并发执行
- ✅ 每个future内部调用`pubsub.account_subscribe()`（共享同一个pubsub）
- ✅ 所有订阅共享单条WS连接

### 2. WebSocket订阅流程（WebsocketAccountSubscriber::subscribe）

#### 步骤1: 初始数据获取 (`websocket_account_subscriber.rs` L48-82)

```rust
if sync {
    // 先通过RPC获取初始数据
    let rpc = RpcClient::new(get_http_url(self.pubsub.url().as_str())?);
    let response = rpc.get_account_with_commitment(&self.pubkey, self.commitment).await;
    // 调用on_update回调
    on_update(&AccountUpdate { ... });
}
```

#### 步骤2: 启动WebSocket订阅任务 (`websocket_account_subscriber.rs` L84-156)

```rust
// 创建取消channel
let (unsub_tx, mut unsub_rx) = oneshot::channel::<()>();

// ⭐ 启动独立任务处理订阅
tokio::spawn(async move {
    loop {
        // 调用pubsub.account_subscribe()获取stream
        let (mut account_updates, account_unsubscribe) = match pubsub
            .account_subscribe(&pubkey, Some(account_config.clone()))
            .await
        {
            Ok(res) => res,
            Err(err) => {
                log::error!("account subscribe {pubkey} failed: {err:?}");
                continue;  // 重连
            }
        };
        
        // ⭐ 使用tokio::select!同时处理更新和取消信号
        loop {
            tokio::select! {
                biased;
                message = account_updates.next() => {
                    // 处理账户更新
                    on_update(&account_update);
                }
                _ = &mut unsub_rx => {
                    // 取消订阅
                    account_unsubscribe().await;
                    break;
                }
            }
        }
    }
});

Ok(unsub_tx)  // 返回取消handle
```

**关键点**：
- ✅ 每个oracle一个独立的`tokio::spawn`任务
- ✅ 任务内部调用`pubsub.account_subscribe()`（共享同一个pubsub）
- ✅ 使用`tokio::select!`处理更新和取消信号
- ✅ 断线后自动重连

### 3. PubsubClient的请求-响应匹配机制（核心）

#### 架构设计 (`pubsub-client/lib.rs` L334-349)

```rust
async fn run_ws(
    url: Url,
    mut subscribe_receiver: mpsc::UnboundedReceiver<SubscribeRequestMsg>,
    mut request_receiver: mpsc::UnboundedReceiver<RequestMsg>,
    mut shutdown_receiver: oneshot::Receiver<()>,
) -> PubsubClientResult {
    // ⭐ 维护request_id计数器
    let mut request_id: u64 = 0;
    
    // ⭐ 维护订阅映射
    let mut subscriptions = BTreeMap::<u64, SubscriptionInfo>::new();  // sid -> SubscriptionInfo
    let mut request_id_to_sid = BTreeMap::<u64, u64>::new();  // request_id -> sid
    
    'reconnect: loop {
        // 建立WS连接
        let mut ws = connect_async(url.as_str()).await?;
        
        // ⭐ 每次重连时重新初始化inflight映射
        let mut inflight_subscribes = BTreeMap::<u64, (String, String, oneshot::Sender<SubscribeResponseMsg>)>::new();
        let mut inflight_unsubscribes = BTreeMap::<u64, oneshot::Sender<()>>::new();
        let mut inflight_requests = BTreeMap::<u64, oneshot::Sender<_>>::new();
        
        // 重连后重新发送所有订阅
        if !subscriptions.is_empty() {
            // 重新发送订阅请求...
        }
```

**关键点**：
- ✅ 自己维护`request_id`计数器（完全控制）
- ✅ 维护`inflight_subscribes`映射：`request_id -> (operation, payload, response_sender)`
- ✅ 维护`subscriptions`映射：`sid -> SubscriptionInfo`
- ✅ 维护`request_id_to_sid`映射：`request_id -> sid`

#### 发送订阅请求 (`pubsub-client/lib.rs` L577-586)

```rust
// 从channel接收订阅请求
subscribe = subscribe_receiver.recv() => {
    let (operation, params, response_sender) = subscribe.expect("subscribe channel");
    
    // ⭐ 自增request_id
    request_id += 1;
    
    // ⭐ 构造JSON-RPC消息（使用自己的request_id）
    let method = format!("{operation}Subscribe");
    let text = json!({
        "jsonrpc":"2.0",
        "id":request_id,  // ⭐ 使用自己的request_id
        "method":method,
        "params":params
    }).to_string();
    
    // ⭐ 发送消息
    ws.send(Message::Text(text.clone().into())).await;
    
    // ⭐ 记录到inflight_subscribes映射
    inflight_subscribes.insert(request_id, (operation, text, response_sender));
}
```

**关键点**：
- ✅ **完全控制`request_id`的生成**
- ✅ **直接构造JSON-RPC消息**
- ✅ **发送前记录映射**

#### 收到订阅确认 (`pubsub-client/lib.rs` L470-543)

```rust
// 收到响应：{"jsonrpc":"2.0","result":5308752,"id":1}
let id = gjson::get(text, "id");
if id.exists() {
    let id = id.u64();  // ⭐ 从响应中提取id
    
    // ⭐ 查找inflight_subscribes映射
    if let Some((operation, payload, response_sender)) = inflight_subscribes.remove(&id) {
        // ⭐ 准确匹配！
        
        // 提取subscription_id (sid)
        let sid = gjson::get(text, "result").u64();
        
        // 创建通知channel
        let (notifications_sender, notifications_receiver) = mpsc::unbounded_channel();
        
        // 发送响应给调用者
        response_sender.send(Ok((notifications_receiver, unsubscribe))).unwrap();
        
        // ⭐ 记录映射
        request_id_to_sid.insert(id, sid);
        subscriptions.insert(sid, SubscriptionInfo {
            sender: notifications_sender,
            payload,
        });
    }
}
```

**关键点**：
- ✅ **从响应消息中提取`id`字段**
- ✅ **使用`id`查找`inflight_subscribes`映射**
- ✅ **准确匹配请求和响应**
- ✅ **建立`request_id -> sid`和`sid -> SubscriptionInfo`映射**

#### 数据更新路由 (`pubsub-client/lib.rs` L443-467)

```rust
// 收到通知：{"method":"accountNotification","params":{"result":...,"subscription":3114862}}
let params = gjson::get(text, "params");
if params.exists() {
    // ⭐ 使用sid路由
    let sid = params.get("subscription").u64();
    
    // ⭐ 查找subscriptions映射
    if let Some(sub) = subscriptions.get(&sid) {
        let result = params.get("result");
        // ⭐ 发送到对应的channel
        sub.sender.send(serde_json::from_str(result.json()).expect("valid json"));
    }
}
```

**关键点**：
- ✅ 使用`params.subscription` (sid)路由数据更新
- ✅ 维护`subscriptions`映射

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

#### Rust SDK
```rust
// 1. 自己维护request_id
request_id += 1;

// 2. 直接构造JSON-RPC消息
let text = json!({"jsonrpc":"2.0","id":request_id,"method":"accountSubscribe","params":params}).to_string();

// 3. 发送前记录映射
inflight_subscribes.insert(request_id, (operation, text, response_sender));

// 4. 收到响应时使用id匹配
let id = gjson::get(text, "id").u64();
inflight_subscribes.remove(&id);  // ✅ 准确匹配
```

#### Python SDK
```python
# 1. 无法获取request_id
await self.ws.account_subscribe(pubkey, ...)  # ⚠️ 不返回request_id

# 2. 无法记录映射
self.pending_subscriptions.append(pubkey)  # ⚠️ 只有顺序列表

# 3. 收到响应时假设顺序
pubkey = self.pending_subscriptions.pop(0)  # ❌ 假设顺序
```

**差异**：
- ❌ Python无法获取`request_id`
- ❌ Python无法记录`request_id -> pubkey`映射
- ❌ Python使用顺序假设，可能错配

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
subscription_id = msg[0].subscription
pubkey = self.subscription_map[subscription_id]  # ⚠️ 但映射可能错误
decode_fn = self.decode_map.get(pubkey)  # ⚠️ 使用错误的解码器
```

**差异**：
- ✅ Python使用`subscription_id`路由（正确）
- ❌ 但`subscription_map`可能错误（因为确认匹配错误）
- ❌ 导致使用错误的解码器

## 第三部分：Python SDK的具体修改方案

### 修改方案1: BUG1修复 - 使用oracle_id作为内部key

#### 文件: `src/driftpy/accounts/ws/multi_account_subscriber.py`

**修改1: 初始化数据结构** (L26-33)

```python
# 原代码
self.subscription_map: Dict[int, Pubkey] = {}
self.pubkey_to_subscription: Dict[Pubkey, int] = {}
self.decode_map: Dict[Pubkey, Callable[[bytes], Any]] = {}
self.data_map: Dict[Pubkey, Optional[DataAndSlot]] = {}
self.pending_subscriptions: list[Pubkey] = []

# 改为
self.subscription_map: Dict[int, Pubkey] = {}  # subscription_id -> pubkey
self.pubkey_to_subscription: Dict[Pubkey, int] = {}  # pubkey -> subscription_id
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
    
    # 确定使用的key
    key = oracle_id if oracle_id is not None else str(pubkey)
    
    async with self._lock:
        # 检查是否已订阅（使用oracle_id或pubkey）
        if oracle_id and oracle_id in self.data_map:
            return
        if not oracle_id and pubkey in self.pubkey_to_subscription:
            return
        
        if pubkey in self.data_map and initial_data is None:
            initial_data = self.data_map[pubkey]
    
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

**修改3: get_data方法** (L224-225)

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
            # 回退到pubkey（向后兼容）
            key = str(key)
    
    return self.data_map.get(key)
```

**修改4: 确认处理逻辑** (L151-165)

```python
if isinstance(result, int):  # 订阅确认
    async with self._lock:
        subscription_id = result
        
        # ⭐ 尝试使用id字段匹配（如果可用）
        request_id = None
        if hasattr(msg[0], 'id') and msg[0].id is not None:
            request_id = msg[0].id
            # 从inflight_subscribes中查找（如果实现了）
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
        
        # 建立映射
        self.subscription_map[subscription_id] = pubkey
        self.pubkey_to_subscription[pubkey] = subscription_id
        
        # ⭐ 如果使用oracle_id，建立oracle_id到subscription_id的映射
        if oracle_id:
            self.oracle_id_to_subscription[oracle_id] = subscription_id
            # 更新decode_map和data_map的key（如果之前用pubkey）
            if str(pubkey) in self.decode_map:
                self.decode_map[oracle_id] = self.decode_map.pop(str(pubkey))
            if str(pubkey) in self.data_map:
                self.data_map[oracle_id] = self.data_map.pop(str(pubkey))
```

**修改5: 数据更新处理** (L167-193)

```python
if hasattr(result, "value") and result.value is not None:
    subscription_id = msg[0].subscription
    
    if subscription_id not in self.subscription_map:
        print(f"Subscription ID {subscription_id} not found in subscription map")
        continue
    
    pubkey = self.subscription_map[subscription_id]
    
    # ⭐ 如果该pubkey有多个oracle_id，需要确定使用哪个
    oracle_ids = self.pubkey_to_oracle_ids.get(pubkey, set())
    
    if len(oracle_ids) == 1:
        # 只有一个oracle_id，直接使用
        oracle_id = list(oracle_ids)[0]
        decode_fn = self.decode_map.get(oracle_id)
    elif len(oracle_ids) > 1:
        # ⭐ 多个oracle_id：尝试所有解码器，选择能成功解码的
        decode_fn = None
        for oracle_id in oracle_ids:
            candidate_decode_fn = self.decode_map.get(oracle_id)
            if candidate_decode_fn:
                try:
                    # 尝试解码
                    test_decoded = candidate_decode_fn(account_bytes)
                    decode_fn = candidate_decode_fn
                    oracle_id_used = oracle_id
                    break
                except Exception:
                    continue
        if decode_fn is None:
            print(f"No valid decode function found for pubkey {pubkey}")
            continue
    else:
        # 没有oracle_id，使用pubkey（向后兼容）
        decode_fn = self.decode_map.get(str(pubkey))
        oracle_id_used = None
    
    if decode_fn is None:
        print(f"No decode function found for pubkey {pubkey}")
        continue
    
    try:
        slot = int(result.context.slot)
        decoded_data = decode_fn(account_bytes)
        new_data = DataAndSlot(slot, decoded_data)
        
        # ⭐ 更新对应的oracle_id或pubkey的数据
        if oracle_id_used:
            self._update_data(oracle_id_used, new_data)
        else:
            self._update_data(str(pubkey), new_data)
    except Exception:
        continue
```

**修改6: _update_data方法** (L216-222)

```python
def _update_data(self, key: str, new_data: Optional[DataAndSlot]):
    if new_data is None:
        return
    
    current_data = self.data_map.get(key)
    if current_data is None or new_data.slot >= current_data.slot:
        self.data_map[key] = new_data
```

### 修改方案2: BUG2修复 - 实现request_id匹配机制

#### 关键挑战

`solana-py`的`account_subscribe`方法可能不允许我们：
- 自己生成`request_id`
- 直接构造JSON-RPC消息
- 获取内部生成的`request_id`

#### 实施步骤

**步骤1: 检查`solana-py`的能力**

创建测试脚本检查：

```python
# test_solana_py_capabilities.py
import asyncio
from solana.rpc.websocket_api import connect, SolanaWsClientProtocol
from solders.pubkey import Pubkey

async def test():
    ws_url = "wss://api.mainnet-beta.solana.com"
    async for ws in connect(ws_url):
        ws = cast(SolanaWsClientProtocol, ws)
        
        # 测试1: 检查account_subscribe的返回值
        result = await ws.account_subscribe(
            Pubkey.default(),  # 测试用
            commitment="confirmed",
            encoding="base64",
        )
        print(f"account_subscribe返回值类型: {type(result)}")
        print(f"account_subscribe返回值: {result}")
        print(f"account_subscribe返回值属性: {dir(result)}")
        
        # 测试2: 检查响应消息的结构
        msg = await ws.recv()
        print(f"响应消息类型: {type(msg)}")
        print(f"响应消息: {msg}")
        print(f"msg[0]类型: {type(msg[0])}")
        print(f"msg[0]属性: {dir(msg[0])}")
        print(f"是否有id属性: {hasattr(msg[0], 'id')}")
        if hasattr(msg[0], 'id'):
            print(f"id值: {msg[0].id}")
        
        # 测试3: 检查ws对象的方法
        print(f"ws对象方法: {dir(ws)}")
        print(f"是否有send方法: {hasattr(ws, 'send')}")
        print(f"是否有_send方法: {hasattr(ws, '_send')}")
        
        break

asyncio.run(test())
```

**步骤2A: 如果`account_subscribe`返回`request_id`**

```python
import itertools

class WebsocketMultiAccountSubscriber:
    def __init__(self, ...):
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

**步骤2B: 如果`ws`对象支持直接发送JSON**

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
            if request_id in self.inflight_subscribes:
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

**步骤4: 如果都不支持，使用备选方案（pubkey锁）**

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

### 修改方案3: 文件修改清单

#### 文件1: `src/driftpy/accounts/ws/multi_account_subscriber.py`

**需要修改的地方**:
1. L26-33: 初始化数据结构（添加oracle_id相关映射）
2. L35-86: `add_account`方法（添加`oracle_id`参数，使用oracle_id作为key）
3. L88-106: `remove_account`方法（支持oracle_id参数）
4. L151-165: 确认处理逻辑（使用id匹配，支持oracle_id）
5. L167-193: 数据更新处理（支持多个oracle_id）
6. L216-222: `_update_data`方法（使用key参数）
7. L224-225: `get_data`方法（支持oracle_id参数）

#### 文件2: `src/driftpy/accounts/ws/drift_client.py`

**需要修改的地方**:
1. L176-192: `subscribe_to_oracle`方法（传递oracle_id，使用oracle_id检查）
2. L194-214: `subscribe_to_oracle_info`方法（传递oracle_id，使用oracle_id检查）
3. L267-273: `get_oracle_price_data_and_slot`方法（使用oracle_id获取数据）

## 第四部分：实施优先级

### 优先级1: BUG1修复（必须）

- ✅ 完全解决同pubkey不同解码器的问题
- ✅ 实现相对简单
- ✅ 不依赖`solana-py`的特殊能力

### 优先级2: BUG2修复（重要）

- ⚠️ 需要先检查`solana-py`的能力
- ⚠️ 如果支持，实现`inflight_subscribes`机制
- ⚠️ 如果不支持，使用pubkey锁备选方案

### 优先级3: 并发优化（可选）

- 使用`asyncio.gather`真正并发执行订阅
- 但需要先解决BUG2，否则并发会加剧匹配问题

## 总结

**Rust SDK的关键优势**:
1. ✅ 完全控制`request_id`的生成和匹配
2. ✅ 直接构造JSON-RPC消息
3. ✅ 使用`inflight_subscribes`映射准确匹配
4. ✅ 使用`FuturesUnordered`真正并发

**Python SDK需要做的**:
1. ✅ 修复BUG1：使用`oracle_id`作为内部key
2. ⚠️ 修复BUG2：实现`request_id`匹配机制（需要检查`solana-py`的能力）
3. ⚠️ 如果无法实现`request_id`匹配，使用pubkey锁备选方案
