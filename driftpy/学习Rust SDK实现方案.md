# 学习Rust SDK实现方案

## Rust SDK (`drift-rs`) 的正确实现方式

### 核心机制

1. **维护自增的`request_id`计数器**
   ```rust
   request_id = auto_increment()
   ```

2. **维护`inflight_subscribes`映射**
   ```rust
   inflight_subscribes: HashMap<request_id, (operation, payload, pubkey, sender)>
   ```

3. **发送请求时**
   ```rust
   // 生成request_id
   let request_id = next_id();
   
   // 记录映射
   inflight_subscribes.insert(request_id, (operation, pubkey, ...));
   
   // 发送JSON-RPC消息
   send_json({
       "jsonrpc": "2.0",
       "id": request_id,  // ⭐ 使用自己的request_id
       "method": "accountSubscribe",
       "params": [...]
   });
   ```

4. **收到响应时**
   ```rust
   // 从响应中获取id
   let response_id = msg.id;
   
   // 查找对应的请求
   let (operation, pubkey, ...) = inflight_subscribes.remove(response_id);
   
   // 准确匹配！
   subscription_map[subscription_id] = pubkey;
   ```

## Python实现方案

### 方案1: 检查`solana-py`是否支持自定义`request_id`

#### 步骤1: 检查`account_subscribe`的源码

查看`solana-py`的`account_subscribe`方法：
- 是否接受`id`参数？
- 是否返回`request_id`？
- 是否可以访问内部状态？

#### 步骤2: 如果可以自定义`id`

```python
import itertools

class WebsocketMultiAccountSubscriber:
    def __init__(self, ...):
        self.request_id_counter = itertools.count(1)
        self.pending_requests: Dict[int, Pubkey] = {}
    
    async def add_account(self, pubkey, ...):
        # 生成request_id
        request_id = next(self.request_id_counter)
        
        # 记录映射
        self.pending_requests[request_id] = pubkey
        
        # 发送请求（如果支持自定义id）
        await self.ws.account_subscribe(
            pubkey,
            commitment=self.commitment,
            encoding="base64",
            request_id=request_id  # ⭐ 传入自定义id
        )
```

#### 步骤3: 收到确认时匹配

```python
if isinstance(result, int):
    if hasattr(msg[0], 'id') and msg[0].id is not None:
        request_id = msg[0].id
        pubkey = self.pending_requests.get(request_id)
        if pubkey:
            # ✅ 准确匹配！
            self.subscription_map[result] = pubkey
            del self.pending_requests[request_id]
        else:
            # 回退到顺序匹配
            pubkey = self.pending_subscriptions.pop(0)
```

### 方案2: 直接构造JSON-RPC消息（如果`solana-py`不支持自定义id）

#### 步骤1: 检查`ws`对象是否支持直接发送JSON

```python
# 检查ws对象的方法
print(dir(self.ws))
# 查找是否有 send_json, send, _send 等方法
```

#### 步骤2: 如果支持，直接发送JSON-RPC消息

```python
import json

async def add_account(self, pubkey, ...):
    # 生成request_id
    request_id = next(self.request_id_counter)
    
    # 记录映射
    self.pending_requests[request_id] = pubkey
    
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
    
    # 发送消息（需要找到正确的方法）
    await self.ws.send(json.dumps(message))
    # 或者
    # await self.ws._send(message)
```

### 方案3: 使用响应消息的`id`，但需要解决发送时不知道`id`的问题

#### 问题

如果我们无法在发送时知道`request_id`，但响应包含`id`，怎么办？

#### 解决方案A: 使用临时映射 + 顺序记录

```python
# 发送请求时
self.pending_subscriptions.append(pubkey)
await self.ws.account_subscribe(pubkey, ...)

# 收到确认时
if isinstance(result, int):
    if hasattr(msg[0], 'id'):
        request_id = msg[0].id
        
        # 问题：我们不知道这个request_id对应哪个pubkey
        # 但我们可以：记录所有待确认的pubkey，按顺序匹配第一个
        # 或者：检查solana-py的内部状态
```

#### 解决方案B: 检查`solana-py`的内部状态

```python
# 检查ws对象是否有内部状态可以获取
if hasattr(self.ws, '_request_id'):
    request_id = self.ws._request_id
    self.pending_requests[request_id] = pubkey
```

## 实施步骤

### 第一步: 检查`solana-py`的能力

1. **检查`account_subscribe`方法**:
   ```python
   import inspect
   from solana.rpc.websocket_api import SolanaWsClientProtocol
   
   # 查看方法签名
   print(inspect.signature(SolanaWsClientProtocol.account_subscribe))
   
   # 查看源码
   import solana.rpc.websocket_api
   print(inspect.getsource(SolanaWsClientProtocol.account_subscribe))
   ```

2. **检查`ws`对象的属性**:
   ```python
   # 在运行时检查
   print(dir(self.ws))
   print(hasattr(self.ws, 'send'))
   print(hasattr(self.ws, 'send_json'))
   print(hasattr(self.ws, '_request_id'))
   ```

3. **检查响应消息的结构**:
   ```python
   # 在收到确认时
   print(type(msg[0]))
   print(dir(msg[0]))
   print(hasattr(msg[0], 'id'))
   if hasattr(msg[0], 'id'):
       print(msg[0].id)
   ```

### 第二步: 根据检查结果选择方案

- **如果`account_subscribe`支持自定义`id`**: 使用方案1
- **如果`ws`对象支持直接发送JSON**: 使用方案2
- **如果都不支持，但响应包含`id`**: 需要找到其他方式获取请求的`id`
- **如果响应不包含`id`**: 使用备选方案（串行订阅）

### 第三步: 实现选定的方案

根据检查结果，实现对应的方案。

## 备选方案: 串行订阅（如果无法使用`id`匹配）

如果无法获取或使用`request_id`，可以：

```python
async def add_account(self, pubkey, ...):
    # 等待前一个订阅确认
    while self.pending_subscriptions:
        await asyncio.sleep(0.01)  # 短暂等待
    
    # 发送订阅请求
    await self.ws.account_subscribe(pubkey, ...)
    
    # 等待确认（在_subscribe_ws中处理）
    self.pending_subscriptions.append(pubkey)
```

**缺点**: 慢，但保证准确。

## 总结

**Rust SDK的关键优势**:
- ✅ 完全控制`request_id`的生成
- ✅ 维护明确的`request_id -> pubkey`映射
- ✅ 使用响应中的`id`字段准确匹配

**Python实现的关键挑战**:
- ❓ `solana-py`是否允许自定义`request_id`？
- ❓ 是否可以直接发送JSON-RPC消息？
- ❓ 响应消息是否包含`id`字段？

**下一步**: 检查`solana-py`的能力，然后选择最适合的方案。
