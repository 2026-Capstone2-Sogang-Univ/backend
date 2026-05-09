"""
NYC 택시 parquet → trips_processed.json 변환 스크립트

사용법:
    python scripts/preprocess_trips.py \
        --input  real_taxi_data/od_month=07/consolidated.parquet \
        --net    sumo_service/sumo_configs/NY/manhattan_car_only.net.xml \
        --output sumo_service/sumo_configs/NY/trips_processed.json \
        --date   2013-07-08 \
        --hour   8 \
        --sample 5000

의존성 (서비스 컨테이너 외부에서만 필요):
    pip install pandas pyarrow fastparquet sumolib pyproj rtree
    (pyproj: 좌표 변환 필수 / rtree: 엣지 탐색 가속, 없으면 brute-force로 동작)
"""

import argparse
import json
import sys
from datetime import date as date_type
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


def latlng_to_edge(net: sumolib.net.Net, lat: float, lon: float, radius: float = 200.0) -> str | None:
    """GPS 좌표(WGS84)를 SUMO 최근접 일반 엣지 ID로 변환. 실패 시 None."""
    try:
        x, y = net.convertLonLat2XY(lon, lat)
        candidates = net.getNeighboringEdges(x, y, radius)
        # 내부 교차로 엣지(:로 시작) 제외
        candidates = [(e, d) for e, d in candidates if not e.getID().startswith(":")]
        if not candidates:
            return None
        return min(candidates, key=lambda ed: ed[1])[0].getID()
    except Exception:
        return None


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
        sample = pickup_date_raw.dropna().iloc[0] if not pickup_date_raw.dropna().empty else None
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NYC 택시 parquet → trips_processed.json 변환"
    )
    parser.add_argument("--input",  required=True, help="입력 parquet 파일 경로")
    parser.add_argument("--net",    required=True, help="SUMO net.xml 파일 경로")
    parser.add_argument("--output", required=True, help="출력 JSON 파일 경로")
    parser.add_argument("--date",   required=True, help="필터링 날짜 (YYYY-MM-DD, 데이터 범위: 2013-07-01~31)")
    parser.add_argument("--hour",   type=int, required=True, help="필터링 시작 시각 (0-23)")
    parser.add_argument("--sample", type=int, default=5000, help="최대 샘플 수 (기본값: 5000)")
    args = parser.parse_args()

    # --date 파싱 (pickup_date 컬럼이 datetime.date 객체)
    try:
        target_date = date_type.fromisoformat(args.date)
    except ValueError:
        print(f"ERROR: --date 형식이 잘못되었습니다. YYYY-MM-DD 형식으로 입력하세요.")
        sys.exit(1)

    if not (0 <= args.hour <= 23):
        print("ERROR: --hour 는 0~23 범위여야 합니다.")
        sys.exit(1)

    # 0. 네트워크 로드
    net = load_net(args.net)
    validate_geo_projection(net)

    # 1. parquet 로드
    df = read_trip_parquet(args.input)

    # 2. 날짜/시간 필터
    print(f"[2/5] 날짜/시간 필터 ({args.date} {args.hour:02d}:xx)")
    mask = (df["pickup_date"] == target_date) & (df["pickup_datetime"].dt.hour == args.hour)
    df = df[mask].copy()
    print(f"      필터 후: {len(df):,}행")
    if len(df) == 0:
        print("ERROR: 필터 결과가 비어있습니다. --date 범위(2013-07-01~31)와 --hour 값을 확인하세요.")
        sys.exit(1)

    # 3. GPS → SUMO 엣지 변환
    print(f"[3/5] GPS→SUMO 엣지 변환 중... (수만 행이면 수 분 소요)")
    df["pickup_edge"] = [
        latlng_to_edge(net, row.pickup_latitude, row.pickup_longitude)
        for row in df.itertuples()
    ]
    df["dropoff_edge"] = [
        latlng_to_edge(net, row.dropoff_latitude, row.dropoff_longitude)
        for row in df.itertuples()
    ]
    before = len(df)
    df = df.dropna(subset=["pickup_edge", "dropoff_edge"])
    df = df[df["pickup_edge"] != df["dropoff_edge"]]
    removed = before - len(df)
    print(f"      변환 성공: {len(df):,}행 / 실패(도로 외·동일 엣지): {removed:,}행 제거")
    if len(df) == 0:
        print("ERROR: 엣지 변환 후 유효한 행이 없습니다.")
        sys.exit(1)

    # 4. sim_time 정규화 (0~3600초)
    print(f"[4/5] sim_time 정규화")
    hour_start = pd.Timestamp(f"{args.date} {args.hour:02d}:00:00")
    df["sim_time"] = (df["pickup_datetime"] - hour_start).dt.total_seconds()
    df = df[(df["sim_time"] >= 0) & (df["sim_time"] < 3600)]

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
    print(f"      sim_time 범위: {records[0]['sim_time']:.1f}s ~ {records[-1]['sim_time']:.1f}s")


if __name__ == "__main__":
    main()
