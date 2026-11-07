"""
Railway: a read-only view of what the platform runs on and what it costs.

Uses Railway's public GraphQL API with a project token in RAILWAY_API_TOKEN.
RAILWAY_PROJECT_ID / RAILWAY_ENVIRONMENT_ID are injected by Railway itself in
production; set them by hand for local use. Usage comes back in minutes of the
unit (vCPU-minutes, GB-minutes) except egress, which is plain GB, and is priced
at Railway's list prices below.

Never raises: returns {"configured": False} without a token and
{"configured": True, "error": ...} when Railway can't be reached.
Results are cached for a few minutes so the dashboard stays fast.
"""
from __future__ import annotations

import logging
import os
import time
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://backboard.railway.com/graphql/v2"
PRICE_PER_VCPU_MONTH = 20.0
PRICE_PER_GB_RAM_MONTH = 10.0
PRICE_PER_GB_DISK_MONTH = 0.15
PRICE_PER_GB_EGRESS = 0.05
MINUTES_PER_MONTH = 60 * 24 * 30
MINUTES_PER_DAY = 60 * 24
CACHE_SECONDS = 300
MEASUREMENTS = "[CPU_USAGE, MEMORY_USAGE_GB, NETWORK_TX_GB, DISK_USAGE_GB]"

_cache: dict[str, Any] = {"at": 0.0, "data": None}


def _cost(cpu_min: float, mem_gb_min: float, disk_gb_min: float, egress_gb: float) -> float:
    return round(
        cpu_min / MINUTES_PER_MONTH * PRICE_PER_VCPU_MONTH
        + mem_gb_min / MINUTES_PER_MONTH * PRICE_PER_GB_RAM_MONTH
        + disk_gb_min / MINUTES_PER_MONTH * PRICE_PER_GB_DISK_MONTH
        + egress_gb * PRICE_PER_GB_EGRESS, 4)


def _gql(client: httpx.Client, token: str, query: str, variables: dict | None = None) -> dict:
    r = client.post(API_URL, json={"query": query, "variables": variables or {}},
                    headers={"Project-Access-Token": token, "Content-Type": "application/json", "User-Agent": "nextgenhrms-admin"})
    r.raise_for_status()
    body = r.json()
    if body.get("errors") and not body.get("data"):
        raise RuntimeError(body["errors"][0].get("message", "Railway error"))
    return body.get("data") or {}


def snapshot(force: bool = False) -> dict[str, Any]:
    token = os.getenv("RAILWAY_API_TOKEN", "").strip()
    project_id = os.getenv("RAILWAY_PROJECT_ID", "").strip()
    if not token or not project_id:
        return {"configured": False}
    if not force and _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_ago = now - timedelta(hours=24)
    env_id = os.getenv("RAILWAY_ENVIRONMENT_ID", "").strip() or None
    try:
        with httpx.Client(timeout=20) as c:
            proj = _gql(c, token, """
                query($id: String!) {
                  project(id: $id) {
                    name subscriptionType createdAt
                    services { edges { node { id name
                      deployments(first: 1) { edges { node { status createdAt } } } } } }
                    volumes { edges { node { name volumeInstances { edges { node { currentSizeMB sizeMB serviceId } } } } } }
                  }
                }""", {"id": project_id}).get("project") or {}
            usage = _gql(c, token, f"""
                query($p: String!, $start: DateTime!, $dayAgo: DateTime!, $now: DateTime!) {{
                  month: usage(projectId: $p, startDate: $start, endDate: $now, groupBy: [SERVICE_ID], measurements: {MEASUREMENTS}) {{ measurement value tags {{ serviceId }} }}
                  day: usage(projectId: $p, startDate: $dayAgo, endDate: $now, groupBy: [SERVICE_ID], measurements: [CPU_USAGE, MEMORY_USAGE_GB]) {{ measurement value tags {{ serviceId }} }}
                  estimatedUsage(projectId: $p, measurements: {MEASUREMENTS}) {{ measurement estimatedValue }}
                }}""", {"p": project_id, "start": month_start.isoformat(), "dayAgo": day_ago.isoformat(), "now": now.isoformat()})
            daily = _gql(c, token, """
                query($p: String!, $start: DateTime!) {
                  metrics(projectId: $p, measurements: [CPU_USAGE, MEMORY_USAGE_GB, NETWORK_TX_GB], startDate: $start, sampleRateSeconds: 3600) {
                    measurement values { ts value }
                  }
                }""", {"p": project_id, "start": (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()})
            # Cost per calendar month since the project began (one request, one alias per month)
            created = proj.get("createdAt")
            first = datetime.fromisoformat(created.replace("Z", "+00:00")) if created else month_start
            first = first.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            months: list[tuple[datetime, datetime]] = []
            m = first
            while m <= month_start and len(months) < 24:
                nxt = (m + timedelta(days=32)).replace(day=1)
                months.append((m, min(nxt, now)))
                m = nxt
            parts = [f'm{i}: usage(projectId: "{project_id}", startDate: "{a.isoformat()}", endDate: "{b.isoformat()}", measurements: {MEASUREMENTS}) {{ measurement value }}'
                     for i, (a, b) in enumerate(months)]
            hist = _gql(c, token, "query { " + " ".join(parts) + " }") if parts else {}
    except Exception as exc:  # network, auth, schema change: the dashboard must still render
        logger.warning("Railway snapshot failed: %s", exc)
        return {"configured": True, "error": str(exc)[:200]}

    def group(rows):
        out: dict[str, dict[str, float]] = {}
        for row in rows or []:
            sid = (row.get("tags") or {}).get("serviceId") or "?"
            out.setdefault(sid, {})[row["measurement"]] = float(row.get("value") or 0)
        return out

    month_u, day_u = group(usage.get("month")), group(usage.get("day"))
    minutes_so_far = max(1.0, (now - month_start).total_seconds() / 60)
    services = []
    for e in (proj.get("services") or {}).get("edges", []):
        s = e["node"]
        u, d = month_u.get(s["id"], {}), day_u.get(s["id"], {})
        dep = ((s.get("deployments") or {}).get("edges") or [{}])[0].get("node") or {}
        services.append({
            "name": s["name"],
            "status": dep.get("status"), "deployed_at": dep.get("createdAt"),
            "cpu_vcpu": round(d.get("CPU_USAGE", 0) / MINUTES_PER_DAY, 3),        # average over the last 24 h
            "mem_gb": round(d.get("MEMORY_USAGE_GB", 0) / MINUTES_PER_DAY, 3),
            "disk_gb": round(u.get("DISK_USAGE_GB", 0) / minutes_so_far, 2),
            "egress_gb": round(u.get("NETWORK_TX_GB", 0), 3),
            "month_cost_usd": _cost(u.get("CPU_USAGE", 0), u.get("MEMORY_USAGE_GB", 0), u.get("DISK_USAGE_GB", 0), u.get("NETWORK_TX_GB", 0)),
        })
    services.sort(key=lambda s: -s["month_cost_usd"])

    # Cost split by resource for the month so far
    tot = {m: sum(v.get(m, 0) for v in month_u.values()) for m in ("CPU_USAGE", "MEMORY_USAGE_GB", "DISK_USAGE_GB", "NETWORK_TX_GB")}
    by_resource = [
        {"resource": "Memory", "usd": _cost(0, tot["MEMORY_USAGE_GB"], 0, 0)},
        {"resource": "CPU", "usd": _cost(tot["CPU_USAGE"], 0, 0, 0)},
        {"resource": "Disk", "usd": _cost(0, 0, tot["DISK_USAGE_GB"], 0)},
        {"resource": "Network", "usd": _cost(0, 0, 0, tot["NETWORK_TX_GB"])},
    ]

    # Daily cost series from hourly samples: average vCPU / GB per day × minutes in a day, egress summed
    days: dict[str, dict[str, list[float]]] = {}
    for row in daily.get("metrics") or []:
        for v in row.get("values") or []:
            day = datetime.fromtimestamp(int(v["ts"]), tz=timezone.utc).strftime("%Y-%m-%d")
            days.setdefault(day, {}).setdefault(row["measurement"], []).append(float(v.get("value") or 0))
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
    cost_daily = [{
        "date": day,
        "usd": _cost(avg(d.get("CPU_USAGE", [])) * MINUTES_PER_DAY, avg(d.get("MEMORY_USAGE_GB", [])) * MINUTES_PER_DAY, 0, sum(d.get("NETWORK_TX_GB", []))),
    } for day, d in sorted(days.items())]

    volumes = []
    for e in (proj.get("volumes") or {}).get("edges", []):
        v = e["node"]
        for ie in (v.get("volumeInstances") or {}).get("edges", []):
            inst = ie["node"]
            volumes.append({"name": v["name"], "used_gb": round((inst.get("currentSizeMB") or 0) / 1024, 2), "size_gb": round((inst.get("sizeMB") or 0) / 1024, 1)})

    month_cost = round(sum(s["month_cost_usd"] for s in services), 2)
    monthly = []
    for i, (a, _b) in enumerate(months):
        u = {row["measurement"]: float(row.get("value") or 0) for row in hist.get(f"m{i}") or []}
        monthly.append({"month": a.strftime("%Y-%m"), "usd": round(_cost(u.get("CPU_USAGE", 0), u.get("MEMORY_USAGE_GB", 0), u.get("DISK_USAGE_GB", 0), u.get("NETWORK_TX_GB", 0)), 2)})
    if monthly:
        monthly[-1]["usd"] = month_cost  # same figure as the per-service total
    total_cost = round(sum(x["usd"] for x in monthly), 2)
    last_month = monthly[-2]["usd"] if len(monthly) > 1 else 0.0
    live_days = max(1, (now - first).days + 1)
    est = {row["measurement"]: float(row.get("estimatedValue") or 0) for row in usage.get("estimatedUsage") or []}
    est_cost = _cost(est.get("CPU_USAGE", 0), est.get("MEMORY_USAGE_GB", 0), est.get("DISK_USAGE_GB", 0), est.get("NETWORK_TX_GB", 0))
    # Railway's estimate can lag early in the month; never show less than the straight-line projection
    days_in_month = monthrange(now.year, now.month)[1]
    straight = month_cost / minutes_so_far * days_in_month * MINUTES_PER_DAY
    data = {
        "configured": True,
        "project": proj.get("name"), "plan": proj.get("subscriptionType"),
        "services": services, "volumes": volumes,
        "cost_daily": cost_daily, "by_resource": by_resource,
        "month_cost_usd": month_cost,
        "last_month_usd": last_month,
        "total_cost_usd": total_cost,
        "monthly": monthly,
        "since": first.date().isoformat(), "live_days": live_days,
        "avg_day_usd": round(total_cost / live_days, 3),
        "egress_month_gb": round(tot["NETWORK_TX_GB"], 2),
        "mem_now_gb": round(sum(x["mem_gb"] for x in services), 2),
        "estimated_month_usd": round(max(est_cost, straight), 2),
        "usd_inr": float(os.getenv("USD_INR", "88")),
        "fetched_at": now.isoformat(),
        "environment_id": env_id,
    }
    _cache.update(at=time.time(), data=data)
    return data
