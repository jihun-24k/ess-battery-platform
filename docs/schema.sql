-- Generated from battery_etl.load.metadata; reference DDL only.

CREATE TABLE source_files (
	id BIGSERIAL NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	batch_label TEXT NOT NULL, 
	batch_date TEXT NOT NULL, 
	size_bytes BIGINT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (sha256)
)

;


CREATE TABLE processed_sources (
	id BIGSERIAL NOT NULL, 
	source_id BIGINT NOT NULL, 
	version TEXT NOT NULL, 
	run_id VARCHAR(36) NOT NULL, 
	source_path TEXT NOT NULL, 
	artifact_path TEXT NOT NULL, 
	result JSONB NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (source_id, version), 
	FOREIGN KEY(source_id) REFERENCES source_files (id), 
	UNIQUE (run_id)
)

;


CREATE TABLE cells (
	id BIGSERIAL NOT NULL, 
	processed_id BIGINT NOT NULL, 
	cell_index INTEGER NOT NULL, 
	policy TEXT NOT NULL, 
	policy_readable TEXT NOT NULL, 
	cycle_life FLOAT, 
	vdlin DOUBLE PRECISION[] NOT NULL, 
	opaque_identifiers JSONB NOT NULL, 
	recorded_cycles INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (processed_id, cell_index), 
	CHECK (cell_index >= 0 AND recorded_cycles >= 0), 
	FOREIGN KEY(processed_id) REFERENCES processed_sources (id)
)

;


CREATE TABLE validation_issues (
	id BIGSERIAL NOT NULL, 
	processed_id BIGINT NOT NULL, 
	issue JSONB NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(processed_id) REFERENCES processed_sources (id)
)

;


CREATE TABLE cycles (
	id BIGSERIAL NOT NULL, 
	cell_id BIGINT NOT NULL, 
	cycle_index INTEGER NOT NULL, 
	cycle_number INTEGER NOT NULL, 
	summary JSONB NOT NULL, 
	source_paths JSONB NOT NULL, 
	"I" DOUBLE PRECISION[] NOT NULL, 
	"Qc" DOUBLE PRECISION[] NOT NULL, 
	"Qd" DOUBLE PRECISION[] NOT NULL, 
	"Qdlin" DOUBLE PRECISION[] NOT NULL, 
	"T" DOUBLE PRECISION[] NOT NULL, 
	"Tdlin" DOUBLE PRECISION[] NOT NULL, 
	"V" DOUBLE PRECISION[] NOT NULL, 
	"discharge_dQdV" DOUBLE PRECISION[] NOT NULL, 
	t DOUBLE PRECISION[] NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (cell_id, cycle_index), 
	UNIQUE (cell_id, cycle_number), 
	CHECK (cycle_index >= 0 AND cycle_number > 0), 
	FOREIGN KEY(cell_id) REFERENCES cells (id)
)

;
