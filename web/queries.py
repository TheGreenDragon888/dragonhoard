"""
web/queries.py

Builds the one JSON payload web/app.py serves. Every derived figure here -
machine levels, mining slots, pool percentages, market targets - is computed
with the same functions the bot itself uses (config.py, data/materials.py),
imported directly rather than re-implemented, so this can never drift from
what the game actually does. Nothing here writes to the database; every
statement is a SELECT against a connection opened read-only by web/app.py.

Two figures the schema simply cannot answer without a live Discord
connection - real member counts and "server active in the last N days" -
are approximated from data that IS in the database (see docstrings on
approx_members and last_activity below) rather than given a Discord API
dependency. Every place that happens is labelled in the payload so the
frontend can say so rather than pass the number off as exact.
"""
import sqlite3
from datetime import datetime, timezone

import config
from data.materials import (
    DRILL_SCRAP_JOB_TARGET,
    DRILL_UPGRADE_JOB_TARGET,
    MINING_EFFICIENCIES,
    MINING_FOCUSES,
    MINING_POOL_BAG_SIZE,
    TRADEABLE_ORDER,
    effective_capacity,
    get_material_info,
    mining_slot_level,
    mining_slots,
    mining_slot_threshold,
    target_stock,
    upgrade_threshold,
)
from utils.job_board import job_board_today

from web import directory

MACHINES = ("furnace", "blast_furnace", "factory", "press", "scrapper")
MACHINE_LABEL = {
    "furnace": "Furnace",
    "blast_furnace": "Blast furnace",
    "factory": "Factory",
    "press": "Hydraulic press",
    "scrapper": "Scrapper",
}
MACHINE_UNIT = {
    "furnace": "items",
    "blast_furnace": "batches",
    "factory": "items",
    "press": "items",
    "scrapper": "items",
}
MACHINE_DEFAULT_FEE = {
    "furnace": config.DEFAULT_FURNACE_FEE,
    "blast_furnace": config.DEFAULT_BLAST_FURNACE_FEE,
    "factory": config.DEFAULT_FACTORY_FEE,
    "press": config.DEFAULT_PRESS_FEE,
    "scrapper": config.DEFAULT_SCRAPPER_FEE,
}
GEM_DRILL_TYPES = ("ruby_drill", "obsidian_drill", "diamond_drill")
GEM_MATERIAL_IDS = ("ruby", "obsidian", "diamond")

_SCHEMA_TABLES = (
    "users", "user_materials", "server_config", "server_currency_balances",
    "server_material_storage", "daily_jobs", "daily_job_progress",
    "notifications", "notification_reads", "user_notifications",
    "drills", "drill_contents", "server_mining_pool", "user_mining_focus",
    "user_mining_efficiency", "user_mining_efficiency_carry", "production_jobs",
)


def _n(v) -> str:
    return f"{round(v):,}"


def _m(v) -> str:
    return f"{v:,.2f}"


def _pct(v) -> str:
    return f"{v * 100:.1f}%"


def _level_for(collected: float) -> int:
    """Same loop as utils/db_helpers.apply_machine_upgrades, minus the write -
    a machine's level, derived from its lifetime fees rather than stored."""
    level = 1
    while collected >= upgrade_threshold(level + 1):
        level += 1
    return level


def _parse_ts(value: str | None) -> datetime | None:
    """Every stored timestamp is `datetime('now')` - naive UTC text - except
    daily_job_progress.claimed_at, which shares the same format."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _material_label(material_id: str) -> str:
    info = get_material_info(material_id)
    return info["name"] if info else material_id


def _job_target_label(conn: sqlite3.Connection, job: sqlite3.Row) -> str:
    """A production job's target column, for display. Most jobs name a
    material; the two drill-sentinel target_ids (DRILL_UPGRADE_JOB_TARGET,
    DRILL_SCRAP_JOB_TARGET) instead point at target_drill_id, so those are
    resolved to the drill's own type/level rather than shown as the raw
    sentinel string - used both for a server's production queue and for a
    player's own outstanding-jobs list, so a mid-upgrade drill reads the
    same way in either place."""
    if job["target_id"] in (DRILL_UPGRADE_JOB_TARGET, DRILL_SCRAP_JOB_TARGET):
        verb = "Upgrade" if job["target_id"] == DRILL_UPGRADE_JOB_TARGET else "Scrap"
        drow = conn.execute(
            "SELECT drill_type, level FROM drills WHERE drill_id = ?",
            (job["target_drill_id"],),
        ).fetchone()
        if drow:
            dinfo = get_material_info(drow["drill_type"])
            dname = dinfo["name"] if dinfo else drow["drill_type"]
            return f"{verb} {dname} (Lv.{drow['level']}) #{job['target_drill_id']}"
        return f"{verb} drill #{job['target_drill_id']}"
    unit = " batches" if job["job_type"] == "blast_furnace" else ""
    return f"{job['quantity']}× {_material_label(job['target_id'])}{unit}"


def build_payload(
    conn: sqlite3.Connection,
    *,
    anonymize: bool = False,
    hide_departed: bool = True,
    dormant_days: int = 14,
    burn_floor: float = 15.0,
    stalled_days: int = 5,
) -> dict:
    directory.reload()
    now = datetime.now(timezone.utc)

    def name_for_user(user_id: int) -> str:
        if anonymize:
            return "user_" + str(user_id)[-4:]
        return directory.user_name(user_id)

    row_total = 0
    for table in _SCHEMA_TABLES:
        row_total += conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # ---- base rows ---------------------------------------------------
    server_configs = conn.execute("SELECT * FROM server_config").fetchall()
    all_users = conn.execute("SELECT * FROM users").fetchall()
    all_drills = conn.execute("SELECT * FROM drills").fetchall()
    all_jobs = conn.execute(
        "SELECT * FROM production_jobs WHERE status != 'complete'"
    ).fetchall()
    today = job_board_today()

    def guild_balances(guild_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT user_id, balance FROM server_currency_balances "
            "WHERE guild_id = ? ORDER BY balance DESC",
            (guild_id,),
        ).fetchall()

    def guild_last_activity(guild_id: int) -> datetime | None:
        """The most recent timestamped thing that happened in this server.
        server_config and drills carry no timestamp of their own (see
        web/README.md), so this is the best a DB-only reader can do: the
        newest of a queued production job, a posted daily job, or a paid
        job-board completion. A server with none of those ever recorded
        returns None rather than a fabricated "quiet forever"."""
        rows = conn.execute(
            "SELECT MAX(queued_at) AS ts FROM production_jobs WHERE guild_id = ? "
            "UNION ALL "
            "SELECT MAX(posted_at) FROM daily_jobs WHERE guild_id = ? "
            "UNION ALL "
            "SELECT MAX(claimed_at) FROM daily_job_progress WHERE guild_id = ? AND claimed_at IS NOT NULL",
            (guild_id, guild_id, guild_id),
        ).fetchall()
        stamps = [_parse_ts(r["ts"]) for r in rows if r["ts"]]
        stamps = [s for s in stamps if s]
        return max(stamps) if stamps else None

    servers = {}
    for cfg in server_configs:
        gid = cfg["guild_id"]
        drills = [d for d in all_drills if d["guild_id"] == gid]
        jobs = [j for j in all_jobs if j["guild_id"] == gid]
        balances = guild_balances(gid)
        approx_members = len(balances)
        circulating = sum(b["balance"] for b in balances)
        minted = cfg["currency_minted_total"]
        burned = cfg["currency_burned_total"]
        burn_ratio = (burned / minted) if minted > 0 else 0.0
        invested = sum(cfg[f"{m}_fees_collected"] for m in MACHINES)
        slot_level = mining_slot_level(invested)
        last_activity = guild_last_activity(gid)
        # Clamped at 0: a timestamp written by the bot process a moment ago
        # can read as "in the future" relative to this process's own clock by
        # a few seconds without the two machines' clocks actually disagreeing.
        quiet_days = max(0, (now - last_activity).days) if last_activity else None
        oldest_hours = 0.0
        for j in jobs:
            ts = _parse_ts(j["queued_at"])
            if ts:
                oldest_hours = max(oldest_hours, (now - ts).total_seconds() / 3600)
        pool_remaining = cfg["mining_pool_remaining"]
        pool_pct = round(pool_remaining / MINING_POOL_BAG_SIZE * 100)

        machines = []
        for m in MACHINES:
            banked = cfg[f"{m}_fees_collected"]
            level = _level_for(banked)
            nxt = upgrade_threshold(level + 1)
            machines.append({
                "key": m,
                "label": MACHINE_LABEL[m],
                "level": level,
                "banked": _m(banked),
                "pct": min(100, round(banked / nxt * 100)) if nxt else 100,
                "next": f"{_m(min(banked, nxt))} / {_m(nxt)}",
                "fee": _m(cfg[f"{m}_fee"]),
            })

        pool_comp = conn.execute(
            "SELECT material_id, quantity FROM server_mining_pool "
            "WHERE guild_id = ? ORDER BY quantity DESC",
            (gid,),
        ).fetchall()
        pool_comp_rows = [
            {"name": _material_label(r["material_id"]), "qty": _n(r["quantity"])}
            for r in pool_comp
        ]

        stock_rows = []
        for material_id in TRADEABLE_ORDER:
            if material_id in GEM_MATERIAL_IDS:
                continue
            have = conn.execute(
                "SELECT quantity FROM server_material_storage WHERE guild_id = ? AND material_id = ?",
                (gid, material_id),
            ).fetchone()
            have_qty = have["quantity"] if have else 0
            target = target_stock(approx_members, material_id)
            stock_rows.append({
                "name": _material_label(material_id),
                "pct": min(100, round(have_qty / target * 100)) if target else 100,
                "label": f"{_n(have_qty)} / {_n(target)}",
            })

        board_row = conn.execute(
            "SELECT * FROM daily_jobs WHERE guild_id = ? AND job_date = ?",
            (gid, today),
        ).fetchone()
        board = None
        if board_row:
            agg = conn.execute(
                "SELECT COALESCE(SUM(claims_paid),0) AS completions, "
                "COUNT(DISTINCT CASE WHEN sold > 0 OR claims_paid > 0 THEN user_id END) AS participants "
                "FROM daily_job_progress WHERE guild_id = ? AND job_date = ?",
                (gid, today),
            ).fetchone()
            board = {
                "material": _material_label(board_row["material_id"]),
                "quantity": board_row["quantity"],
                "reward": board_row["reward"],
                "completions": agg["completions"],
                "participants": agg["participants"],
            }

        job_rows = []
        for j in jobs:
            target_label = _job_target_label(conn, j)
            ts = _parse_ts(j["queued_at"])
            age_hours = (now - ts).total_seconds() / 3600 if ts else 0
            job_rows.append({
                "machine": MACHINE_LABEL[j["job_type"]],
                "target": target_label,
                "owner": name_for_user(j["user_id"]),
                "age": f"{int(age_hours // 24)}d old" if age_hours >= 24 else f"{int(age_hours)}h old",
                "ageColor": "var(--accent-blue)" if age_hours >= stalled_days * 24 else "var(--text-subtle)",
            })

        members2 = []
        for i, b in enumerate(balances):
            own = [d for d in drills if d["owner_id"] == b["user_id"]]
            members2.append({
                "rank": i + 1,
                "user_id": str(b["user_id"]),
                "name": name_for_user(b["user_id"]),
                "balance": _m(b["balance"]), "balance_raw": b["balance"],
                "share": _pct(b["balance"] / circulating) if circulating else "—",
                "drills": len(own),
                "stored": _n(sum(d["stored_amount"] for d in own)),
            })

        servers[str(gid)] = {
            "guild_id": str(gid),
            "name": directory.guild_name(gid),
            "present": bool(cfg["bot_present"]),
            "currency_name": cfg["currency_name"],
            "currency_emoji": cfg["currency_emoji"],
            "currencyChip": f"{cfg['currency_emoji']} {cfg['currency_name']}" if cfg["currency_name"] else "No currency set",
            "currencyName": f"{cfg['currency_name']} {cfg['currency_emoji']}" if cfg["currency_name"] else "not set",
            "idLine": f"guild {gid} · {'bot present' if cfg['bot_present'] else 'bot removed, row retained'}"
                      + (f" · quiet {quiet_days}d" if quiet_days is not None else " · no recorded activity"),
            "approx_members": approx_members,
            "members": f"~{_n(approx_members)} (approx., from balance holders)",
            "players": approx_members,
            "drills_placed": len(drills),
            "drills_full": sum(1 for d in drills if d["is_full"]),
            "drills_stored": sum(d["stored_amount"] for d in drills),
            "minted": _m(minted), "minted_raw": minted,
            "burned": _m(burned), "burned_raw": burned,
            "circulating": _m(circulating), "circulating_raw": circulating,
            "reconcile": f"minted − burned = {_m(minted - burned)}",
            "burnPct": _pct(burn_ratio), "burn_ratio": burn_ratio,
            "burnNote": f"Below your {burn_floor:.0f}% floor" if burn_ratio * 100 < burn_floor else "Inside the healthy band",
            "invested": _m(invested), "invested_raw": invested,
            "slots": mining_slots(slot_level), "slotLevel": slot_level,
            "nextSlotThreshold": mining_slot_threshold(slot_level + 1),
            "slotPct": min(100, round(invested / mining_slot_threshold(slot_level + 1) * 100)),
            "slotNote": f"{_m(min(invested, mining_slot_threshold(slot_level + 1)))} / "
                        f"{_m(mining_slot_threshold(slot_level + 1))} lifetime fees toward slot {mining_slots(slot_level) + 1}",
            "machines": machines,
            "poolLabel": f"{_n(pool_remaining)} / {_n(MINING_POOL_BAG_SIZE)}",
            "poolPct": pool_pct,
            "poolComp": pool_comp_rows,
            "stock": stock_rows,
            "channel": f"locked to {directory.channel_name(cfg['bot_channel_id'])}" if cfg["bot_channel_id"] else "answers anywhere",
            "replies": "public" if cfg["public_messages"] else "ephemeral",
            "prompt": "sent" if cfg["setup_prompt_sent"] else "pending",
            "quiet_days": quiet_days,
            "jobs": job_rows,
            "noJobs": len(job_rows) == 0,
            "queueSummary": f"{len(job_rows)} jobs · {_n(sum(j['quantity'] for j in jobs))} outstanding",
            "board": board,
            "members2": members2,
            "oldest_hours": oldest_hours,
            "pool_remaining_raw": pool_remaining,
            "fees_collected": {m: cfg[f"{m}_fees_collected"] for m in MACHINES},
        }

    active_ids = [gid for gid, s in servers.items() if s["present"]]
    visible_ids = [gid for gid, s in servers.items() if s["present"] or not hide_departed]

    # ---- alerts --------------------------------------------------------
    alerts = []
    for gid in active_ids:
        s = servers[gid]
        if s["quiet_days"] is not None and s["quiet_days"] >= dormant_days:
            alerts.append({
                "kicker": "Dormant", "accent": "var(--accent-blue)",
                "title": f"{s['name']} has been quiet for {s['quiet_days']} days",
                "detail": f"No production job, daily job post, or job-board claim recorded since. {s['players']} players have ever traded here.",
                "action": "Open server", "view": "servers", "guild_id": gid,
            })
        if s["poolPct"] == 0:
            alerts.append({
                "kicker": "Pool empty", "accent": "var(--accent-blue)",
                "title": f"{s['name']}'s mining bag is exhausted",
                "detail": "Refills on the next draw — drills there are idle until it does.",
                "action": "Open server", "view": "servers", "guild_id": gid,
            })
        if s["oldest_hours"] >= stalled_days * 24:
            alerts.append({
                "kicker": "Queue stalled", "accent": "var(--accent-blue)",
                "title": f"{s['name']} has a job {int(s['oldest_hours'] // 24)} days old",
                "detail": f"{len(s['jobs'])} jobs outstanding on this server.",
                "action": "Open server", "view": "servers", "guild_id": gid,
            })
        if s["burn_ratio"] * 100 < burn_floor:
            alerts.append({
                "kicker": "Minting unchecked", "accent": "var(--accent-blue)",
                "title": f"{s['name']} has burned only {_pct(s['burn_ratio'])} of what it minted",
                "detail": f"Below the {burn_floor:.0f}% floor. Nothing is pulling currency back out of circulation here.",
                "action": "Open server", "view": "servers", "guild_id": gid,
            })
    full_total = sum(1 for d in all_drills if d["is_full"])
    uncollected_total = sum(d["stored_amount"] for d in all_drills)
    if full_total > 0:
        alerts.append({
            "kicker": "Drills stopped", "accent": "var(--accent-green)",
            "title": f"{full_total} drills are full and have stopped mining",
            "detail": f"{_n(uncollected_total)} raw materials are sitting uncollected across every server.",
            "action": "See players", "view": "players", "guild_id": None,
        })

    # ---- machine rollup --------------------------------------------------
    machine_rows = []
    for m in MACHINES:
        levels = sorted(_level_for(servers[gid]["fees_collected"][m]) for gid in active_ids)
        js = [j for j in all_jobs if j["job_type"] == m]
        median_level = levels[len(levels) // 2] if levels else 0
        machine_rows.append({
            "label": MACHINE_LABEL[m],
            "median": median_level,
            "max": max(levels) if levels else 0,
            "past": f"{sum(1 for l in levels if l > 1)} of {len(active_ids)}",
            "jobs": len(js),
            "items": f"{_n(sum(j['quantity'] for j in js))} {MACHINE_UNIT[m]}",
            "fee": _m(MACHINE_DEFAULT_FEE[m]),
        })

    # ---- pool / wealth rollups --------------------------------------------
    pool_rows = [
        {"name": servers[gid]["name"], "pct": servers[gid]["poolPct"], "label": servers[gid]["poolLabel"]}
        for gid in active_ids
    ]
    ratios = sorted(servers[gid]["burn_ratio"] for gid in active_ids)
    pool_pcts = sorted(servers[gid]["poolPct"] for gid in active_ids)
    median = lambda arr: arr[len(arr) // 2] if arr else 0

    wealth = []
    for gid in visible_ids:
        s = servers[gid]
        circ = s["circulating_raw"]
        for m in s["members2"]:
            wealth.append({
                "user_id": m["user_id"], "player": m["name"], "server": s["name"],
                "guild_id": gid, "balance_raw": m["balance_raw"],
                "share": (m["balance_raw"] / circ) if circ else 0,
            })
    wealth.sort(key=lambda w: -w["share"])
    wealth_rows = [
        {"rank": i + 1, "player": w["player"], "user_id": w["user_id"], "server": w["server"],
         "balance": _m(w["balance_raw"]), "share": _pct(w["share"]), "guild_id": w["guild_id"]}
        for i, w in enumerate(wealth[:10])
    ]

    placed_total = sum(1 for d in all_drills if d["guild_id"] is not None)
    unplaced_total = len(all_drills) - placed_total
    gem_drills = sum(1 for d in all_drills if d["drill_type"] in GEM_DRILL_TYPES)
    dragoncoin_total = sum(u["dragoncoin"] for u in all_users)

    overview = {
        "kServers": len(active_ids),
        "kServersSub": f"{len(servers) - len(active_ids)} removed, rows kept so balances survive a re-invite",
        "kPlayers": len(all_users),
        "kPlayersSub": f"across {len(active_ids)} separate economies",
        "kDrills": placed_total,
        "kDrillsSub": f"{unplaced_total} more unplaced, in inventories",
        "kJobs": len(all_jobs),
        "kJobsSub": f"{_n(sum(j['quantity'] for j in all_jobs))} items and batches queued",
        "alerts": alerts, "noAlerts": len(alerts) == 0,
        "serverRows": [
            {
                "guild_id": gid, "name": servers[gid]["name"],
                "currency": servers[gid]["currency_name"] or "no currency set",
                "players": servers[gid]["players"], "drills": servers[gid]["drills_placed"],
                "pool": _n(servers[gid]["pool_remaining_raw"]),
                "poolColor": "var(--accent-blue)" if servers[gid]["poolPct"] == 0 else "var(--text-muted)",
                "invested": servers[gid]["invested"], "slots": servers[gid]["slots"],
                "minted": servers[gid]["minted"], "burned": servers[gid]["burned"],
                "circulating": servers[gid]["circulating"], "burnPct": servers[gid]["burnPct"],
                "burnColor": "var(--accent-blue)" if servers[gid]["burn_ratio"] * 100 < burn_floor else "var(--text-muted)",
            }
            for gid in visible_ids
        ],
        "medianBurn": _pct(median(ratios)),
        "inflatingCount": sum(1 for gid in active_ids if servers[gid]["burn_ratio"] * 100 < burn_floor),
        "inflatingSub": f"Servers burning less than {burn_floor:.0f}% of what they mint. Fees and market resales are the only sinks.",
        "dragoncoin": _m(dragoncoin_total),
        "machineRows": machine_rows,
        "uncollected": _n(uncollected_total), "fullDrills": full_total,
        "medianPool": f"{median(pool_pcts)}%", "gemCount": gem_drills,
        "gemSub": "Ruby, obsidian and diamond drills owned. Diamond drops at 1 in a million.",
        "poolRows": pool_rows,
        "wealthRows": wealth_rows,
    }

    server_rail = [
        {
            "guild_id": gid, "name": servers[gid]["name"],
            "meta": f"{servers[gid]['players']} players · {servers[gid]['drills_placed']} drills"
                    + ("" if servers[gid]["present"] else " · removed"),
            "present": servers[gid]["present"],
        }
        for gid in visible_ids
    ]

    # ---- players ------------------------------------------------------
    players = {}
    player_rail = []
    for u in all_users:
        uid = u["user_id"]
        focus_row = conn.execute(
            "SELECT focus_id FROM user_mining_focus WHERE user_id = ?", (uid,)
        ).fetchone()
        eff_row = conn.execute(
            "SELECT efficiency_id FROM user_mining_efficiency WHERE user_id = ?", (uid,)
        ).fetchone()
        focus = MINING_FOCUSES.get(focus_row["focus_id"], {}).get("name", focus_row["focus_id"]) if focus_row else None
        efficiency = MINING_EFFICIENCIES.get(eff_row["efficiency_id"], {}).get("name", eff_row["efficiency_id"]) if eff_row else None
        gem_rows = conn.execute(
            "SELECT material_id, quantity FROM user_materials WHERE user_id = ? AND material_id IN (?,?,?) AND quantity > 0",
            (uid, *GEM_MATERIAL_IDS),
        ).fetchall()
        gems = ", ".join(f"{_material_label(r['material_id'])} ×{r['quantity']}" for r in gem_rows) or "—"

        my_drills = [d for d in all_drills if d["owner_id"] == uid]
        my_balances = []
        for gid, s in servers.items():
            gid_int = int(gid)
            row = conn.execute(
                "SELECT balance FROM server_currency_balances WHERE guild_id = ? AND user_id = ?",
                (gid_int, uid),
            ).fetchone()
            if row is None:
                continue
            circ = s["circulating_raw"]
            my_balances.append({
                "server": s["name"], "guild_id": gid,
                "amount": _m(row["balance"]) + (f" {s['currency_name']}" if s["currency_name"] else ""),
                "share": _pct(row["balance"] / circ) if circ else "—",
            })

        inv_rows = conn.execute(
            "SELECT material_id, quantity FROM user_materials WHERE user_id = ? AND quantity > 0 "
            "ORDER BY quantity DESC LIMIT 6",
            (uid,),
        ).fetchall()
        inventory = [{"name": _material_label(r["material_id"]), "qty": _n(r["quantity"])} for r in inv_rows]

        my_jobs = []
        for j in all_jobs:
            if j["user_id"] != uid:
                continue
            ts = _parse_ts(j["queued_at"])
            age_hours = (now - ts).total_seconds() / 3600 if ts else 0
            my_jobs.append({
                "machine": MACHINE_LABEL[j["job_type"]],
                "target": _job_target_label(conn, j),
                "server": directory.guild_name(j["guild_id"]) if str(j["guild_id"]) not in servers else servers[str(j["guild_id"])]["name"],
                "age": f"{int(age_hours // 24)}d" if age_hours >= 24 else f"{int(age_hours)}h",
                "ageColor": "var(--accent-blue)" if age_hours >= stalled_days * 24 else "var(--text-subtle)",
            })

        display_name = name_for_user(uid)
        drills_placed = [d for d in my_drills if d["guild_id"] is not None]
        players[str(uid)] = {
            "user_id": str(uid), "name": display_name,
            "idLine": f"user {'•••••' + str(uid)[-4:] if anonymize else uid} · first seen {u['created_at']}",
            "unlocks": [
                {"label": f"Focus · {focus}" if focus else "Focus · locked"},
                {"label": f"Efficiency · {efficiency}" if efficiency else "Efficiency · locked"},
                {"label": f"Gems · {gems}"},
            ],
            "serverCount": len(my_balances),
            "drillCount": len(my_drills),
            "drillSub": f"{len(drills_placed)} placed, {len(my_drills) - len(drills_placed)} in inventory",
            "stored": _n(sum(d["stored_amount"] for d in my_drills)),
            "storedSub": f"{sum(1 for d in my_drills if d['is_full'])} drills full and stopped",
            "dragoncoin": _m(u["dragoncoin"]),
            "balances": my_balances,
            "inventory": inventory,
            "drills": [
                {
                    "id": f"#{d['drill_id']}",
                    "type": get_material_info(d["drill_type"])["name"] if get_material_info(d["drill_type"]) else d["drill_type"],
                    "level": d["level"],
                    "container": (get_material_info(d["container_type"])["name"] if d["container_type"] and get_material_info(d["container_type"]) else "—"),
                    "where": servers[str(d["guild_id"])]["name"] if d["guild_id"] and str(d["guild_id"]) in servers else "inventory",
                    "holding": (f"{_n(d['stored_amount'])} / {_n(effective_capacity(d['container_type']))}" + (" · full" if d["is_full"] else "")) if d["guild_id"] else "—",
                    "holdColor": "var(--accent-blue)" if d["is_full"] else "var(--text-muted)",
                }
                for d in my_drills
            ],
            "jobs": my_jobs, "noJobs": len(my_jobs) == 0,
        }
        player_rail.append({
            "user_id": str(uid), "name": display_name,
            "meta": f"{len(my_drills)} drills · joined {u['created_at']}",
        })

    return {
        "meta": {
            "captured_at": now.isoformat(),
            "db_path": config.DATABASE_PATH,
            "version": config.VERSION,
            "table_count": len(_SCHEMA_TABLES),
            "row_count": row_total,
            "settings": {
                "anonymize": anonymize, "hideDeparted": hide_departed,
                "dormantDays": dormant_days, "burnFloor": burn_floor, "stalledDays": stalled_days,
            },
        },
        "overview": overview,
        "serverRail": server_rail,
        "servers": servers,
        "playerRail": player_rail,
        "players": players,
    }
