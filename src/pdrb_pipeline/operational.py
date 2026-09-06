import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import psycopg

from .quality import QualityReport, evaluate_loaded_data


PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
BACKUP_PATTERN = re.compile(
    r"^pdrb-enrekang-pipeline-(?P<timestamp>\d{8}T\d{6}Z)\.tar\.gz$"
)
DISK_USAGE_FAIL_PERCENT = 90.0
COMPOSE_POSTGRES_COMMAND = (
    "docker",
    "compose",
    "run",
    "--rm",
    "--no-deps",
    "-T",
    "pipeline",
    "python",
    "-m",
    "pdrb_pipeline",
    "status",
    "--format",
    "json",
    "--database-only",
)


@dataclass(frozen=True)
class OperationalCheck:
    check_id: str
    status: str
    required: bool
    summary: str
    observed: Any
    cause: str | None = None
    action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )


@dataclass(frozen=True)
class OperationalReport:
    checks: tuple[OperationalCheck, ...]

    @property
    def status(self) -> str:
        required = tuple(check for check in self.checks if check.required)
        if any(check.status == FAIL for check in required):
            return FAIL
        if any(check.status == BLOCKED for check in required):
            return BLOCKED
        return PASS

    @property
    def exit_code(self) -> int:
        return {PASS: 0, FAIL: 1, BLOCKED: 2}[self.status]

    def to_dict(self) -> dict[str, Any]:
        counts = {
            status: sum(check.status == status for check in self.checks)
            for status in (PASS, FAIL, BLOCKED)
        }
        return {
            "checks": [check.to_dict() for check in self.checks],
            "counts": counts,
            "overall_status": self.status,
            "schema_version": 1,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )

    def to_human(self) -> str:
        lines = [f"PDRB operational status: {self.status}"]
        for check in self.checks:
            lines.append(f"[{check.status}] {check.check_id}: {check.summary}")
            if check.status != PASS and check.cause:
                lines.append(f"  Cause: {check.cause}")
            if check.status != PASS and check.action:
                lines.append(f"  Next: {check.action}")
        return "\n".join(lines)


def _run_command(
    command: Sequence[str], cwd: Path, timeout: int = 5
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )


def check_repository(project_root: Path) -> OperationalCheck:
    try:
        result = _run_command(
            (
                "git",
                "--no-optional-locks",
                "status",
                "--porcelain=v1",
                "--branch",
            ),
            project_root,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return OperationalCheck(
            "repository",
            BLOCKED,
            True,
            "repository state unavailable",
            "UNAVAILABLE",
            "Git is unavailable or did not respond within 5 seconds.",
            "Run git status --short --branch from the repository root.",
        )

    if result.returncode != 0:
        return OperationalCheck(
            "repository",
            BLOCKED,
            True,
            "repository state unavailable",
            {"exit_code": result.returncode},
            "Git could not inspect the working tree.",
            "Run git status --short --branch from the repository root.",
        )

    lines = result.stdout.splitlines()
    branch = lines[0][3:] if lines and lines[0].startswith("## ") else "UNKNOWN"
    changes = len(lines[1:]) if lines else 0
    observed = {"branch": branch, "change_count": changes}
    if changes:
        return OperationalCheck(
            "repository",
            FAIL,
            True,
            f"working tree has {changes} change(s)",
            observed,
            "Tracked or untracked files differ from the checked-out commit.",
            "Review git status --short --branch; do not discard changes automatically.",
        )
    return OperationalCheck(
        "repository", PASS, True, f"clean on {branch}", observed
    )


def _parse_compose_rows(output: str) -> list[dict[str, Any]]:
    if not output.strip():
        return []
    try:
        decoded = json.loads(output)
        return decoded if isinstance(decoded, list) else [decoded]
    except json.JSONDecodeError:
        return [json.loads(line) for line in output.splitlines() if line.strip()]


def check_compose(project_root: Path) -> OperationalCheck:
    command = ("docker", "compose", "ps", "--all", "--format", "json")
    try:
        result = _run_command(command, project_root)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return OperationalCheck(
            "compose",
            BLOCKED,
            False,
            "Compose status unavailable",
            "UNAVAILABLE",
            "Docker Compose is unavailable or did not respond within 5 seconds.",
            "Run docker compose ps --all and check Docker daemon access.",
        )

    if result.returncode != 0:
        cause = (
            "Docker daemon access is denied."
            if "permission denied" in result.stderr.lower()
            else "Docker Compose could not inspect project services."
        )
        return OperationalCheck(
            "compose",
            BLOCKED,
            False,
            "Compose status unavailable",
            {"exit_code": result.returncode},
            cause,
            "Run docker compose ps --all with an account allowed to inspect Docker.",
        )

    try:
        rows = _parse_compose_rows(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return OperationalCheck(
            "compose",
            BLOCKED,
            False,
            "Compose returned unreadable status",
            "INVALID_OUTPUT",
            "The installed Compose version returned unexpected JSON.",
            "Run docker compose ps --all --format json and inspect its format.",
        )

    services = sorted(
        (
            {
                "health": row.get("Health") or "",
                "service": row.get("Service") or row.get("Name") or "unknown",
                "state": row.get("State") or "unknown",
            }
            for row in rows
        ),
        key=lambda item: item["service"],
    )
    db = next((item for item in services if item["service"] == "db"), None)
    if db is None or db["state"].lower() != "running":
        return OperationalCheck(
            "compose",
            FAIL,
            False,
            "database service is not running",
            services,
            "The Compose db service is absent or stopped.",
            (
                "Run docker compose ps --all, then inspect db logs; "
                "do not restart automatically."
            ),
        )
    if db["health"] and db["health"].lower() != "healthy":
        return OperationalCheck(
            "compose",
            FAIL,
            False,
            f"database service health is {db['health']}",
            services,
            "The Compose db healthcheck is not healthy.",
            "Inspect docker compose logs db and the configured healthcheck.",
        )
    return OperationalCheck(
        "compose", PASS, False, "database service is running", services
    )


def _quality_result(report: QualityReport, rule_id: str) -> str:
    return next(
        result.observed for result in report.results if result.rule_id == rule_id
    )


def _with_connection_mode(
    check: OperationalCheck, connection_mode: str
) -> OperationalCheck:
    observed = (
        {"connection_mode": connection_mode, **check.observed}
        if isinstance(check.observed, dict)
        else {"connection_mode": connection_mode, "result": check.observed}
    )
    return OperationalCheck(
        check.check_id,
        check.status,
        check.required,
        check.summary,
        observed,
        check.cause,
        check.action,
    )


def check_postgres_direct(database_url: str | None) -> OperationalCheck:
    if not database_url:
        return OperationalCheck(
            "postgres",
            BLOCKED,
            True,
            "PostgreSQL is not configured",
            "NOT_CONFIGURED",
            "DATABASE_URL is not available to the status command.",
            (
                "Provide DATABASE_URL through the existing protected "
                "runtime configuration."
            ),
        )
    try:
        report = evaluate_loaded_data(
            database_url, connect_timeout=3, read_only=True
        )
    except psycopg.OperationalError:
        return OperationalCheck(
            "postgres",
            FAIL,
            True,
            "PostgreSQL connection failed",
            "UNREACHABLE",
            "PostgreSQL is stopped, unreachable, or rejected the connection.",
            "Check docker compose ps and db logs, then verify connectivity separately.",
        )
    except (psycopg.Error, LookupError):
        return OperationalCheck(
            "postgres",
            FAIL,
            True,
            "PostgreSQL quality query failed",
            "QUERY_FAILED",
            "The expected PDRB schema or query result is unavailable.",
            "Inspect schema state and run the documented read-only count query.",
        )

    failed_rules = [result.rule_id for result in report.results if not result.passed]
    observed = {
        "failed_rules": failed_rules,
        "industry_count": int(_quality_result(report, "industry_count")),
        "row_count": int(_quality_result(report, "row_count")),
        "year_range": _quality_result(report, "year_range"),
    }
    if failed_rules:
        return OperationalCheck(
            "postgres",
            FAIL,
            True,
            "loaded PDRB slice failed quality checks",
            observed,
            f"Quality rules failed: {','.join(failed_rules)}.",
            "Run the documented quality diagnostics; do not reload automatically.",
        )
    return OperationalCheck(
        "postgres",
        PASS,
        True,
        (
            f"slice rows={observed['row_count']} "
            f"industries={observed['industry_count']} "
            f"years={observed['year_range']}"
        ),
        observed,
    )


def _parse_compose_postgres_check(output: str) -> OperationalCheck:
    payload = json.loads(output)
    if not isinstance(payload, dict):
        raise ValueError("database status output is not an object")
    required_fields = {"check_id", "status", "required", "summary", "observed"}
    if not required_fields.issubset(payload):
        raise ValueError("database status output is incomplete")
    if payload["check_id"] != "postgres" or payload["status"] not in {
        PASS,
        FAIL,
        BLOCKED,
    }:
        raise ValueError("database status output is invalid")
    if payload["required"] is not True or not isinstance(payload["summary"], str):
        raise ValueError("database status output has invalid field types")
    cause = payload.get("cause")
    action = payload.get("action")
    if cause is not None and not isinstance(cause, str):
        raise ValueError("database status cause is invalid")
    if action is not None and not isinstance(action, str):
        raise ValueError("database status action is invalid")
    return OperationalCheck(
        "postgres",
        payload["status"],
        payload["required"],
        payload["summary"],
        payload["observed"],
        cause,
        action,
    )


def check_postgres_via_compose(project_root: Path) -> OperationalCheck:
    try:
        result = _run_command(COMPOSE_POSTGRES_COMMAND, project_root, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return OperationalCheck(
            "postgres",
            BLOCKED,
            True,
            "PostgreSQL quality check through Compose is unavailable",
            {"connection_mode": "compose", "result": "UNAVAILABLE"},
            "Docker Compose is unavailable or the bounded check did not complete.",
            "Run docker compose ps and verify the pipeline image is available.",
        )

    try:
        check = _parse_compose_postgres_check(result.stdout.strip())
        expected_exit_code = {PASS: 0, FAIL: 1, BLOCKED: 2}[check.status]
        if result.returncode != expected_exit_code:
            raise ValueError("database status exit code does not match its result")
    except (json.JSONDecodeError, TypeError, ValueError):
        return OperationalCheck(
            "postgres",
            BLOCKED,
            True,
            "PostgreSQL quality check through Compose is unavailable",
            {"connection_mode": "compose", "exit_code": result.returncode},
            "The disposable pipeline check did not return valid status JSON.",
            "Run docker compose ps and verify the pipeline image is current.",
        )
    return _with_connection_mode(check, "compose")


def check_postgres(
    database_url: str | None, project_root: Path | None = None
) -> OperationalCheck:
    if database_url:
        return _with_connection_mode(check_postgres_direct(database_url), "direct")
    if project_root is not None:
        return check_postgres_via_compose(project_root)
    return check_postgres_direct(None)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def check_backup(project_root: Path, configured_dir: str | None) -> OperationalCheck:
    if not configured_dir:
        return OperationalCheck(
            "backup",
            BLOCKED,
            False,
            "project backup is not configured",
            "NOT_CONFIGURED",
            "No PDRB_BACKUP_DIR or repository-documented backup location exists.",
            "Configure PDRB_BACKUP_DIR after a project backup workflow is defined.",
        )

    backup_path = Path(configured_dir).expanduser()
    if not backup_path.is_absolute():
        return OperationalCheck(
            "backup",
            FAIL,
            True,
            "configured backup directory is not absolute",
            {"directory": configured_dir},
            "Relative backup paths are ambiguous for unattended status checks.",
            "Configure PDRB_BACKUP_DIR with an absolute external path.",
        )
    try:
        backup_dir = backup_path.resolve()
    except OSError:
        return OperationalCheck(
            "backup",
            BLOCKED,
            True,
            "configured backup path cannot be resolved",
            {"directory": configured_dir},
            "The status process cannot resolve the configured path.",
            "Verify the configured path and parent-directory permissions.",
        )
    if _is_within(backup_dir, project_root):
        return OperationalCheck(
            "backup",
            FAIL,
            True,
            "configured backup directory is unsafe",
            {"directory": str(backup_dir)},
            "The backup directory is inside the repository.",
            "Configure PDRB_BACKUP_DIR to an external backup location.",
        )
    try:
        backup_mode = backup_dir.stat().st_mode
    except FileNotFoundError:
        return OperationalCheck(
            "backup",
            FAIL,
            True,
            "configured backup directory is unavailable",
            {"directory": str(backup_dir)},
            "The configured directory does not exist or is not a directory.",
            "Verify the configured path and its read permissions.",
        )
    except OSError:
        return OperationalCheck(
            "backup",
            BLOCKED,
            True,
            "configured backup directory cannot be inspected",
            {"directory": str(backup_dir)},
            "The status process lacks access to the configured directory.",
            "Verify directory ownership and read permissions.",
        )
    if not stat.S_ISDIR(backup_mode):
        return OperationalCheck(
            "backup",
            FAIL,
            True,
            "configured backup path is not a directory",
            {"directory": str(backup_dir)},
            "The configured path is not a directory.",
            "Configure PDRB_BACKUP_DIR with an absolute external directory.",
        )

    try:
        archives = sorted(
            path for path in backup_dir.iterdir() if BACKUP_PATTERN.fullmatch(path.name)
        )
    except OSError:
        return OperationalCheck(
            "backup",
            BLOCKED,
            True,
            "configured backup directory cannot be read",
            {"directory": str(backup_dir)},
            "The status process lacks read access to the configured directory.",
            "Verify directory ownership and read permissions.",
        )
    if not archives:
        return OperationalCheck(
            "backup",
            FAIL,
            True,
            "no matching project backup found",
            {"archive_count": 0, "directory": str(backup_dir)},
            "The configured directory has no recognized PDRB backup archive.",
            "Inspect the project backup workflow and archive naming convention.",
        )

    latest = archives[-1]
    try:
        latest_size = latest.stat().st_size
    except OSError:
        return OperationalCheck(
            "backup",
            BLOCKED,
            True,
            "latest backup metadata cannot be read",
            {"latest_name": latest.name},
            "The status process cannot inspect the latest matching archive.",
            "Verify archive ownership and read permissions.",
        )
    timestamp = BACKUP_PATTERN.fullmatch(latest.name).group("timestamp")
    observed = {
        "archive_count": len(archives),
        "directory": str(backup_dir),
        "latest_name": latest.name,
        "latest_size_bytes": latest_size,
        "latest_timestamp_utc": timestamp,
    }
    return OperationalCheck(
        "backup", PASS, True, f"latest archive is {latest.name}", observed
    )


def check_disk(project_root: Path) -> OperationalCheck:
    try:
        usage = shutil.disk_usage(project_root)
    except OSError:
        return OperationalCheck(
            "disk",
            BLOCKED,
            True,
            "disk usage unavailable",
            "UNAVAILABLE",
            "The project filesystem could not be inspected.",
            "Run df -h on the project filesystem.",
        )
    used_percent = round((usage.used / usage.total) * 100, 1)
    observed = {
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "used_percent": used_percent,
    }
    if used_percent >= DISK_USAGE_FAIL_PERCENT:
        return OperationalCheck(
            "disk",
            FAIL,
            True,
            f"filesystem usage is {used_percent}%",
            observed,
            f"Disk usage is at or above {DISK_USAGE_FAIL_PERCENT:.0f}%.",
            (
                "Run df -h and inspect large files without deleting "
                "anything automatically."
            ),
        )
    return OperationalCheck(
        "disk", PASS, True, f"filesystem usage is {used_percent}%", observed
    )


def build_operational_report(
    project_root: Path, environ: Mapping[str, str] | None = None
) -> OperationalReport:
    environment = os.environ if environ is None else environ
    checks = (
        check_repository(project_root),
        check_compose(project_root),
        check_postgres(environment.get("DATABASE_URL"), project_root),
        check_backup(project_root, environment.get("PDRB_BACKUP_DIR")),
        check_disk(project_root),
    )
    return OperationalReport(checks)
