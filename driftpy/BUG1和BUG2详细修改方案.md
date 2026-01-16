# BUG1和BUG2详细修改方案

## 概述

基于Rust SDK的实现方式，修复两个严重BUG：
- **BUG1**: 同一pubkey不同解码器的问题（使用oracle_id作为key）
- **BUG2**: 订阅确认顺序假设错误（实现request_id匹配机制）

## 修改文件清单

1. `src/driftpy/accounts/ws/multi_account_subscriber.py` - 核心修改
2. `src/driftpy/accounts/ws/drift_client.py` - 传递oracle_id参数
3. `src/driftpy/oracles/oracle_id.py` - 修复get_oracle_source_num的匹配顺序（可选，但建议修复）

---

## BUG1修复：使用oracle_id作为内部key

### 文件1: `src/driftpy/accounts/ws/multi_account_subscriber.py`

#### 修改1: 初始化数据结构（L26-33）

**原代码**:
```python
self.subscription_map: Dict[int, Pubkey] = {}
self.pubkey_to_subscription: Dict[Pubkey, int] = {}
self.decode_map: Dict[Pubkey, Callable[[bytes], Any]] = {}
self.data_map: Dict[Pubkey, Optional[DataAndSlot]] = {}
self.initial_data_map: Dict[Pubkey, Optional[DataAndSlot]] = {}
self.pending_subscriptions: list[Pubkey] = []
```

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

#### 修改2: add_account方法（L35-86）

**原代码**:
```python
async def add_account(
    self,
    pubkey: Pubkey,
    decode: Optional[Callable[[bytes], Any]] = None,
    initial_data: Optional[DataAndSlot] = None,
):
```

**修改为**:
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

#### 修改3: remove_account方法（L88-106）

**原代码**:
```python
async def remove_account(self, pubkey: Pubkey):
    async with self._lock:
        if pubkey not in self.pubkey_to_subscription:
            return
        
        subscription_id = self.pubkey_to_subscription[pubkey]
        # ... 删除逻辑
        del self.decode_map[pubkey]
        del self.data_map[pubkey]
```

**修改为**:
```python
async def remove_account(self, pubkey: Pubkey, oracle_id: Optional[str] = None):
    async with self._lock:
        # ⭐ 如果提供oracle_id，只删除该oracle_id
        if oracle_id:
            if oracle_id not in self.oracle_id_to_subscription:
                return
            subscription_id = self.oracle_id_to_subscription[oracle_id]
            del self.oracle_id_to_subscription[oracle_id]
            del self.decode_map[oracle_id]
            del self.data_map[oracle_id]
            if oracle_id in self.initial_data_map:
                del self.initial_data_map[oracle_id]
            
            # 更新pubkey到oracle_ids的映射
            if pubkey in self.pubkey_to_oracle_ids:
                self.pubkey_to_oracle_ids[pubkey].discard(oracle_id)
                if not self.pubkey_to_oracle_ids[pubkey]:
                    del self.pubkey_to_oracle_ids[pubkey]
                    # 如果该pubkey没有其他oracle_id，也删除pubkey相关的映射
                    if pubkey in self.pubkey_to_subscription:
                        del self.pubkey_to_subscription[pubkey]
                    if subscription_id in self.subscription_map:
                        del self.subscription_map[subscription_id]
        else:
            # ⭐ 向后兼容：如果没有提供oracle_id，删除所有相关的oracle_id
            if pubkey not in self.pubkey_to_subscription:
                return
            
            subscription_id = self.pubkey_to_subscription[pubkey]
            
            # 删除所有相关的oracle_id
            if pubkey in self.pubkey_to_oracle_ids:
                for oid in list(self.pubkey_to_oracle_ids[pubkey]):
                    if oid in self.oracle_id_to_subscription:
                        del self.oracle_id_to_subscription[oid]
                    if oid in self.decode_map:
                        del self.decode_map[oid]
                    if oid in self.data_map:
                        del self.data_map[oid]
                    if oid in self.initial_data_map:
                        del self.initial_data_map[oid]
                del self.pubkey_to_oracle_ids[pubkey]
            else:
                # 向后兼容：使用pubkey作为key
                key = str(pubkey)
                if key in self.decode_map:
                    del self.decode_map[key]
                if key in self.data_map:
                    del self.data_map[key]
                if key in self.initial_data_map:
                    del self.initial_data_map[key]
            
            del self.pubkey_to_subscription[pubkey]
            if subscription_id in self.subscription_map:
                del self.subscription_map[subscription_id]
        
        if self.ws is not None and subscription_id is not None:
            try:
                await self.ws.account_unsubscribe(subscription_id)
            except Exception:
                pass
```

#### 修改4: _subscribe_ws方法 - 订阅确认处理（L151-165）

**原代码**:
```python
if isinstance(result, int):
    async with self._lock:
        if self.pending_subscriptions:
            pubkey = self.pending_subscriptions.pop(0)
            subscription_id = result
            self.subscription_map[subscription_id] = pubkey
            self.pubkey_to_subscription[pubkey] = subscription_id
```

**修改为**（这部分会在BUG2修复中一起修改，见下文）:
```python
if isinstance(result, int):  # 订阅确认
    async with self._lock:
        subscription_id = result
        pubkey = None
        oracle_id = None
        
        # ⭐ BUG2修复：使用id字段匹配（见BUG2修复部分）
        # 如果实现了inflight_subscribes，使用它
        if hasattr(self, 'inflight_subscribes') and hasattr(msg[0], 'id') and msg[0].id is not None:
            request_id = msg[0].id
            if request_id in self.inflight_subscribes:
                pubkey, oracle_id = self.inflight_subscribes.pop(request_id)
        else:
            # 回退到顺序匹配（向后兼容）
            if self.pending_subscriptions:
                pubkey, oracle_id = self.pending_subscriptions.pop(0)
        
        if pubkey:
            # 建立映射
            self.subscription_map[subscription_id] = pubkey
            self.pubkey_to_subscription[pubkey] = subscription_id
            
            # ⭐ 如果使用oracle_id，建立oracle_id到subscription_id的映射
            if oracle_id:
                self.oracle_id_to_subscription[oracle_id] = subscription_id
```

#### 修改5: _subscribe_ws方法 - 数据更新处理（L167-196）

**原代码**:
```python
if hasattr(result, "value") and result.value is not None:
    subscription_id = msg[0].subscription
    pubkey = self.subscription_map[subscription_id]
    decode_fn = self.decode_map.get(pubkey)
    # ...
    self._update_data(pubkey, new_data)
```

**修改为**:
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
        key = oracle_id
    elif len(oracle_ids) > 1:
        # ⭐ 多个oracle_id：尝试所有解码器，选择能成功解码的
        key = None
        for oid in oracle_ids:
            candidate_decode_fn = self.decode_map.get(oid)
            if candidate_decode_fn:
                try:
                    # 尝试解码
                    account_bytes = cast(bytes, result.value.data)
                    test_decoded = candidate_decode_fn(account_bytes)
                    key = oid
                    break
                except Exception:
                    continue
        if key is None:
            print(f"No valid decode function found for pubkey {pubkey}")
            continue
    else:
        # 没有oracle_id，使用pubkey（向后兼容）
        key = str(pubkey)
    
    decode_fn = self.decode_map.get(key)
    if decode_fn is None:
        print(f"No decode function found for key {key}")
        continue
    
    try:
        slot = int(result.context.slot)
        account_bytes = cast(bytes, result.value.data)
        decoded_data = decode_fn(account_bytes)
        new_data = DataAndSlot(slot, decoded_data)
        self._update_data(key, new_data)  # ⭐ 使用key更新
    except Exception:
        continue
```

#### 修改6: _update_data方法（L216-222）

**原代码**:
```python
def _update_data(self, pubkey: Pubkey, new_data: Optional[DataAndSlot]):
    if new_data is None:
        return
    
    current_data = self.data_map.get(pubkey)
    if current_data is None or new_data.slot >= current_data.slot:
        self.data_map[pubkey] = new_data
```

**修改为**:
```python
def _update_data(self, key: str, new_data: Optional[DataAndSlot]):
    """
    更新数据
    key可以是oracle_id（新方式）或pubkey字符串（向后兼容）
    """
    if new_data is None:
        return
    
    current_data = self.data_map.get(key)
    if current_data is None or new_data.slot >= current_data.slot:
        self.data_map[key] = new_data
```

#### 修改7: get_data方法（L224-225）

**原代码**:
```python
def get_data(self, pubkey: Pubkey) -> Optional[DataAndSlot]:
    return self.data_map.get(pubkey)
```

**修改为**:
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
        elif len(oracle_ids) > 1:
            # 多个oracle_id，返回第一个（或可以抛出异常要求明确指定）
            key = list(oracle_ids)[0]
        else:
            key = str(key)
    
    return self.data_map.get(key)
```

#### 修改8: fetch方法（L227-248）

**原代码**:
```python
async def fetch(self, pubkey: Optional[Pubkey] = None):
    if pubkey is not None:
        decode_fn = self.decode_map.get(pubkey)
        # ...
    else:
        for pubkey, decode_fn in self.decode_map.items():
            # ...
```

**修改为**:
```python
async def fetch(self, pubkey: Optional[Pubkey] = None, oracle_id: Optional[str] = None):
    if pubkey is not None:
        key = oracle_id if oracle_id is not None else str(pubkey)
        decode_fn = self.decode_map.get(key)
        if decode_fn is None:
            return
        new_data = await get_account_data_and_slot(
            pubkey, self.program, self.commitment, decode_fn
        )
        self._update_data(key, new_data)
    else:
        tasks = []
        keys = []
        for key, decode_fn in self.decode_map.items():
            # 从key中提取pubkey（如果是oracle_id格式：pubkey-source_num）
            if '-' in key and key.split('-')[-1].isdigit():
                # 这是oracle_id格式
                pubkey_str = '-'.join(key.split('-')[:-1])
                try:
                    pubkey = Pubkey.from_string(pubkey_str)
                except:
                    continue
            else:
                # 这是pubkey字符串
                try:
                    pubkey = Pubkey.from_string(key)
                except:
                    continue
            
            keys.append(key)
            tasks.append(
                get_account_data_and_slot(
                    pubkey, self.program, self.commitment, decode_fn
                )
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for key, result in zip(keys, results):
            if isinstance(result, Exception):
                continue
            self._update_data(key, result)
```

#### 修改9: _subscribe_ws方法 - 初始订阅（L122-141）

**原代码**:
```python
async with self._lock:
    initial_accounts = []
    for pubkey in list(self.data_map.keys()):
        if pubkey not in self.pubkey_to_subscription:
            initial_accounts.append(pubkey)
    self.pending_subscriptions.extend(initial_accounts)

for pubkey in initial_accounts:
    await ws.account_subscribe(pubkey, ...)
```

**修改为**:
```python
async with self._lock:
    initial_subscriptions = []
    # ⭐ 遍历data_map的key（现在是oracle_id或pubkey字符串）
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
        
        # 检查是否已订阅
        if oracle_id and oracle_id in self.oracle_id_to_subscription:
            continue
        if not oracle_id and pubkey in self.pubkey_to_subscription:
            continue
        
        initial_subscriptions.append((pubkey, oracle_id))
        self.pending_subscriptions.append((pubkey, oracle_id))

for pubkey, oracle_id in initial_subscriptions:
    try:
        await ws.account_subscribe(
            pubkey,
            commitment=self.commitment,
            encoding="base64",
        )
    except Exception as e:
        print(f"Error subscribing to account {pubkey}: {e}")
        async with self._lock:
            if (pubkey, oracle_id) in self.pending_subscriptions:
                self.pending_subscriptions.remove((pubkey, oracle_id))
```

#### 修改10: unsubscribe方法（L253-277）

**原代码**:
```python
async def unsubscribe(self):
    # ...
    async with self._lock:
        self.subscription_map.clear()
        self.pubkey_to_subscription.clear()
        self.decode_map.clear()
        self.data_map.clear()
        self.initial_data_map.clear()
        self.pending_subscriptions.clear()
```

**修改为**:
```python
async def unsubscribe(self):
    # ...
    async with self._lock:
        self.subscription_map.clear()
        self.pubkey_to_subscription.clear()
        self.decode_map.clear()
        self.data_map.clear()
        self.initial_data_map.clear()
        self.pubkey_to_oracle_ids.clear()
        self.oracle_id_to_subscription.clear()
        self.pending_subscriptions.clear()
        if hasattr(self, 'inflight_subscribes'):
            self.inflight_subscribes.clear()
```

---

## BUG2修复：实现request_id匹配机制

### 文件1: `src/driftpy/accounts/ws/multi_account_subscriber.py`

#### 修改1: 初始化 - 添加request_id相关数据结构（在__init__中）

**在L33之后添加**:
```python
import itertools
import json
from typing import Tuple

# 在__init__方法中，L33之后添加
self.request_id_counter = itertools.count(1)  # ⭐ request_id计数器
self.inflight_subscribes: Dict[int, Tuple[Pubkey, Optional[str]]] = {}  # ⭐ request_id -> (pubkey, oracle_id)
```

#### 修改2: add_account方法 - 使用直接发送JSON-RPC消息（L66-86）

**原代码**:
```python
if self.ws is not None:
    try:
        async with self._lock:
            self.pending_subscriptions.append((pubkey, oracle_id))
        
        await self.ws.account_subscribe(
            pubkey,
            commitment=self.commitment,
            encoding="base64",
        )
```

**修改为**:
```python
if self.ws is not None:
    try:
        # ⭐ 生成request_id
        request_id = next(self.request_id_counter)
        
        # ⭐ 记录映射
        async with self._lock:
            self.inflight_subscribes[request_id] = (pubkey, oracle_id)
            self.pending_subscriptions.append((pubkey, oracle_id))  # 保留作为备选
        
        # ⭐ 直接构造并发送JSON-RPC消息
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
        
        # ⭐ 直接使用ws.send()发送
        await self.ws.send(json.dumps(message))
    except Exception as e:
        print(f"Error subscribing to account {pubkey}: {e}")
        async with self._lock:
            # 清理inflight_subscribes
            request_id_to_remove = None
            for rid, (p, oid) in self.inflight_subscribes.items():
                if p == pubkey and oid == oracle_id:
                    request_id_to_remove = rid
                    break
            if request_id_to_remove is not None:
                del self.inflight_subscribes[request_id_to_remove]
            
            # 清理pending_subscriptions
            if (pubkey, oracle_id) in self.pending_subscriptions:
                self.pending_subscriptions.remove((pubkey, oracle_id))
```

#### 修改3: _subscribe_ws方法 - 订阅确认处理（L151-165）

**原代码**:
```python
if isinstance(result, int):
    async with self._lock:
        if self.pending_subscriptions:
            pubkey = self.pending_subscriptions.pop(0)
            subscription_id = result
            self.subscription_map[subscription_id] = pubkey
            self.pubkey_to_subscription[pubkey] = subscription_id
```

**修改为**:
```python
if isinstance(result, int):  # 订阅确认
    async with self._lock:
        subscription_id = result
        pubkey = None
        oracle_id = None
        
        # ⭐ 使用id字段匹配
        if hasattr(msg[0], 'id') and msg[0].id is not None:
            request_id = msg[0].id
            
            # ⭐ 从inflight_subscribes中查找
            if request_id in self.inflight_subscribes:
                pubkey, oracle_id = self.inflight_subscribes.pop(request_id)
                # ✅ 准确匹配！
            else:
                # 回退到顺序匹配（向后兼容）
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
            
            # ⭐ 如果使用oracle_id，建立oracle_id到subscription_id的映射
            if oracle_id:
                self.oracle_id_to_subscription[oracle_id] = subscription_id
```

#### 修改4: _subscribe_ws方法 - 初始订阅（L130-141）

**原代码**:
```python
for pubkey in initial_accounts:
    try:
        await ws.account_subscribe(
            pubkey,
            commitment=self.commitment,
            encoding="base64",
        )
```

**修改为**:
```python
for pubkey, oracle_id in initial_subscriptions:
    try:
        # ⭐ 生成request_id
        request_id = next(self.request_id_counter)
        
        # ⭐ 记录映射
        async with self._lock:
            self.inflight_subscribes[request_id] = (pubkey, oracle_id)
        
        # ⭐ 直接构造并发送JSON-RPC消息
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
        
        # ⭐ 直接使用ws.send()发送
        await ws.send(json.dumps(message))
    except Exception as e:
        print(f"Error subscribing to account {pubkey}: {e}")
        async with self._lock:
            # 清理inflight_subscribes
            request_id_to_remove = None
            for rid, (p, oid) in self.inflight_subscribes.items():
                if p == pubkey and oid == oracle_id:
                    request_id_to_remove = rid
                    break
            if request_id_to_remove is not None:
                del self.inflight_subscribes[request_id_to_remove]
            
            if (pubkey, oracle_id) in self.pending_subscriptions:
                self.pending_subscriptions.remove((pubkey, oracle_id))
```

---

## 文件2: `src/driftpy/accounts/ws/drift_client.py`

#### 修改1: subscribe_to_oracle方法（L176-192）

**原代码**:
```python
async def subscribe_to_oracle(self, full_oracle_wrapper: FullOracleWrapper):
    if full_oracle_wrapper.pubkey == Pubkey.default():
        return
    
    oracle_id = get_oracle_id(
        full_oracle_wrapper.pubkey,
        full_oracle_wrapper.oracle_source,
    )
    if full_oracle_wrapper.pubkey in self.oracle_subscriber.data_map:
        return
    
    await self.oracle_subscriber.add_account(
        full_oracle_wrapper.pubkey,
        get_oracle_decode_fn(full_oracle_wrapper.oracle_source),
        initial_data=full_oracle_wrapper.oracle_price_data_and_slot,
    )
    self.oracle_id_to_pubkey[oracle_id] = full_oracle_wrapper.pubkey
```

**修改为**:
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

#### 修改2: subscribe_to_oracle_info方法（L194-214）

**原代码**:
```python
async def subscribe_to_oracle_info(
    self, oracle_info: OracleInfo | FullOracleWrapper
):
    # ...
    if oracle_info.pubkey in self.oracle_subscriber.data_map:
        return
    
    await self.oracle_subscriber.add_account(
        oracle_info.pubkey,
        get_oracle_decode_fn(source),
    )
```

**修改为**:
```python
async def subscribe_to_oracle_info(
    self, oracle_info: OracleInfo | FullOracleWrapper
):
    source = None
    if isinstance(oracle_info, FullOracleWrapper):
        source = oracle_info.oracle_source
    else:
        source = oracle_info.source
    
    oracle_id = get_oracle_id(oracle_info.pubkey, source)
    if oracle_info.pubkey == Pubkey.default():
        return
    
    # ⭐ 使用oracle_id检查是否已订阅
    if oracle_id in self.oracle_subscriber.data_map:
        return
    
    # ⭐ 传递oracle_id参数
    await self.oracle_subscriber.add_account(
        oracle_info.pubkey,
        get_oracle_decode_fn(source),
        oracle_id=oracle_id,  # ⭐ 新增参数
    )
    self.oracle_id_to_pubkey[oracle_id] = oracle_info.pubkey
```

#### 修改3: get_oracle_price_data_and_slot方法（L267-273）

**原代码**:
```python
def get_oracle_price_data_and_slot(
    self, oracle_id: str
) -> Optional[DataAndSlot[OraclePriceData]]:
    pubkey = self.oracle_id_to_pubkey.get(oracle_id)
    if pubkey is None:
        return None
    return self.oracle_subscriber.get_data(pubkey)
```

**修改为**:
```python
def get_oracle_price_data_and_slot(
    self, oracle_id: str
) -> Optional[DataAndSlot[OraclePriceData]]:
    # ⭐ 直接使用oracle_id获取数据
    return self.oracle_subscriber.get_data(oracle_id)
```

---

## 文件3: `src/driftpy/oracles/oracle_id.py`（可选但建议修复）

#### 修改: get_oracle_source_num方法 - 修复匹配顺序（L6-42）

**原代码**:
```python
def get_oracle_source_num(source: OracleSource) -> int:
    source_str = str(source)
    
    if "Pyth1M" in source_str:
        return OracleSourceNum.PYTH_1M
    elif "Pyth1K" in source_str:
        return OracleSourceNum.PYTH_1K
    elif "PythPull" in source_str:
        # ...
```

**问题**: `"Pyth1M"`会匹配`"Pyth1MPull"`，导致`Pyth1MPull`被错误识别为`Pyth1M`。

**修改为**:
```python
def get_oracle_source_num(source: OracleSource) -> int:
    source_str = str(source)
    
    # ⭐ 关键：先匹配更具体的（长的），再匹配通用的（短的）
    if "Pyth1MPull" in source_str:
        return OracleSourceNum.PYTH_1M_PULL
    elif "Pyth1KPull" in source_str:
        return OracleSourceNum.PYTH_1K_PULL
    elif "PythStableCoinPull" in source_str:
        return OracleSourceNum.PYTH_STABLE_COIN_PULL
    elif "PythLazer1M" in source_str:
        return OracleSourceNum.PYTH_LAZER_1M
    elif "PythLazer1K" in source_str:
        return OracleSourceNum.PYTH_LAZER_1K
    elif "PythLazerStableCoin" in source_str:
        return OracleSourceNum.PYTH_LAZER_STABLE_COIN
    elif "Pyth1M" in source_str:
        return OracleSourceNum.PYTH_1M
    elif "Pyth1K" in source_str:
        return OracleSourceNum.PYTH_1K
    elif "PythLazer" in source_str:
        return OracleSourceNum.PYTH_LAZER
    elif "PythStableCoin" in source_str:
        return OracleSourceNum.PYTH_STABLE_COIN
    elif "PythPull" in source_str:
        return OracleSourceNum.PYTH_PULL
    elif "Pyth" in source_str:
        return OracleSourceNum.PYTH
    elif "SwitchboardOnDemand" in source_str:
        return OracleSourceNum.SWITCHBOARD_ON_DEMAND
    elif "Switchboard" in source_str:
        return OracleSourceNum.SWITCHBOARD
    elif "QuoteAsset" in source_str:
        return OracleSourceNum.QUOTE_ASSET
    elif "Prelaunch" in source_str:
        return OracleSourceNum.PRELAUNCH
    
    raise ValueError("Invalid oracle source")
```

---

## 实施步骤

1. **第一步：修复BUG1**
   - 修改`multi_account_subscriber.py`的数据结构
   - 修改`add_account`、`remove_account`、`get_data`等方法
   - 修改`drift_client.py`传递`oracle_id`参数

2. **第二步：修复BUG2**
   - 添加`request_id_counter`和`inflight_subscribes`
   - 修改`add_account`使用`ws.send()`直接发送JSON-RPC消息
   - 修改订阅确认处理逻辑使用`id`字段匹配

3. **第三步：修复get_oracle_source_num（可选但建议）**
   - 修复匹配顺序，先匹配更具体的

4. **第四步：测试**
   - 测试同一pubkey不同oracle_source的订阅
   - 测试并发订阅时的确认匹配
   - 验证价格数据正确性

---

## 注意事项

1. **向后兼容性**：
   - `oracle_id`参数设为可选
   - 非oracle账户仍使用`pubkey`作为key
   - 如果无法获取`request_id`，回退到顺序匹配

2. **错误处理**：
   - 如果`ws.send()`失败，回退到使用`account_subscribe`
   - 如果响应消息没有`id`字段，回退到顺序匹配

3. **性能考虑**：
   - `inflight_subscribes`映射需要定期清理（超时的请求）
   - 多个oracle_id时，尝试解码可能影响性能（但这是必要的）

4. **测试重点**：
   - `PUMP-PERP`和`1KPUMP-PERP`共享pubkey的情况
   - 并发订阅时的确认匹配
   - 重连后的订阅恢复
