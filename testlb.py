#!/usr/bin/env python3
"""Simulate different load balancing algorithms using real usage history."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Dict, Tuple
import statistics

DB_PATH = '~/.c2switcher/store.db'


@dataclass
class UsageWindow:
    utilization: float
    hours_until_reset: float


@dataclass
class AccountSnapshot:
    name: str
    sonnet: UsageWindow
    overall: UsageWindow
    five_hour: float
    timestamp: datetime


@dataclass
class SimResult:
    algo_name: str
    selections: List[Tuple[datetime, str, float]]  # (time, account, score)
    total_requests: int
    account_usage_counts: Dict[str, int]
    final_utilization: Dict[str, float]


# ============================================================================
# Algorithm Implementations
# ============================================================================


def algo_baseline(
    accounts: List[AccountSnapshot],
) -> Tuple[AccountSnapshot, float, dict]:
    """Original algorithm with pace alignment."""
    WINDOW_LENGTH_HOURS = 168.0
    PACE_GAIN = 1.0
    PACE_AHEAD_DAMPING = 0.5
    MAX_PACE_ADJUSTMENT = 4.0
    FRESH_UTILIZATION_THRESHOLD = 25.0
    FRESH_ACCOUNT_MAX_BONUS = 3.0

    best, best_score, best_debug = None, -999, {}

    for acc in accounts:
        # Choose window
        if acc.sonnet.utilization < 99:
            util, hrs = acc.sonnet.utilization, acc.sonnet.hours_until_reset
        else:
            util, hrs = acc.overall.utilization, acc.overall.hours_until_reset

        if util >= 99:
            continue

        headroom = max(99 - util, 0)
        hrs = max(hrs, 0.001)
        drain = headroom / hrs

        # Pace
        elapsed = max(WINDOW_LENGTH_HOURS - min(hrs, WINDOW_LENGTH_HOURS), 0)
        expected_util = (elapsed / WINDOW_LENGTH_HOURS) * 100
        pace_gap = expected_util - util
        pace_adj = 0
        if headroom > 0:
            pace_adj = (pace_gap / hrs) * PACE_GAIN
            if pace_gap < 0:
                pace_adj *= PACE_AHEAD_DAMPING
            pace_adj = max(min(pace_adj, MAX_PACE_ADJUSTMENT), -MAX_PACE_ADJUSTMENT)

        # Fresh bonus
        fresh_bonus = 0
        if headroom > 0 and util < FRESH_UTILIZATION_THRESHOLD and pace_gap > 0:
            freshness = (FRESH_UTILIZATION_THRESHOLD - util) / FRESH_UTILIZATION_THRESHOLD
            fresh_bonus = freshness * FRESH_ACCOUNT_MAX_BONUS

        score = drain + pace_adj + fresh_bonus

        if score > best_score:
            best, best_score = acc, score
            best_debug = {
                'drain': drain,
                'pace': pace_adj,
                'fresh': fresh_bonus,
                'util': util,
                'hrs': hrs,
            }

    return best, best_score, best_debug


def algo_pace_gated(
    accounts: List[AccountSnapshot],
) -> Tuple[AccountSnapshot, float, dict]:
    """Skip pace unless sonnet >= 90%."""
    WINDOW_LENGTH_HOURS = 168.0
    PACE_GAIN = 1.0
    PACE_AHEAD_DAMPING = 0.5
    MAX_PACE_ADJUSTMENT = 4.0
    PACE_GATE = 90.0

    best, best_score, best_debug = None, -999, {}

    for acc in accounts:
        if acc.sonnet.utilization < 99:
            util, hrs = acc.sonnet.utilization, acc.sonnet.hours_until_reset
        else:
            util, hrs = acc.overall.utilization, acc.overall.hours_until_reset

        if util >= 99:
            continue

        headroom = max(99 - util, 0)
        hrs = max(hrs, 0.001)
        drain = headroom / hrs

        # Pace only if sonnet >= 90
        pace_adj = 0
        if headroom > 0 and acc.sonnet.utilization >= PACE_GATE:
            elapsed = max(WINDOW_LENGTH_HOURS - min(hrs, WINDOW_LENGTH_HOURS), 0)
            expected_util = (elapsed / WINDOW_LENGTH_HOURS) * 100
            pace_gap = expected_util - util
            pace_adj = (pace_gap / hrs) * PACE_GAIN
            if pace_gap < 0:
                pace_adj *= PACE_AHEAD_DAMPING
            pace_adj = max(min(pace_adj, MAX_PACE_ADJUSTMENT), -MAX_PACE_ADJUSTMENT)

        score = drain + pace_adj

        if score > best_score:
            best, best_score = acc, score
            best_debug = {'drain': drain, 'pace': pace_adj, 'util': util, 'hrs': hrs}

    return best, best_score, best_debug


def algo_low_usage_bonus(
    accounts: List[AccountSnapshot],
) -> Tuple[AccountSnapshot, float, dict]:
    """Pace gated + low-usage bonus (cap=60, gain=5, floor=20)."""
    WINDOW_LENGTH_HOURS = 168.0
    PACE_GAIN = 1.0
    PACE_AHEAD_DAMPING = 0.5
    MAX_PACE_ADJUSTMENT = 4.0
    PACE_GATE = 90.0
    LOW_BONUS_CAP = 60.0
    LOW_BONUS_GAIN = 5.0
    LOW_BONUS_FLOOR = 20.0

    best, best_score, best_debug = None, -999, {}

    for acc in accounts:
        if acc.sonnet.utilization < 99:
            util, hrs = acc.sonnet.utilization, acc.sonnet.hours_until_reset
        else:
            util, hrs = acc.overall.utilization, acc.overall.hours_until_reset

        if util >= 99:
            continue

        headroom = max(99 - util, 0)
        hrs = max(hrs, 0.001)
        drain = headroom / hrs

        # Pace only if sonnet >= 90
        pace_adj = 0
        if headroom > 0 and acc.sonnet.utilization >= PACE_GATE:
            elapsed = max(WINDOW_LENGTH_HOURS - min(hrs, WINDOW_LENGTH_HOURS), 0)
            expected_util = (elapsed / WINDOW_LENGTH_HOURS) * 100
            pace_gap = expected_util - util
            pace_adj = (pace_gap / hrs) * PACE_GAIN
            if pace_gap < 0:
                pace_adj *= PACE_AHEAD_DAMPING
            pace_adj = max(min(pace_adj, MAX_PACE_ADJUSTMENT), -MAX_PACE_ADJUSTMENT)

        # Low-usage bonus
        low_bonus = 0
        if headroom > 0 and util < LOW_BONUS_CAP:
            clamped = max(util, LOW_BONUS_FLOOR)
            normalized = (LOW_BONUS_CAP - clamped) / LOW_BONUS_CAP
            low_bonus = normalized * LOW_BONUS_GAIN

        score = drain + pace_adj + low_bonus

        if score > best_score:
            best, best_score = acc, score
            best_debug = {
                'drain': drain,
                'pace': pace_adj,
                'low_bonus': low_bonus,
                'util': util,
                'hrs': hrs,
            }

    return best, best_score, best_debug


def algo_simple_lowest(
    accounts: List[AccountSnapshot],
) -> Tuple[AccountSnapshot, float, dict]:
    """Just pick lowest overall utilization."""
    best, best_score = None, 999

    for acc in accounts:
        if acc.overall.utilization < best_score and acc.overall.utilization < 99:
            best, best_score = acc, acc.overall.utilization

    return best, -best_score, {'util': best_score} if best else (None, -999, {})


def algo_sonnet_zones(
    accounts: List[AccountSnapshot],
) -> Tuple[AccountSnapshot, float, dict]:
    """Sonnet-aware zones: <85 bonus, 85-95 neutral, >95 penalty, pace when >90."""
    WINDOW_LENGTH_HOURS = 168.0
    PACE_GAIN = 1.0
    PACE_AHEAD_DAMPING = 0.5
    MAX_PACE_ADJUSTMENT = 4.0
    LOW_BONUS_CAP = 60.0
    LOW_BONUS_GAIN = 5.0
    LOW_BONUS_FLOOR = 20.0

    # Sonnet zones
    SONNET_BONUS_ZONE = 85.0
    SONNET_NEUTRAL_ZONE = 95.0
    SONNET_PACE_GATE = 90.0
    HIGH_UTIL_PENALTY = -2.0

    best, best_score, best_debug = None, -999, {}

    for acc in accounts:
        # Prefer overall window unless exhausted
        if acc.overall.utilization < 99:
            util, hrs = acc.overall.utilization, acc.overall.hours_until_reset
            window = 'overall'
        else:
            util, hrs = acc.sonnet.utilization, acc.sonnet.hours_until_reset
            window = 'sonnet'

        if util >= 99:
            continue

        headroom = max(99 - util, 0)
        hrs = max(hrs, 0.001)
        drain = headroom / hrs

        # Pace if sonnet >= 90 (help drain catch up)
        pace_adj = 0
        if headroom > 0 and acc.sonnet.utilization >= SONNET_PACE_GATE:
            elapsed = max(WINDOW_LENGTH_HOURS - min(hrs, WINDOW_LENGTH_HOURS), 0)
            expected_util = (elapsed / WINDOW_LENGTH_HOURS) * 100
            pace_gap = expected_util - util
            pace_adj = (pace_gap / hrs) * PACE_GAIN
            if pace_gap < 0:
                pace_adj *= PACE_AHEAD_DAMPING
            pace_adj = max(min(pace_adj, MAX_PACE_ADJUSTMENT), -MAX_PACE_ADJUSTMENT)

        # Sonnet zone logic
        low_bonus = 0
        high_penalty = 0

        if acc.sonnet.utilization < SONNET_BONUS_ZONE:
            # <85: low-usage bonus active
            if util < LOW_BONUS_CAP:
                clamped = max(util, LOW_BONUS_FLOOR)
                normalized = (LOW_BONUS_CAP - clamped) / LOW_BONUS_CAP
                low_bonus = normalized * LOW_BONUS_GAIN
        elif acc.sonnet.utilization >= SONNET_NEUTRAL_ZONE:
            # >95: penalty to prefer cooler accounts
            high_penalty = HIGH_UTIL_PENALTY

        score = drain + pace_adj + low_bonus + high_penalty

        if score > best_score:
            best, best_score = acc, score
            best_debug = {
                'drain': drain,
                'pace': pace_adj,
                'low_bonus': low_bonus,
                'high_penalty': high_penalty,
                'util': util,
                'sonnet': acc.sonnet.utilization,
                'window': window,
                'hrs': hrs,
            }

    return best, best_score, best_debug


def algo_combined(
    accounts: List[AccountSnapshot],
) -> Tuple[AccountSnapshot, float, dict]:
    """Overall-first window + low-usage bonus + pace gate."""
    WINDOW_LENGTH_HOURS = 168.0
    PACE_GAIN = 1.0
    PACE_AHEAD_DAMPING = 0.5
    MAX_PACE_ADJUSTMENT = 4.0
    PACE_GATE = 90.0
    LOW_BONUS_CAP = 60.0
    LOW_BONUS_GAIN = 5.0
    LOW_BONUS_FLOOR = 20.0

    best, best_score, best_debug = None, -999, {}

    for acc in accounts:
        # Prefer overall window unless exhausted
        if acc.overall.utilization < 99:
            util, hrs = acc.overall.utilization, acc.overall.hours_until_reset
            window = 'overall'
        else:
            util, hrs = acc.sonnet.utilization, acc.sonnet.hours_until_reset
            window = 'sonnet'

        if util >= 99:
            continue

        headroom = max(99 - util, 0)
        hrs = max(hrs, 0.001)
        drain = headroom / hrs

        # Pace only if sonnet >= 90
        pace_adj = 0
        if headroom > 0 and acc.sonnet.utilization >= PACE_GATE:
            elapsed = max(WINDOW_LENGTH_HOURS - min(hrs, WINDOW_LENGTH_HOURS), 0)
            expected_util = (elapsed / WINDOW_LENGTH_HOURS) * 100
            pace_gap = expected_util - util
            pace_adj = (pace_gap / hrs) * PACE_GAIN
            if pace_gap < 0:
                pace_adj *= PACE_AHEAD_DAMPING
            pace_adj = max(min(pace_adj, MAX_PACE_ADJUSTMENT), -MAX_PACE_ADJUSTMENT)

        # Low-usage bonus
        low_bonus = 0
        if headroom > 0 and util < LOW_BONUS_CAP:
            clamped = max(util, LOW_BONUS_FLOOR)
            normalized = (LOW_BONUS_CAP - clamped) / LOW_BONUS_CAP
            low_bonus = normalized * LOW_BONUS_GAIN

        score = drain + pace_adj + low_bonus

        if score > best_score:
            best, best_score = acc, score
            best_debug = {
                'drain': drain,
                'pace': pace_adj,
                'low_bonus': low_bonus,
                'util': util,
                'window': window,
                'hrs': hrs,
            }

    return best, best_score, best_debug


def algo_overall_first(
    accounts: List[AccountSnapshot],
) -> Tuple[AccountSnapshot, float, dict]:
    """Use overall window by default (only use sonnet if overall exhausted)."""
    best, best_score, best_debug = None, -999, {}

    for acc in accounts:
        # Prefer overall window unless exhausted
        if acc.overall.utilization < 99:
            util, hrs = acc.overall.utilization, acc.overall.hours_until_reset
            window = 'overall'
        else:
            util, hrs = acc.sonnet.utilization, acc.sonnet.hours_until_reset
            window = 'sonnet'

        if util >= 99:
            continue

        headroom = max(99 - util, 0)
        hrs = max(hrs, 0.001)
        score = headroom / hrs

        if score > best_score:
            best, best_score = acc, score
            best_debug = {
                'headroom': headroom,
                'hrs': hrs,
                'util': util,
                'window': window,
            }

    return best, best_score, best_debug


def algo_headroom_per_hour(
    accounts: List[AccountSnapshot],
) -> Tuple[AccountSnapshot, float, dict]:
    """Pure headroom/hours (no pace, no bonuses)."""
    best, best_score, best_debug = None, -999, {}

    for acc in accounts:
        util = acc.overall.utilization
        hrs = acc.overall.hours_until_reset

        if util >= 99:
            continue

        headroom = max(99 - util, 0)
        hrs = max(hrs, 0.001)
        score = headroom / hrs

        if score > best_score:
            best, best_score = acc, score
            best_debug = {'headroom': headroom, 'hrs': hrs, 'util': util}

    return best, best_score, best_debug


# ============================================================================
# Simulation Engine
# ============================================================================


def load_usage_history() -> List[AccountSnapshot]:
    """Load all usage snapshots from DB."""
    import os

    db = os.path.expanduser(DB_PATH)
    conn = sqlite3.connect(db)
    cur = conn.cursor()

    snapshots = []

    cur.execute(
        """
      SELECT a.nickname,
             h.queried_at,
             h.five_hour_utilization,
             h.seven_day_utilization,
             h.seven_day_resets_at,
             h.seven_day_sonnet_utilization,
             h.seven_day_sonnet_resets_at
      FROM accounts a
      JOIN usage_history h ON a.uuid = h.account_uuid
      ORDER BY h.queried_at ASC
   """
    )

    for row in cur.fetchall():
        name, ts_str, fh, overall_util, overall_reset, sonnet_util, sonnet_reset = row

        # Parse timestamp (handle both naive and aware)
        if 'T' in ts_str:
            ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        else:
            ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=None)

        # Calculate hours until reset
        def hrs_until(reset_str):
            if not reset_str:
                return 24.0
            reset_dt = datetime.fromisoformat(reset_str.replace('Z', '+00:00'))
            # Make both naive for comparison
            if ts.tzinfo:
                reset_naive = reset_dt.replace(tzinfo=None) if reset_dt.tzinfo else reset_dt
                ts_naive = ts.replace(tzinfo=None)
            else:
                reset_naive = reset_dt.replace(tzinfo=None) if reset_dt.tzinfo else reset_dt
                ts_naive = ts
            delta = (reset_naive - ts_naive).total_seconds() / 3600
            return max(delta, 0.001)

        sonnet = UsageWindow(
            utilization=float(sonnet_util or 0),
            hours_until_reset=hrs_until(sonnet_reset),
        )
        overall = UsageWindow(
            utilization=float(overall_util or 0),
            hours_until_reset=hrs_until(overall_reset),
        )

        # Normalize to naive datetime
        ts_naive = ts.replace(tzinfo=None) if ts.tzinfo else ts

        snapshots.append(
            AccountSnapshot(
                name=name,
                sonnet=sonnet,
                overall=overall,
                five_hour=float(fh or 0),
                timestamp=ts_naive,
            )
        )

    conn.close()
    return snapshots


def simulate_algorithm(algo_func, snapshots: List[AccountSnapshot], requests_per_hour=10) -> SimResult:
    """Simulate usage over time."""
    # Group by timestamp
    by_time: Dict[datetime, List[AccountSnapshot]] = {}
    for snap in snapshots:
        if snap.timestamp not in by_time:
            by_time[snap.timestamp] = []
        by_time[snap.timestamp].append(snap)

    times = sorted(by_time.keys())
    if len(times) < 2:
        return SimResult(algo_func.__name__, [], 0, {}, {})

    # Simulate requests between first and last snapshot
    start, end = times[0], times[-1]
    duration_hours = (end - start).total_seconds() / 3600
    total_requests = int(duration_hours * requests_per_hour)

    selections = []
    usage_counts = {}

    current_time = start
    time_step = timedelta(hours=1.0 / requests_per_hour)

    for req_num in range(total_requests):
        # Find most recent snapshot <= current_time
        snapshot_time = max([t for t in times if t <= current_time], default=times[0])
        available = by_time[snapshot_time]

        # Run algorithm
        selected, score, debug = algo_func(available)

        if selected:
            selections.append((current_time, selected.name, score))
            usage_counts[selected.name] = usage_counts.get(selected.name, 0) + 1

        current_time += time_step

    # Final utilization from last snapshot
    final_util = {acc.name: acc.overall.utilization for acc in by_time[times[-1]]}

    return SimResult(
        algo_name=algo_func.__name__,
        selections=selections,
        total_requests=total_requests,
        account_usage_counts=usage_counts,
        final_utilization=final_util,
    )


# ============================================================================
# Analysis
# ============================================================================


def analyze_results(results: List[SimResult]):
    """Compare algorithm performance."""
    print('=' * 80)
    print('LOAD BALANCER SIMULATION RESULTS')
    print('=' * 80)
    print()

    for res in results:
        print(f'\n{"=" * 80}')
        print(f'Algorithm: {res.algo_name}')
        print(f'{"=" * 80}')

        print(f'\nTotal Requests: {res.total_requests}')
        print('\nAccount Selection Distribution:')
        for acc, count in sorted(res.account_usage_counts.items()):
            pct = (count / res.total_requests * 100) if res.total_requests > 0 else 0
            print(f'  {acc:10s}: {count:5d} ({pct:5.1f}%)')

        print('\nFinal Utilization (overall %):')
        for acc, util in sorted(res.final_utilization.items()):
            print(f'  {acc:10s}: {util:5.1f}%')

        # Balance metric: std dev of usage counts
        if res.account_usage_counts:
            counts = list(res.account_usage_counts.values())
            balance_score = statistics.stdev(counts) if len(counts) > 1 else 0
            print(f'\nBalance Score (lower=better): {balance_score:.1f}')

        # Show sample selections
        if res.selections:
            print('\nSample Selections (first 10):')
            for ts, acc, score in res.selections[:10]:
                print(f'  {ts.strftime("%Y-%m-%d %H:%M")} → {acc:10s} (score: {score:6.3f})')

    print('\n' + '=' * 80)
    print('COMPARISON SUMMARY')
    print('=' * 80)

    for res in results:
        counts = list(res.account_usage_counts.values())
        balance = statistics.stdev(counts) if len(counts) > 1 else 0
        max_util = max(res.final_utilization.values()) if res.final_utilization else 0
        avg_util = statistics.mean(res.final_utilization.values()) if res.final_utilization else 0

        print(f'\n{res.algo_name:30s}: balance={balance:6.1f}, max_util={max_util:5.1f}%, avg_util={avg_util:5.1f}%')


# ============================================================================
# Main
# ============================================================================


def test_current_state():
    """Test with current actual values from user's system."""
    print('\n' + '=' * 80)
    print('CURRENT STATE TEST (as of Nov 3, 2025)')
    print('=' * 80)

    # Current values from SQLite
    snapshots = [
        AccountSnapshot(
            name='last',
            sonnet=UsageWindow(27, 54.1),
            overall=UsageWindow(16, 54.1),
            five_hour=0,
            timestamp=datetime.now(),
        ),
        AccountSnapshot(
            name='main',
            sonnet=UsageWindow(74, 7.1),
            overall=UsageWindow(36, 88),
            five_hour=34,
            timestamp=datetime.now(),
        ),
        AccountSnapshot(
            name='s1m',
            sonnet=UsageWindow(5, 135.1),
            overall=UsageWindow(31, 133),
            five_hour=0,
            timestamp=datetime.now(),
        ),
    ]

    algos = [
        ('baseline', algo_baseline),
        ('sonnet_zones', algo_sonnet_zones),
        ('pace_gated (sonnet>=90)', algo_pace_gated),
        ('low_usage_bonus', algo_low_usage_bonus),
        ('combined (overall+low+pace)', algo_combined),
        ('overall_first', algo_overall_first),
        ('simple_lowest', algo_simple_lowest),
        ('headroom_per_hour', algo_headroom_per_hour),
    ]

    results = []
    for name, algo in algos:
        selected, score, debug = algo(snapshots)
        results.append((name, selected.name if selected else 'None', score, debug))

    print('\nRanking (higher score = selected):\n')
    for name, selected, score, debug in sorted(results, key=lambda x: x[2], reverse=True):
        print(f'{name:25s} → {selected:6s} (score: {score:7.3f})')
        if debug:
            print(f'  {debug}')

    print('\n' + '=' * 80)


def test_sonnet_spike():
    """Test sonnet spike scenario (main@95% sonnet, last@16% overall)."""
    print('\n' + '=' * 80)
    print('SONNET SPIKE TEST (main maxing sonnet last week)')
    print('=' * 80)

    # Simulate: main had sonnet spike to 95%
    snapshots = [
        AccountSnapshot(
            name='last',
            sonnet=UsageWindow(27, 54.1),
            overall=UsageWindow(16, 54.1),
            five_hour=0,
            timestamp=datetime.now(),
        ),
        AccountSnapshot(
            name='main',
            sonnet=UsageWindow(95, 10),  # Spiked to 95%, resets soon
            overall=UsageWindow(52, 88),
            five_hour=85,
            timestamp=datetime.now(),
        ),
        AccountSnapshot(
            name='s1m',
            sonnet=UsageWindow(5, 135.1),
            overall=UsageWindow(31, 133),
            five_hour=0,
            timestamp=datetime.now(),
        ),
    ]

    algos = [
        ('baseline', algo_baseline),
        ('sonnet_zones (<85 bonus, >95 penalty)', algo_sonnet_zones),
        ('combined (overall+low+pace)', algo_combined),
        ('overall_first', algo_overall_first),
    ]

    # Show all candidates for sonnet_zones
    print('\nDetailed sonnet_zones scoring:')
    for acc in snapshots:
        selected, score, debug = algo_sonnet_zones([acc])
        if selected:
            print(f'  {acc.name:6s}: {score:7.3f} {debug}')

    results = []
    for name, algo in algos:
        selected, score, debug = algo(snapshots)
        results.append((name, selected.name if selected else 'None', score, debug))

    print('\nRanking (higher score = selected):\n')
    for name, selected, score, debug in sorted(results, key=lambda x: x[2], reverse=True):
        print(f'{name:35s} → {selected:6s} (score: {score:7.3f})')
        if debug:
            print(f'  {debug}')

    print('\n' + '=' * 80)


if __name__ == '__main__':
    print('Loading usage history from database...')
    snapshots = load_usage_history()
    print(f'Loaded {len(snapshots)} snapshots')

    # Group by account to show data range
    by_acc = {}
    for s in snapshots:
        if s.name not in by_acc:
            by_acc[s.name] = []
        by_acc[s.name].append(s)

    print('\nData Summary:')
    for name, snaps in sorted(by_acc.items()):
        print(f'  {name:10s}: {len(snaps):4d} snapshots, {snaps[0].timestamp} to {snaps[-1].timestamp}')

    print('\nRunning simulations (10 req/hour)...\n')

    algos = [
        algo_baseline,
        algo_sonnet_zones,
        algo_pace_gated,
        algo_low_usage_bonus,
        algo_combined,
        algo_simple_lowest,
        algo_headroom_per_hour,
    ]

    results = []
    for algo in algos:
        print(f'Simulating {algo.__name__}...')
        res = simulate_algorithm(algo, snapshots, requests_per_hour=10)
        results.append(res)

    analyze_results(results)

    # Test current state
    test_current_state()

    # Test sonnet spike scenario
    test_sonnet_spike()
