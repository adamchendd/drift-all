# data_map key 类型变化说明

## 问题背景

### 修改前的代码结构

**`multi_account_subscriber.py`** (修改前):
```python
self.data_map: Dict[Pubkey, Optional[DataAndSlot]] = {}
# key 类型：Pubkey 对象
# 例如：data_map[Pubkey("5r8RWTaRiMgr9Lph3FTUE3sGb1vymhpCrm83Bovjfcps")] = data
```

**`drift_client.py`** (修改前):
```python
async def _set_perp_oracle_map(self):
    oracle = perp_market_account.amm.oracle  # oracle 是 Pubkey 对象
    if oracle not in self.oracle_subscriber.data_map:  # ✅ 可以工作
        await self.add_oracle(...)
```

### 修改后的代码结构

**`multi_account_subscriber.py`** (修改后):
```python
self.data_map: Dict[str, Optional[DataAndSlot]] = {}
# key 类型：str（oracle_id 或 pubkey 字符串）
# 例如：data_map["5r8RWTaRiMgr9Lph3FTUE3sGb1vymhpCrm83Bovjfcps-1"] = data
# 或者：data_map["5r8RWTaRiMgr9Lph3FTUE3sGb1vymhpCrm83Bovjfcps"] = data（非oracle账户）
```

**`drift_client.py`** (修改后，但有问题):
```python
async def _set_perp_oracle_map(self):
    oracle = perp_market_account.amm.oracle  # oracle 是 Pubkey 对象
    oracle_id = get_oracle_id(oracle, perp_market_account.amm.oracle_source)  # oracle_id 是字符串
    if oracle not in self.oracle_subscriber.data_map:  # ❌ 问题：oracle 是 Pubkey，但 data_map 的 key 是 str
        await self.add_oracle(...)
```

---

## 问题详解

### 为什么 `oracle not in self.oracle_subscriber.data_map` 会失败？

**Python 字典查找机制**：
- 字典的 `in` 操作符使用 key 的**值相等性**（`==`）和**哈希值**（`hash()`）来查找
- `Pubkey` 对象和字符串 `str` 是**不同类型**，即使它们表示同一个地址，也不会相等

**示例**：
```python
# 修改后的 data_map
data_map = {
    "5r8RWTaRiMgr9Lph3FTUE3sGb1vymhpCrm83Bovjfcps-1": data1,  # oracle_id（字符串）
    "5r8RWTaRiMgr9Lph3FTUE3sGb1vymhpCrm83Bovjfcps-2": data2,  # oracle_id（字符串）
}

# 检查
oracle = Pubkey("5r8RWTaRiMgr9Lph3FTUE3sGb1vymhpCrm83Bovjfcps")  # Pubkey 对象
oracle_id = "5r8RWTaRiMgr9Lph3FTUE3sGb1vymhpCrm83Bovjfcps-1"     # 字符串

# ❌ 错误：oracle（Pubkey对象）不在 data_map 中（因为 key 是字符串）
oracle in data_map  # False（即使有对应的数据）

# ✅ 正确：oracle_id（字符串）在 data_map 中
oracle_id in data_map  # True
```

### 实际影响

**问题场景**：
1. 第一次调用 `_set_perp_oracle_map()`：
   - `oracle` = `Pubkey("5r8RWTaRiMgr9Lph3FTUE3sGb1vymhpCrm83Bovjfcps")`
   - `oracle_id` = `"5r8RWTaRiMgr9Lph3FTUE3sGb1vymhpCrm83Bovjfcps-1"`
   - 检查：`oracle not in data_map` → `True`（因为 `Pubkey` 对象不在字符串 key 的字典中）
   - 结果：调用 `add_oracle()`，订阅成功，`data_map[oracle_id] = data`

2. 第二次调用 `_set_perp_oracle_map()`（例如重连后）：
   - `oracle` = `Pubkey("5r8RWTaRiMgr9Lph3FTUE3sGb1vymhpCrm83Bovjfcps")`
   - `oracle_id` = `"5r8RWTaRiMgr9Lph3FTUE3sGb1vymhpCrm83Bovjfcps-1"`
   - 检查：`oracle not in data_map` → `True`（仍然返回 `True`，因为 `Pubkey` 对象不在字符串 key 的字典中）
   - 结果：**错误地再次调用 `add_oracle()`**，导致重复订阅

---

## 修复方案

### 修复后的代码

**`drift_client.py`** (修复后):
```python
async def _set_perp_oracle_map(self):
    perp_market_account = market.data
    market_index = perp_market_account.market_index
    oracle = perp_market_account.amm.oracle  # Pubkey 对象
    oracle_id = get_oracle_id(oracle, perp_market_account.amm.oracle_source)  # 字符串
    
    # ✅ 修复：检查 oracle_id（字符串）而不是 oracle（Pubkey对象）
    if oracle_id not in self.oracle_subscriber.data_map:
        await self.add_oracle(
            OracleInfo(oracle, perp_market_account.amm.oracle_source)
        )
    self.perp_market_oracle_map[market_index] = oracle
    self.perp_market_oracle_strings_map[market_index] = oracle_id
```

### 为什么修复后可以工作？

**修复后的逻辑**：
1. 第一次调用：
   - `oracle_id` = `"5r8RWTaRiMgr9Lph3FTUE3sGb1vymhpCrm83Bovjfcps-1"`
   - 检查：`oracle_id not in data_map` → `True`（确实不存在）
   - 结果：调用 `add_oracle()`，订阅成功，`data_map[oracle_id] = data`

2. 第二次调用：
   - `oracle_id` = `"5r8RWTaRiMgr9Lph3FTUE3sGb1vymhpCrm83Bovjfcps-1"`
   - 检查：`oracle_id not in data_map` → `False`（已存在）
   - 结果：**跳过 `add_oracle()`**，避免重复订阅

---

## 总结

### 问题根源

1. **类型不匹配**：`data_map` 的 key 从 `Pubkey` 改为 `str`（oracle_id）
2. **检查逻辑未更新**：`_set_perp_oracle_map` 和 `_set_spot_oracle_map` 仍在使用 `Pubkey` 对象检查
3. **结果**：检查总是返回 `True`，导致重复订阅

### 修复方法

- **修改前**：`if oracle not in self.oracle_subscriber.data_map:`
- **修改后**：`if oracle_id not in self.oracle_subscriber.data_map:`

### 关键点

- `oracle` 是 `Pubkey` 对象
- `oracle_id` 是字符串（格式：`"pubkey-source_num"`）
- `data_map` 的 key 现在是字符串
- 必须使用 `oracle_id`（字符串）来检查，而不是 `oracle`（Pubkey对象）
