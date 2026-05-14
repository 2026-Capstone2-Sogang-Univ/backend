CREATE TABLE simulation_run (
    id               BIGSERIAL PRIMARY KEY,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at         TIMESTAMPTZ,
    end_reason       TEXT,
    sim_duration_s   DOUBLE PRECISION NOT NULL,
    passenger_source TEXT NOT NULL,
    params           JSONB NOT NULL
);

CREATE TABLE passenger (
    run_id              BIGINT NOT NULL REFERENCES simulation_run(id) ON DELETE CASCADE,
    passenger_id        TEXT NOT NULL,
    spawn_sim_time      DOUBLE PRECISION NOT NULL,
    pickup_edge         TEXT NOT NULL,
    dropoff_edge        TEXT NOT NULL,
    pickup_lat          DOUBLE PRECISION NOT NULL,
    pickup_lng          DOUBLE PRECISION NOT NULL,
    dropoff_lat         DOUBLE PRECISION NOT NULL,
    dropoff_lng         DOUBLE PRECISION NOT NULL,
    expected_distance_m DOUBLE PRECISION NOT NULL,
    expected_fare       INTEGER NOT NULL,
    h3_pickup           TEXT,
    source              TEXT NOT NULL,
    PRIMARY KEY (run_id, passenger_id)
);

CREATE TABLE taxi (
    run_id  BIGINT NOT NULL REFERENCES simulation_run(id) ON DELETE CASCADE,
    taxi_id TEXT NOT NULL,
    PRIMARY KEY (run_id, taxi_id)
);

CREATE TABLE dispatch (
    id                          TEXT PRIMARY KEY,
    run_id                      BIGINT NOT NULL,
    passenger_id                TEXT NOT NULL,
    taxi_id                     TEXT NOT NULL,
    dispatch_sim_time           DOUBLE PRECISION NOT NULL,
    estimated_pickup_distance_m DOUBLE PRECISION,
    timed_out                   BOOLEAN NOT NULL DEFAULT FALSE,
    accepted                    BOOLEAN NOT NULL DEFAULT TRUE,
    FOREIGN KEY (run_id, passenger_id) REFERENCES passenger(run_id, passenger_id),
    FOREIGN KEY (run_id, taxi_id)      REFERENCES taxi(run_id, taxi_id)
);
CREATE INDEX ON dispatch (run_id, passenger_id);
CREATE INDEX ON dispatch (run_id, dispatch_sim_time);

CREATE TABLE trip (
    id                BIGSERIAL PRIMARY KEY,
    run_id            BIGINT NOT NULL,
    passenger_id      TEXT NOT NULL,
    taxi_id           TEXT NOT NULL,
    dispatch_id       TEXT NOT NULL REFERENCES dispatch(id),
    dispatch_sim_time DOUBLE PRECISION NOT NULL,
    pickup_sim_time   DOUBLE PRECISION NOT NULL,
    dropoff_sim_time  DOUBLE PRECISION,
    distance_m        DOUBLE PRECISION NOT NULL,
    low_speed_seconds DOUBLE PRECISION NOT NULL,
    fare              INTEGER NOT NULL,
    expected_fare     INTEGER NOT NULL,
    completion        TEXT NOT NULL,
    FOREIGN KEY (run_id, passenger_id) REFERENCES passenger(run_id, passenger_id)
);
CREATE INDEX ON trip (run_id, dropoff_sim_time);
CREATE INDEX ON trip (run_id, taxi_id);
