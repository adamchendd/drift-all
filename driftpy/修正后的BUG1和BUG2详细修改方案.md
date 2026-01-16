# 修正后的BUG1和BUG2详细修改方案（完全参考Rust SDK）

## 关键修正：与Rust SDK保持一致

### 重要发现

**Rust SDK的BUG1解决方案关键点**：
1. ✅ **同一pubkey只订阅一次**（即使有多个source）
2. ✅ **收到更新时，遍历所有sources解码并存储**
3. ✅ **使用`(Pubkey, u8)`作为存储key**

**我的原始方案的问题**：
- ❌ 每个oracle_id都订阅（导致同一pubkey订阅多次）
- ⚠️ 更新处理逻辑不够准确

---

## 修正后的BUG1修复方案

### 核心逻辑（完全参考Rust SDK）

1. **订阅时按pubkey去重**：同一pubkey只订阅一次
2. **存储所有oracle_id的解码器**：即使只订阅一次，也存储所有oracle_id
3. **收到更新时遍历所有oracle_id解码**：类似Rust SDK的`for source in sources`

### 文件1: `src/driftpy/accounts/ws/multi_account_subscriber.py`

#### 修改1: 初始化数据结构（L26-33）

**修改为**:
```python
self.subscription_map: Dict[int, Pubkey] = {}  # subscription_id -> pubkey
self.pubkey_to_subscription: Dict[Pubkey, int] = {}  # pubkey -> subscription_id
self.decode_map: Dict[str, Callable[[bytes], Any]] = {}  # oracle_id -> decode_fn
self.data_map: Dict[str, Optional[DataAndSlot]] = {}  # oracle_id -> data
self.initial_data_map: Dict[str, Optional[DataAndSlot]] = {}  # oracle_id -> initial_data
self.pubkey_to_oracle_ids: Dict[Pubkey, set[str]] = {}  # pubkey -> {oracle_id, ...}
self.oracle_id_to_subscription: Dict[str, int] = {}  # oracle_id -> subscription_id
self.pending_subscriptions: list[Tuple[Pubkey, Optional[str]]] = []  # (pubkey, oracle_id)
```

#### 修改2: add_account方法 - 关键修正（L35-86）

**关键修正**：按pubkey去重，同一pubkey只订阅一次

```python
async def add_account(
    self,
    pubkey: Pubkey,
    decode: Optional[Callable[[bytes], Any]] = None,
    initial_data: Optional[DataAndSlot] = None,
    oracle_id: Optional[str] = None,
):
    decode_fn = decode if decode is not None else self.program.coder.accounts.decode
    key = oracle_id if oracle_id is not None else str(pubkey)
    
    async with self._lock:
        # 检查oracle_id是否已存在
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
        
        # ⭐ 存储解码器和数据（使用oracle_id作为key）
        self.decode_map[key] = decode_fn
        self.initial_data_map[key] = initial_data
        self.data_map[key] = initial_data
        
        # ⭐ 维护pubkey到oracle_id的映射
        if oracle_id:
            if pubkey not in self.pubkey_to_oracle_ids:
                self.pubkey_to_oracle_ids[pubkey] = set()
            self.pubkey_to_oracle_ids[pubkey].add(oracle_id)
    
    # ⭐ 关键修正：检查pubkey是否已订阅（按pubkey去重，类似Rust SDK）
    async with self._lock:
        pubkey_already_subscribed = pubkey in self.pubkey_to_subscription
    
    # ⭐ 只有pubkey未订阅时才发送订阅请求（同一pubkey只订阅一次）
    if not pubkey_already_subscribed and self.ws is not None:
        try:
            # BUG2修复：生成request_id并发送JSON-RPC消息
            request_id = next(self.request_id_counter)
            
            async with self._lock:
                # ⭐ 注意：这里oracle_id设为None，因为可能多个oracle_id共享同一个订阅
                self.inflight_subscribes[request_id] = (pubkey, None)
                self.pending_subscriptions.append((pubkey, None))
            
            # 直接构造并发送JSON-RPC消息
            commitment_str = str(self.commitment) if self.commitment else "confirmed"
            message = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "accountSubscribe",
                "params": [
                    str(pubkey),
                    {
                        "encoding": "base64",
                        "commitment": commitment_str
                    }
                ]
            }
            
            await self.ws.send(json.dumps(message))
        except Exception as e:
            print(f"Error subscribing to account {pubkey}: {e}")
            async with self._lock:
                # 清理inflight_subscribes
                request_id_to_remove = None
                for rid, (p, _) in self.inflight_subscribes.items():
                    if p == pubkey:
                        request_id_to_remove = rid
                        break
                if request_id_to_remove is not None:
                    del self.inflight_subscribes[request_id_to_remove]
                
                # 清理pending_subscriptions
                if (pubkey, None) in self.pending_subscriptions:
                    self.pending_subscriptions.remove((pubkey, None))
```

**关键点**：
- ✅ **按pubkey去重**：`if not pubkey_already_subscribed`
- ✅ **同一pubkey只订阅一次**：即使有多个oracle_id
- ✅ **存储所有oracle_id**：即使只订阅一次，也存储所有oracle_id的解码器

#### 修改3: 订阅确认处理 - 记录所有oracle_id（L151-165）

```python
if isinstance(result, int):  # 订阅确认
    async with self._lock:
        subscription_id = result
        pubkey = None
        
        # BUG2修复：使用id字段匹配
        if hasattr(msg[0], 'id') and msg[0].id is not None:
            request_id = msg[0].id
            if request_id in self.inflight_subscribes:
                pubkey, _ = self.inflight_subscribes.pop(request_id)
        else:
            # 回退到顺序匹配
            if self.pending_subscriptions:
                pubkey, _ = self.pending_subscriptions.pop(0)
        
        if pubkey:
            # 建立映射
            self.subscription_map[subscription_id] = pubkey
            self.pubkey_to_subscription[pubkey] = subscription_id
            
            # ⭐ 关键修正：为该pubkey的所有oracle_id建立映射
            # 因为同一pubkey的所有oracle_id共享同一个订阅
            if pubkey in self.pubkey_to_oracle_ids:
                for oracle_id in self.pubkey_to_oracle_ids[pubkey]:
                    self.oracle_id_to_subscription[oracle_id] = subscription_id
```

**关键点**：
- ✅ **一个subscription_id对应多个oracle_id**：因为同一pubkey的所有oracle_id共享同一个订阅

#### 修改4: 数据更新处理 - 遍历所有oracle_id解码（L167-196）

**关键修正**：类似Rust SDK的`for source in sources`

```python
if hasattr(result, "value") and result.value is not None:
    subscription_id = msg[0].subscription
    
    if subscription_id not in self.subscription_map:
        continue
    
    pubkey = self.subscription_map[subscription_id]
    
    # ⭐ 关键修正：获取该pubkey对应的所有oracle_id
    oracle_ids = self.pubkey_to_oracle_ids.get(pubkey, set())
    
    account_bytes = cast(bytes, result.value.data)
    slot = int(result.context.slot)
    
    if len(oracle_ids) == 0:
        # 向后兼容：没有oracle_id，使用pubkey
        key = str(pubkey)
        decode_fn = self.decode_map.get(key)
        if decode_fn:
            try:
                decoded_data = decode_fn(account_bytes)
                new_data = DataAndSlot(slot, decoded_data)
                self._update_data(key, new_data)
            except Exception:
                pass
    else:
        # ⭐ 关键修正：遍历所有oracle_id，每个都解码并存储
        # 类似Rust SDK的：for source in sources { update_handler(update, *source, &oraclemap) }
        for oracle_id in oracle_ids:
            decode_fn = self.decode_map.get(oracle_id)
            if decode_fn:
                try:
                    decoded_data = decode_fn(account_bytes)
                    new_data = DataAndSlot(slot, decoded_data)
                    self._update_data(oracle_id, new_data)  # ⭐ 每个oracle_id都存储
                except Exception as e:
                    # 某个解码器失败，继续尝试其他的（不应该发生，但容错处理）
                    print(f"Error decoding with oracle_id {oracle_id}: {e}")
                    continue
```

**关键点**：
- ✅ **遍历所有oracle_id**：`for oracle_id in oracle_ids`
- ✅ **每个oracle_id都解码并存储**：类似Rust SDK的`update_handler(update, *source, &oraclemap)`
- ✅ **完全符合Rust SDK的Mixed模式逻辑**

#### 修改5: 初始订阅逻辑（L122-141）

**关键修正**：按pubkey去重

```python
async with self._lock:
    initial_subscriptions = []
    subscribed_pubkeys = set()
    
    # 遍历data_map的key（现在是oracle_id或pubkey字符串）
    for key in list(self.data_map.keys()):
        # 从key中提取pubkey
        if '-' in key and key.split('-')[-1].isdigit():
            # 这是oracle_id格式
            pubkey_str = '-'.join(key.split('-')[:-1])
            try:
                pubkey = Pubkey.from_string(pubkey_str)
                oracle_id = key
            except:
                continue
        else:
            # 这是pubkey字符串（向后兼容）
            try:
                pubkey = Pubkey.from_string(key)
                oracle_id = None
            except:
                continue
        
        # ⭐ 关键修正：按pubkey去重（类似Rust SDK）
        if pubkey in subscribed_pubkeys:
            continue  # 该pubkey已准备订阅，跳过
        
        # 检查是否已订阅
        if pubkey in self.pubkey_to_subscription:
            subscribed_pubkeys.add(pubkey)
            continue
        
        subscribed_pubkeys.add(pubkey)
        initial_subscriptions.append((pubkey, None))  # oracle_id设为None，因为可能多个
        self.pending_subscriptions.append((pubkey, None))

# ⭐ 关键修正：每个pubkey只订阅一次
for pubkey, _ in initial_subscriptions:
    try:
        request_id = next(self.request_id_counter)
        async with self._lock:
            self.inflight_subscribes[request_id] = (pubkey, None)
        
        commitment_str = str(self.commitment) if self.commitment else "confirmed"
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "accountSubscribe",
            "params": [str(pubkey), {"encoding": "base64", "commitment": commitment_str}]
        }
        
        await ws.send(json.dumps(message))
    except Exception as e:
        print(f"Error subscribing to account {pubkey}: {e}")
        async with self._lock:
            request_id_to_remove = None
            for rid, (p, _) in self.inflight_subscribes.items():
                if p == pubkey:
                    request_id_to_remove = rid
                    break
            if request_id_to_remove is not None:
                del self.inflight_subscribes[request_id_to_remove]
            if (pubkey, None) in self.pending_subscriptions:
                self.pending_subscriptions.remove((pubkey, None))
```

**关键点**：
- ✅ **按pubkey去重**：`if pubkey in subscribed_pubkeys: continue`
- ✅ **每个pubkey只订阅一次**：即使有多个oracle_id

---

## BUG2修复：实现request_id匹配机制

### 这部分与Rust SDK完全一致

#### 修改1: 初始化 - 添加request_id相关数据结构

```python
import itertools
import json
from typing import Tuple

# 在__init__方法中，L33之后添加
self.request_id_counter = itertools.count(1)  # ⭐ request_id计数器
self.inflight_subscribes: Dict[int, Tuple[Pubkey, Optional[str]]] = {}  # ⭐ request_id -> (pubkey, oracle_id)
```

#### 修改2: add_account方法 - 使用直接发送JSON-RPC消息

（已在BUG1修复中修改，见上文）

#### 修改3: 订阅确认处理 - 使用id字段匹配

（已在BUG1修复中修改，见上文）

---

## 与Rust SDK的一致性对比

| 方面 | Rust SDK | 修正后的方案 |
|------|----------|-------------|
| **订阅逻辑** | 同一pubkey只订阅一次 | ✅ 同一pubkey只订阅一次 |
| **存储key** | `(Pubkey, u8)` | ✅ `oracle_id` (字符串) |
| **更新处理** | 遍历所有sources解码 | ✅ 遍历所有oracle_id解码 |
| **request_id管理** | 自己维护计数器 | ✅ 自己维护计数器 |
| **JSON-RPC消息** | 直接构造并发送 | ✅ 直接构造并发送 |
| **匹配机制** | 使用id字段查找映射 | ✅ 使用id字段查找映射 |

---

## 总结

**修正后的方案完全参考Rust SDK的逻辑**：

1. ✅ **订阅时按pubkey去重**：同一pubkey只订阅一次
2. ✅ **存储所有oracle_id**：即使只订阅一次，也存储所有oracle_id的解码器
3. ✅ **收到更新时遍历所有oracle_id解码**：类似Rust SDK的`for source in sources`
4. ✅ **request_id匹配机制**：完全一致

**关键修正点**：
- 从"每个oracle_id都订阅"改为"同一pubkey只订阅一次"
- 从"尝试所有解码器选择"改为"遍历所有oracle_id解码并存储"
