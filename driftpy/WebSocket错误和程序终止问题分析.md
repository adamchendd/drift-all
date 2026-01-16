# WebSocket 错误和程序终止问题分析

## 1. `Error in websocket connection: 3873` / `Error in websocket connection: 2217`

### 错误位置

**文件**：`src/driftpy/accounts/ws/multi_account_subscriber.py`  
**行号**：L357

### 代码上下文

```python
except Exception as e:
    print(f"Error in websocket connection: {e}")
    self.ws = None
    async with self._lock:
        self.subscription_map.clear()
        self.pubkey_to_subscription.clear()
        self.inflight_subscribes.clear()
    await asyncio.sleep(1)
    continue
```

### 问题分析

**错误原因**：
- 这些数字（`3873`、`2217`）很可能是**异常对象的字符串表示**，而不是错误消息
- 可能的情况：
  1. 异常对象是一个整数（不太可能）
  2. 异常对象是某种特殊类型，`str(e)` 返回了数字
  3. 异常对象是 `websockets` 库的某种错误码

**可能的具体错误**：
- `websockets` 库的连接错误（如连接关闭、超时等）
- JSON 解析错误
- 网络错误

### 修复建议

**改进错误处理**，打印更详细的错误信息：

```python
except Exception as e:
    error_type = type(e).__name__
    error_msg = str(e)
    error_repr = repr(e)
    print(f"Error in websocket connection: {error_type}: {error_msg} (repr: {error_repr})")
    import traceback
    traceback.print_exc()  # 打印完整堆栈跟踪
    self.ws = None
    async with self._lock:
        self.subscription_map.clear()
        self.pubkey_to_subscription.clear()
        self.inflight_subscribes.clear()
    await asyncio.sleep(1)
    continue
```

**或者使用日志系统**：

```python
import logging
log = logging.getLogger(__name__)

except Exception as e:
    log.error(f"Error in websocket connection: {type(e).__name__}: {e}", exc_info=True)
    self.ws = None
    # ... 清理代码
```

---

## 2. `No feed IDs to subscribe`

### 错误位置

**文件**：`测试/liquidator/pyth_subscriber.py`  
**行号**：L148

### 代码上下文

```python
# 获取要订阅的 feed_ids
feed_ids = []
for mi in market_indexes:
    if mi in PERP_MARKET_INDEX_TO_FEED_ID:
        feed_ids.append(PERP_MARKET_INDEX_TO_FEED_ID[mi])

if not feed_ids:
    log.warning("No feed IDs to subscribe")
    return
```

### 问题分析

**错误原因**：
- `PERP_MARKET_INDEX_TO_FEED_ID` 字典中**没有**对应市场索引的 feed_id
- 这意味着要订阅的市场（`market_indexes`）在 `PERP_MARKET_INDEX_TO_FEED_ID` 中找不到对应的 Pyth feed ID

**可能的原因**：
1. `PERP_MARKET_INDEX_TO_FEED_ID` 字典不完整，缺少某些市场
2. 传入的 `market_indexes` 包含了不在字典中的市场索引
3. 字典定义有问题

### 修复建议

**1. 检查 `PERP_MARKET_INDEX_TO_FEED_ID` 字典**：

```python
# 在 pyth_subscriber.py 中
from settings import TARGET_PERP_MARKETS

# 检查哪些市场缺少 feed_id
missing_feed_ids = []
for mi in market_indexes:
    if mi not in PERP_MARKET_INDEX_TO_FEED_ID:
        missing_feed_ids.append(mi)

if missing_feed_ids:
    log.warning(f"Missing feed IDs for markets: {missing_feed_ids}")
```

**2. 改进警告信息**：

```python
if not feed_ids:
    log.warning(
        f"No feed IDs to subscribe. "
        f"Requested markets: {market_indexes}, "
        f"Available feed IDs: {list(PERP_MARKET_INDEX_TO_FEED_ID.keys())}"
    )
    return
```

**3. 如果这是预期的行为**（某些市场不需要 Pyth 订阅），可以降低日志级别：

```python
if not feed_ids:
    log.debug("No feed IDs to subscribe (this may be expected for some markets)")
    return
```

---

## 3. 程序无法使用 CTRL+C 终止

### 问题分析

**查看 `main1.py` 的信号处理代码**：

```python
def _handle_signal():
    try:
        stop_event.set()
    except Exception:
        pass

try:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass
except Exception:
    pass

try:
    await stop_event.wait()
finally:
    for t in tasks:
        t.cancel()
    # ... 清理代码
```

### 可能的问题

**1. `uvloop` 的信号处理问题**：
- 代码使用了 `uvloop.install()`，这可能影响信号处理
- `uvloop` 在某些平台上的信号处理可能与标准 `asyncio` 不同

**2. 任务没有正确取消**：
- 某些任务可能在 `cancel()` 后仍在运行
- WebSocket 连接可能阻塞了事件循环

**3. 信号处理被忽略**：
- `add_signal_handler` 在某些情况下可能失败（Windows 平台限制）
- 异常被静默捕获，导致信号处理未生效

### 修复建议

**1. 改进信号处理**（兼容 Windows 和 Unix）：

```python
import sys
import platform

def _handle_signal():
    try:
        stop_event.set()
        logger.info("收到终止信号，正在关闭...")
    except Exception as e:
        logger.error(f"处理信号时出错: {e}")

# Windows 不支持 add_signal_handler，需要使用其他方法
if platform.system() == "Windows":
    # Windows 使用不同的方法
    import signal
    signal.signal(signal.SIGINT, lambda s, f: _handle_signal())
    signal.signal(signal.SIGTERM, lambda s, f: _handle_signal())
else:
    # Unix/Linux 使用 add_signal_handler
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle_signal)
            except NotImplementedError:
                # 回退到 signal.signal
                signal.signal(sig, lambda s, f: _handle_signal())
    except Exception as e:
        logger.warning(f"无法设置信号处理器: {e}")
        # 回退到 signal.signal
        signal.signal(signal.SIGINT, lambda s, f: _handle_signal())
        signal.signal(signal.SIGTERM, lambda s, f: _handle_signal())
```

**2. 添加超时机制**：

```python
try:
    # 等待停止事件，但设置超时以便定期检查
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            # 定期检查，确保可以响应信号
            continue
except KeyboardInterrupt:
    logger.info("收到 KeyboardInterrupt，正在关闭...")
    stop_event.set()
finally:
    # 强制取消所有任务
    logger.info("正在取消所有任务...")
    for t in tasks:
        if not t.done():
            t.cancel()
    
    # 等待任务取消，但设置超时
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=5.0
        )
    except asyncio.TimeoutError:
        logger.warning("某些任务取消超时，强制退出")
    
    # 强制关闭 WebSocket 连接
    if ORACLE_SUB is not None:
        try:
            await asyncio.wait_for(ORACLE_SUB.unsubscribe(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("Oracle 订阅取消超时")
        except Exception:
            pass
    
    # ... 其他清理代码
```

**3. 添加强制退出机制**：

```python
import sys

# 在 finally 块的最后
logger.info("清理完成，退出程序")
sys.exit(0)  # 强制退出
```

**4. 检查是否有阻塞操作**：

确保所有 WebSocket 连接和长时间运行的任务都有适当的取消机制：

```python
# 在 multi_account_subscriber.py 的 _subscribe_ws 方法中
async def _subscribe_ws(self):
    self._running = True
    while self._running:  # 使用标志而不是 True
        try:
            # ... WebSocket 代码
        except asyncio.CancelledError:
            logger.info("WebSocket 订阅被取消")
            break
        except Exception as e:
            # ... 错误处理
```

---

## 总结

### 问题1：WebSocket 连接错误
- **位置**：`multi_account_subscriber.py` L357
- **原因**：异常对象转换为字符串时显示为数字
- **修复**：改进错误处理，打印详细的错误信息和堆栈跟踪

### 问题2：No feed IDs to subscribe
- **位置**：`pyth_subscriber.py` L148
- **原因**：`PERP_MARKET_INDEX_TO_FEED_ID` 字典中缺少某些市场的 feed_id
- **修复**：检查字典完整性，改进警告信息

### 问题3：无法使用 CTRL+C 终止
- **位置**：`main1.py` 信号处理
- **原因**：`uvloop` 信号处理问题、任务未正确取消、Windows 平台限制
- **修复**：改进信号处理（兼容 Windows/Unix）、添加超时机制、强制退出机制

## 建议的修复优先级

1. **高优先级**：修复 CTRL+C 无法终止的问题（影响用户体验）
2. **中优先级**：改进 WebSocket 错误处理（便于调试）
3. **低优先级**：改进 "No feed IDs" 警告（如果这是预期行为）
