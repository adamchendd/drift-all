# Rust SDK如何解决BUG1和BUG2

## BUG1解决方案：同一pubkey不同解码器

### 问题描述

Python SDK的问题：
- 使用`pubkey`作为唯一key存储解码器和数据
- 同一pubkey的不同`oracle_source`会覆盖
- 例如：`PUMP-PERP` (PythLazer) 和 `1KPUMP-PERP` (PythLazer1K) 共享pubkey，第二个会覆盖第一个

### Rust SDK的解决方案

#### 1. 数据结构设计：使用`(Pubkey, u8)`作为key

**代码位置**: `oraclemap.rs` L55

```rust
pub struct OracleMap {
    /// ⭐ 关键：Oracle data keyed by pubkey AND source
    /// 使用 (Pubkey, u8) 作为key，其中u8是OracleSource的枚举值
    pub oraclemap: Arc<DashMap<(Pubkey, u8), Oracle, ahash::RandomState>>,
    
    /// Oracle subscription handles by pubkey (只按pubkey存储，因为WS订阅是按pubkey的)
    subscriptions: DashMap<Pubkey, UnsubHandle, ahash::RandomState>,
    
    /// ⭐ 关键：记录每个pubkey对应的所有sources
    /// map from oracle to consuming markets/source types
    shared_oracles: ReadOnlyView<Pubkey, OracleShareMode, ahash::RandomState>,
}
```

**关键点**：
- ✅ **使用`(Pubkey, u8)`作为key**：`u8`是`OracleSource`的枚举值
- ✅ **同一个pubkey可以存储多个Oracle数据**：每个source对应一个key
- ✅ **WS订阅只按pubkey**：`subscriptions: DashMap<Pubkey, UnsubHandle>`

#### 2. 初始化时检测共享oracle

**代码位置**: `oraclemap.rs` L90-111

```rust
pub fn new(
    pubsub_client: Arc<PubsubClient>,
    all_oracles: &[(MarketId, Pubkey, OracleSource)],
    commitment: CommitmentConfig,
) -> Self {
    // ...
    
    // ⭐ 关键：检测每个pubkey对应的所有sources
    let shared_oracles = DashMap::<Pubkey, OracleShareMode, ahash::RandomState>::default();
    for (_market, (pubkey, source)) in oracle_by_market.iter() {
        shared_oracles
            .entry(*pubkey)
            .and_modify(|m| match m {
                OracleShareMode::Normal {
                    source: existing_source,
                } => {
                    // ⭐ 如果同一个pubkey有不同的source，标记为Mixed
                    if existing_source != source {
                        *m = OracleShareMode::Mixed {
                            sources: vec![*existing_source, *source],
                        }
                    }
                }
                OracleShareMode::Mixed { sources } => {
                    // ⭐ 如果已经是Mixed，添加新的source
                    if !sources.contains(source) {
                        sources.push(*source);
                    }
                }
            })
            .or_insert(OracleShareMode::Normal { source: *source });
    }
}
```

**关键点**：
- ✅ **检测共享oracle**：遍历所有markets，检测同一pubkey是否有多个source
- ✅ **标记为Mixed**：如果同一pubkey有多个source，标记为`OracleShareMode::Mixed`
- ✅ **记录所有sources**：`Mixed { sources: Vec<OracleSource> }`

#### 3. 订阅时只订阅一次（按pubkey去重）

**代码位置**: `oraclemap.rs` L157-172

```rust
for market in markets {
    let (oracle_pubkey, _oracle_source) =
        self.oracle_by_market.get(market).expect("oracle exists");

    // ⭐ 关键：markets can share oracle pubkeys, only want one sub per oracle pubkey
    // 同一个pubkey只订阅一次（即使有多个source）
    if self.subscriptions.contains_key(oracle_pubkey)
        || pending_subscriptions
            .iter()
            .any(|sub| &sub.pubkey == oracle_pubkey)
    {
        log::debug!(
            target: LOG_TARGET,
            "subscription exists: {market:?}/{oracle_pubkey:?}"
        );
        continue;  // 已订阅，跳过
    }

    // 创建订阅器（只按pubkey）
    let oracle_subscriber = WebsocketAccountSubscriber::new(
        Arc::clone(&self.pubsub),
        *oracle_pubkey,
        self.commitment,
    );

    pending_subscriptions.push(oracle_subscriber);
}
```

**关键点**：
- ✅ **按pubkey去重**：同一个pubkey只订阅一次（即使有多个source）
- ✅ **共享WS订阅**：所有使用同一pubkey的markets共享同一个WS订阅

#### 4. 收到更新时，用所有sources解码并更新

**代码位置**: `oraclemap.rs` L183-210

```rust
let futs_iter = pending_subscriptions.into_iter().map(|sub_fut| {
    let oraclemap = Arc::clone(&self.oraclemap);
    // ⭐ 关键：获取该pubkey对应的所有sources
    let oracle_shared_mode = self
        .shared_oracles
        .get(&sub_fut.pubkey)
        .expect("oracle exists")
        .clone();
    let oracle_shared_mode_ref = oracle_shared_mode.clone();
    
    async move {
        let unsub = sub_fut
            .subscribe(Self::SUBSCRIPTION_ID, true, move |update| {
                // ⭐ 关键：根据shared_mode处理更新
                match &oracle_shared_mode_ref {
                    OracleShareMode::Normal { source } => {
                        // 只有一个source，直接处理
                        update_handler(update, *source, &oraclemap)
                    }
                    OracleShareMode::Mixed { sources } => {
                        // ⭐ 关键：多个sources，遍历所有sources处理
                        for source in sources {
                            update_handler(update, *source, &oraclemap);
                        }
                    }
                }
                on_account(update);
            })
            .await;
        ((sub_fut.pubkey, oracle_shared_mode), unsub)
    }
});
```

**关键点**：
- ✅ **获取shared_mode**：从`shared_oracles`获取该pubkey对应的所有sources
- ✅ **Mixed模式处理**：如果是`Mixed`，遍历所有sources，每个source都调用`update_handler`

#### 5. update_handler：用指定source解码并存储

**代码位置**: `oraclemap.rs` L240-280 (推测)

```rust
fn update_handler(
    update: &AccountUpdate,
    source: OracleSource,
    oraclemap: &Arc<DashMap<(Pubkey, u8), Oracle>>,
) {
    let pubkey = update.pubkey;
    let source_u8 = source as u8;
    
    // ⭐ 关键：使用 (pubkey, source_u8) 作为key
    let key = (pubkey, source_u8);
    
    // ⭐ 关键：用指定的source解码
    let oracle_data = decode_with_source(update.account.data, source);
    
    // ⭐ 关键：存储到对应的key
    oraclemap.insert(key, Oracle {
        pubkey,
        data: oracle_data,
        source,
        slot: update.slot,
        raw: update.account.data.clone(),
    });
}
```

**关键点**：
- ✅ **使用`(pubkey, source_u8)`作为key**：每个source对应一个独立的存储位置
- ✅ **用指定source解码**：`decode_with_source(data, source)`
- ✅ **独立存储**：每个source的数据独立存储，不会覆盖

### 总结：Rust SDK解决BUG1的方法

1. **数据结构**：使用`(Pubkey, u8)`作为key，支持同一pubkey多个source
2. **检测共享**：初始化时检测同一pubkey是否有多个source，标记为`Mixed`
3. **订阅去重**：按pubkey去重，同一pubkey只订阅一次
4. **更新处理**：收到更新时，如果是`Mixed`，遍历所有sources，每个source都解码并存储到对应的key

**关键优势**：
- ✅ 同一pubkey的多个source数据独立存储，不会覆盖
- ✅ WS订阅只按pubkey，节省连接
- ✅ 收到更新时，用所有sources解码，确保所有markets都能获取正确的数据

---

## BUG2解决方案：订阅确认顺序假设错误

### 问题描述

Python SDK的问题：
- 使用`pending_subscriptions.pop(0)`假设顺序匹配
- 没有使用响应消息的`id`字段
- 无法获取`request_id`进行准确匹配
- RPC乱序返回确认时，`subscription_id`和`pubkey`错配

### Rust SDK的解决方案

#### 1. 自己维护request_id计数器

**代码位置**: `pubsub-client/lib.rs` L346

```rust
async fn run_ws(...) -> PubsubClientResult {
    // ⭐ 关键：自己维护request_id计数器
    let mut request_id: u64 = 0;
    
    // ...
}
```

**关键点**：
- ✅ **完全控制request_id的生成**：`request_id += 1`
- ✅ **不依赖外部库**：自己维护计数器

#### 2. 直接构造JSON-RPC消息

**代码位置**: `pubsub-client/lib.rs` L577-586

```rust
// 从channel接收订阅请求
subscribe = subscribe_receiver.recv() => {
    let (operation, params, response_sender) = subscribe.expect("subscribe channel");
    
    // ⭐ 关键1: 自增request_id
    request_id += 1;
    
    // ⭐ 关键2: 直接构造JSON-RPC消息（使用自己的request_id）
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
- ✅ **直接构造JSON-RPC消息**：不依赖外部库的方法
- ✅ **使用自己的request_id**：完全控制
- ✅ **发送前记录映射**：`inflight_subscribes.insert(request_id, ...)`

#### 3. 维护inflight_subscribes映射

**代码位置**: `pubsub-client/lib.rs` L381-382

```rust
// ⭐ 关键：维护inflight_subscribes映射
let mut inflight_subscribes = BTreeMap::<u64, (String, String, oneshot::Sender<SubscribeResponseMsg>)>::new();
// request_id -> (operation, payload, response_sender)
```

**关键点**：
- ✅ **维护映射**：`request_id -> (operation, payload, response_sender)`
- ✅ **记录所有未完成的订阅请求**

#### 4. 收到响应时使用id字段匹配

**代码位置**: `pubsub-client/lib.rs` L470-543

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
- ✅ **从响应中提取id字段**：`gjson::get(text, "id")`
- ✅ **使用id查找映射**：`inflight_subscribes.remove(&id)`
- ✅ **准确匹配**：100%准确，不依赖顺序

### 总结：Rust SDK解决BUG2的方法

1. **自己维护request_id计数器**：完全控制request_id的生成
2. **直接构造JSON-RPC消息**：不依赖外部库，使用自己的request_id
3. **维护inflight_subscribes映射**：记录所有未完成的订阅请求
4. **使用id字段匹配**：收到响应时，使用响应消息的`id`字段查找映射，准确匹配

**关键优势**：
- ✅ 100%准确的请求-响应匹配
- ✅ 不依赖顺序假设
- ✅ 支持并发订阅，即使RPC乱序返回也能正确匹配

---

## 对比总结

| 方面 | Python SDK (当前) | Rust SDK |
|------|------------------|----------|
| **BUG1: 同一pubkey不同解码器** | ❌ 使用pubkey作为key，会覆盖 | ✅ 使用`(Pubkey, u8)`作为key，独立存储 |
| **BUG1: 检测共享oracle** | ❌ 没有检测 | ✅ 初始化时检测，标记为`Mixed` |
| **BUG1: 更新处理** | ❌ 只用一个解码器 | ✅ 遍历所有sources，每个都解码并存储 |
| **BUG2: request_id管理** | ❌ 无法获取 | ✅ 自己维护计数器 |
| **BUG2: 请求-响应匹配** | ❌ 假设顺序 | ✅ 使用id字段查找映射 |
| **BUG2: 匹配准确性** | ❌ 可能错配 | ✅ 100%准确 |

## Python SDK需要学习的关键点

### 对于BUG1：
1. **使用复合key**：`(pubkey, oracle_source)` 或 `oracle_id` 作为key
2. **检测共享oracle**：初始化时检测同一pubkey是否有多个source
3. **更新处理**：收到更新时，如果是共享oracle，遍历所有sources解码

### 对于BUG2：
1. **检查solana-py的能力**：是否支持获取或传入request_id
2. **如果支持**：实现inflight_subscribes机制
3. **如果不支持**：使用pubkey锁备选方案（串行化同一pubkey的订阅）
