import asyncio
import json
import traceback
from typing import Optional, List, Any, Tuple

import aiohttp
from events import Events as EventEmitter
from dataclasses import dataclass
from solders.pubkey import Pubkey

from driftpy.dlob.client_types import DLOBClientConfig
from driftpy.dlob.orderbook_levels import (
    DEFAULT_TOP_OF_BOOK_QUOTE_AMOUNTS,
    L3Level,
    L3OrderBook,
    L2Level,
    L2OrderBook,
    L2OrderBookGenerator,
    get_vamm_l2_generator,
)
from driftpy.types import MarketType, is_variant


@dataclass
class MarketId:
    index: int
    kind: MarketType


class _TupleL2GenWrapper:
    """
    driftpy 的某些版本/分支里，get_vamm_l2_generator 可能返回 tuple/list：
      (get_l2_bids, get_l2_asks)
    但 DLOB.get_l2 期望的是“对象”，包含：
      - get_l2_bids()
      - get_l2_asks()
    所以这里做一个兼容包装。
    """

    def __init__(self, bids_part: Any, asks_part: Any):
        self._bids_part = bids_part
        self._asks_part = asks_part

    def get_l2_bids(self):
        return self._bids_part() if callable(self._bids_part) else self._bids_part

    def get_l2_asks(self):
        return self._asks_part() if callable(self._asks_part) else self._asks_part


def _normalize_fallback_generator(raw: Any) -> Any:
    """将 raw 规范成 DLOB.get_l2 可接受的 fallback generator。"""
    if hasattr(raw, "get_l2_bids") and hasattr(raw, "get_l2_asks"):
        return raw
    if isinstance(raw, (tuple, list)) and len(raw) >= 2:
        return _TupleL2GenWrapper(raw[0], raw[1])
    raise TypeError(f"Unsupported fallback generator type: {type(raw)} raw={raw!r}")


class DLOBSubscriber:
    _session: Optional[aiohttp.ClientSession] = None

    def __init__(
        self,
        url: Optional[str] = None,  # Provide this if using DLOB server
        config: Optional[DLOBClientConfig] = None,  # Provide this if building from usermap
    ):
        if url:
            self.url = url.rstrip("/")
        self.dlob = None

        # ✅ events.Events 的正确用法：声明事件名；外部通过 `+=` 订阅
        # 例如：dlob_sub.event_emitter.on_dlob_update += cb
        self.event_emitter = EventEmitter(("on_dlob_update",))
        # ❌ 千万不要 self.event_emitter.on("on_dlob_update")，否则会抛 Event 'on' is not declared

        if config is not None:
            self.drift_client = config.drift_client
            self.dlob_source = config.dlob_source
            self.slot_source = config.slot_source
            self.update_frequency = config.update_frequency
            self.interval_task = None

    async def on_dlob_update(self):
        # 触发事件：外部订阅者会收到 dlob
        self.event_emitter.on_dlob_update(self.dlob)

    async def subscribe(self):
        """
        This function CANNOT be used unless a `DLOBClientConfig` was provided in the constructor.
        If used without config, it returns nothing.
        """
        dlob_source = getattr(self, "dlob_source", None)
        slot_source = getattr(self, "slot_source", None)
        drift_client = getattr(self, "drift_client", None)

        if not all([dlob_source, slot_source, drift_client]):
            return

        if getattr(self, "interval_task", None) is not None:
            return

        await self.update_dlob()

        async def interval_loop():
            while True:
                try:
                    await self.update_dlob()
                    await self.on_dlob_update()
                except Exception as e:
                    print(f"Error in DLOB subscription: {e}")
                    traceback.print_exc()
                await asyncio.sleep(self.update_frequency)

        self.interval_task = asyncio.create_task(interval_loop())

    def unsubscribe(self):
        """
        兼容两种调用：
        - 同步：dlob_sub.unsubscribe()
        - 异步：await dlob_sub.unsubscribe()   # 你工程里这么写也不会炸
        """
        if getattr(self, "interval_task", None) is not None:
            self.interval_task.cancel()

        # 返回已完成 Future：避免外部 await 时 TypeError
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        fut = loop.create_future()
        fut.set_result(None)
        return fut

    async def update_dlob(self):
        self.dlob = await self.dlob_source.get_DLOB(self.slot_source.get_slot())

    def get_dlob(self):
        return self.dlob

    @classmethod
    async def get_session(cls):
        if cls._session is None or cls._session.closed:
            cls._session = aiohttp.ClientSession()
        return cls._session

    @classmethod
    async def close_session(cls):
        if cls._session is not None:
            await cls._session.close()
            cls._session = None

    async def get_l2_orderbook(self, market: MarketId) -> L2OrderBook:
        session = await self.get_session()
        market_type = "perp" if is_variant(market.kind, "Perp") else "spot"
        async with session.get(
            f"{self.url}/l2?marketType={market_type}&marketIndex={market.index}"
        ) as response:
            if response.status == 200:
                data = await response.text()
                return self.decode_l2_orderbook(data)
            raise Exception("Failed to fetch L2 OrderBook data")

    def decode_l2_orderbook(self, data: str) -> L2OrderBook:
        data = json.loads(data)
        asks = [
            L2Level(ask["price"], ask["size"], ask["sources"]) for ask in data["asks"]
        ]
        bids = [
            L2Level(bid["price"], bid["size"], bid["sources"]) for bid in data["bids"]
        ]
        slot = data.get("slot")
        return L2OrderBook(asks, bids, slot)

    async def get_l3_orderbook(self, market: MarketId) -> L3OrderBook:
        session = await self.get_session()
        market_type = "perp" if is_variant(market.kind, "Perp") else "spot"
        async with session.get(
            f"{self.url}/l3?marketType={market_type}&marketIndex={market.index}"
        ) as response:
            if response.status == 200:
                data = await response.text()
                return self.decode_l3_orderbook(data)
            raise Exception("Failed to fetch L3 OrderBook data")

    def decode_l3_orderbook(self, data: str) -> L3OrderBook:
        data = json.loads(data)
        asks = [
            L3Level(
                ask["price"],
                ask["size"],
                Pubkey.from_string(ask["maker"]),
                ask["orderId"],
            )
            for ask in data["asks"]
        ]
        bids = [
            L3Level(
                bid["price"],
                bid["size"],
                Pubkey.from_string(bid["maker"]),
                bid["orderId"],
            )
            for bid in data["bids"]
        ]
        slot = data.get("slot")
        return L3OrderBook(asks, bids, slot)

    def get_l2_orderbook_sync(
        self,
        market_name: Optional[str] = None,
        market_index: Optional[int] = None,
        market_type: Optional[MarketType] = None,
        include_vamm: Optional[bool] = False,
        num_vamm_orders: Optional[int] = None,
        fallback_l2_generators: Optional[List[L2OrderBookGenerator]] = None,
        depth: Optional[int] = 10,
    ) -> Any:
        """
        ✅ 对齐你当前 driftpy 的 DLOB.get_l2 / get_vamm_l2_generator 签名：
          - 需要 slot + oracle_price_data
          - DEFAULT_TOP_OF_BOOK_QUOTE_AMOUNTS 是 list，用 list(DEFAULT_...) 即可
          - driftpy 内部要求：len(top_of_book_quote_amounts) < num_orders
        """
        # --- 1) 解析 market ---
        if market_name:
            derived_market_info = self.drift_client.get_market_index_and_type(market_name)
            if not derived_market_info:
                raise ValueError(f"Market index and type for {market_name} could not be found.")
            market_index = derived_market_info[0]
            market_type = derived_market_info[1]
        else:
            if market_index is None or market_type is None:
                raise ValueError("Either market_name or market_index and market_type must be provided")

        # --- 2) 拿 oracle ---
        market_is_perp = is_variant(market_type, "Perp")
        if market_is_perp:
            oracle_price_data = self.drift_client.get_oracle_price_data_for_perp_market(market_index)
        else:
            oracle_price_data = self.drift_client.get_oracle_price_data_for_spot_market(market_index)

        # --- 3) fallback list 初始化 ---
        if fallback_l2_generators is None:
            fallback_l2_generators = []

        # --- 4) include_vamm：构造 vAMM fallback（并做 tuple->对象 兼容）---
        if market_is_perp and include_vamm:
            if len(fallback_l2_generators) > 0:
                raise ValueError("include_vamm can only be used if fallback_l2_generators is empty")

            # vAMM 档位数：默认用 num_vamm_orders，否则用 depth
            num_orders = int(num_vamm_orders if num_vamm_orders is not None else (depth or 10))

            # ✅ 关键修复：DEFAULT_TOP_OF_BOOK_QUOTE_AMOUNTS 可能是 list，不能 .get
            tob = list(DEFAULT_TOP_OF_BOOK_QUOTE_AMOUNTS)

            # driftpy 内部要求：len(tob) < num_orders
            if num_orders <= len(tob):
                num_orders = len(tob) + 1

            perp_mkt = self.drift_client.get_perp_market_account(market_index)
            raw = get_vamm_l2_generator(
                perp_mkt,
                oracle_price_data,
                num_orders,
                now=None,
                top_of_book_quote_amounts=tob,
            )
            fallback_l2_generators = [_normalize_fallback_generator(raw)]

        # --- 5) 生成 L2 ---
        dlob = self.get_dlob()
        if dlob is None:
            raise ValueError("dlob not ready")

        return dlob.get_l2(
            market_index,
            market_type,
            self.slot_source.get_slot(),
            oracle_price_data,
            depth,
            fallback_l2_generators,
        )

    def get_l3_orderbook_sync(
        self,
        market_name: Optional[str] = None,
        market_index: Optional[int] = None,
        market_type: Optional[MarketType] = None,
    ) -> Any:
        if market_name:
            derived_market_info = self.drift_client.get_market_index_and_type(market_name)
            if not derived_market_info:
                raise ValueError(f"Market index and type for {market_name} could not be found.")
            market_index = derived_market_info[0]
            market_type = derived_market_info[1]
        else:
            if market_index is None or market_type is None:
                raise ValueError("Either market_name or market_index and market_type must be provided")

        market_is_perp = is_variant(market_type, "Perp")
        if market_is_perp:
            oracle_price_data = self.drift_client.get_oracle_price_data_for_perp_market(market_index)
        else:
            oracle_price_data = self.drift_client.get_oracle_price_data_for_spot_market(market_index)

        dlob = self.get_dlob()
        if dlob is None:
            raise ValueError("dlob not ready")

        return dlob.get_l3(
            market_index,
            market_type,
            self.slot_source.get_slot(),
            oracle_price_data,
        )
