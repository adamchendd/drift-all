# 合约爆仓判断对齐方案（Python SDK vs Rust SDK vs 链上）

> 目标：仅监控“是否可触发合约爆仓处理”，对齐 Python SDK 的判断口径到 Rust SDK 与链上程序。
> 约束：不改变对外公共 API（现有方法签名不变）；新增方法必须为私有或内部使用。
> 备注：不做“退出爆仓/退出清算”判断；只关注三类状态：可清算 / 不可清算 / 正在清算。

## 1. Python SDK 当前判断流程（已更新）

- 文件：`driftpy/src/driftpy/drift_user.py`
- 当前流程：
  - `_calculate_margin_info(MAINTENANCE, margin_buffer, strict, include_open_orders)`
    - 计算 `spot_asset_weighted + perp_pnl_weighted` → `total_collateral`
    - 计算 `spot_liab_weighted + perp_liab_weighted` → `margin_requirement`
    - **不将 liquidation_buffer 加入 margin_requirement**
    - 仅用于 `margin_requirement_plus_buffer` / `total_collateral_buffer`
  - `_liquidation_state(...)`:
    - `is_being_liquidated()` → `BEING_LIQUIDATED`
    - `margin_calc.meets_margin_requirement()` → `NOT_LIQUIDATABLE`
    - 否则 → `LIQUIDATABLE`

**说明**：
  - 入口判断严格对齐链上：使用 `MarginContext::liquidation`，但不把 buffer 加入 `margin_requirement`。
  - buffer 仅用于“退出清算/安全区”相关字段。

---

## 2. Rust SDK 判断流程（对齐链上）

- 文件：`drift-rs-main/crates/src/math/liquidation.rs`（调用链上相同计算口径）
- 核心流程：
  1) 使用 `AccountsListBuilder` 构建账户集合
  2) 调用 `calculate_margin_requirement_and_total_collateral_and_liability_info`
  3) 得到 `MarginCalculation`（统一结构）
  4) 判定逻辑：
     - 非清算：`meets_margin_requirement()` -> 不可清算

---

## 3. 链上程序判断流程（真正确认标准）

- 文件：`protocol-v2-master/programs/drift/src/controller/liquidation.rs`
- 核心流程：
  - 调用 `calculate_margin_requirement_and_total_collateral_and_liability_info`
  - 使用 `MarginContext::liquidation(liquidation_margin_buffer_ratio)`
  - 判定逻辑：
    - 非清算状态：`meets_margin_requirement()` -> 拒绝清算
  - buffer 只影响：
    - `margin_requirement_plus_buffer`
    - `total_collateral_buffer`
  - **不影响进入清算的 `margin_requirement`**

---

## 4. Python SDK 与 Rust/链上的不对齐点（详细）

1) **入口口径需要严格遵循链上 MarginContext::liquidation**
   - 不能把 liquidation buffer 加到 `margin_requirement`
   - 只能用于 `margin_requirement_plus_buffer` / `total_collateral_buffer`

2) **高杠杆模式 margin ratio 路径**
   - 必须基于 `user_high_leverage_mode` 使用 `high_leverage_margin_ratio_maintenance`
   - 再叠加 IMF/size premium 与 `max_margin_ratio` 下限

3) **IMF/size premium 与基础维护比率区分**
   - 前端展示的“维护保证金比率”是基础值
   - 实际用于清算的是 `calculate_market_margin_ratio` 产物

4) **日志缺乏可比对字段**
   - 需要输出 `mr_norm/mr_hl/imf/max_mr/buf` 等字段

5) **三态输出**
   - 明确区分：可清算 / 不可清算 / 正在清算

---

## 5. 对齐思路（不改公共 API，详细实施步骤）

### Step A：补齐 MarginCalculation 关键方法

**文件**：`driftpy/src/driftpy/math/margin_calculation.py`

新增（仅内部使用）：
- `get_free_collateral()`：
  - 返回 `total_collateral - margin_requirement`
- `margin_shortage()`：
  - 返回 `max(0, margin_requirement - total_collateral)`

### Step B：新增统一 margin 计算入口（私有）

**文件**：`driftpy/src/driftpy/drift_user.py`

新增私有方法（示例）：`_calculate_margin_info(...)`

参数建议：
- `margin_category: MarginCategory`
- `margin_buffer: Optional[int]`（仅用于 plus_buffer 字段）
- `strict: bool`
- `include_open_orders: bool`

返回：Python 版 `MarginCalculation`（字段对齐 Rust）

内部计算建议：
- 使用现有方法组合：
  - `get_spot_market_asset_and_liability_value(...)`
  - `get_total_perp_position_liability(...)`
  - `get_unrealized_pnl(...)`
- 汇总字段：
  - `total_collateral = spot_asset_value + perp_pnl`
  - `margin_requirement = perp_liability + spot_liability`
  - `open_orders_margin_requirement`（从 spot/perp 计算中汇总）
  - `total_spot_asset_value / total_spot_liability_value / total_perp_pnl`
  - `margin_requirement_plus_buffer`（仅由 buffer 计算）
  - `total_collateral_buffer`（仅由 buffer 计算）

### Step C：三态判定（仅监控可清算）

**文件**：`driftpy/src/driftpy/drift_user.py`

新增私有方法（示例）：`_liquidation_state()`，返回三态字符串：
- `"LIQUIDATABLE"`：可清算
- `"NOT_LIQUIDATABLE"`：不可清算
- `"BEING_LIQUIDATED"`：正在清算（仅依据用户状态位）

**判定逻辑**：
- 若 `is_being_liquidated()` -> `BEING_LIQUIDATED`
- 否则：
  - 若 `margin_calc.meets_margin_requirement()` -> `NOT_LIQUIDATABLE`
  - 否则 -> `LIQUIDATABLE`

**保持公共 API 不变**：
- `can_be_liquidated()` 仍返回 bool，只基于 `LIQUIDATABLE` 判定。
- 需要三态信息时使用 `_liquidation_state()`（内部使用）。

### Step D：详细日志输出（符合测试程序）

**推荐输出位置**：
- `C:/Users/Administrator/Desktop/fsdownload/测试/liquidator/worker.py` 在发现可清算或正在清算用户时输出详细信息

**日志字段建议（用户级别）**：
- `user_pk`
- `liquidation_state` (LIQUIDATABLE/NOT_LIQUIDATABLE/BEING_LIQUIDATED)
- `total_collateral`
- `margin_requirement`
- `margin_requirement_plus_buffer`
- `free_collateral`
- `margin_shortage`
- `open_orders_margin_requirement`
- `total_perp_pnl`
- `total_spot_asset_value`
- `total_spot_liability_value`
- `num_spot_liabilities`
- `num_perp_liabilities`
- `margin_buffer`
- `strict`
- `user_high_leverage_mode`

**每个标的（market-level）建议字段**：
- `market_index`
- `market_type` (perp/spot)
- `oracle_price`
- `position_base_asset_amount` (perp)
- `position_quote_asset_amount` (perp)
- `spot_token_amount` (spot)
- `liability_value` (perp/spot)
- `asset_value` (spot)
- `open_orders` (perp/spot)
- `margin_ratio`（若可获取）
- `margin_ratio_source`（normal/high_leverage）
- `mr_norm` / `mr_hl` / `imf` / `max_mr` / `buf`

---

## 6. 验收标准（明确对比点）

1) Python 与 Rust 计算出的：
   - total_collateral
   - margin_requirement
   - free_collateral
   - margin_shortage
   应保持一致（误差只来自 oracle 或 slot 时间差）

2) 三态输出准确：
   - 正在清算 -> BEING_LIQUIDATED
   - 可清算 -> LIQUIDATABLE
   - 不可清算 -> NOT_LIQUIDATABLE

3) 当发现可清算用户时，日志按 market-level 输出每个标的计算明细

---

## 7. 修改优先级建议

1) MarginCalculation 补齐 `get_free_collateral` / `margin_shortage`
2) DriftUser 内部新增统一 margin 计算入口
3) 新增 `_liquidation_state()` 三态判定
4) Liquidator 输出结构化日志

---

## 8. 预期效果

- Python SDK 的爆仓判断口径更接近 Rust SDK 与链上程序
- 日志可直接对比链上 MarginCalculation 输出，便于排错与验证
- 输出三态结果，符合“只监控是否可清算”的需求

