"""SQLite schema for the Docker-native deployment intelligence layer.

Kept in this module (not in `orchestrator/models/project.py`) so that a future
migration to Postgres / Alembic can lift these tables out without surgery on
the project model. The only entry point is `init_deployment_schema(db)`, which
is idempotent and called from `models.project.init_db`.

The existing `deploy_runs` table is **repurposed** rather than replaced — see
`_extend_deploy_runs` for the columns added. This avoids parallel-table
ambiguity and keeps `save_deploy_run` / `get_deploy_runs` callable from the
new orchestrator.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import aiosqlite

logger = logging.getLogger(__name__)


# Tables created with raw CREATE TABLE IF NOT EXISTS. Order matters for FK
# integrity (parents before children).
_TABLES: tuple[tuple[str, str], ...] = (
    (
        "deployment_targets",
        """
        CREATE TABLE IF NOT EXISTS deployment_targets (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            host TEXT NOT NULL,
            ssh_user TEXT NOT NULL,
            ssh_port INTEGER NOT NULL DEFAULT 22,
            environment TEXT NOT NULL DEFAULT 'production',
            domain TEXT,
            app_name TEXT NOT NULL,
            deploy_path TEXT NOT NULL,
            preferred_strategy TEXT NOT NULL DEFAULT 'auto_detect',
            auth_mode TEXT NOT NULL DEFAULT 'ssh_key',
            auth_reference TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_inventory_snapshot_id TEXT,
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
        """,
    ),
    (
        "deployment_inventory_snapshots",
        """
        CREATE TABLE IF NOT EXISTS deployment_inventory_snapshots (
            id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            raw_json TEXT NOT NULL DEFAULT '{}',
            summary_json TEXT NOT NULL DEFAULT '{}',
            detected_reverse_proxy TEXT,
            detected_deployment_manager TEXT,
            confidence REAL NOT NULL DEFAULT 0.0,
            error_message TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (target_id) REFERENCES deployment_targets(id),
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
        """,
    ),
    (
        "docker_ai_analyses",
        """
        CREATE TABLE IF NOT EXISTS docker_ai_analyses (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            target_id TEXT,
            inventory_snapshot_id TEXT,
            provider TEXT NOT NULL,
            analysis_type TEXT NOT NULL,
            raw_output TEXT NOT NULL DEFAULT '',
            normalized_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'ok',
            error_message TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
        """,
    ),
    (
        "docker_agent_runs",
        """
        CREATE TABLE IF NOT EXISTS docker_agent_runs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            target_id TEXT,
            agent_name TEXT NOT NULL,
            input_json TEXT NOT NULL DEFAULT '{}',
            output_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            started_at TEXT,
            finished_at TEXT,
            error_message TEXT,
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
        """,
    ),
    (
        "deployment_plans",
        """
        CREATE TABLE IF NOT EXISTS deployment_plans (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            inventory_snapshot_id TEXT,
            docker_ai_analysis_id TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            plan_json TEXT NOT NULL DEFAULT '{}',
            plan_content_hash TEXT NOT NULL DEFAULT '',
            summary_markdown TEXT,
            risk_level TEXT NOT NULL DEFAULT 'medium',
            blocking_questions_json TEXT NOT NULL DEFAULT '[]',
            created_by TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id),
            FOREIGN KEY (target_id) REFERENCES deployment_targets(id)
        )
        """,
    ),
    (
        "deployment_plan_validations",
        """
        CREATE TABLE IF NOT EXISTS deployment_plan_validations (
            id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            status TEXT NOT NULL,
            plan_content_hash TEXT NOT NULL DEFAULT '',
            checks_json TEXT NOT NULL DEFAULT '[]',
            blocking_errors_json TEXT NOT NULL DEFAULT '[]',
            warnings_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            FOREIGN KEY (plan_id) REFERENCES deployment_plans(id)
        )
        """,
    ),
    (
        "deployment_run_steps",
        """
        CREATE TABLE IF NOT EXISTS deployment_run_steps (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            started_at TEXT,
            finished_at TEXT,
            stdout_excerpt TEXT,
            stderr_excerpt TEXT,
            error_message TEXT,
            FOREIGN KEY (run_id) REFERENCES deploy_runs(id)
        )
        """,
    ),
    (
        "deployment_artifacts",
        """
        CREATE TABLE IF NOT EXISTS deployment_artifacts (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            path_or_ref TEXT NOT NULL,
            content_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES deploy_runs(id)
        )
        """,
    ),
    (
        "deployment_approvals",
        """
        CREATE TABLE IF NOT EXISTS deployment_approvals (
            id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            plan_content_hash TEXT NOT NULL DEFAULT '',
            approved_by TEXT NOT NULL,
            approved_at TEXT NOT NULL,
            approval_note TEXT,
            FOREIGN KEY (plan_id) REFERENCES deployment_plans(id)
        )
        """,
    ),
    (
        "deployment_rollbacks",
        """
        CREATE TABLE IF NOT EXISTS deployment_rollbacks (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            rollback_status TEXT NOT NULL DEFAULT 'pending',
            previous_release_ref TEXT,
            rollback_plan_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            executed_at TEXT,
            FOREIGN KEY (run_id) REFERENCES deploy_runs(id)
        )
        """,
    ),
    (
        "deployment_secrets",
        """
        CREATE TABLE IF NOT EXISTS deployment_secrets (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            target_id TEXT,
            name TEXT NOT NULL,
            reference_kind TEXT NOT NULL,
            reference_value TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
        """,
    ),
)


# Columns added to the existing `deploy_runs` table. The spec calls this table
# `deployment_runs`; we reuse the existing one rather than creating a parallel.
_DEPLOY_RUNS_EXTRA_COLUMNS: Mapping[str, str] = {
    "plan_id": "TEXT",
    "target_id": "TEXT",
    "started_by": "TEXT",
    "started_at": "TEXT",
    "finished_at": "TEXT",
    "current_step": "INTEGER",
    "health_status": "TEXT NOT NULL DEFAULT 'unknown'",
    "rollback_available": "INTEGER NOT NULL DEFAULT 0",
}


_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_deployment_targets_project ON deployment_targets(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_deployment_inv_target ON deployment_inventory_snapshots(target_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_docker_ai_analyses_target ON docker_ai_analyses(target_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_docker_agent_runs_project ON docker_agent_runs(project_id, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_deployment_plans_target ON deployment_plans(target_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_deployment_plan_validations_plan ON deployment_plan_validations(plan_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_deployment_run_steps_run ON deployment_run_steps(run_id, step_index)",
    "CREATE INDEX IF NOT EXISTS idx_deployment_artifacts_run ON deployment_artifacts(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_deployment_approvals_plan ON deployment_approvals(plan_id)",
    "CREATE INDEX IF NOT EXISTS idx_deployment_rollbacks_run ON deployment_rollbacks(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_deployment_secrets_project ON deployment_secrets(project_id, name)",
)


async def _add_missing_columns(
    db: aiosqlite.Connection, table: str, columns: Mapping[str, str]
) -> None:
    """Idempotent ADD COLUMN for any table. Uses PRAGMA table_info to check."""
    cursor = await db.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cursor.fetchall()}
    for column, definition in columns.items():
        if column not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def _extend_deploy_runs(db: aiosqlite.Connection) -> None:
    await _add_missing_columns(db, "deploy_runs", _DEPLOY_RUNS_EXTRA_COLUMNS)


# Idempotent migrations for new plan_content_hash columns (P0-4)
_PLAN_HASH_MIGRATIONS: tuple[tuple[str, Mapping[str, str]], ...] = (
    ("deployment_plans", {"plan_content_hash": "TEXT NOT NULL DEFAULT ''"}),
    ("deployment_plan_validations", {"plan_content_hash": "TEXT NOT NULL DEFAULT ''"}),
    ("deployment_approvals", {"plan_content_hash": "TEXT NOT NULL DEFAULT ''"}),
)


async def init_deployment_schema(db: aiosqlite.Connection) -> None:
    """Create the deployment intelligence tables. Idempotent.

    Call after the `deploy_runs` table exists (FK target). The caller owns
    `db.commit()` — this function does not commit so it can be folded into a
    larger init_db transaction.
    """
    for _name, ddl in _TABLES:
        await db.execute(ddl)

    await _extend_deploy_runs(db)

    # P0-4: add plan_content_hash columns to plans/validations/approvals
    for table, cols in _PLAN_HASH_MIGRATIONS:
        await _add_missing_columns(db, table, cols)

    for stmt in _INDEXES:
        await db.execute(stmt)

    logger.debug("Deployment intelligence schema ensured (%d tables)", len(_TABLES))
