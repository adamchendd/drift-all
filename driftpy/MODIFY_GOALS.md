# 修改目标（待完善版）

> 目的：补齐当前修改中仍存在的三类风险，并尽量与 Rust SDK 行为保持一致。
> 范围：仅针对 WebSocket 订阅、多源 oracle 解码与对外 API 行为，不引入新特性。

## 目标 1：彻底避免同一 pubkey 的重复订阅（inflight 去重）

### 问题
- 目前只在 `pubkey_to_subscription` 里去重。
- 订阅确认之前的 in-flight 请求不在该 map，导致并发 add_account 仍可能重复订阅。

### 目标
- 对“已订阅”和“订阅请求已发出但未确认”的 pubkey 都进行去重。

### 建议实现
- 新增 `pubkey_inflight: Set[Pubkey]` 或者反查 `inflight_subscribes`。
- 当发送 accountSubscribe 前：
  - 若 pubkey 在 `pubkey_to_subscription` 或 `pubkey_inflight` 中，则不再发起订阅。
  - 若发送成功，则把 pubkey 放入 `pubkey_inflight`。
  - 当收到订阅确认或失败时，移除 `pubkey_inflight`。

### 验收
- 同一 pubkey 的多个 oracle_id 并发 add_account 不会产生多个 subscription_id。

---

## 目标 2：取消订阅必须与 raw websocket 路径一致

### 问题
- 当前连接用的是 `websockets.connect`，但仍调用 `account_unsubscribe`。
- raw websocket 没有该方法，异常被吞掉导致 unsubscribe 实际失效。

### 目标
- 统一订阅和取消订阅的协议方式。

### 建议实现
- 使用 JSON-RPC 发送 `accountUnsubscribe`：
  - `{"jsonrpc":"2.0","id":<request_id>,"method":"accountUnsubscribe","params":[subscription_id]}`
- 可复用现有 `request_id_counter` 生成 id。

### 验收
- `remove_account` 和 `unsubscribe` 能真正停止链上推送。

---

## 目标 3：对外 API 语义与 Rust 一致（避免歧义）

### 问题
- `get_data(pubkey)` 在多 oracle_id 时返回不确定值。
- `fetch(pubkey)` 不传 oracle_id 时无法正确 refresh。

### 目标
- 与 Rust SDK 一致：oracle 数据应以 `oracle_id`（或 `(pubkey, source)`）为主索引。

### 建议实现
- 对外明确：
  - `get_data(oracle_id)` 为主路径。
  - `get_data(pubkey)` 若多 source 则返回 None 并提示（避免随机返回）。
- `fetch(pubkey)` 改为：
  - 若该 pubkey 有多个 oracle_id，必须显式传 oracle_id。
  - 若只有一个 oracle_id，可自动选唯一值。

### 验收
- 对 oracle 的读取刷新逻辑与 Rust “(pubkey, source)” 结构一致且确定性。

---

## 目标 4：多源 oracle 订阅逻辑与 Rust 对齐

### 问题
- 逻辑层散落在 subscriber；Rust 在 OracleMap 层集中处理 mixed source。

### 目标
- 继续保持 subscriber 只负责“单 pubkey 订阅与更新分发”。
- oracle 多源逻辑尽量集中到 `drift_client` / `oracle map` 语义层。

### 建议实现（可选）
- 订阅层只产出 pubkey 更新（bytes + slot）。
- oracle 解码与分发由上层根据 `oracle_id -> (pubkey, source)` 进行。

---

## 目标 5：补充简单并发/订阅一致性测试（可选）

- 并发 add_account 同一 pubkey + 多 oracle_id：确认只产生 1 次订阅。
- unsubscribe 后确保服务端不再推送消息。
- get_data/fetch 在多 oracle_id 情况下行为稳定。

