"""The tick's extra passes and the extra lines they add to the status block, the prompt, `hubkit mine` and the digest.

Standing rules (standrules), the user's review debt (reviewdebt), the end-of-day clock (eodclock), stale tracked items
(stale) and routed items with no PR (nopr). Each part fails alone: an error is logged to hook-errors.jsonl and that part
shows nothing."""

from __future__ import annotations

from collections.abc import Callable

from . import config, eodclock, nopr, reviewdebt, stale, standrules, status, store, threads
from .orders import Orders


def _safe(cfg: dict, where: str, fn: Callable[[], list]) -> list:
    try:
        return fn()
    except Exception as e:  # one broken part must not take the tick, the hook or `mine` down
        store.append_row(config.state_dir(cfg) / "hook-errors.jsonl", {"where": where, "error": repr(e)[:200]})
        return []


def tick_pass(cfg: dict, orders: Orders, now: float) -> list[dict]:
    """Alerts from the passes that must see the PRs before the routed refresh may close their items."""
    return [*_safe(cfg, "standing", lambda: standrules.tick(cfg, orders, now)),
            *_safe(cfg, "review_debt", lambda: reviewdebt.sweep(cfg, now)),
            *_safe(cfg, "eod_clock", lambda: eodclock.run(cfg, now)),
            *_safe(cfg, "thread_link", lambda: [threads.autolink_pass(cfg)][:0])]


def alerts(cfg: dict, now: float) -> list[dict]:
    return [*_safe(cfg, "nopr", lambda: nopr.alerts(cfg, now)), *_safe(cfg, "threads", lambda: threads.alerts(cfg, now))]


def block_lines(cfg: dict, work: list[dict], now: float) -> list[str]:
    return [*_safe(cfg, "standing_block", lambda: standrules.block_lines(cfg, now)), *_safe(cfg, "nopr_block", lambda: nopr.block_lines(cfg, work))]


def prompt_lines(cfg: dict, now: float) -> list[str]:
    return [*_safe(cfg, "standing_prompt", lambda: standrules.prompt_lines(cfg)), *_safe(cfg, "nopr_prompt", lambda: nopr.prompt_lines(cfg, now))]


def mine_rows(cfg: dict, items: list[dict], now: float, user: list[str], hub: list[str], peers: list[str], others: list[str]) -> list[list[str]]:
    """The four `mine` groups with the review debt under the user, fired standing rules under the hub and the stale marks on item rows."""
    user = [*user, *_safe(cfg, "review_debt_rows", lambda: reviewdebt.rows(cfg, now))]
    hub = [*hub, *_safe(cfg, "standing_rows", lambda: standrules.mine_rows(cfg, now))]
    marks: dict[str, str] = {}
    try:
        marks = stale.notes(cfg, items, now)
    except Exception as e:
        store.append_row(config.state_dir(cfg) / "hook-errors.jsonl", {"where": "stale", "error": repr(e)[:200]})
    return [[r + marks.get(r.split()[0], "") if r.strip() else r for r in rows] for rows in (user, hub, peers, others)]


def digest_lines(cfg: dict, now: float) -> list[str]:
    items = status.work_load(store.state(cfg))
    return [*_safe(cfg, "review_debt_digest", lambda: reviewdebt.digest_lines(cfg, now)),
            *_safe(cfg, "stale_digest", lambda: stale.digest_lines(cfg, items, now))]
