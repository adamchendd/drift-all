# solana-py KeyError 原因和 Rust SDK 实现对比分析

## 一、solana-py 的 KeyError 原因

### 1.1 solana-py 的正常工作流程

当使用 `solana-py` 的标准 API 时：

```python
# 标准用法
subscription = await ws.account_subscribe(pubkey, commitment="confirmed")
```

**内部流程：**

1. **发送请求阶段**：
   ```python
   # solana-py 内部（简化版）
   def account_subscribe(self, pubkey, commitment):
       message = {
           "jsonrpc": "2.0",
           "id": self.increment_counter_and_get_id(),  # 生成 request_id
           "method": "accountSubscribe",
           "params": [...]
       }
       # ⭐ 关键：将请求存储在 sent_subscriptions 中
       self.sent_subscriptions[message.id] = message
       await self.send(message)  # 发送到 WebSocket
   ```

2. **接收响应阶段**：
   ```python
   # solana-py 内部（简化版）
   def _process_rpc_response(self, raw_message):
       parsed = parse_websocket_message(raw_message)
       for item in parsed:
           if isinstance(item, SubscriptionResult):
               # ⭐ 关键：从 sent_subscriptions 中查找原始请求
               original_request = self.sent_subscriptions[item.id]  # 这里可能 KeyError
               subscription_id = item.result
               # 建立 subscription_id -> request 的映射
               self.subscriptions[subscription_id] = original_request
   ```

### 1.2 我们的代码导致 KeyError 的原因

**问题代码：**

```python
# multi_account_subscriber.py (修复前)
# 我们直接发送 JSON-RPC 消息，绕过了 solana-py 的 API
message = {
    "jsonrpc": "2.0",
    "id": request_id,  # 我们自己生成的 request_id
    "method": "accountSubscribe",
    "params": [...]
}
await ws.send(json.dumps(message))  # ⚠️ 直接发送，没有通过 solana-py 的 API

# 然后使用 solana-py 的 recv() 接收
msg = await ws.recv()  # ⚠️ solana-py 内部会调用 _process_rpc_response
```

**问题分析：**

1. **我们直接发送 JSON-RPC**：
   - 使用 `raw_ws.send(json.dumps(message))` 直接发送
   - **没有调用** `ws.account_subscribe()`，所以 `solana-py` 的 `sent_subscriptions` 中没有记录

2. **solana-py 的 recv() 尝试处理响应**：
   - `ws.recv()` 内部会调用 `_process_rpc_response()`
   - `_process_rpc_response()` 尝试查找 `self.sent_subscriptions[item.id]`
   - **找不到**，因为我们的请求没有通过 `solana-py` 的 API 发送
   - **结果**：抛出 `KeyError: X`

### 1.3 为什么需要绕过 solana-py？

**原因：修复 BUG2（订阅确认顺序混乱）**

- 原代码使用 `ws.account_subscribe()`，但无法控制 `request_id`
- 我们需要自己的 `request_id` 来匹配请求和响应
- 所以必须直接发送 JSON-RPC 消息

## 二、Rust SDK 的实现方式

### 2.1 Rust SDK 的架构

Rust SDK **不依赖外部的 WebSocket 库**，而是自己实现了完整的 WebSocket 客户端：

```
drift-rs
├── crates/
│   ├── pubsub-client/          # ⭐ 自己的 WebSocket 客户端实现
│   │   ├── src/
│   │   │   ├── lib.rs          # 核心实现
│   │   │   └── websocket.rs    # WebSocket 连接管理
│   └── oraclemap/              # Oracle 订阅管理
```

### 2.2 Rust SDK 的请求-响应匹配机制

**核心数据结构：**

```rust
// pubsub-client/src/lib.rs (简化版)
pub struct PubsubClient {
    // ⭐ 自己维护的请求-响应映射
    inflight_subscribes: HashMap<u64, (Operation, Payload, Pubkey, oneshot::Sender<...>)>,
    inflight_unsubscribes: HashMap<u64, oneshot::Sender<...>>,
    inflight_requests: HashMap<u64, oneshot::Sender<...>>,
    
    // subscription_id -> SubscriptionInfo 映射
    subscriptions: HashMap<u64, SubscriptionInfo>,
    
    // request_id -> subscription_id 映射（用于重连）
    request_id_to_sid: HashMap<u64, u64>,
    
    request_id_counter: AtomicU64,  // 自增 request_id
}
```

**发送订阅请求：**

```rust
// pubsub-client/src/lib.rs (简化版)
pub async fn account_subscribe(&self, pubkey: Pubkey, ...) -> Result<u64> {
    let request_id = self.request_id_counter.fetch_add(1, Ordering::SeqCst);
    
    let message = json!({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "accountSubscribe",
        "params": [...]
    });
    
    // ⭐ 存储到自己的 inflight_subscribes
    let (tx, rx) = oneshot::channel();
    self.inflight_subscribes.insert(request_id, (operation, payload, pubkey, tx));
    
    // 发送到 WebSocket
    self.ws.send(message.to_string()).await?;
    
    // 等待响应
    let subscription_id = rx.await?;
    Ok(subscription_id)
}
```

**接收和处理响应：**

```rust
// pubsub-client/src/lib.rs (简化版)
async fn run_ws(&mut self) -> Result<()> {
    loop {
        let raw_message = self.ws.recv().await?;
        let parsed: Value = serde_json::from_str(&raw_message)?;
        
        // ⭐ 自己解析 JSON-RPC 响应
        if let Some(id) = parsed.get("id") {
            let request_id = id.as_u64().unwrap();
            
            // 查找对应的 inflight 请求
            if let Some((operation, payload, pubkey, sender)) = 
                self.inflight_subscribes.remove(&request_id) {
                
                // 获取 subscription_id
                let subscription_id = parsed["result"].as_u64().unwrap();
                
                // 建立映射
                self.request_id_to_sid.insert(request_id, subscription_id);
                self.subscriptions.insert(subscription_id, SubscriptionInfo {
                    sender,
                    payload,
                });
                
                // 通知等待的发送者
                let _ = sender.send(subscription_id);
            }
        }
        
        // 处理数据更新通知
        if let Some(method) = parsed.get("method") {
            if method.as_str() == Some("accountNotification") {
                let subscription_id = parsed["params"]["subscription"].as_u64().unwrap();
                if let Some(sub_info) = self.subscriptions.get(&subscription_id) {
                    // 发送数据到对应的 channel
                    let _ = sub_info.sender.send(data);
                }
            }
        }
    }
}
```

### 2.3 Rust SDK 的优势

1. **完全控制**：
   - 不依赖外部库的状态管理
   - 自己维护所有映射关系
   - 可以精确控制请求-响应匹配

2. **无 KeyError 风险**：
   - 所有请求都通过自己的 API 发送
   - 所有响应都通过自己的逻辑处理
   - 不会出现状态不一致

3. **更好的错误处理**：
   - 可以自定义错误处理逻辑
   - 可以处理重连、超时等场景

## 三、我们的修复方案

### 3.1 为什么不能完全像 Rust 那样？

**限制：**

1. **Python 生态**：
   - `solana-py` 是标准的 Solana Python SDK
   - 我们不能完全重写 WebSocket 客户端（工作量太大）
   - 需要保持与 `solana-py` 的兼容性

2. **现有代码依赖**：
   - 其他部分可能依赖 `solana-py` 的功能
   - 完全替换会导致大量重构

### 3.2 我们的解决方案

**方案：直接使用底层 WebSocket，绕过 solana-py 的处理**

```python
# multi_account_subscriber.py (修复后)
async with websockets.connect(ws_endpoint) as raw_ws:
    # ⭐ 直接使用原始 WebSocket，不通过 solana-py
    self.ws = cast(SolanaWsClientProtocol, raw_ws)  # 仅用于兼容性
    
    # 发送订阅请求
    message = {
        "jsonrpc": "2.0",
        "id": self.request_id_counter,  # 我们自己生成的 request_id
        "method": "accountSubscribe",
        "params": [...]
    }
    await raw_ws.send(json.dumps(message))  # 直接发送
    
    # 接收响应
    raw_data = await raw_ws.recv()  # 直接接收原始 JSON
    msg_data = json.loads(raw_data)  # 自己解析
    
    # ⭐ 自己处理请求-响应匹配
    if msg_data.get("id") == request_id:
        subscription_id = msg_data["result"]
        # 建立映射
        self.subscription_map[subscription_id] = pubkey
```

**优势：**

1. **避免 KeyError**：
   - 不通过 `solana-py` 的 `recv()`，所以不会触发 `_process_rpc_response`
   - 自己解析 JSON，自己处理响应

2. **完全控制**：
   - 自己维护 `inflight_subscribes` 映射
   - 自己匹配 `request_id` 和响应

3. **最小改动**：
   - 不需要重写整个 WebSocket 客户端
   - 只需要修改 `multi_account_subscriber.py`

### 3.3 与 Rust SDK 的对比

| 特性 | Rust SDK | 我们的方案 |
|------|----------|-----------|
| WebSocket 客户端 | 自己实现 | 使用 `websockets` 库 |
| JSON-RPC 解析 | 自己实现 | 使用 `json.loads()` |
| 请求-响应匹配 | 自己维护映射 | 自己维护映射 |
| 状态管理 | 完全控制 | 完全控制 |
| 依赖外部库 | 无 | 依赖 `websockets` 和 `json` |
| 代码复杂度 | 高（完整实现） | 低（仅处理订阅） |

## 四、总结

### 4.1 solana-py 的 KeyError 根本原因

- **原因**：我们直接发送 JSON-RPC，绕过了 `solana-py` 的 API
- **结果**：`solana-py` 的 `sent_subscriptions` 中没有记录
- **触发**：`solana-py` 的 `recv()` 尝试查找时找不到，抛出 `KeyError`

### 4.2 Rust SDK 的做法

- **完全自己实现** WebSocket 客户端和 JSON-RPC 协议
- **自己维护** 所有状态映射
- **无外部依赖**，不会出现状态不一致

### 4.3 我们的方案

- **直接使用底层 WebSocket**，绕过 `solana-py` 的处理
- **自己解析 JSON**，自己处理响应
- **最小改动**，保持与现有代码的兼容性

**结论**：我们的方案在 Python 生态的限制下，实现了与 Rust SDK 类似的效果（完全控制请求-响应匹配），同时避免了 `solana-py` 的 KeyError 问题。
