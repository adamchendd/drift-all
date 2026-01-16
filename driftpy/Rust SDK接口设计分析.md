# Rust SDK接口设计分析：关键发现

## 关键发现：Rust SDK的接口设计

### Rust SDK的获取Oracle数据接口

**Rust SDK** (`oraclemap.rs` L366-374):
```rust
/// Return Oracle data by market, if known
pub fn get_by_market(&self, market: &MarketId) -> Option<Oracle> {
    if let Some((oracle_pubkey, oracle_source)) = self.oracle_by_market.get(market) {
        self.oraclemap
            .get(&(*oracle_pubkey, *oracle_source as u8))
            .map(|o| o.clone())
    } else {
        None
    }
}
```

**关键点**：
- ✅ **使用`MarketId`作为参数**，不是`pubkey`！
- ✅ **通过`oracle_by_market`映射**：`MarketId -> (Pubkey, OracleSource)`
- ✅ **每个market对应一个唯一的`(pubkey, source)`组合**
- ✅ **不存在"多个oracle_id时返回哪个"的问题**，因为每个market唯一对应一个oracle

### Rust SDK的架构

**Rust SDK** (`oraclemap.rs` L58-61):
```rust
pub struct OracleMap {
    /// Oracle data keyed by pubkey and source
    pub oraclemap: Arc<DashMap<(Pubkey, u8), Oracle>>,
    /// Oracle (pubkey, source) by MarketId (immutable)
    pub oracle_by_market: ReadOnlyView<MarketId, (Pubkey, OracleSource)>,
    /// map from oracle to consuming markets/source types
    shared_oracles: ReadOnlyView<Pubkey, OracleShareMode>,
}
```

**关键点**：
- ✅ **`oracle_by_market`**：`MarketId -> (Pubkey, OracleSource)`（每个market唯一对应）
- ✅ **`oraclemap`**：`(Pubkey, u8) -> Oracle`（存储数据）
- ✅ **`shared_oracles`**：`Pubkey -> OracleShareMode`（记录共享关系）

### Rust SDK的调用方式

**Rust SDK** (`lib.rs` L1536-1538):
```rust
pub fn try_get_oracle_price_data_and_slot(&self, market: MarketId) -> Option<Oracle> {
    self.oracle_map.get_by_market(&market)
}
```

**关键点**：
- ✅ **通过`MarketId`获取**，不是通过`pubkey`
- ✅ **每个market唯一对应一个oracle**，不存在歧义

---

## Python SDK的接口设计

### Python SDK的获取Oracle数据接口

**Python SDK** (`drift_client.py` L267-273):
```python
def get_oracle_price_data_and_slot(
    self, oracle_id: str
) -> Optional[DataAndSlot[OraclePriceData]]:
    pubkey = self.oracle_id_to_pubkey.get(oracle_id)
    if pubkey is None:
        return None
    return self.oracle_subscriber.get_data(pubkey)  # ⚠️ 使用pubkey
```

**关键点**：
- ✅ **已经接受`oracle_id`参数**（与Rust SDK的`MarketId`类似）
- ⚠️ **但内部使用`get_data(pubkey)`**，这会导致问题

### Python SDK的架构

**Python SDK** (`drift_client.py` L57-58):
```python
self.oracle_subscriber = WebsocketMultiAccountSubscriber(program, commitment)
self.oracle_id_to_pubkey: dict[str, Pubkey] = {}
```

**关键点**：
- ⚠️ **`WebsocketMultiAccountSubscriber`是通用的多账户订阅器**
- ⚠️ **不仅用于oracle，还可能用于其他账户**
- ⚠️ **`get_data(pubkey)`是通用接口**

---

## 关键差异和问题

### 差异1：接口设计层次

**Rust SDK**：
- `OracleMap`是**专门用于oracle的类**
- 接口：`get_by_market(market: MarketId)`
- 每个market唯一对应一个oracle

**Python SDK**：
- `WebsocketMultiAccountSubscriber`是**通用的多账户订阅器**
- 接口：`get_data(pubkey: Pubkey)`（通用）
- 可能用于oracle、User账户、Market账户等

### 差异2：数据获取方式

**Rust SDK**：
- 通过`MarketId`获取（每个market唯一对应）
- 不存在"多个oracle_id时返回哪个"的问题

**Python SDK**：
- 通过`pubkey`获取（可能多个oracle_id共享）
- 存在"多个oracle_id时返回哪个"的问题

---

## 修正后的方案：完全参考Rust SDK

### 关键修正：接口设计层次

**Rust SDK的设计**：
- `OracleMap`专门用于oracle
- 接口：`get_by_market(market: MarketId)`
- 每个market唯一对应一个oracle

**Python SDK应该的设计**：
- `WebsocketMultiAccountSubscriber`是通用的
- 但对于oracle，应该通过`oracle_id`获取（类似Rust SDK的`MarketId`）
- `get_data(pubkey)`保持通用性，用于非oracle账户

### 修正后的接口设计

#### 1. `WebsocketMultiAccountSubscriber.get_data`（通用接口）

**保持向后兼容**：
```python
def get_data(self, key: Union[Pubkey, str]) -> Optional[DataAndSlot]:
    """
    获取数据（通用接口）
    - 如果key是Pubkey：用于非oracle账户（向后兼容）
    - 如果key是str（oracle_id）：用于oracle账户（新方式）
    """
    if isinstance(key, Pubkey):
        # 向后兼容：非oracle账户
        oracle_ids = self.pubkey_to_oracle_ids.get(key, set())
        if len(oracle_ids) == 0:
            # 没有oracle_id，使用pubkey（非oracle账户）
            key = str(key)
        elif len(oracle_ids) == 1:
            # 只有一个oracle_id，使用它
            key = list(oracle_ids)[0]
        else:
            # ⚠️ 多个oracle_id，返回第一个（向后兼容，但建议使用oracle_id）
            key = list(oracle_ids)[0]
            print(f"Warning: pubkey {key} has multiple oracle_ids, returning first one. "
                  f"Please use get_data(oracle_id) to specify.")
    return self.data_map.get(key)
```

#### 2. `WebsocketDriftClientAccountSubscriber.get_oracle_price_data_and_slot`（Oracle专用接口）

**完全参考Rust SDK**：
```python
def get_oracle_price_data_and_slot(
    self, oracle_id: str
) -> Optional[DataAndSlot[OraclePriceData]]:
    """
    获取Oracle价格数据（Oracle专用接口，类似Rust SDK的get_by_market）
    - 使用oracle_id作为参数（类似Rust SDK的MarketId）
    - 每个oracle_id唯一对应一个oracle数据
    """
    # ✅ 直接使用oracle_id获取（类似Rust SDK使用MarketId）
    return self.oracle_subscriber.get_data(oracle_id)
```

**关键点**：
- ✅ **类似Rust SDK的`get_by_market(market: MarketId)`**
- ✅ **使用`oracle_id`作为参数**（类似`MarketId`）
- ✅ **每个`oracle_id`唯一对应一个oracle数据**（类似每个`MarketId`唯一对应）

---

## 完整的兼容性方案

### 1. 保持`get_data(pubkey)`的通用性

**用途**：
- 非oracle账户（如User账户、Market账户）
- 向后兼容

**实现**：
```python
def get_data(self, key: Union[Pubkey, str]) -> Optional[DataAndSlot]:
    if isinstance(key, Pubkey):
        # 非oracle账户：使用pubkey
        oracle_ids = self.pubkey_to_oracle_ids.get(key, set())
        if len(oracle_ids) == 0:
            key = str(key)  # 非oracle账户
        elif len(oracle_ids) == 1:
            key = list(oracle_ids)[0]  # 只有一个oracle_id
        else:
            # 多个oracle_id：返回第一个（向后兼容）
            key = list(oracle_ids)[0]
    return self.data_map.get(key)
```

### 2. Oracle专用接口使用`oracle_id`

**用途**：
- Oracle账户
- 类似Rust SDK的`get_by_market(market: MarketId)`

**实现**：
```python
def get_oracle_price_data_and_slot(
    self, oracle_id: str
) -> Optional[DataAndSlot[OraclePriceData]]:
    # ✅ 直接使用oracle_id（类似Rust SDK使用MarketId）
    return self.oracle_subscriber.get_data(oracle_id)
```

### 3. 订阅检查使用`oracle_id`

**原有代码** (`drift_client.py` L184, L207):
```python
if full_oracle_wrapper.pubkey in self.oracle_subscriber.data_map:  # ⚠️ 错误
    return
```

**修正后**：
```python
oracle_id = get_oracle_id(...)
if oracle_id in self.oracle_subscriber.data_map:  # ✅ 使用oracle_id
    return
```

---

## 总结：完全参考Rust SDK的设计

### Rust SDK的设计原则

1. **专用接口**：`OracleMap.get_by_market(market: MarketId)`
   - 专门用于oracle
   - 使用`MarketId`作为参数（每个market唯一对应）

2. **存储结构**：`(Pubkey, u8) -> Oracle`
   - 使用复合key支持同一pubkey多个source

3. **映射关系**：`MarketId -> (Pubkey, OracleSource)`
   - 每个market唯一对应一个oracle

### Python SDK应该的设计

1. **通用接口**：`WebsocketMultiAccountSubscriber.get_data(key)`
   - 用于所有账户类型
   - 支持`Pubkey`（非oracle）或`oracle_id`（oracle）

2. **Oracle专用接口**：`WebsocketDriftClientAccountSubscriber.get_oracle_price_data_and_slot(oracle_id)`
   - 专门用于oracle
   - 使用`oracle_id`作为参数（类似Rust SDK的`MarketId`）

3. **存储结构**：`oracle_id -> DataAndSlot`
   - 使用`oracle_id`作为key（类似Rust SDK的`(Pubkey, u8)`）

4. **映射关系**：`oracle_id -> (Pubkey, OracleSource)`
   - 每个`oracle_id`唯一对应一个oracle（类似每个`MarketId`唯一对应）

---

## 关键修正点

1. ✅ **`get_data(pubkey)`保持通用性**：用于非oracle账户
2. ✅ **`get_oracle_price_data_and_slot(oracle_id)`使用oracle_id**：类似Rust SDK的`get_by_market(market)`
3. ✅ **订阅检查使用`oracle_id`**：不是`pubkey`
4. ✅ **每个`oracle_id`唯一对应一个oracle数据**：类似Rust SDK的每个`MarketId`唯一对应
