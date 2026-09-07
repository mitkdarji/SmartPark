"""Command-line evaluation harness.

    python -m scripts.benchmark --trials 5 --vehicles 800
    python -m scripts.benchmark --sweep          # degrade the occupancy map
    python -m scripts.benchmark --anpr           # recognition accuracy curve
    python -m scripts.benchmark --all --json results.json

Prints the same numbers the /benchmark API and the Evaluation Lab produce,
because all three call the same functions.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.allocation.simulator import compare, sweep_ghost_rates


def print_allocation(report: dict) -> None:
    lot = report["lot"]
    work = report["workload"]
    print(
        f"\nLot: {lot['capacity']} bays in {lot['zones']} zones · "
        f"{work['vehicles']} vehicles over {work['hours']} h · "
        f"{work['trials']} seeds · ghost rate {work['ghost_rate']:.0%}"
    )
    header = (
        f"{'strategy':<11}{'walk':>9}{'eff_walk':>10}{'p90':>8}"
        f"{'misalloc':>10}{'conflict':>10}{'reuse':>9}{'gini':>7}{'score':>8}"
    )
    print(header)
    print("-" * len(header))
    for row in report["results"]:
        print(
            f"{row['strategy']:<11}"
            f"{row['walk_m']:>8.1f}m"
            f"{row['effective_walk_m']:>9.1f}m"
            f"{row['p90_walk_m']:>8.1f}"
            f"{row['misallocation_rate']:>10.3f}"
            f"{row['conflict_rate']:>10.3f}"
            f"{row['reuse_gap_min']:>8.1f}m"
            f"{row['slot_gini']:>7.3f}"
            f"{row['composite_score']:>8.1f}"
        )
    print(f"\nBest under this weighting: {report['winner']}")
    print(
        "Weights: "
        + ", ".join(f"{k} {v:.0%}" for k, v in report["weights"].items())
        + "  (a stated value judgement, not a derived truth)"
    )


def run_anpr(samples: int) -> list[dict]:
    from app.services.anpr.pipeline import anpr
    from app.services.anpr.plate_utils import plate_similarity
    from app.services.anpr.synthetic import synth_capture

    print(f"\nANPR accuracy — {samples} labelled frames per difficulty level")
    print(f"{'difficulty':>11}{'exact':>9}{'near':>9}{'conf':>8}{'ms':>8}")
    print("-" * 45)

    rows = []
    for difficulty in (0.0, 0.15, 0.30, 0.45, 0.60, 0.75):
        exact = near = 0
        confidences: list[float] = []
        latencies: list[float] = []
        for seed in range(samples):
            frame, truth = synth_capture(difficulty=difficulty, seed=seed)
            started = time.perf_counter()
            result = anpr.recognize(frame, persist=False)
            latencies.append((time.perf_counter() - started) * 1000)
            confidences.append(result.confidence)
            if result.plate == truth:
                exact += 1
                near += 1
            elif plate_similarity(result.plate, truth) >= 0.9:
                near += 1

        row = {
            "difficulty": difficulty,
            "exact_match_rate": exact / samples,
            "near_match_rate": near / samples,
            "mean_confidence": sum(confidences) / samples,
            "mean_latency_ms": sum(latencies) / samples,
        }
        rows.append(row)
        print(
            f"{difficulty:>11.2f}{row['exact_match_rate']:>9.1%}"
            f"{row['near_match_rate']:>9.1%}{row['mean_confidence']:>8.2f}"
            f"{row['mean_latency_ms']:>8.1f}"
        )

    print(
        "\nMeasured on frames SmartPark renders itself, so this is an upper bound:\n"
        "the built-in segmentation reader shares a font family with the generator.\n"
        "Install easyocr or set ANTHROPIC_API_KEY for representative real-world numbers."
    )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="SmartPark evaluation harness")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--vehicles", type=int, default=800)
    parser.add_argument("--zones", type=int, default=5)
    parser.add_argument("--slots-per-zone", type=int, default=24)
    parser.add_argument("--hours", type=float, default=14.0)
    parser.add_argument("--ghost-rate", type=float, default=0.0)
    parser.add_argument("--sweep", action="store_true", help="Sweep occupancy-map staleness.")
    parser.add_argument("--anpr", action="store_true", help="Run the recognition accuracy curve.")
    parser.add_argument("--all", action="store_true", help="Run everything.")
    parser.add_argument("--samples", type=int, default=25, help="Frames per ANPR difficulty.")
    parser.add_argument("--json", type=str, help="Write the full results to this path.")
    args = parser.parse_args()

    output: dict = {}

    if not args.anpr or args.all:
        common = {
            "trials": args.trials, "vehicles": args.vehicles, "zones": args.zones,
            "slots_per_zone": args.slots_per_zone, "hours": args.hours,
        }
        print("=" * 78)
        print("  ALLOCATION STRATEGY BENCHMARK — perfect occupancy information")
        print("=" * 78)
        clean = compare(**common, ghost_rate=args.ghost_rate)
        print_allocation(clean)
        output["allocation_clean"] = clean

        if args.sweep or args.all:
            print("\n" + "=" * 78)
            print("  DEGRADED INFORMATION — vehicles parking without being logged")
            print("=" * 78)
            degraded = compare(**common, ghost_rate=0.10)
            print_allocation(degraded)
            output["allocation_degraded"] = degraded

            print("\n" + "=" * 78)
            print("  STALENESS SWEEP — composite score by ghost rate")
            print("=" * 78)
            # Use the full seed count: at two seeds the composite scores bounce
            # around by more than the differences between policies.
            sweep = sweep_ghost_rates(trials=args.trials, vehicles=args.vehicles)
            strategies = [row["strategy"] for row in next(iter(sweep["runs"].values()))]
            header = f"{'ghost':>7}" + "".join(f"{s:>11}" for s in strategies)
            print(header)
            print("-" * len(header))
            for rate, rows in sweep["runs"].items():
                by_name = {row["strategy"]: row for row in rows}
                line = f"{float(rate):>6.0%} "
                line += "".join(f"{by_name[s]['composite_score']:>11.1f}" for s in strategies)
                print(line)
            output["sweep"] = sweep

    if args.anpr or args.all:
        print("\n" + "=" * 78)
        print("  ANPR RECOGNITION ACCURACY")
        print("=" * 78)
        output["anpr"] = run_anpr(args.samples)

    if args.json:
        Path(args.json).write_text(json.dumps(output, indent=2, default=str))
        print(f"\nFull results written to {args.json}")


if __name__ == "__main__":
    main()
