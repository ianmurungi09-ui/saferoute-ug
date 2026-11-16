-- SafeRoute UG: production schema for PostgreSQL 14+ with PostGIS.
-- REFERENCE ONLY: written to mirror the sqlite dev store (saferoute/store.py) but never executed,
-- because no Postgres server was available when this was generated. Run it against a scratch
-- database first and expect to adjust.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE vehicles (
  plate        text PRIMARY KEY,
  vehicle_type text NOT NULL CHECK (vehicle_type IN ('bus','taxi','truck','boda','private','other')),
  sacco        text,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE devices (
  device_id  text PRIMARY KEY,
  plate      text NOT NULL REFERENCES vehicles(plate),
  key_hash   text NOT NULL,                       -- sha256 of the device key; plaintext is never stored
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Next-of-kin. Only rows with consent = true may be messaged (Uganda Data Protection and Privacy Act, 2019:
-- get legal advice on consent wording, retention and cross-border processing).
CREATE TABLE contacts (
  id         bigserial PRIMARY KEY,
  plate      text NOT NULL REFERENCES vehicles(plate),
  name       text NOT NULL,
  phone      text NOT NULL CHECK (phone ~ '^\+256[0-9]{9}$'),
  relation   text,
  consent    boolean NOT NULL DEFAULT false,
  consent_at timestamptz
);

-- Module 1
CREATE TABLE inspections (
  id           bigserial PRIMARY KEY,
  plate        text NOT NULL REFERENCES vehicles(plate),
  inspector_id text NOT NULL,
  inspected_at timestamptz NOT NULL,
  valid_until  timestamptz NOT NULL,
  status       text NOT NULL CHECK (status IN ('COMPLIANT','CONDITIONAL','DMC')),
  failed_items jsonb NOT NULL DEFAULT '[]',
  items        jsonb NOT NULL
);
CREATE INDEX ix_inspections_plate ON inspections (plate, inspected_at DESC);

CREATE TABLE checkpoints (
  id    bigserial PRIMARY KEY,
  name  text NOT NULL,
  geom  geography(Point, 4326) NOT NULL,
  phone text
);
CREATE INDEX ix_checkpoints_geom ON checkpoints USING gist (geom);

-- Module 2: raw GPS tracks are deliberately not stored, only derived events and distance.
CREATE TABLE speed_zones (
  id        bigserial PRIMARY KEY,
  name      text NOT NULL,
  geom      geography(Polygon, 4326) NOT NULL,   -- real polygons beat the circles used in the dev store
  limit_kmh integer NOT NULL CHECK (limit_kmh > 0)
);
CREATE INDEX ix_speed_zones_geom ON speed_zones USING gist (geom);

CREATE TABLE driving_events (
  id         bigserial PRIMARY KEY,
  device_id  text NOT NULL REFERENCES devices(device_id),
  plate      text NOT NULL REFERENCES vehicles(plate),
  ts         timestamptz NOT NULL,
  kind       text NOT NULL CHECK (kind IN ('overspeed','harsh_brake','harsh_accel','swerve')),
  geom       geography(Point, 4326) NOT NULL,
  value      double precision NOT NULL,
  duration_s double precision NOT NULL DEFAULT 0
);
CREATE INDEX ix_events_plate_ts ON driving_events (plate, ts DESC);

CREATE TABLE distance_log (
  device_id text NOT NULL REFERENCES devices(device_id),
  plate     text NOT NULL REFERENCES vehicles(plate),
  ts        timestamptz NOT NULL,
  km        double precision NOT NULL
);
CREATE INDEX ix_distance_plate_ts ON distance_log (plate, ts DESC);

-- Module 3
CREATE TABLE hazards (
  id          bigserial PRIMARY KEY,
  geom        geography(Point, 4326) NOT NULL,
  n_reports   integer NOT NULL DEFAULT 1,
  max_g       real NOT NULL,
  severity    text NOT NULL CHECK (severity IN ('low','medium','high')),
  status      text NOT NULL CHECK (status IN ('reported','confirmed','repaired','dismissed')),
  first_seen  timestamptz NOT NULL,
  last_seen   timestamptz NOT NULL,
  repaired_at timestamptz
);
CREATE INDEX ix_hazards_geom ON hazards USING gist (geom);

CREATE TABLE hazard_reports (
  id        bigserial PRIMARY KEY,
  hazard_id bigint NOT NULL REFERENCES hazards(id),
  device_id text NOT NULL REFERENCES devices(device_id),
  ts        timestamptz NOT NULL,
  geom      geography(Point, 4326) NOT NULL,
  peak_g    real NOT NULL,
  speed_kmh real NOT NULL
);
CREATE INDEX ix_hazard_reports_hazard ON hazard_reports (hazard_id, ts);

-- Nearest hazard within r metres: replaces the bounding-box + haversine search in the dev store.
--   SELECT id FROM hazards
--   WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, 25)
--   ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography LIMIT 1;

-- Public feed for UNRA / municipal GIS (no device or vehicle identifiers).
CREATE VIEW v_hazard_map AS
SELECT id, status, severity, n_reports AS reports, max_g, first_seen, last_seen,
       ST_X(geom::geometry) AS lon, ST_Y(geom::geometry) AS lat
FROM hazards WHERE status IN ('reported','confirmed');

-- Module 4
CREATE TABLE emergency_units (
  id    bigserial PRIMARY KEY,
  name  text NOT NULL,
  kind  text NOT NULL CHECK (kind IN ('police','hospital','ambulance')),
  geom  geography(Point, 4326) NOT NULL,
  phone text NOT NULL CHECK (phone ~ '^\+256[0-9]{9}$')
);
CREATE INDEX ix_units_geom ON emergency_units USING gist (geom);

CREATE TABLE crashes (
  id               bigserial PRIMARY KEY,
  device_id        text NOT NULL REFERENCES devices(device_id),
  event_id         text NOT NULL,
  plate            text NOT NULL REFERENCES vehicles(plate),
  ts               timestamptz NOT NULL,
  geom             geography(Point, 4326) NOT NULL,
  peak_g           real, delta_v_kmh real, tilt_deg real, speed_before_kmh real, stationary_s real,
  confidence       real NOT NULL,
  status           text NOT NULL CHECK (status IN
                     ('PENDING','DISPATCHED','NOK_NOTIFIED','DISPATCH_FAILED','LOGGED_ONLY','CANCELLED','CONFIRMED')),
  landmark         text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (device_id, event_id)                    -- makes device retries idempotent
);

-- Audit trail of every message sent, including cancel notices.
CREATE TABLE dispatches (
  id             bigserial PRIMARY KEY,
  crash_id       bigint NOT NULL REFERENCES crashes(id),
  recipient_kind text NOT NULL,
  recipient_name text NOT NULL,
  phone          text NOT NULL,
  message        text NOT NULL,
  status         text NOT NULL CHECK (status IN ('sent','failed')),
  detail         text,
  sent_at        timestamptz NOT NULL DEFAULT now()
);

-- Scale notes: partition driving_events / distance_log by month once they pass tens of millions of
-- rows, and drop old partitions on a retention schedule you've agreed with your legal adviser.
