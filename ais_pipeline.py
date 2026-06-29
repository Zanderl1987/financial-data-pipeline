#!/usr/bin/env python3
"""
AIS Vessel Tracking Pipeline — real-time ship positions across key trade chokepoints.

Uses AISStream.io WebSocket API (free tier, up to 10 bounding boxes, all vessel types).
Register for a free key at: https://aisstream.io/

What this captures:
  Vessel positions in 10 economically significant maritime zones, filtered to
  cargo ships (type 70-79) and tankers (type 80-89). Runs for a configurable
  collection window, then writes a snapshot to Parquet.

Financial signal value:
  - VLCC/ULCC tankers anchored off coasts = floating storage build → bearish oil
  - Container ship queues at LA/Long Beach → supply-chain inflation leading indicator
  - Tanker flow through Strait of Hormuz → Middle East supply disruption proxy
  - LNG carrier density in Gulf of Mexico → US nat gas export signal
  - Dry bulk slowdown in Malacca Strait → Asia iron ore/coal demand softening
  - High navigational status "4" (moored) at major ports = port congestion

Outputs:
  storage/raw/ais/positions/year=YYYY/month=MM/ais_positions_{mode}_{YYYYMMDD_HHMM}.parquet
  storage/raw/ais/zone_summary/year=YYYY/month=MM/ais_zone_summary_{mode}_{YYYYMMDD_HHMM}.parquet
  CATALOG tables: ais_positions, ais_zone_summary

Usage:
  python ais_pipeline.py                    # 10-minute snapshot (default)
  python ais_pipeline.py --minutes 30       # extended collection window
  python ais_pipeline.py --all-types        # include all ship types, not just cargo+tanker
"""

import argparse
import asyncio
import datetime
import json
import os

import pandas as pd
import websockets
from dotenv import load_dotenv

from storage_utils import write_partitioned

load_dotenv()

AISSTREAM_API_KEY = os.environ.get("AISSTREAM_API_KEY", "")
WS_URL = "wss://stream.aisstream.io/v0/stream"

POSITIONS_DIR    = os.path.join("storage", "raw", "ais", "positions")
ZONE_SUMMARY_DIR = os.path.join("storage", "raw", "ais", "zone_summary")

DEFAULT_MINUTES  = 10

# AIS ship type ranges for cargo and tankers
CARGO_TYPES  = set(range(70, 80))   # General cargo, container, bulk carrier
TANKER_TYPES = set(range(80, 90))   # Oil, chemical, LNG/LPG tankers
CARGO_AND_TANKER = CARGO_TYPES | TANKER_TYPES

# Navigational status codes (field: NavigationalStatus)
NAV_STATUS = {
    0: "under_way_engine",
    1: "at_anchor",
    2: "not_under_command",
    3: "restricted_maneuverability",
    4: "constrained_by_draught",
    5: "moored",
    6: "aground",
    7: "engaged_in_fishing",
    8: "under_way_sailing",
    15: "not_defined",
}

# Ship type buckets for the summary table
def _type_label(t: int | None) -> str:
    if t is None:
        return "unknown"
    if t in CARGO_TYPES:
        return "cargo"
    if t in TANKER_TYPES:
        return "tanker"
    if 60 <= t <= 69:
        return "passenger"
    if t == 30:
        return "fishing"
    return "other"


# ── Geographic zones ───────────────────────────────────────────────────────────
# Format: [[lat_min, lon_min], [lat_max, lon_max]]
ZONES: list[dict] = [
    # ── Oil supply chokepoints ───────────────────────────────────────────────
    {
        "name":    "Strait of Hormuz",
        "signal":  "oil_supply",
        "bbox":    [[25.5, 55.5], [27.0, 57.5]],
    },
    {
        "name":    "Suez Canal",
        "signal":  "oil_supply",
        "bbox":    [[29.5, 32.2], [31.5, 33.0]],
    },
    {
        "name":    "Strait of Malacca",
        "signal":  "asia_trade",
        "bbox":    [[1.0, 99.0], [5.5, 104.5]],
    },
    # ── US energy hubs ───────────────────────────────────────────────────────
    {
        "name":    "Gulf of Mexico (offshore)",
        "signal":  "us_oil_lng",
        "bbox":    [[25.0, -97.0], [30.0, -87.0]],
    },
    {
        "name":    "Port of Houston / Galveston",
        "signal":  "us_oil_lng",
        "bbox":    [[29.3, -95.3], [29.8, -94.5]],
    },
    # ── US container / retail supply chain ───────────────────────────────────
    {
        "name":    "Port of LA / Long Beach",
        "signal":  "us_retail_supply_chain",
        "bbox":    [[33.6, -118.4], [33.9, -118.0]],
    },
    {
        "name":    "Port of NY / NJ",
        "signal":  "us_retail_supply_chain",
        "bbox":    [[40.4, -74.2], [40.7, -73.8]],
    },
    {
        "name":    "Port of Savannah",
        "signal":  "us_retail_supply_chain",
        "bbox":    [[31.9, -81.2], [32.2, -80.8]],
    },
    # ── European commodities ─────────────────────────────────────────────────
    {
        "name":    "Rotterdam approaches",
        "signal":  "europe_commodities",
        "bbox":    [[51.7, 3.5], [52.2, 4.5]],
    },
    # ── North Sea oil ────────────────────────────────────────────────────────
    {
        "name":    "North Sea (oil fields)",
        "signal":  "north_sea_oil",
        "bbox":    [[55.0, -1.0], [61.0, 6.0]],
    },
]


# ── WebSocket collector ────────────────────────────────────────────────────────

class VesselCollector:
    """Accumulates position reports and static data during the collection window."""

    def __init__(self) -> None:
        self.positions: list[dict]     = []
        self.vessel_meta: dict[int, dict] = {}   # MMSI -> {name, ship_type, imo}

    def handle_message(self, raw: str, zone_name: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("MessageType", "")
        meta     = msg.get("MetaData", {})
        mmsi     = meta.get("MMSI") or meta.get("mmsi")

        if msg_type == "ShipStaticData":
            inner = msg.get("Message", {}).get("ShipStaticData", {})
            if mmsi:
                self.vessel_meta[mmsi] = {
                    "ship_name": inner.get("Name", meta.get("ShipName", "")).strip(),
                    "ship_type": inner.get("Type"),
                    "imo":       inner.get("ImoNumber"),
                    "call_sign": inner.get("CallSign", "").strip(),
                    "destination": inner.get("Destination", "").strip(),
                }

        elif msg_type in ("PositionReport", "ExtendedClassBPositionReport"):
            inner = msg.get("Message", {}).get(msg_type, {})
            if not inner:
                return
            lat = inner.get("Latitude") or meta.get("latitude")
            lon = inner.get("Longitude") or meta.get("longitude")
            if lat is None or lon is None:
                return

            self.positions.append({
                "mmsi":               mmsi,
                "zone":               zone_name,
                "ship_name":          meta.get("ShipName", "").strip(),
                "latitude":           lat,
                "longitude":          lon,
                "speed_over_ground":  inner.get("Sog"),
                "course_over_ground": inner.get("Cog"),
                "true_heading":       inner.get("TrueHeading"),
                "nav_status_code":    inner.get("NavigationalStatus"),
                "nav_status":         NAV_STATUS.get(inner.get("NavigationalStatus"), "unknown"),
                "timestamp_utc":      meta.get("time_utc", ""),
            })

    def to_dataframes(self, ship_type_filter: set | None,
                      fetched_at: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        if not self.positions:
            return pd.DataFrame(), pd.DataFrame()

        df = pd.DataFrame(self.positions)
        df["fetched_at"] = fetched_at

        # Enrich positions with static metadata where available
        def _enrich(row):
            m = self.vessel_meta.get(row["mmsi"], {})
            row["ship_type"]    = m.get("ship_type")
            row["ship_type_label"] = _type_label(m.get("ship_type"))
            row["imo"]          = m.get("imo")
            row["call_sign"]    = m.get("call_sign", "")
            row["destination"]  = m.get("destination", "")
            if not row["ship_name"] and m.get("ship_name"):
                row["ship_name"] = m["ship_name"]
            return row

        df = df.apply(_enrich, axis=1)

        # Apply ship type filter
        if ship_type_filter is not None:
            known_type_mask = df["ship_type"].notna()
            in_filter_mask  = df["ship_type"].apply(
                lambda t: t in ship_type_filter if pd.notna(t) else False
            )
            # Keep vessels where type matches OR type is unknown (may be cargo/tanker, just not yet seen static msg)
            df = df[in_filter_mask | ~known_type_mask]

        df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], errors="coerce", utc=True)
        df = df.drop_duplicates(subset=["mmsi", "zone", "timestamp_utc"])

        # ── Zone summary ──────────────────────────────────────────────────────
        summary_rows = []
        for zone in df["zone"].unique():
            zdf = df[df["zone"] == zone]
            for type_label in zdf["ship_type_label"].unique():
                tdf = zdf[zdf["ship_type_label"] == type_label]
                sog = tdf["speed_over_ground"].dropna()
                nav = tdf["nav_status_code"].dropna()
                summary_rows.append({
                    "zone":              zone,
                    "ship_type_label":   type_label,
                    "vessel_count":      tdf["mmsi"].nunique(),
                    "position_reports":  len(tdf),
                    "avg_speed_knots":   round(sog.mean(), 2) if len(sog) else None,
                    "pct_anchored":      round(
                        100 * (nav.isin([1, 5])).sum() / len(nav), 1
                    ) if len(nav) else None,
                    "pct_underway":      round(
                        100 * (nav == 0).sum() / len(nav), 1
                    ) if len(nav) else None,
                    "fetched_at":        fetched_at,
                })

        df_summary = pd.DataFrame(summary_rows) if summary_rows else pd.DataFrame()
        return df, df_summary


# ── Async collection ───────────────────────────────────────────────────────────

async def _collect(api_key: str, zones: list[dict],
                   duration_seconds: int, all_types: bool) -> VesselCollector:
    collector = VesselCollector()

    # Build subscription — one bounding box per zone; tag each with zone name
    # AISStream allows up to 10 bounding boxes per subscription
    bboxes     = [z["bbox"] for z in zones]
    zone_names = [z["name"] for z in zones]

    subscribe_msg = {
        "APIKey":          api_key,
        "BoundingBoxes":   bboxes,
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    }

    print(f"  Connecting to AISStream WebSocket...")
    print(f"  Zones: {len(zones)} | Duration: {duration_seconds}s")

    deadline = asyncio.get_event_loop().time() + duration_seconds
    msg_count = 0

    try:
        async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=30) as ws:
            await ws.send(json.dumps(subscribe_msg))
            print("  Subscribed. Collecting vessel data...\n")

            while asyncio.get_event_loop().time() < deadline:
                try:
                    remaining = deadline - asyncio.get_event_loop().time()
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 30))
                    msg_count += 1

                    # Determine which zone this position falls in based on lat/lon
                    try:
                        parsed = json.loads(raw)
                        meta   = parsed.get("MetaData", {})
                        lat    = meta.get("latitude")
                        lon    = meta.get("longitude")
                        zone_name = "unknown"
                        if lat is not None and lon is not None:
                            for z in zones:
                                bb = z["bbox"]
                                if (bb[0][0] <= lat <= bb[1][0] and
                                        bb[0][1] <= lon <= bb[1][1]):
                                    zone_name = z["name"]
                                    break
                        collector.handle_message(raw, zone_name)
                    except Exception:
                        pass

                    if msg_count % 500 == 0:
                        elapsed = duration_seconds - (deadline - asyncio.get_event_loop().time())
                        print(f"    {msg_count:,} messages | {len(collector.positions):,} positions "
                              f"| {int(elapsed)}s elapsed")

                except asyncio.TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed:
                    print("  WebSocket closed by server.")
                    break

    except Exception as exc:
        print(f"  Connection error: {exc}")

    print(f"\n  Collection complete: {msg_count:,} messages | "
          f"{len(collector.positions):,} position reports | "
          f"{len(collector.vessel_meta):,} vessels with static data")
    return collector


# ── Main ───────────────────────────────────────────────────────────────────────

def main(minutes: int = DEFAULT_MINUTES, all_types: bool = False) -> None:
    if not AISSTREAM_API_KEY:
        print("ERROR: AISSTREAM_API_KEY must be set in .env")
        print("  Register free at https://aisstream.io/")
        return

    os.makedirs(POSITIONS_DIR,    exist_ok=True)
    os.makedirs(ZONE_SUMMARY_DIR, exist_ok=True)

    now        = datetime.datetime.utcnow()
    timestamp  = now.strftime("%Y%m%d_%H%M")
    fetched_at = now.isoformat()
    mode       = "extended" if minutes > DEFAULT_MINUTES else "snapshot"

    ship_type_filter = None if all_types else CARGO_AND_TANKER
    filter_desc = "all types" if all_types else "cargo + tanker only"

    print(f"AIS Vessel Tracking Pipeline")
    print(f"Mode: {mode}  |  Window: {minutes} min  |  Filter: {filter_desc}")
    print(f"Zones: {len(ZONES)}")
    print()

    collector = asyncio.run(
        _collect(AISSTREAM_API_KEY, ZONES, minutes * 60, all_types)
    )

    df_positions, df_summary = collector.to_dataframes(ship_type_filter, fetched_at)

    if df_positions.empty:
        print("\nNo position data collected. Check API key and network.")
        return

    path_pos = write_partitioned(
        df_positions, POSITIONS_DIR, f"ais_positions_{mode}_{timestamp}.parquet"
    )
    print(f"\n[+] {path_pos}")
    print(f"    {len(df_positions):,} position records | "
          f"{df_positions['mmsi'].nunique()} unique vessels | "
          f"{df_positions['zone'].nunique()} zones")

    if not df_summary.empty:
        path_sum = write_partitioned(
            df_summary, ZONE_SUMMARY_DIR, f"ais_zone_summary_{mode}_{timestamp}.parquet"
        )
        print(f"\n[+] {path_sum}")
        print(f"    {len(df_summary):,} zone-type rows\n")

        # Print a quick signal table
        print("  Zone Signal Snapshot:")
        print(f"  {'Zone':<35} {'Type':<10} {'Vessels':>7}  {'Avg kts':>7}  {'% Anchored':>10}")
        print("  " + "-" * 75)
        for _, row in df_summary.sort_values("vessel_count", ascending=False).head(20).iterrows():
            anchored = f"{row['pct_anchored']:.0f}%" if pd.notna(row.get("pct_anchored")) else "n/a"
            spd      = f"{row['avg_speed_knots']:.1f}" if pd.notna(row.get("avg_speed_knots")) else "n/a"
            print(f"  {row['zone']:<35} {row['ship_type_label']:<10} "
                  f"{int(row['vessel_count']):>7}  {spd:>7}  {anchored:>10}")

    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="AIS vessel tracking pipeline — real-time positions across 10 trade zones"
    )
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES,
                        help=f"Collection window in minutes (default: {DEFAULT_MINUTES})")
    parser.add_argument("--all-types", action="store_true",
                        help="Collect all ship types, not just cargo (70-79) and tankers (80-89)")
    args = parser.parse_args()
    main(minutes=args.minutes, all_types=args.all_types)
