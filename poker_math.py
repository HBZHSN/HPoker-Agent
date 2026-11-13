"""Local Hold'em calculations using only cards visible to the account."""

import math
import random
from collections import Counter
from itertools import combinations

RANKS = "23456789TJQKA"
SUITS = "shcd"
DECK = [rank + suit for rank in RANKS for suit in SUITS]
HAND_NAMES = ["高牌", "一对", "两对", "三条", "顺子", "同花", "葫芦", "四条", "同花顺"]


def card_text(card):
    return card["notation"] if isinstance(card, dict) else card


def validate_cards(hole, board, dead=()):
    cards = [card_text(c) for c in [*hole, *board, *dead]]
    if len(hole) != 2 or len(board) not in (0, 3, 4, 5):
        raise ValueError("需要两张手牌，公共牌应为 0、3、4 或 5 张")
    if any(c not in DECK for c in cards) or len(set(cards)) != len(cards):
        raise ValueError("牌面无效或存在重复牌")
    return cards[:2], cards[2:2 + len(board)], cards[2 + len(board):]


def straight_high(ranks):
    values = set(ranks)
    if 14 in values:
        values.add(1)
    return next((high for high in range(14, 4, -1)
                 if set(range(high - 4, high + 1)) <= values), 0)


def evaluate(cards):
    """Comparable score for the best five of five to seven cards."""
    ranks = [RANKS.index(c[0]) + 2 for c in cards]
    counts = Counter(ranks)
    groups = sorted(((n, r) for r, n in counts.items()), reverse=True)
    flush = next(([RANKS.index(c[0]) + 2 for c in cards if c[1] == suit]
                  for suit in SUITS if sum(c[1] == suit for c in cards) >= 5), None)
    if flush and (high := straight_high(flush)):
        return (8, high)
    if groups[0][0] == 4:
        quad = groups[0][1]
        return (7, quad, max(r for r in ranks if r != quad))
    trips = sorted((r for r, n in counts.items() if n >= 3), reverse=True)
    if trips:
        pairs = sorted((r for r, n in counts.items() if n >= 2 and r != trips[0]), reverse=True)
        if pairs:
            return (6, trips[0], pairs[0])
    if flush:
        return (5, *sorted(flush, reverse=True)[:5])
    if high := straight_high(ranks):
        return (4, high)
    if trips:
        return (3, trips[0], *sorted((r for r in counts if r != trips[0]), reverse=True)[:2])
    pairs = sorted((r for r, n in counts.items() if n == 2), reverse=True)
    if len(pairs) >= 2:
        return (2, *pairs[:2], max(r for r in counts if r not in pairs[:2]))
    if pairs:
        return (1, pairs[0], *sorted((r for r in counts if r != pairs[0]), reverse=True)[:3])
    return (0, *sorted(counts, reverse=True)[:5])


def outs(hole, board, dead=()):
    if len(board) not in (3, 4):
        return None
    known = set(hole + board + list(dead))
    remaining = [c for c in DECK if c not in known]
    current = evaluate(hole + board)
    categories = {}
    for card in remaining:
        result = evaluate(hole + board + [card])
        if result[0] <= current[0]:
            continue
        if len(board) == 4 and result <= evaluate(board + [card]):
            continue
        categories.setdefault(HAND_NAMES[result[0]], []).append(card)
    all_outs = sorted({c for group in categories.values() for c in group})
    n, count = len(remaining), len(all_outs)
    return {"count": count, "cards": all_outs, "categories": categories,
            "next_card_probability": count / n,
            "by_river_probability": 1 - math.comb(n - count, 5 - len(board)) / math.comb(n, 5 - len(board)),
            "note": "提升自身牌型的候选 outs；不保证击败对手，不能当作净胜率。"}


def equity(hole, board, opponents=1, iterations=600, dead=(), seed=None):
    hole, board, dead = validate_cards(hole, board, dead)
    if type(opponents) is not int or not 1 <= opponents <= 8:
        raise ValueError("对手数量应为 1–8")
    if type(iterations) is not int or not 100 <= iterations <= 3000:
        raise ValueError("模拟次数应为 100–3000")
    remaining = [c for c in DECK if c not in set(hole + board + dead)]
    missing = 5 - len(board)
    if missing + opponents * 2 > len(remaining):
        raise ValueError("剩余牌不足")
    exact = len(board) == 5 and opponents == 1
    rng = random.Random(seed)
    deals = combinations(remaining, 2) if exact else (
        rng.sample(remaining, missing + opponents * 2) for _ in range(iterations))
    wins = ties = samples = 0
    share = 0.0
    for deal in deals:
        final_board = board + list(deal[:missing])
        hero = evaluate(hole + final_board)
        scores = [evaluate(list(deal[missing + i * 2:missing + i * 2 + 2]) + final_board)
                  for i in range(opponents)]
        best = max(scores)
        if hero > best:
            wins += 1
            share += 1
        elif hero == best:
            ties += 1
            share += 1 / (1 + scores.count(hero))
        samples += 1
    value = share / samples
    return {"equity": value, "win_rate": wins / samples, "tie_rate": ties / samples,
            "samples": samples, "method": "exact" if exact else "monte_carlo",
            "standard_error": 0 if exact else math.sqrt(value * (1 - value) / samples),
            "opponents": opponents, "current_hand": HAND_NAMES[evaluate(hole + board)[0]] if board else "翻牌前",
            "outs": outs(hole, board, dead),
            "assumption": "仅为均匀随机未知手牌的基准权益，未考虑本次下注对范围的选择；不能直接视为面对大下注/全下的胜率或正EV依据。增加样本不会推断范围。"}
