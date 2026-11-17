-- CreateTable
CREATE TABLE IF NOT EXISTS "LiteLLM_DashboardToken" (
    "token_hash"  TEXT NOT NULL,
    "team_id"     TEXT,
    "scope_type"  TEXT NOT NULL,
    "label"       TEXT,
    "created_at"  TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "created_by"  TEXT,
    "revoked_at"  TIMESTAMP(3),
    CONSTRAINT "LiteLLM_DashboardToken_pkey" PRIMARY KEY ("token_hash")
);

-- CreateIndex (secondary lookup by team)
CREATE INDEX IF NOT EXISTS "LiteLLM_DashboardToken_team_id_idx"
    ON "LiteLLM_DashboardToken"("team_id");

-- Partial unique index: one live token per team
CREATE UNIQUE INDEX IF NOT EXISTS "LiteLLM_DashboardToken_one_live_per_team"
    ON "LiteLLM_DashboardToken"("team_id")
    WHERE "revoked_at" IS NULL AND "scope_type" = 'team';

-- Partial unique index: one live global token
CREATE UNIQUE INDEX IF NOT EXISTS "LiteLLM_DashboardToken_one_live_global"
    ON "LiteLLM_DashboardToken"(("scope_type"))
    WHERE "revoked_at" IS NULL AND "scope_type" = 'global';

-- Check constraint on scope shape: team scope has a team_id, global scope does not
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'LiteLLM_DashboardToken_scope_shape'
    ) THEN
        ALTER TABLE "LiteLLM_DashboardToken"
          ADD CONSTRAINT "LiteLLM_DashboardToken_scope_shape"
          CHECK (
            (scope_type = 'global' AND team_id IS NULL) OR
            (scope_type = 'team'   AND team_id IS NOT NULL)
          );
    END IF;
END $$;

-- Foreign key to LiteLLM_TeamTable: on team delete, cascade the token row
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'LiteLLM_DashboardToken_team_id_fkey'
    ) THEN
        ALTER TABLE "LiteLLM_DashboardToken"
          ADD CONSTRAINT "LiteLLM_DashboardToken_team_id_fkey"
          FOREIGN KEY ("team_id")
          REFERENCES "LiteLLM_TeamTable"("team_id")
          ON DELETE CASCADE
          ON UPDATE CASCADE;
    END IF;
END $$;
