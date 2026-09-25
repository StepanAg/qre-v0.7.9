from enum import Enum


class Category(str, Enum):
    LINEAR = "linear"   # USDT perpetuals (first market)
    SPOT = "spot"


class Side(str, Enum):
    BUY = "Buy"
    SELL = "Sell"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def entry_side(self) -> Side:
        return Side.BUY if self is PositionSide.LONG else Side.SELL

    @property
    def sign(self) -> int:
        return 1 if self is PositionSide.LONG else -1


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class OrderType(str, Enum):
    MARKET = "Market"
    LIMIT = "Limit"


class TimeInForce(str, Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    POST_ONLY = "PostOnly"


class OrderStatus(str, Enum):
    CREATED = "created"            # persisted locally, not yet sent
    SUBMITTING = "submitting"      # request in flight; outcome may be unknown
    ACCEPTED = "accepted"          # exchange acked
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"            # lost ack / timeout -> must be reconciled

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED}


class EventSource(str, Enum):
    LOCAL = "local"
    EXCHANGE_REST = "exchange_rest"
    EXCHANGE_WS = "exchange_ws"
    RECONCILIATION = "reconciliation"
    SIMULATOR = "simulator"


class ReconciliationStatus(str, Enum):
    NOT_APPLICABLE = "n/a"      # e.g. paper/simulated
    PENDING = "pending"
    MATCHED = "matched"
    MISMATCH = "mismatch"
    LOCAL_ONLY = "local_only"       # we have it, exchange does not
    EXCHANGE_ONLY = "exchange_only" # exchange has it, we did not know


class FillRole(str, Enum):
    ENTRY = "entry"   # increases exposure
    EXIT = "exit"     # reduces exposure


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    TIME_EXIT = "time_exit"
    SIGNAL_EXIT = "signal_exit"
    MANUAL = "manual"
    LIQUIDATION = "liquidation"
    RECONCILIATION = "reconciliation"  # closed on exchange, discovered later
    UNKNOWN = "unknown"


class TradeStatus(str, Enum):
    PLANNED = "planned"   # no fills yet
    OPEN = "open"
    CLOSED = "closed"


class PositionEventType(str, Enum):
    OPEN = "open"
    INCREASE = "increase"
    REDUCE = "reduce"
    CLOSE = "close"


class MarketRegime(str, Enum):
    """Observed market state (descriptive, not a forecast)."""
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"
    TRANSITION = "transition"
    UNKNOWN = "unknown"


class TrendDirection(str, Enum):
    UP = "up"
    DOWN = "down"
    NEUTRAL = "neutral"      # no component beyond its neutral band
    CONFLICT = "conflict"    # components point in opposite directions
    UNKNOWN = "unknown"


class TrendStrength(str, Enum):
    """Categorical by construction (no probability semantics)."""
    STRONG = "strong"
    MODERATE = "moderate"
    NONE = "none"
    UNKNOWN = "unknown"


class VolatilityState(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    UNKNOWN = "unknown"


class MtfAlignment(str, Enum):
    SINGLE = "single"        # only one timeframe analysed
    ALIGNED = "aligned"      # all known timeframes trend the same way
    CONFLICT = "conflict"    # an up-trend and a down-trend on different timeframes
    MIXED = "mixed"          # no opposite trends, but regimes differ
    PARTIAL = "partial"      # at least one timeframe UNKNOWN


class ContextStatus(str, Enum):
    AVAILABLE = "available"
    SELF = "self"            # the analysed symbol IS the context symbol
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class QualityStatus(str, Enum):
    """Data-quality classification of a time series (or of its use by a feature)."""
    VALID = "valid"
    MISSING_HISTORY = "missing_history"        # no data at all for the series
    GAP = "gap"                                # missing bars inside the required window
    DUPLICATE = "duplicate"                    # two bars with the same open time
    INVALID = "invalid"                        # wrong symbol/timeframe/misaligned or non-finite result
    STALE = "stale"                            # the bar that should have closed at as_of is missing
    OPEN_CANDLE = "open_candle"                # an unclosed bar was present and excluded
    INSUFFICIENT_HISTORY = "insufficient_history"


class GapSeverity(str, Enum):
    """Severity is RELATIVE to a feature's dependence window, not an absolute count."""
    NO_GAP = "no_gap"          # no gap anywhere in the loaded history
    MINOR_GAP = "minor_gap"    # gaps exist, but all older than the feature's window -> no influence
    MAJOR_GAP = "major_gap"    # gap inside the window -> feature not computable until it rolls out
    UNUSABLE = "unusable"      # series unusable as a whole (duplicate/invalid/stale)


class FeatureStatus(str, Enum):
    VALUE = "value"
    NOT_AVAILABLE = "not_available"                  # not implemented / undefined for this input
    INSUFFICIENT_HISTORY = "insufficient_history"
    DATA_QUALITY_FAILURE = "data_quality_failure"
    INVALID = "invalid"                              # computed but non-finite (NaN/inf)


# ---------------------------------------------------------------- Phase 5: structure
class SwingKind(str, Enum):
    HIGH = "high"
    LOW = "low"


class StructureDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    UNKNOWN = "unknown"


class StructureEventType(str, Enum):
    BOS = "bos"                                  # break in the direction of the prior structure
    CHOCH = "choch"                              # break against the prior structure
    BREAK_UNCLASSIFIED = "break_unclassified"    # prior direction unknown -> no BOS/CHoCH claim


class BreakMethod(str, Enum):
    CLOSE = "close"      # bar CLOSES beyond the level
    WICK = "wick"        # bar TRADES beyond the level (explicit alternative, never a silent substitute)


class LiquiditySide(str, Enum):
    BUY_SIDE = "buy_side"      # above highs (hypothesised buy stops / breakout buys)
    SELL_SIDE = "sell_side"    # below lows


class LiquiditySource(str, Enum):
    SWING_HIGH = "swing_high"
    SWING_LOW = "swing_low"
    EQUAL_HIGHS = "equal_highs"
    EQUAL_LOWS = "equal_lows"


class LevelStatus(str, Enum):
    ACTIVE = "active"            # created, not yet interacted with
    SWEPT = "swept"              # traded through and closed back on the original side in the same bar
    INVALIDATED = "invalidated"  # a bar CLOSED beyond the level (accepted, not swept)
    EXPIRED = "expired"          # older than level_expiry_bars without interaction
    MERGED = "merged"            # swing level absorbed into an equal-highs/lows level


# ---------------------------------------------------------------- Phase 6: setups
class SetupType(str, Enum):
    BREAKOUT = "breakout"                  # continuation after a BOS
    PULLBACK = "pullback"                  # return to a level broken by a BOS
    SWEEP_REVERSAL = "sweep_reversal"      # potential reversal after a liquidity sweep
    RANGE_REJECTION = "range_rejection"    # rejection of a range boundary without piercing it


class SetupStatus(str, Enum):
    CANDIDATE = "candidate"        # trigger observed; not confirmed by the engine's rules yet
    CONFIRMED = "confirmed"        # confirmation rule met (NOT a permission to trade)
    INVALIDATED = "invalidated"    # invalidation rule met (terminal)
    EXPIRED = "expired"            # ttl elapsed without invalidation (terminal)


class RegimeFit(str, Enum):
    ALLOWED = "allowed"            # the as_of regime is in the setup type's allowed list
    NOT_ALLOWED = "not_allowed"
    UNKNOWN = "unknown"            # no regime context / regime UNKNOWN / invalid data


# ---------------------------------------------------------------- Phase 7: research
class OutcomeStatus(str, Enum):
    VALID = "valid"              # all bars of the horizon exist and are contiguous
    CENSORED = "censored"        # horizon runs past the end of the dataset (NOT a zero result)
    INCOMPLETE = "incomplete"    # a gap / invalid segment inside the horizon
    NO_ANCHOR = "no_anchor"      # the anchor bar itself is not in the dataset


class SimExitReason(str, Enum):
    STOP = "stop"
    TAKE_PROFIT = "take_profit"
    STOP_AMBIGUOUS = "stop_ambiguous"   # stop and target inside one bar, order unknown -> worse case taken
    TIME = "time"
    END_OF_DATA = "end_of_data"         # still open at the last bar: marked to the last close


# ---------------------------------------------------------------- Phase 9: strategy & risk
class RiskStatus(str, Enum):
    APPROVED = "approved"      # may enter; carries a RiskPlan (a size reduction is still APPROVED + reason code)
    REJECTED = "rejected"      # this candidate cannot be traded (reason code)
    HALTED = "halted"          # hard drawdown: no new entries for the rest of the run; positions are NOT closed
