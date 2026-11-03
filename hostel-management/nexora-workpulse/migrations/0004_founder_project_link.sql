-- NEXORA WORKPULSE: dedicated column linking a founder to their project.
-- Some databases already had a "project_id" column on nwp_users from an older app (different type),
-- which broke the founder pages. WorkPulse now uses its own column and never touches the old one.
ALTER TABLE nwp_users ADD COLUMN IF NOT EXISTS founder_project_id INTEGER REFERENCES nwp_founder_projects (id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS ix_nwp_users_founder_project ON nwp_users (founder_project_id);

-- Keep existing founder links if the old column was WorkPulse's own integer column.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'nwp_users' AND column_name = 'project_id' AND data_type = 'integer') THEN
    EXECUTE 'UPDATE nwp_users u SET founder_project_id = u.project_id
             WHERE u.founder_project_id IS NULL AND u.role = ''founder''
               AND EXISTS (SELECT 1 FROM nwp_founder_projects p WHERE p.id = u.project_id)';
  END IF;
END $$;
