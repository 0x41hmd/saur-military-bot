import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

rng = random.SystemRandom()


SLOT_SYMBOLS: List[Tuple[str, str, int, int]] = [
    ("cherry",  "Cherry",  30,   6),
    ("lemon",   "Lemon",   25,   9),
    ("bell",    "Bell",    18,  16),
    ("star",    "Star",    12,  28),
    ("diamond", "Diamond",  8,  55),
    ("bar",     "BAR",      5, 120),
    ("seven",   "Seven",    2, 750),
]
SLOT_KEYS = [s[0] for s in SLOT_SYMBOLS]
SLOT_PAIR_MULT = 1.0


def spin_slots() -> List[str]:
    weights = [s[2] for s in SLOT_SYMBOLS]
    return rng.choices(SLOT_KEYS, weights=weights, k=3)


WIN_TIER_WEIGHTS = (70, 25, 5)
SLOT_WIN_TIERS: List[Tuple[str, float, List[str]]] = [
    ("Small win", 1.5, ["cherry", "lemon"]),
    ("Nice win",  2.4, ["bell", "star", "diamond"]),
    ("JACKPOT",   4.0, ["bar", "seven"]),
]


def slots_outcome(win: bool) -> Tuple[List[str], float, str]:
    if not win:
        return rng.sample(SLOT_KEYS, 3), 0.0, "loss"
    idx = rng.choices(range(len(SLOT_WIN_TIERS)), weights=WIN_TIER_WEIGHTS, k=1)[0]
    name, mult, symbols = SLOT_WIN_TIERS[idx]
    sym = rng.choice(symbols)
    return [sym, sym, sym], mult, name


def slots_multiplier(reels: List[str]) -> float:
    counts = Counter(reels)
    top, n = counts.most_common(1)[0]
    if n == 3:
        return float(next(s[3] for s in SLOT_SYMBOLS if s[0] == top))
    if n == 2:
        return SLOT_PAIR_MULT
    return 0.0


ROLL_TIERS: List[Tuple[int, float, str]] = [
    (50,  0.0,  "Bust"),
    (80,  1.5,  "Nice"),
    (95,  2.4,  "Great"),
    (99,  4.0,  "Huge"),
    (100, 4.0,  "PERFECT 100"),
]


def roll_outcome(win: bool) -> int:
    if not win:
        return rng.randint(1, 50)
    idx = rng.choices(range(3), weights=WIN_TIER_WEIGHTS, k=1)[0]
    lo, hi = ((51, 80), (81, 95), (96, 100))[idx]
    return rng.randint(lo, hi)


def roll_dice() -> int:
    return rng.randint(1, 100)


def roll_result(value: int) -> Tuple[float, str]:
    for top, mult, name in ROLL_TIERS:
        if value <= top:
            return mult, name
    return 0.0, "Bust"


WHEEL_SLICES: List[int] = [
    200_000,
    0, 20_000, 0, 50_000, 30_000, 0, 100_000,
    20_000, 0, 80_000, 50_000, 0, 150_000, 20_000, 0,
]
WHEEL_JACKPOT = max(WHEEL_SLICES)


def spin_wheel() -> Tuple[int, int]:
    i = rng.randrange(len(WHEEL_SLICES))
    return i, WHEEL_SLICES[i]


SUITS = ["S", "H", "D", "C"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
Card = Tuple[str, str]


def new_shoe(decks: int = 1) -> List[Card]:
    shoe = [(r, s) for _ in range(decks) for s in SUITS for r in RANKS]
    rng.shuffle(shoe)
    return shoe


def card_value(rank: str) -> int:
    if rank == "A":
        return 11
    if rank in ("J", "Q", "K", "10"):
        return 10
    return int(rank)


def hand_total(cards: List[Card]) -> Tuple[int, bool]:
    total = sum(card_value(r) for r, _ in cards)
    aces = sum(1 for r, _ in cards if r == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total, aces > 0 and total <= 21


def is_blackjack(cards: List[Card]) -> bool:
    return len(cards) == 2 and hand_total(cards)[0] == 21


@dataclass
class BJHand:
    cards: List[Card] = field(default_factory=list)
    bet: int = 0
    doubled: bool = False
    from_split: bool = False
    split_aces: bool = False
    surrendered: bool = False
    done: bool = False

    @property
    def total(self) -> int:
        return hand_total(self.cards)[0]

    @property
    def busted(self) -> bool:
        return self.total > 21


class Blackjack:
    BLACKJACK_PAYS = 1.5

    def __init__(self, bet: int):
        self.shoe = new_shoe(6)
        self.dealer: List[Card] = []
        self.hands: List[BJHand] = [BJHand(bet=bet)]
        self.active = 0
        self.finished = False
        for _ in range(2):
            self.hands[0].cards.append(self.shoe.pop())
            self.dealer.append(self.shoe.pop())
        self.dealer_blackjack = is_blackjack(self.dealer)
        if is_blackjack(self.hands[0].cards) or self.dealer_blackjack:
            self.hands[0].done = True
            self.finished = True

    @property
    def hand(self) -> BJHand:
        return self.hands[self.active]

    @property
    def total_bet(self) -> int:
        return sum(h.bet for h in self.hands)

    def can_hit(self) -> bool:
        return not self.finished and not self.hand.split_aces

    def can_double(self) -> bool:
        return not self.finished and len(self.hand.cards) == 2 and not self.hand.split_aces

    def can_split(self) -> bool:
        h = self.hand
        return (not self.finished and len(self.hands) == 1 and len(h.cards) == 2
                and card_value(h.cards[0][0]) == card_value(h.cards[1][0]))

    def can_surrender(self) -> bool:
        return not self.finished and len(self.hands) == 1 and len(self.hand.cards) == 2 and not self.hand.from_split

    def _next_hand(self) -> None:
        self.hand.done = True
        for i, h in enumerate(self.hands):
            if not h.done:
                self.active = i
                if h.total == 21:
                    self._next_hand()
                return
        self._dealer_play()

    def _dealer_play(self) -> None:
        if any(not h.busted and not h.surrendered for h in self.hands):
            while hand_total(self.dealer)[0] < 17:
                self.dealer.append(self.shoe.pop())
        self.finished = True

    def hit(self) -> None:
        self.hand.cards.append(self.shoe.pop())
        if self.hand.busted or self.hand.total == 21:
            self._next_hand()

    def stand(self) -> None:
        self._next_hand()

    def double(self) -> int:
        extra = self.hand.bet
        self.hand.bet += extra
        self.hand.doubled = True
        self.hand.cards.append(self.shoe.pop())
        self._next_hand()
        return extra

    def split(self) -> int:
        h = self.hand
        extra = h.bet
        second = BJHand(cards=[h.cards.pop()], bet=extra, from_split=True)
        h.from_split = True
        aces = h.cards[0][0] == "A"
        h.split_aces = second.split_aces = aces
        h.cards.append(self.shoe.pop())
        second.cards.append(self.shoe.pop())
        self.hands.append(second)
        if aces:
            h.done = second.done = True
            self.finished = False
            self._dealer_play()
        elif h.total == 21:
            self._next_hand()
        return extra

    def surrender(self) -> None:
        self.hand.surrendered = True
        self.hand.done = True
        self.finished = True

    def settle(self) -> List[Dict]:
        d_total = hand_total(self.dealer)[0]
        d_bj = self.dealer_blackjack
        results = []
        for h in self.hands:
            if h.surrendered:
                payout, outcome = h.bet // 2, "surrender"
            elif is_blackjack(h.cards) and not h.from_split:
                if d_bj:
                    payout, outcome = h.bet, "push"
                else:
                    payout, outcome = h.bet + int(h.bet * self.BLACKJACK_PAYS), "blackjack"
            elif d_bj:
                payout, outcome = 0, "loss"
            elif h.busted:
                payout, outcome = 0, "bust"
            elif d_total > 21 or h.total > d_total:
                payout, outcome = h.bet * 2, "win"
            elif h.total == d_total:
                payout, outcome = h.bet, "push"
            else:
                payout, outcome = 0, "loss"
            results.append({"bet": h.bet, "payout": payout, "outcome": outcome, "total": h.total})
        return results


POKER_PAYTABLE: List[Tuple[str, int]] = [
    ("Royal Flush",     250),
    ("Straight Flush",   50),
    ("Four of a Kind",   25),
    ("Full House",        9),
    ("Flush",             6),
    ("Straight",          4),
    ("Three of a Kind",   3),
    ("Two Pair",          2),
    ("Jacks or Better",   1),
]
POKER_PAYS = dict(POKER_PAYTABLE)


def evaluate_poker(cards: List[Card]) -> Optional[str]:
    values = sorted(RANKS.index(r) for r, _ in cards)
    counts = sorted(Counter(values).values(), reverse=True)
    flush = len({s for _, s in cards}) == 1
    straight = (len(set(values)) == 5 and values[-1] - values[0] == 4) or values == [0, 1, 2, 3, 12]
    if straight and flush:
        return "Royal Flush" if values == [8, 9, 10, 11, 12] else "Straight Flush"
    if counts[0] == 4:
        return "Four of a Kind"
    if counts == [3, 2]:
        return "Full House"
    if flush:
        return "Flush"
    if straight:
        return "Straight"
    if counts[0] == 3:
        return "Three of a Kind"
    if counts[:2] == [2, 2]:
        return "Two Pair"
    if counts[0] == 2:
        pair_value = Counter(values).most_common(1)[0][0]
        if pair_value >= RANKS.index("J"):
            return "Jacks or Better"
    return None


class Poker:
    def __init__(self, bet: int):
        self.bet = bet
        self.deck = new_shoe(1)
        self.cards: List[Card] = [self.deck.pop() for _ in range(5)]
        self.held = [False] * 5
        self.drawn = False

    def toggle(self, i: int) -> None:
        if not self.drawn:
            self.held[i] = not self.held[i]

    def draw(self) -> None:
        for i in range(5):
            if not self.held[i]:
                self.cards[i] = self.deck.pop()
        self.drawn = True

    def result(self) -> Tuple[Optional[str], int]:
        name = evaluate_poker(self.cards)
        return name, (self.bet * POKER_PAYS[name] if name else 0)
