-- NEXORA WORKPULSE v2: onboarding skills, first-login attendance, daily task updates, GitHub pull requests.
-- Additive only: adds missing columns/tables, never drops or rewrites existing data.

ALTER TABLE nwp_users ADD COLUMN IF NOT EXISTS first_login_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE nwp_users ADD COLUMN IF NOT EXISTS skill_backend VARCHAR(10);
ALTER TABLE nwp_users ADD COLUMN IF NOT EXISTS skill_database VARCHAR(10);
ALTER TABLE nwp_users ADD COLUMN IF NOT EXISTS skill_frontend VARCHAR(10);
ALTER TABLE nwp_users ADD COLUMN IF NOT EXISTS onboarded_at TIMESTAMP WITHOUT TIME ZONE;

ALTER TABLE nwp_tasks ADD COLUMN IF NOT EXISTS work_type VARCHAR(12);
ALTER TABLE nwp_tasks ADD COLUMN IF NOT EXISTS repo_url VARCHAR(300);

ALTER TABLE nwp_task_assignments ADD COLUMN IF NOT EXISTS pr_url VARCHAR(500);
ALTER TABLE nwp_task_assignments ADD COLUMN IF NOT EXISTS pr_merged_at TIMESTAMP WITHOUT TIME ZONE;

CREATE TABLE IF NOT EXISTS nwp_task_updates (
	id SERIAL NOT NULL,
	assignment_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	update_date DATE NOT NULL,
	progress INTEGER NOT NULL,
	note TEXT NOT NULL,
	pr_url VARCHAR(500),
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(assignment_id) REFERENCES nwp_task_assignments (id) ON DELETE CASCADE,
	FOREIGN KEY(user_id) REFERENCES nwp_users (id)
);

CREATE INDEX IF NOT EXISTS ix_nwp_updates_user_date ON nwp_task_updates (user_id, update_date);
CREATE INDEX IF NOT EXISTS ix_nwp_updates_assignment ON nwp_task_updates (assignment_id);

CREATE TABLE IF NOT EXISTS nwp_settings (
	key VARCHAR(60) NOT NULL,
	value VARCHAR(1000),
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (key)
);

-- Existing members: attendance starts from their first recorded session (if any).
UPDATE nwp_users u SET first_login_at = s.first_in
FROM (SELECT user_id, MIN(clock_in) AS first_in FROM nwp_attendance_sessions GROUP BY user_id) s
WHERE u.id = s.user_id AND u.first_login_at IS NULL;
