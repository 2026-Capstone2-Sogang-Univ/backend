"""
NYC 택시 parquet → trips_processed.json 변환 스크립트

사용법:
    python scripts/preprocess_trips.py \
        --input  real_taxi_data/od_month=07/consolidated.parquet \
        --net    sumo_service/sumo_configs/NY/manhattan_car_only.net.xml \
        --output sumo_service/sumo_configs/NY/trips_processed.json \
        --start  "2013-07-08 08-00-00" \
        --end    "2013-07-08 09-00-00" \
        --sample 5000

의존성 (서비스 컨테이너 외부에서만 필요):
    pip install pandas pyarrow fastparquet sumolib pyproj rtree
    (pyproj: 좌표 변환 필수 / rtree: 엣지 탐색 가속, 없으면 brute-force로 동작)
"""

import argparse
import os
import json
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime as datetime_type
from pathlib import Path

import pandas as pd
import sumolib

REQUIRED_COLUMNS = [
    "pickup_datetime",
    "pickup_date",
    "pickup_latitude",
    "pickup_longitude",
    "dropoff_latitude",
    "dropoff_longitude",
    "h3_pickup",
]

DATETIME_FORMAT = "%Y-%m-%d %H-%M-%S"
DEFAULT_WORKERS = min(32, (os.cpu_count() or 1) + 4)
thread_local = threading.local()


class ProgressTracker:
    def __init__(self, total_rows: int) -> None:
        self.total_rows = total_rows
        self.total_edges = total_rows * 2
        self.completed_rows = 0
        self.completed_edges = 0
        self.start_time = time.time()
        self.lock = threading.Lock()

    def edge_done(self) -> None:
        with self.lock:
            self.completed_edges += 1

    def row_done(self) -> None:
        with self.lock:
            self.completed_rows += 1

    def snapshot(self) -> tuple[int, int, float]:
        with self.lock:
            return self.completed_rows, self.completed_edges, self.start_time


def load_net(net_path: str) -> sumolib.net.Net:
    print(f"[0/5] 네트워크 로드: {net_path}")
    net = sumolib.net.readNet(net_path, withInternal=False)
    print(f"      완료")
    return net


def validate_geo_projection(net: sumolib.net.Net) -> None:
    try:
        net.convertLonLat2XY(-73.9857, 40.7484)
    except ModuleNotFoundError as exc:
        print("ERROR: 좌표 변환에 필요한 pyproj가 설치되어 있지 않습니다.")
        print("       실행 전 `pip install pyproj` 또는 의존성 재설치를 수행하세요.")
        raise SystemExit(1) from exc
    except Exception:
        # pyproj가 있으면 실제 좌표별 실패는 latlng_to_edge에서 개별 처리한다.
        pass


def latlng_to_edge(
    net: sumolib.net.Net, lat: float, lon: float, radius: float = 200.0
) -> tuple[str | None, str | None]:
    """GPS 좌표(WGS84)를 SUMO 최근접 일반 엣지 ID로 변환. 실패 시 (None, reason)."""
    try:
        x, y = net.convertLonLat2XY(lon, lat)
        candidates = net.getNeighboringEdges(x, y, radius)
        # 내부 교차로 엣지(:로 시작) 제외
        candidates = [(e, d) for e, d in candidates if not e.getID().startswith(":")]
        if not candidates:
            return None, "no_neighboring_edge"
        return min(candidates, key=lambda ed: ed[1])[0].getID(), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def read_trip_parquet(input_path: str) -> pd.DataFrame:
    print(f"[1/5] parquet 로드: {input_path}")
    try:
        df = pd.read_parquet(input_path, columns=REQUIRED_COLUMNS)
        print("      엔진: pyarrow")
    except OSError as exc:
        if "Repetition level histogram size mismatch" not in str(exc):
            raise
        print("      pyarrow 읽기 실패, fastparquet로 재시도합니다.")
        df = pd.read_parquet(input_path, engine="fastparquet", columns=REQUIRED_COLUMNS)
        print("      엔진: fastparquet")

    df["pickup_datetime"] = pd.to_datetime(df["pickup_datetime"], errors="coerce")

    # fastparquet에서 DATE 컬럼이 ns 단위 정수(object)로 들어오는 케이스를 정규화한다.
    pickup_date_raw = df["pickup_date"]
    if pd.api.types.is_numeric_dtype(pickup_date_raw):
        df["pickup_date"] = pd.to_datetime(pickup_date_raw, errors="coerce").dt.date
    else:
        sample = (
            pickup_date_raw.dropna().iloc[0]
            if not pickup_date_raw.dropna().empty
            else None
        )
        if isinstance(sample, int):
            df["pickup_date"] = pd.to_datetime(pickup_date_raw, errors="coerce").dt.date
        else:
            df["pickup_date"] = pd.to_datetime(pickup_date_raw, errors="coerce").dt.date

    before = len(df)
    df = df.dropna(subset=["pickup_datetime", "pickup_date"])
    dropped = before - len(df)
    if dropped:
        print(f"      경고: datetime/date 파싱 실패 {dropped:,}행 제거")

    print(f"      총 {len(df):,}행")
    return df


def get_thread_net(net_path: str) -> sumolib.net.Net:
    net = getattr(thread_local, "net", None)
    if net is None:
        net = sumolib.net.readNet(net_path, withInternal=False)
        thread_local.net = net
    return net


def convert_trip_edges(
    net_path: str, row, tracker: ProgressTracker
) -> tuple[str | None, str | None, str | None, str | None]:
    net = get_thread_net(net_path)
    pickup_edge, pickup_error = latlng_to_edge(
        net, row.pickup_latitude, row.pickup_longitude
    )
    tracker.edge_done()
    dropoff_edge, dropoff_error = latlng_to_edge(
        net, row.dropoff_latitude, row.dropoff_longitude
    )
    tracker.edge_done()
    return pickup_edge, dropoff_edge, pickup_error, dropoff_error


def print_progress(done_rows: int, total_rows: int, done_edges: int, total_edges: int, start_time: float) -> None:
    elapsed = max(time.time() - start_time, 1e-9)
    row_rate = done_rows / elapsed
    edge_rate = done_edges / elapsed
    row_percent = (done_rows / total_rows) * 100 if total_rows else 100.0
    edge_percent = (done_edges / total_edges) * 100 if total_edges else 100.0
    print(
        f"\r      진행률: row {done_rows:,}/{total_rows:,} ({row_percent:5.1f}%) | "
        f"edge {done_edges:,}/{total_edges:,} ({edge_percent:5.1f}%) | "
        f"{row_rate:,.1f} rows/s | {edge_rate:,.1f} edges/s",
        end="",
        flush=True,
    )


def map_edges_parallel(
    net_path: str, df: pd.DataFrame, workers: int
) -> tuple[list[str | None], list[str | None], dict[str, int], list[str]]:
    total = len(df)
    pickup_edges: list[str | None] = []
    dropoff_edges: list[str | None] = []
    error_counts: dict[str, int] = {}
    error_samples: list[str] = []
    rows = df.itertuples(index=False)
    tracker = ProgressTracker(total)
    stop_event = threading.Event()

    def progress_reporter() -> None:
        while not stop_event.wait(0.5):
            done_rows, done_edges, start_time = tracker.snapshot()
            print_progress(
                done_rows, total, done_edges, tracker.total_edges, start_time
            )

    reporter = threading.Thread(target=progress_reporter, daemon=True)
    reporter.start()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for idx, (pickup_edge, dropoff_edge, pickup_error, dropoff_error) in enumerate(
            executor.map(lambda row: convert_trip_edges(net_path, row, tracker), rows),
            start=1,
        ):
            pickup_edges.append(pickup_edge)
            dropoff_edges.append(dropoff_edge)
            tracker.row_done()
            for phase, error in (("pickup", pickup_error), ("dropoff", dropoff_error)):
                if error is not None:
                    key = f"{phase}:{error}"
                    error_counts[key] = error_counts.get(key, 0) + 1
                    if len(error_samples) < 10:
                        error_samples.append(f"row={idx} {key}")

    stop_event.set()
    reporter.join()
    done_rows, done_edges, start_time = tracker.snapshot()
    print_progress(done_rows, total, done_edges, tracker.total_edges, start_time)
    print()
    return pickup_edges, dropoff_edges, error_counts, error_samples


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NYC 택시 parquet → trips_processed.json 변환"
    )
    parser.add_argument("--input", required=True, help="입력 parquet 파일 경로")
    parser.add_argument("--net", required=True, help="SUMO net.xml 파일 경로")
    parser.add_argument("--output", required=True, help="출력 JSON 파일 경로")
    parser.add_argument(
        "--start", required=True, help="필터링 시작 시각 (YYYY-MM-DD HH-MM-SS)"
    )
    parser.add_argument(
        "--end", required=True, help="필터링 종료 시각 (YYYY-MM-DD HH-MM-SS)"
    )
    parser.add_argument(
        "--sample", type=int, default=2147483647, help="최대 샘플 수 (기본값: 5000)"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"엣지 변환 스레드 수 (기본값: {DEFAULT_WORKERS})",
    )
    args = parser.parse_args()

    # --start/--end 파싱
    try:
        start_dt = datetime_type.strptime(args.start, DATETIME_FORMAT)
        end_dt = datetime_type.strptime(args.end, DATETIME_FORMAT)
    except ValueError:
        print(
            f"ERROR: --start/--end 형식이 잘못되었습니다. {DATETIME_FORMAT} 형식으로 입력하세요."
        )
        sys.exit(1)

    if start_dt >= end_dt:
        print("ERROR: --start 는 --end 보다 이전이어야 합니다.")
        sys.exit(1)

    if args.workers < 1:
        print("ERROR: --workers 는 1 이상이어야 합니다.")
        sys.exit(1)

    # 0. 네트워크 로드
    net = load_net(args.net)
    validate_geo_projection(net)

    # 1. parquet 로드
    df = read_trip_parquet(args.input)

    # 2. 시간 구간 필터
    print(f"[2/5] 시간 구간 필터 ({args.start} ~ {args.end})")
    mask = (df["pickup_datetime"] >= start_dt) & (df["pickup_datetime"] < end_dt)
    df = df[mask].copy()
    print(f"      필터 후: {len(df):,}행")
    if len(df) == 0:
        print("ERROR: 필터 결과가 비어있습니다. --start/--end 범위를 확인하세요.")
        sys.exit(1)

    # 3. GPS → SUMO 엣지 변환
    print(
        f"[3/5] GPS→SUMO 엣지 변환 중... (workers={args.workers}, 수만 행이면 수 분 소요)"
    )
    pickup_edges, dropoff_edges, error_counts, error_samples = map_edges_parallel(
        args.net, df, args.workers
    )
    df["pickup_edge"] = pickup_edges
    df["dropoff_edge"] = dropoff_edges
    before = len(df)
    df = df.dropna(subset=["pickup_edge", "dropoff_edge"])
    df = df[df["pickup_edge"] != df["dropoff_edge"]]
    removed = before - len(df)
    print(
        f"      변환 성공: {len(df):,}행 / 실패(도로 외·동일 엣지): {removed:,}행 제거"
    )
    if error_counts:
        print("      엣지 변환 실패 원인 요약:")
        for reason, count in sorted(
            error_counts.items(), key=lambda item: item[1], reverse=True
        )[:5]:
            print(f"        - {reason}: {count:,}건")
    if error_samples:
        print("      실패 예시:")
        for sample in error_samples[:5]:
            print(f"        - {sample}")
    if len(df) == 0:
        print("ERROR: 엣지 변환 후 유효한 행이 없습니다.")
        sys.exit(1)

    # 4. sim_time 정규화 (start 기준 상대 초)
    print(f"[4/5] sim_time 정규화")
    df["sim_time"] = (df["pickup_datetime"] - pd.Timestamp(start_dt)).dt.total_seconds()

    # 5. 샘플링
    if len(df) > args.sample:
        df = df.sample(n=args.sample, random_state=42)
        print(f"      샘플링: {len(df):,}행 → {args.sample}행 선택")
    else:
        print(f"      전체 사용: {len(df):,}행 (요청 샘플 수 {args.sample}보다 적음)")

    # 6. 저장
    print(f"[5/5] 저장: {args.output}")
    records = (
        df[["sim_time", "pickup_edge", "dropoff_edge", "h3_pickup"]]
        .sort_values("sim_time")
        .to_dict("records")
    )
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)
    print(f"      완료: {len(records):,}건 저장 → {args.output}")
    print(
        f"      sim_time 범위: {records[0]['sim_time']:.1f}s ~ {records[-1]['sim_time']:.1f}s"
    )


if __name__ == "__main__":
    main()
