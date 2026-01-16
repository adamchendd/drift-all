import asyncio
from typing import Optional

from events.events import Events, _EventSlot
from solders.pubkey import Pubkey

from driftpy.accounts.types import DataAndSlot, WebsocketProgramAccountOptions
from driftpy.accounts.ws.program_account_subscriber import (
    WebSocketProgramAccountSubscriber,
)
from driftpy.auction_subscriber.types import AuctionSubscriberConfig
from driftpy.decode.user import decode_user
# 改：不再导入 get_user_with_auction_filter，改为导入 非idle 与 有挂单 的过滤器
from driftpy.memcmp import (
    get_user_filter,
    get_non_idle_user_filter,
    #get_user_with_order_filter,
)
from driftpy.types import UserAccount


class AuctionEvents(Events):
    """
    显式声明 on_account_update 事件。
    事件参数：(user: UserAccount, account_pubkey: Pubkey, slot: int)
    """
    __events__ = ("on_account_update",)
    on_account_update: _EventSlot


class AuctionSubscriber:
    """
    订阅 Drift 的 UserAccount 更新。
    过滤条件（AND）：
      - get_user_filter()           : 账户类型为 UserAccount
      - get_non_idle_user_filter()  : 非 idle
      - get_user_with_order_filter(): 有挂单
    """
    def __init__(self, config: AuctionSubscriberConfig):
        self.drift_client = config.drift_client
        self.commitment = (
            config.commitment
            if config.commitment is not None
            else self.drift_client.connection.commitment
        )
        self.resub_timeout_ms = config.resub_timeout_ms
        self.subscriber: Optional[WebSocketProgramAccountSubscriber] = None
        self.event_emitter = AuctionEvents()

    async def on_update(self, account_pubkey: str, data: DataAndSlot[UserAccount]):
        self.event_emitter.on_account_update(
            data.data,
            Pubkey.from_string(account_pubkey),
            data.slot,  # type: ignore
        )

    async def subscribe(self):
        if self.subscriber is None:
            # 改：使用 (UserAccount AND 非idle AND 有挂单) 的组合过滤
            filters = (
                get_user_filter(),
                get_non_idle_user_filter(),
                #get_user_with_order_filter(),
            )
            options = WebsocketProgramAccountOptions(filters, self.commitment, "base64")
            self.subscriber = WebSocketProgramAccountSubscriber(
                "AuctionSubscriber",  # 保持原名；只是一个标识字符串
                self.drift_client.program,
                options,
                self.on_update,
                decode_user,
                self.resub_timeout_ms,
            )

        if self.subscriber.subscribed:
            return

        await self.subscriber.subscribe()

    def unsubscribe(self):
        if self.subscriber is None:
            return
        asyncio.create_task(self.subscriber.unsubscribe())
