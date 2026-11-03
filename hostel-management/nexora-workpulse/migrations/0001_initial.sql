-- NEXORA WORKPULSE initial schema. Safe: creates only missing nwp_* objects, never drops or alters.
CREATE TABLE IF NOT EXISTS nwp_users (
	id SERIAL NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	email VARCHAR(254) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	role VARCHAR(10) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	session_version INTEGER NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	last_login_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_nwp_users_role CHECK (role IN ('admin','member')), 
	UNIQUE (email)
);

CREATE TABLE IF NOT EXISTS nwp_acknowledgements (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	ack_date DATE NOT NULL, 
	tasks_worked TEXT NOT NULL, 
	work_completed TEXT NOT NULL, 
	deliverable_links TEXT, 
	problems TEXT, 
	pending_work TEXT, 
	next_steps TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES nwp_users (id)
);

CREATE INDEX IF NOT EXISTS ix_nwp_ack_user_date ON nwp_acknowledgements (user_id, ack_date);

CREATE TABLE IF NOT EXISTS nwp_attendance_sessions (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	clock_in TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	clock_out TIMESTAMP WITHOUT TIME ZONE, 
	incomplete BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES nwp_users (id)
);

CREATE INDEX IF NOT EXISTS ix_nwp_att_user_in ON nwp_attendance_sessions (user_id, clock_in);

CREATE UNIQUE INDEX IF NOT EXISTS uq_nwp_att_one_open ON nwp_attendance_sessions (user_id) WHERE clock_out IS NULL;

CREATE TABLE IF NOT EXISTS nwp_audit_logs (
	id SERIAL NOT NULL, 
	user_id INTEGER, 
	action VARCHAR(60) NOT NULL, 
	detail VARCHAR(500), 
	ip_address VARCHAR(45), 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES nwp_users (id)
);

CREATE INDEX IF NOT EXISTS ix_nwp_audit_created ON nwp_audit_logs (created_at);

CREATE TABLE IF NOT EXISTS nwp_tasks (
	id SERIAL NOT NULL, 
	title VARCHAR(150) NOT NULL, 
	description TEXT NOT NULL, 
	priority VARCHAR(10) NOT NULL, 
	target_hours FLOAT NOT NULL, 
	start_date DATE NOT NULL, 
	deadline DATE NOT NULL, 
	expected_deliverable VARCHAR(1000), 
	instructions TEXT, 
	created_by INTEGER NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(created_by) REFERENCES nwp_users (id)
);

CREATE INDEX IF NOT EXISTS ix_nwp_tasks_deadline ON nwp_tasks (deadline);

CREATE TABLE IF NOT EXISTS nwp_break_records (
	id SERIAL NOT NULL, 
	session_id INTEGER NOT NULL, 
	started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	ended_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES nwp_attendance_sessions (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_nwp_break_session ON nwp_break_records (session_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_nwp_break_one_open ON nwp_break_records (session_id) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS nwp_task_assignments (
	id SERIAL NOT NULL, 
	task_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	status VARCHAR(15) NOT NULL, 
	progress INTEGER NOT NULL, 
	work_notes TEXT, 
	deliverable_url VARCHAR(500), 
	submitted_at TIMESTAMP WITHOUT TIME ZONE, 
	completed_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_nwp_assignment_task_user UNIQUE (task_id, user_id), 
	FOREIGN KEY(task_id) REFERENCES nwp_tasks (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES nwp_users (id)
);

CREATE INDEX IF NOT EXISTS ix_nwp_assign_user_status ON nwp_task_assignments (user_id, status);

CREATE TABLE IF NOT EXISTS nwp_task_reviews (
	id SERIAL NOT NULL, 
	assignment_id INTEGER NOT NULL, 
	reviewer_id INTEGER NOT NULL, 
	decision VARCHAR(10) NOT NULL, 
	feedback TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(assignment_id) REFERENCES nwp_task_assignments (id) ON DELETE CASCADE, 
	FOREIGN KEY(reviewer_id) REFERENCES nwp_users (id)
);

CREATE INDEX IF NOT EXISTS ix_nwp_reviews_assignment ON nwp_task_reviews (assignment_id);

