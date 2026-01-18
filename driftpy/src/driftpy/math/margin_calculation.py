from dataclasses import dataclass

from driftpy.constants.numeric_constants import MARGIN_PRECISION


@dataclass
class MarginCalculation:
    margin_buffer: int = 0
    total_collateral: int = 0
    total_collateral_buffer: int = 0
    margin_requirement: int = 0
    margin_requirement_plus_buffer: int = 0
    spot_margin_requirement: int = 0
    perp_margin_requirement: int = 0
    num_spot_liabilities: int = 0
    num_perp_liabilities: int = 0
    with_spot_isolated_liability: bool = False
    with_perp_isolated_liability: bool = False
    total_spot_asset_value: int = 0
    total_spot_liability_value: int = 0
    total_spot_asset_value_weighted: int = 0
    total_spot_liability_value_weighted: int = 0
    total_perp_liability_value: int = 0
    total_perp_pnl: int = 0
    open_orders_margin_requirement: int = 0
    tracked_market_margin_requirement: int = 0

    def add_total_collateral(self, total_collateral: int) -> None:
        self.total_collateral += total_collateral
        if self.margin_buffer > 0 and total_collateral < 0:
            self.total_collateral_buffer += (
                total_collateral * self.margin_buffer // MARGIN_PRECISION
            )

    def add_margin_requirement(
        self,
        margin_requirement: int,
        liability_value: int,
        is_spot: bool,
        track_market_requirement: bool = False,
    ) -> None:
        self.margin_requirement += margin_requirement
        if is_spot:
            self.spot_margin_requirement += margin_requirement
        else:
            self.perp_margin_requirement += margin_requirement

        if self.margin_buffer > 0:
            self.margin_requirement_plus_buffer += margin_requirement + (
                liability_value * self.margin_buffer // MARGIN_PRECISION
            )

        if track_market_requirement:
            self.tracked_market_margin_requirement += margin_requirement

    def add_open_orders_margin_requirement(self, margin_requirement: int) -> None:
        self.open_orders_margin_requirement += margin_requirement

    def add_spot_liability(self) -> None:
        self.num_spot_liabilities += 1

    def add_perp_liability(self) -> None:
        self.num_perp_liabilities += 1

    def get_total_collateral_plus_buffer(self) -> int:
        return self.total_collateral + self.total_collateral_buffer

    def meets_margin_requirement(self) -> bool:
        return self.total_collateral >= self.margin_requirement

    def meets_margin_requirement_with_buffer(self) -> bool:
        return self.get_total_collateral_plus_buffer() >= self.margin_requirement_plus_buffer

    def can_exit_liquidation(self) -> bool:
        return self.meets_margin_requirement_with_buffer()

    def get_free_collateral(self) -> int:
        return int(self.total_collateral - self.margin_requirement)

    def margin_shortage(self) -> int:
        return max(0, int(self.margin_requirement - self.total_collateral))
