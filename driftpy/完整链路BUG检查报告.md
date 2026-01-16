# 完整链路BUG检查报告

## 检查范围

1. **订阅链路**：从 `subscribe_to_oracle` 到 WebSocket 订阅确认
2. **存储链路**：从 WebSocket 数据更新到 `data_map` 存储
3. **读取链路**：从 `get_data` / `get_oracle_price_data_and_slot` 到数据返回

---

## 1. 订阅链路检查

### 1.1 `subscribe_to_oracle` → `add_account`

**流程**：
```
subscribe_to_oracle(full_oracle_wrapper)
  → 检查 pubkey in pubkey_to_subscription
  → 如果已订阅：只添加 oracle_id 映射 ✅
  → 如果未订阅：调用 add_account(pubkey, decode_fn, oracle_id) ✅
```

**检查结果**：✅ **正确**

### 1.2 `add_account` → WebSocket订阅

**流程**：
```
add_account(pubkey, decode_fn, oracle_id)
  → 检查 pubkey in pubkey_to_subscription ✅
  → 存储 decode_map[oracle_id] = decode_fn ✅
  → 存储 data_map[oracle_id] = initial_data ✅
  → 维护 pubkey_to_oracle_ids[pubkey].add(oracle_id) ✅
  → 生成 request_id ✅
  → 发送 JSON-RPC 订阅请求 ✅
```

**检查结果**：✅ **正确**

### 1.3 订阅确认处理

**流程**：
```
收到订阅确认（result是int）
  → 使用 msg[0].id 查找 inflight_subscribes ✅
  → 获取 (pubkey, oracle_id) ✅
  → 建立 subscription_map[subscription_id] = pubkey ✅
  → 建立 pubkey_to_subscription[pubkey] = subscription_id ✅
  → 为所有相关的 oracle_id 建立映射 ✅
```

**检查结果**：✅ **正确**

**潜在问题**：
- ⚠️ **问题1**：订阅确认时，`pubkey_to_oracle_ids[pubkey]` 可能只包含订阅时的 `oracle_id`
  - **场景**：第一次订阅 `oracle_id1`，确认时只有 `{oracle_id1}`
  - **后续**：添加 `oracle_id2` 时，在 `subscribe_to_oracle_info` L234 中会更新映射
  - **结论**：✅ 已正确处理

---

## 2. 存储链路检查

### 2.1 WebSocket数据更新处理

**流程**：
```
收到数据更新（result.value存在）
  → 从 subscription_id 获取 pubkey ✅
  → 获取 pubkey_to_oracle_ids[pubkey] ✅
  → 如果 oracle_ids 不为空：
    → 遍历所有 oracle_id ✅
    → 获取 decode_map[oracle_id] ✅
    → 解码并存储到 data_map[oracle_id] ✅
  → 如果 oracle_ids 为空：
    → 使用 str(pubkey) 作为 key ✅
    → 获取 decode_map[str(pubkey)] ✅
    → 解码并存储到 data_map[str(pubkey)] ✅
```

**检查结果**：✅ **基本正确**

**潜在问题**：
- ⚠️ **问题A**：如果 `pubkey_to_oracle_ids[pubkey]` 为空，但 `pubkey` 在 `pubkey_to_subscription` 中（说明是oracle账户，但映射丢失）
  - **场景**：Oracle账户，但 `pubkey_to_oracle_ids` 映射丢失
  - **当前行为**：会进入 `else` 分支，尝试使用 `str(pubkey)` 作为 key
  - **问题**：`decode_map[str(pubkey)]` 可能不存在（因为存储时使用的是 `oracle_id`）
  - **影响**：数据更新失败，但不会崩溃（`decode_fn is None` 会跳过）
  - **修复优先级**：低（不应该发生，但可以添加容错处理）

### 2.2 `_update_data` 方法

**流程**：
```
_update_data(key: str, new_data)
  → 检查 current_data = data_map.get(key) ✅
  → 如果 new_data.slot >= current_data.slot，更新 ✅
```

**检查结果**：✅ **正确**

---

## 3. 读取链路检查

### 3.1 `get_oracle_price_data_and_slot`

**流程**：
```
get_oracle_price_data_and_slot(oracle_id: str)
  → 直接调用 oracle_subscriber.get_data(oracle_id) ✅
```

**检查结果**：✅ **正确**

### 3.2 `get_data` 方法

**流程**：
```
get_data(key: Union[Pubkey, str])
  → 如果 key 是 Pubkey：
    → 获取 pubkey_to_oracle_ids[key] ✅
    → 如果只有一个 oracle_id：使用它 ✅
    → 如果有多个 oracle_id：返回第一个（警告）✅
    → 如果没有 oracle_id：使用 str(pubkey) ✅
  → 返回 data_map.get(key) ✅
```

**检查结果**：✅ **正确**

---

## 4. 发现的潜在问题

### ⚠️ 问题A：数据更新时 pubkey_to_oracle_ids 为空的情况

**位置**：`multi_account_subscriber.py` L316-344

**问题描述**：
- Oracle账户，但 `pubkey_to_oracle_ids[pubkey]` 为空（不应该发生，但可能由于bug导致）
- 会进入 `else` 分支，尝试使用 `str(pubkey)` 作为 key
- 但 `decode_map[str(pubkey)]` 可能不存在（因为存储时使用的是 `oracle_id`）

**当前代码**：
```python
oracle_ids = self.pubkey_to_oracle_ids.get(pubkey, set())

if oracle_ids:
    # 遍历所有 oracle_id
    for oracle_id in oracle_ids:
        ...
else:
    # 非 oracle 账户（向后兼容）
    pubkey_str = str(pubkey)
    decode_fn = self.decode_map.get(pubkey_str)  # ⚠️ 可能为 None
    if decode_fn is not None:
        ...
```

**修复建议**：
```python
else:
    # 非 oracle 账户或 oracle 账户但映射丢失
    pubkey_str = str(pubkey)
    decode_fn = self.decode_map.get(pubkey_str)
    
    # ⚠️ 容错处理：如果是 oracle 账户但映射丢失，尝试从 decode_map 中查找
    if decode_fn is None and pubkey in self.pubkey_to_subscription:
        # 尝试查找所有以 pubkey 开头的 oracle_id（从 decode_map.keys() 中查找）
        pubkey_str_prefix = str(pubkey) + "-"
        for key in self.decode_map.keys():
            if key.startswith(pubkey_str_prefix):
                decode_fn = self.decode_map.get(key)
                if decode_fn is not None:
                    # 使用找到的 oracle_id 作为 key
                    try:
                        slot = int(result.context.slot)
                        account_bytes = cast(bytes, result.value.data)
                        decoded_data = decode_fn(account_bytes)
                        new_data = DataAndSlot(slot, decoded_data)
                        self._update_data(key, new_data)
                    except Exception:
                        continue
        # 如果找到了，就不需要继续处理 pubkey_str
        if decode_fn is not None:
            continue
    
    if decode_fn is not None:
        try:
            slot = int(result.context.slot)
            account_bytes = cast(bytes, result.value.data)
            decoded_data = decode_fn(account_bytes)
            new_data = DataAndSlot(slot, decoded_data)
            self._update_data(pubkey_str, new_data)
        except Exception:
            continue
```

**修复优先级**：低（不应该发生，但可以添加容错处理）

---

### ⚠️ 问题B：初始订阅时 oracle_id 提取

**位置**：`multi_account_subscriber.py` L233

**问题描述**：
- 初始订阅时，从 `key` 中提取 `oracle_id`：`oracle_id = key if key != str(pubkey) else None`
- 但如果 `key` 是 `oracle_id` 格式（`"pubkey-source_num"`），`oracle_id` 会被正确提取
- 但如果 `key` 是 `str(pubkey)`，`oracle_id` 会是 `None`

**当前代码**：
```python
oracle_id = key if key != str(pubkey) else None
self.inflight_subscribes[request_id] = (pubkey, oracle_id)
```

**检查**：
- ✅ 如果 `key` 是 `oracle_id` 格式，`oracle_id` 会被正确提取
- ✅ 如果 `key` 是 `str(pubkey)`，`oracle_id` 是 `None`（非oracle账户）
- ✅ 订阅确认时，如果 `oracle_id` 是 `None`，不会建立 `oracle_id_to_subscription` 映射（正确）

**检查结果**：✅ **正确**

---

### ⚠️ 问题C：订阅确认时，新添加的 oracle_id 映射

**位置**：`multi_account_subscriber.py` L280-283

**问题描述**：
- 订阅确认时，只为 `pubkey_to_oracle_ids[pubkey]` 中的 `oracle_id` 建立映射
- 后续添加的 `oracle_id` 不会自动建立映射

**当前处理**：
- ✅ 在 `subscribe_to_oracle_info` L234 中会更新映射
- ✅ 在 `add_account` L68-69 中会更新映射

**检查结果**：✅ **已正确处理**

---

## 5. 总结

### ✅ 正确的部分

1. **订阅链路**：使用 `pubkey` 检查避免重复订阅，使用 `request_id` 匹配
2. **存储链路**：遍历所有 `oracle_id` 分别解码和存储
3. **读取链路**：支持 `Pubkey` 和 `str`，处理多个 `oracle_id` 的情况

### ⚠️ 潜在问题

1. **问题A**：数据更新时，如果 `pubkey_to_oracle_ids` 为空，会尝试使用 `str(pubkey)`，但 `decode_map[str(pubkey)]` 可能不存在
   - **影响**：数据更新失败，但不会崩溃
   - **修复优先级**：低（不应该发生，但可以添加容错处理）

### 建议

1. **添加容错处理**（可选）：在数据更新的 `else` 分支中，如果 `pubkey` 在 `pubkey_to_subscription` 中，尝试从 `decode_map.keys()` 中查找所有以 `pubkey` 开头的 key
2. **添加日志**（可选）：在关键位置添加日志，便于调试

### 总体评估

**当前实现**：✅ **基本正确，只有一个小问题（问题A）**

**问题A的影响**：
- 只有在 `pubkey_to_oracle_ids` 映射丢失时才会发生
- 不应该在正常情况下发生
- 即使发生，也不会导致崩溃（`decode_fn is None` 会跳过）
- 可以添加容错处理，但优先级较低
