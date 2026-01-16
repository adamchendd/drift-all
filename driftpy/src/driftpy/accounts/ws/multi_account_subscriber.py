import asyncio
import json
import logging
import base64
from typing import Any, Callable, Dict, Optional, Set, Tuple, Union, cast

import websockets
import websockets.exceptions  # force eager imports
from anchorpy.program.core import Program
from solana.rpc.commitment import Commitment
from solana.rpc.websocket_api import SolanaWsClientProtocol, connect
from solders.pubkey import Pubkey

from driftpy.accounts import DataAndSlot, get_account_data_and_slot
from driftpy.types import get_ws_url

log = logging.getLogger(__name__)


class WebsocketMultiAccountSubscriber:
    def __init__(
        self,
        program: Program,
        commitment: Commitment = Commitment("confirmed"),
    ):
        self.program = program
        self.commitment = commitment
        self.ws: Optional[SolanaWsClientProtocol] = None
        self.task: Optional[asyncio.Task] = None

        self.subscription_map: Dict[int, Pubkey] = {}
        self.pubkey_to_subscription: Dict[Pubkey, int] = {}
        # ⭐ 修改：key从Pubkey改为str（oracle_id或pubkey字符串）
        self.decode_map: Dict[str, Callable[[bytes], Any]] = {}
        self.data_map: Dict[str, Optional[DataAndSlot]] = {}
        self.initial_data_map: Dict[str, Optional[DataAndSlot]] = {}
        self.pending_subscriptions: list[Pubkey] = []
        
        # ⭐ 新增：BUG1修复相关数据结构
        self.pubkey_to_oracle_ids: Dict[Pubkey, Set[str]] = {}  # pubkey -> {oracle_id1, oracle_id2, ...}
        self.oracle_id_to_subscription: Dict[str, int] = {}  # oracle_id -> subscription_id
        
        # ⭐ 新增：BUG2修复相关数据结构
        self.request_id_counter: int = 0  # 自增request_id
        self.inflight_subscribes: Dict[int, Tuple[Pubkey, Optional[str]]] = {}  # request_id -> (pubkey, oracle_id)

        self._lock = asyncio.Lock()
        self._running = False  # 控制循环运行标志

    async def add_account(
        self,
        pubkey: Pubkey,
        decode: Optional[Callable[[bytes], Any]] = None,
        initial_data: Optional[DataAndSlot] = None,
        oracle_id: Optional[str] = None,  # ⭐ 新增可选参数
    ):
        decode_fn = decode if decode is not None else self.program.coder.accounts.decode
        
        # 确定内部key
        if oracle_id is not None:
            key = oracle_id
        else:
            key = str(pubkey)

        async with self._lock:
            # ⭐ 关键：检查 pubkey 是否已经订阅（避免重复订阅）
            if pubkey in self.pubkey_to_subscription:
                # pubkey 已经订阅，只需要添加 oracle_id 映射
                if oracle_id is not None:
                    if pubkey not in self.pubkey_to_oracle_ids:
                        self.pubkey_to_oracle_ids[pubkey] = set()
                    self.pubkey_to_oracle_ids[pubkey].add(oracle_id)
                    subscription_id = self.pubkey_to_subscription[pubkey]
                    self.oracle_id_to_subscription[oracle_id] = subscription_id
                    # 存储解码器和数据
                    self.decode_map[key] = decode_fn
                    if initial_data is not None:
                        self.data_map[key] = initial_data
                return
            
            # 存储解码器和初始数据
            self.decode_map[key] = decode_fn
            if initial_data is not None:
                self.data_map[key] = initial_data
            elif key in self.data_map:
                initial_data = self.data_map[key]
            
            # ⭐ 维护 pubkey_to_oracle_ids 映射
            if oracle_id is not None:
                if pubkey not in self.pubkey_to_oracle_ids:
                    self.pubkey_to_oracle_ids[pubkey] = set()
                self.pubkey_to_oracle_ids[pubkey].add(oracle_id)

        # 获取初始数据（如果需要）
        if initial_data is None:
            try:
                initial_data = await get_account_data_and_slot(
                    pubkey, self.program, self.commitment, decode_fn
                )
                async with self._lock:
                    self.data_map[key] = initial_data
            except Exception as e:
                print(f"Error fetching initial data for {pubkey}: {e}")
                return

        # 如果已连接，发送订阅请求
        if self.ws is not None:
            try:
                async with self._lock:
                    # ⭐ 再次检查（避免并发问题）
                    if pubkey in self.pubkey_to_subscription:
                        return
                    
                    # ⭐ 生成 request_id（BUG2修复）
                    self.request_id_counter += 1
                    request_id = self.request_id_counter
                    
                    # 存储到 inflight_subscribes
                    self.inflight_subscribes[request_id] = (pubkey, oracle_id)
                
                # ⭐ 直接发送 JSON-RPC 消息（类似 Rust SDK，BUG2修复）
                message = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "accountSubscribe",
                    "params": [
                        str(pubkey),
                        {
                            "encoding": "base64",
                            "commitment": str(self.commitment),
                        }
                    ]
                }
                await self.ws.send(json.dumps(message))
            except Exception as e:
                print(f"Error subscribing to account {pubkey}: {e}")
                async with self._lock:
                    if request_id in self.inflight_subscribes:
                        del self.inflight_subscribes[request_id]

    async def remove_account(self, pubkey: Pubkey, oracle_id: Optional[str] = None):
        async with self._lock:
            if oracle_id is not None:
                # 只删除指定的 oracle_id
                if pubkey in self.pubkey_to_oracle_ids:
                    self.pubkey_to_oracle_ids[pubkey].discard(oracle_id)
                    if oracle_id in self.oracle_id_to_subscription:
                        del self.oracle_id_to_subscription[oracle_id]
                    if oracle_id in self.decode_map:
                        del self.decode_map[oracle_id]
                    if oracle_id in self.data_map:
                        del self.data_map[oracle_id]
                
                # 如果还有其他的 oracle_id，不取消订阅
                if pubkey in self.pubkey_to_oracle_ids and self.pubkey_to_oracle_ids[pubkey]:
                    return
            else:
                # 删除所有相关的 oracle_id
                if pubkey in self.pubkey_to_oracle_ids:
                    for oid in list(self.pubkey_to_oracle_ids[pubkey]):
                        if oid in self.oracle_id_to_subscription:
                            del self.oracle_id_to_subscription[oid]
                        if oid in self.decode_map:
                            del self.decode_map[oid]
                        if oid in self.data_map:
                            del self.data_map[oid]
                    del self.pubkey_to_oracle_ids[pubkey]
            
            # 如果没有 oracle_id 了，取消订阅
            if pubkey not in self.pubkey_to_subscription:
                return

            subscription_id = self.pubkey_to_subscription[pubkey]

            if self.ws is not None:
                try:
                    await self.ws.account_unsubscribe(subscription_id)
                except Exception:
                    pass

            del self.subscription_map[subscription_id]
            del self.pubkey_to_subscription[pubkey]
            
            # 清理非oracle账户的数据（如果存在）
            pubkey_str = str(pubkey)
            if pubkey_str in self.decode_map:
                del self.decode_map[pubkey_str]
            if pubkey_str in self.data_map:
                del self.data_map[pubkey_str]
            if pubkey_str in self.initial_data_map:
                del self.initial_data_map[pubkey_str]

    async def subscribe(self):
        if self.task is not None:
            return

        self.task = asyncio.create_task(self._subscribe_ws())

    async def _subscribe_ws(self):
        endpoint = self.program.provider.connection._provider.endpoint_uri
        ws_endpoint = get_ws_url(endpoint)
        self._running = True

        try:
            # 使用循环重连机制
            while self._running:
                try:
                    # 直接使用 websockets 库的原始 API，绕过 solana-py 的处理
                    # 这样可以避免 KeyError 导致订阅确认被跳过
                    async with websockets.connect(ws_endpoint) as raw_ws:
                        # 将原始 WebSocket 包装为 SolanaWsClientProtocol（用于兼容性）
                        # 但实际上我们直接使用 raw_ws 接收消息
                        self.ws = cast(SolanaWsClientProtocol, raw_ws)
                        
                        if not self._running:
                            break
                        
                        # 初始化订阅
                        async with self._lock:
                            initial_accounts = []
                            seen_pubkeys = set()
                            for key in list(self.data_map.keys()):
                                # 从key中提取pubkey
                                if '-' in key and key.split('-')[-1].isdigit():
                                    # oracle_id格式：pubkey-source_num
                                    pubkey_str = '-'.join(key.split('-')[:-1])
                                    try:
                                        pubkey = Pubkey.from_string(pubkey_str)
                                    except:
                                        continue
                                else:
                                    # pubkey字符串
                                    try:
                                        pubkey = Pubkey.from_string(key)
                                    except:
                                        continue
                                
                                # ⭐ 按 pubkey 去重（类似 Rust SDK）
                                if pubkey not in seen_pubkeys and pubkey not in self.pubkey_to_subscription:
                                    initial_accounts.append((pubkey, key))
                                    seen_pubkeys.add(pubkey)

                        # ⭐ 按 pubkey 去重后订阅
                        for pubkey, key in initial_accounts:
                            try:
                                async with self._lock:
                                    # ⭐ 生成 request_id（BUG2修复）
                                    self.request_id_counter += 1
                                    request_id = self.request_id_counter
                                    oracle_id = key if key != str(pubkey) else None
                                    self.inflight_subscribes[request_id] = (pubkey, oracle_id)
                                
                                # ⭐ 直接发送 JSON-RPC 消息（BUG2修复）
                                message = {
                                    "jsonrpc": "2.0",
                                    "id": request_id,
                                    "method": "accountSubscribe",
                                    "params": [
                                        str(pubkey),
                                        {
                                            "encoding": "base64",
                                            "commitment": str(self.commitment),
                                        }
                                    ]
                                }
                                await raw_ws.send(json.dumps(message))
                            except Exception as e:
                                print(f"Error subscribing to account {pubkey}: {e}")
                                async with self._lock:
                                    if request_id in self.inflight_subscribes:
                                        del self.inflight_subscribes[request_id]

                        # 使用循环而不是 async for，以便可以检查 _running 标志
                        try:
                            while self._running:
                                try:
                                    # 直接使用原始 WebSocket 接收 JSON 消息，绕过 solana-py 的处理
                                    # 这样可以避免 KeyError 导致订阅确认被跳过
                                    raw_data = await asyncio.wait_for(raw_ws.recv(), timeout=1.0)
                                    
                                    # 解析 JSON 消息
                                    try:
                                        msg_data = json.loads(raw_data)
                                        # 手动构造消息对象，兼容 solana-py 的格式
                                        class FakeResult:
                                            """模拟 solana-py 的 result 对象"""
                                            def __init__(self, result_data):
                                                if isinstance(result_data, dict):
                                                    # 数据更新：{"context": {...}, "value": {"data": [...]}}
                                                    self.context = type('Context', (), result_data.get("context", {}))()
                                                    value_data = result_data.get("value", {})
                                                    # 处理 base64 编码的数据
                                                    data_field = value_data.get("data")
                                                    if isinstance(data_field, list) and len(data_field) >= 1:
                                                        # ["base64_string", "base64"]
                                                        data_str = data_field[0]
                                                        self.data = base64.b64decode(data_str)
                                                    elif isinstance(data_field, str):
                                                        # 直接是 base64 字符串
                                                        self.data = base64.b64decode(data_field)
                                                    else:
                                                        self.data = data_field
                                                    self.value = type('Value', (), {"data": self.data})()
                                                else:
                                                    self.value = None
                                        
                                        class FakeMsg:
                                            def __init__(self, data):
                                                # 处理订阅确认：{"jsonrpc":"2.0","result":123,"id":1}
                                                if "result" in data and isinstance(data["result"], int):
                                                    self.result = data["result"]
                                                # 处理数据更新：{"jsonrpc":"2.0","method":"accountNotification","params":{"result":{...},"subscription":123}}
                                                elif "method" in data and "params" in data:
                                                    params = data["params"]
                                                    result_data = params.get("result")
                                                    if result_data:
                                                        self.result = FakeResult(result_data)
                                                    else:
                                                        self.result = None
                                                    self.subscription = params.get("subscription")
                                                else:
                                                    result_data = data.get("result")
                                                    if isinstance(result_data, dict):
                                                        self.result = FakeResult(result_data)
                                                    else:
                                                        self.result = result_data
                                                    self.subscription = data.get("params", {}).get("subscription") if "params" in data else None
                                                self.id = data.get("id")
                                        msg = [FakeMsg(msg_data)]
                                    except Exception as parse_err:
                                        log.debug(f"Failed to parse raw message: {parse_err}")
                                        continue
                                
                                except KeyError as e:
                                    # 不应该发生，因为我们直接使用原始 WebSocket
                                    log.debug(f"Unexpected KeyError in raw_ws.recv: {e}")
                                    continue
                                except asyncio.TimeoutError:
                                    # 超时后继续检查 _running 标志
                                    continue
                                except asyncio.CancelledError:
                                    log.info("WebSocket recv cancelled")
                                    raise
                                except websockets.exceptions.ConnectionClosed:
                                    # 连接已关闭，退出循环
                                    log.info("WebSocket connection closed during recv")
                                    break
                                except Exception as e:
                                    # 其他异常（包括可能被包装的 KeyError）
                                    # 检查是否是 KeyError 的变体
                                    if "KeyError" in str(type(e)) or isinstance(getattr(e, '__cause__', None), KeyError):
                                        log.debug(f"solana-py internal KeyError (wrapped, expected): {e}")
                                        continue
                                    # 其他异常需要记录但继续
                                    log.debug(f"Unexpected error in recv: {e}")
                                    continue
                                
                                if not self._running:
                                    break
                                
                                # 处理消息
                                try:
                                    if len(msg) == 0:
                                        print("No message received")
                                        continue

                                    result = msg[0].result

                                    if isinstance(result, int):
                                        # 这是订阅确认
                                        async with self._lock:
                                            # ⭐ 使用 request_id 匹配（修复BUG2）
                                            request_id = None
                                            if hasattr(msg[0], 'id') and msg[0].id is not None:
                                                request_id = msg[0].id
                                                # 确保 request_id 是整数类型（JSON 可能返回字符串）
                                                if isinstance(request_id, str):
                                                    try:
                                                        request_id = int(request_id)
                                                    except ValueError:
                                                        log.warning(f"Invalid request_id format: {request_id}")
                                                        request_id = None
                                            
                                            if request_id is not None and request_id in self.inflight_subscribes:
                                                pubkey, oracle_id = self.inflight_subscribes.pop(request_id)
                                                subscription_id = result
                                                
                                                # 建立映射关系
                                                self.subscription_map[subscription_id] = pubkey
                                                self.pubkey_to_subscription[pubkey] = subscription_id
                                                
                                                # ⭐ 为所有相关的 oracle_id 建立映射
                                                if pubkey in self.pubkey_to_oracle_ids:
                                                    for oid in self.pubkey_to_oracle_ids[pubkey]:
                                                        self.oracle_id_to_subscription[oid] = subscription_id
                                                
                                                log.debug(f"Subscription confirmed: request_id={request_id}, subscription_id={subscription_id}, pubkey={pubkey}")
                                            else:
                                                # 向后兼容：如果没有 id，使用 pop(0)（但会记录警告）
                                                if self.pending_subscriptions:
                                                    pubkey = self.pending_subscriptions.pop(0)
                                                    subscription_id = result
                                                    self.subscription_map[subscription_id] = pubkey
                                                    self.pubkey_to_subscription[pubkey] = subscription_id
                                                    log.warning("Using fallback pop(0) for subscription confirmation")
                                                else:
                                                    log.warning(
                                                        f"No pending subscriptions but got a confirmation. "
                                                        f"request_id={request_id}, subscription_id={result}, "
                                                        f"inflight_subscribes keys: {list(self.inflight_subscribes.keys())}"
                                                    )
                                        continue

                                    if hasattr(result, "value") and result.value is not None:
                                        subscription_id = None
                                        if hasattr(msg[0], "subscription"):
                                            subscription_id = msg[0].subscription

                                        if (
                                            subscription_id is None
                                            or subscription_id not in self.subscription_map
                                        ):
                                            log.warning(
                                                f"Subscription ID {subscription_id} not found in subscription map. "
                                                f"Available subscription IDs: {list(self.subscription_map.keys())[:10]}"
                                            )
                                            continue

                                        pubkey = self.subscription_map[subscription_id]
                                        
                                        # ⭐ 获取该 pubkey 的所有 oracle_id（修复BUG1）
                                        oracle_ids = self.pubkey_to_oracle_ids.get(pubkey, set())
                                        
                                        if oracle_ids:
                                            # ⭐ 遍历所有 oracle_id，分别解码和存储（类似 Rust SDK 的 Mixed 模式）
                                            for oracle_id in oracle_ids:
                                                decode_fn = self.decode_map.get(oracle_id)
                                                if decode_fn is not None:
                                                    try:
                                                        slot = int(result.context.slot)
                                                        account_bytes = cast(bytes, result.value.data)
                                                        decoded_data = decode_fn(account_bytes)
                                                        new_data = DataAndSlot(slot, decoded_data)
                                                        self._update_data(oracle_id, new_data)  # ⭐ 使用 oracle_id 作为 key
                                                    except Exception:
                                                        continue
                                        else:
                                            # 非 oracle 账户（向后兼容）
                                            pubkey_str = str(pubkey)
                                            decode_fn = self.decode_map.get(pubkey_str)
                                            if decode_fn is not None:
                                                try:
                                                    slot = int(result.context.slot)
                                                    account_bytes = cast(bytes, result.value.data)
                                                    decoded_data = decode_fn(account_bytes)
                                                    new_data = DataAndSlot(slot, decoded_data)
                                                    self._update_data(pubkey_str, new_data)
                                                except Exception:
                                                    # this is RPC noise?
                                                    continue
                                except Exception as e:
                                    # 处理消息时的其他错误
                                    log.debug(f"Error processing websocket message: {e}")
                                    continue
                        except asyncio.CancelledError:
                            log.info("WebSocket message loop cancelled")
                            raise
                        except websockets.exceptions.ConnectionClosed:
                            log.info("WebSocket connection closed during message loop")
                            self.ws = None
                            async with self._lock:
                                self.subscription_map.clear()
                                self.pubkey_to_subscription.clear()
                                self.inflight_subscribes.clear()
                            break  # 退出内层循环，重新连接
                        except Exception as e:
                            log.error(f"Error in message loop: {e}", exc_info=True)
                            # 继续内层循环，尝试重新接收消息
                            continue
                
                except websockets.exceptions.ConnectionClosed:
                    log.info("WebSocket connection closed, reconnecting...")
                    self.ws = None
                    async with self._lock:
                        self.subscription_map.clear()
                        self.pubkey_to_subscription.clear()
                        self.inflight_subscribes.clear()
                    # 等待一段时间后重连
                    await asyncio.sleep(1)
                    continue
                except asyncio.CancelledError:
                    log.info("WebSocket subscription cancelled")
                    self._running = False
                    # 关闭 WebSocket 连接以中断循环（使用超时避免阻塞）
                    if self.ws is not None:
                        try:
                            await asyncio.wait_for(self.ws.close(), timeout=0.5)
                        except (asyncio.TimeoutError, Exception):
                            # 如果超时或出错，强制关闭底层连接
                            try:
                                if hasattr(self.ws, 'transport') and self.ws.transport:
                                    self.ws.transport.close()
                            except Exception:
                                pass
                    self.ws = None
                    async with self._lock:
                        self.subscription_map.clear()
                        self.pubkey_to_subscription.clear()
                        self.inflight_subscribes.clear()
                    raise  # 重新抛出 CancelledError，让调用者知道任务被取消
                except Exception as e:
                    # 改进错误处理：打印详细的错误信息
                    error_type = type(e).__name__
                    error_msg = str(e)
                    error_repr = repr(e)
                    log.error(
                        f"Error in websocket connection: {error_type}: {error_msg} (repr: {error_repr})",
                        exc_info=True  # 打印完整堆栈跟踪
                    )
                    self.ws = None
                    async with self._lock:
                        self.subscription_map.clear()
                        self.pubkey_to_subscription.clear()
                        self.inflight_subscribes.clear()
                    await asyncio.sleep(1)
                    continue
        except asyncio.CancelledError:
            log.info("WebSocket subscription task cancelled")
            self._running = False
            # 关闭 WebSocket 连接以中断循环（使用超时避免阻塞）
            if self.ws is not None:
                try:
                    await asyncio.wait_for(self.ws.close(), timeout=0.5)
                except (asyncio.TimeoutError, Exception):
                    # 如果超时或出错，强制关闭底层连接
                    try:
                        if hasattr(self.ws, 'transport') and self.ws.transport:
                            self.ws.transport.close()
                    except Exception:
                        pass
            self.ws = None
            async with self._lock:
                self.subscription_map.clear()
                self.pubkey_to_subscription.clear()
                self.inflight_subscribes.clear()
            raise  # 重新抛出，让任务正确取消

    def _update_data(self, key: str, new_data: Optional[DataAndSlot]):  # ⭐ key从Pubkey改为str
        if new_data is None:
            return

        current_data = self.data_map.get(key)
        if current_data is None or new_data.slot >= current_data.slot:
            self.data_map[key] = new_data

    def get_data(self, key: Union[Pubkey, str]) -> Optional[DataAndSlot]:  # ⭐ 支持Pubkey和str
        if isinstance(key, Pubkey):
            # 向后兼容：非oracle账户或单个oracle_id
            oracle_ids = self.pubkey_to_oracle_ids.get(key, set())
            if len(oracle_ids) == 0:
                # 非oracle账户
                key = str(key)
            elif len(oracle_ids) == 1:
                # 只有一个oracle_id
                key = list(oracle_ids)[0]
            else:
                # 多个oracle_id，返回第一个（向后兼容，但建议使用oracle_id）
                key = list(oracle_ids)[0]
                print(f"Warning: pubkey {key} has multiple oracle_ids, returning first one. "
                      f"Please use get_data(oracle_id) to specify.")
        return self.data_map.get(key)

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
                # 从key中提取pubkey
                if '-' in key and key.split('-')[-1].isdigit():
                    # oracle_id格式：pubkey-source_num
                    pubkey_str = '-'.join(key.split('-')[:-1])
                    try:
                        pubkey = Pubkey.from_string(pubkey_str)
                    except:
                        continue
                else:
                    # pubkey字符串
                    try:
                        pubkey = Pubkey.from_string(key)
                    except:
                        continue
                tasks.append(
                    get_account_data_and_slot(
                        pubkey, self.program, self.commitment, decode_fn
                    )
                )
                keys.append(key)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for key, result in zip(keys, results):
                if isinstance(result, Exception):
                    continue
                self._update_data(key, result)

    def is_subscribed(self):
        return self.ws is not None and self.task is not None

    async def unsubscribe(self):
        self._running = False  # 停止循环
        
        # 先取消任务
        if self.task:
            self.task.cancel()
        
        # 关闭 WebSocket 连接以中断循环（使用超时避免阻塞）
        if self.ws is not None:
            try:
                # 尝试正常关闭，但设置超时
                await asyncio.wait_for(self.ws.close(), timeout=0.5)
            except (asyncio.TimeoutError, Exception):
                # 如果超时或出错，强制关闭底层连接
                try:
                    if hasattr(self.ws, 'transport') and self.ws.transport:
                        self.ws.transport.close()
                except Exception:
                    pass
        
        # 等待任务完成（使用超时）
        if self.task:
            try:
                await asyncio.wait_for(self.task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self.task = None

        if self.ws:
            async with self._lock:
                for subscription_id in list(self.subscription_map.keys()):
                    try:
                        await asyncio.wait_for(self.ws.account_unsubscribe(subscription_id), timeout=0.1)
                    except Exception:
                        pass
            try:
                await asyncio.wait_for(self.ws.close(), timeout=0.5)
            except Exception:
                # 如果关闭失败，强制关闭底层连接
                try:
                    if hasattr(self.ws, 'transport') and self.ws.transport:
                        self.ws.transport.close()
                except Exception:
                    pass
            self.ws = None

        async with self._lock:
            self.subscription_map.clear()
            self.pubkey_to_subscription.clear()
            self.decode_map.clear()
            self.data_map.clear()
            self.initial_data_map.clear()
            self.pending_subscriptions.clear()
            # ⭐ 清理新增的数据结构
            self.pubkey_to_oracle_ids.clear()
            self.oracle_id_to_subscription.clear()
            self.inflight_subscribes.clear()
            self.request_id_counter = 0