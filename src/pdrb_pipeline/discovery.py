import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from .operational import BLOCKED, FAIL, OperationalCheck, OperationalReport


ISSUE_LIMIT = 200
FINGERPRINT_PATTERN = re.compile(
    r"<!--\s*pdrb-ops-fingerprint:\s*([^\s]+)\s*-->", re.IGNORECASE
)
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(password|passwd|token|secret|database_url)\s*[=:]\s*\S+"
)
URI_CREDENTIAL_PATTERN = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@")

EVIDENCE_FIELDS = {
    "repository": ("change_count",),
    "compose": ("health", "service", "state"),
    "postgres": (
        "connection_mode",
        "exit_code",
        "failed_rules",
        "industry_count",
        "result",
        "row_count",
        "year_range",
    ),
    "backup": (
        "archive_count",
        "latest_name",
        "latest_size_bytes",
        "latest_timestamp_utc",
        "result",
    ),
    "disk": ("free_bytes", "total_bytes", "used_percent"),
}


@dataclass(frozen=True)
class GitHubIssue:
    number: int
    title: str
    body: str
    state: str
    url: str


@dataclass(frozen=True)
class IssueInspection:
    available: bool
    issues: tuple[GitHubIssue, ...]
    summary: str


@dataclass(frozen=True)
class DuplicateMatch:
    number: int
    state: str
    title: str
    url: str


@dataclass(frozen=True)
class DuplicateResult:
    classification: str
    matches: tuple[DuplicateMatch, ...]
    summary: str


@dataclass(frozen=True)
class IssueCandidate:
    fingerprint: str
    title: str
    observed_check_ids: tuple[str, ...]
    facts: dict[str, Any]
    impact: str
    hypotheses: tuple[dict[str, str], ...]
    affected_area: str
    acceptance_criteria: tuple[str, ...]
    duplicate: DuplicateResult
    approval_required: bool
    approval_ready: bool
    issue_body: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryReport:
    proposals: tuple[IssueCandidate, ...]
    skipped_checks: tuple[dict[str, str], ...]
    duplicate_inspection: str

    @property
    def exit_code(self) -> int:
        return 0 if self.duplicate_inspection == "PASS" else 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "duplicate_inspection": self.duplicate_inspection,
            "proposals": [proposal.to_dict() for proposal in self.proposals],
            "schema_version": 1,
            "skipped_checks": list(self.skipped_checks),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )

    def to_human(self) -> str:
        lines = [
            f"Operational issue proposals: {len(self.proposals)} "
            f"(duplicate inspection: {self.duplicate_inspection})"
        ]
        for proposal in self.proposals:
            readiness = "READY" if proposal.approval_ready else "HOLD"
            lines.append(
                f"[{readiness}] {proposal.title} "
                f"duplicate={proposal.duplicate.classification}"
            )
        if self.skipped_checks:
            lines.append(f"Skipped non-actionable checks: {len(self.skipped_checks)}")
        lines.append("No GitHub issue was created; explicit approval is required.")
        return "\n".join(lines)


def _run_command(
    command: Sequence[str], cwd: Path, timeout: int = 10
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )


def inspect_github_issues(project_root: Path) -> IssueInspection:
    command = (
        "gh",
        "issue",
        "list",
        "--state",
        "all",
        "--limit",
        str(ISSUE_LIMIT),
        "--json",
        "number,title,body,state,url",
    )
    try:
        result = _run_command(command, project_root)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return IssueInspection(False, (), "GitHub issue inspection is unavailable.")
    if result.returncode != 0:
        return IssueInspection(False, (), "GitHub issue inspection failed.")
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or len(payload) >= ISSUE_LIMIT:
            raise ValueError("issue result is invalid or may be truncated")
        issues = tuple(
            GitHubIssue(
                number=int(item["number"]),
                title=str(item["title"]),
                body=str(item.get("body") or ""),
                state=str(item["state"]).upper(),
                url=str(item["url"]),
            )
            for item in payload
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return IssueInspection(False, (), "GitHub returned incomplete issue data.")
    return IssueInspection(True, issues, f"Inspected {len(issues)} GitHub issue(s).")


def _sanitize_text(value: str) -> str:
    value = URI_CREDENTIAL_PATTERN.sub(r"\1[REDACTED]@", value)
    return SECRET_ASSIGNMENT_PATTERN.sub(r"\1=[REDACTED]", value)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if re.search(
                    r"(?i)password|passwd|token|secret|database_url", str(key)
                )
                else _sanitize_value(item)
            )
            for key, item in sorted(value.items())
        }
    return value if isinstance(value, (int, float, bool)) or value is None else str(value)


def _safe_evidence(check: OperationalCheck) -> Any:
    allowed = EVIDENCE_FIELDS.get(check.check_id, ())
    observed = check.observed
    if isinstance(observed, dict):
        return {
            key: _sanitize_value(observed[key])
            for key in allowed
            if key in observed
        }
    if isinstance(observed, list) and check.check_id == "compose":
        return [
            {
                key: _sanitize_value(item[key])
                for key in EVIDENCE_FIELDS["compose"]
                if isinstance(item, dict) and key in item
            }
            for item in observed
        ]
    if isinstance(observed, str) and observed in {
        "INVALID_OUTPUT",
        "NOT_CONFIGURED",
        "QUERY_FAILED",
        "UNAVAILABLE",
        "UNREACHABLE",
    }:
        return observed
    return "REDACTED_OR_UNSUPPORTED"


def _problem_class(check: OperationalCheck) -> str:
    observed = check.observed if isinstance(check.observed, dict) else {}
    failed_rules = observed.get("failed_rules")
    if check.check_id == "postgres" and isinstance(failed_rules, list) and failed_rules:
        rules = ",".join(sorted(str(rule) for rule in failed_rules))
        return f"quality-{rules}"
    result = observed.get("result")
    if isinstance(result, str) and result:
        return result.lower().replace("_", "-")
    known = {
        "repository": "working-tree-state",
        "compose": "database-service-state",
        "backup": "backup-state",
        "disk": "capacity-threshold",
        "postgres": "database-check",
    }
    return known.get(check.check_id, "unknown")


def _fingerprint(check: OperationalCheck) -> str:
    return (
        f"pdrb-ops-v1:{check.check_id}:{check.status.lower()}:"
        f"{_problem_class(check)}"
    )


def _title(check: OperationalCheck) -> str:
    observed = check.observed if isinstance(check.observed, dict) else {}
    if check.check_id == "postgres" and observed.get("failed_rules"):
        rules = ", ".join(sorted(str(rule) for rule in observed["failed_rules"]))
        return f"Operational: loaded PDRB quality failed ({rules})"
    titles = {
        "repository": "Operational: repository state requires attention",
        "compose": "Operational: Compose database service requires attention",
        "postgres": "Operational: PostgreSQL check requires attention",
        "backup": "Operational: configured backup requires attention",
        "disk": "Operational: project filesystem capacity requires attention",
    }
    return titles.get(check.check_id, f"Operational: {check.check_id} check requires attention")


def _affected_area(check: OperationalCheck) -> str:
    if check.check_id == "postgres":
        observed = check.observed if isinstance(check.observed, dict) else {}
        return (
            "PostgreSQL loaded-data quality"
            if observed.get("failed_rules")
            else "PostgreSQL connectivity or runtime"
        )
    return {
        "repository": "repository working tree",
        "compose": "Docker Compose runtime",
        "backup": "backup operations",
        "disk": "host storage",
    }.get(check.check_id, "unknown")


def _impact(check: OperationalCheck) -> str:
    if check.required and check.status == FAIL:
        return "A required operational check is failing, so system health is degraded."
    if check.required and check.status == BLOCKED:
        return "A required operational check could not complete, so system health is unverified."
    return "An optional operational check is failing and requires review."


def _acceptance_criteria(check: OperationalCheck) -> tuple[str, ...]:
    return (
        f"The `{check.check_id}` operational check reports `PASS` in the affected environment.",
        "A regression test covers the observed failure or blocked condition.",
        "Operational checks remain read-only and emit no credentials or sensitive values.",
    )


def _normalize_title(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def _duplicate_result(
    fingerprint: str,
    title: str,
    check_id: str,
    inspection: IssueInspection,
) -> DuplicateResult:
    if not inspection.available:
        return DuplicateResult("UNKNOWN", (), inspection.summary)

    exact = tuple(
        issue
        for issue in inspection.issues
        if fingerprint in FINGERPRINT_PATTERN.findall(issue.body)
    )
    open_exact = tuple(issue for issue in exact if issue.state == "OPEN")
    selected = open_exact or exact
    if selected:
        classification = "OPEN_DUPLICATE" if open_exact else "RECURRENCE"
        matches = tuple(
            DuplicateMatch(
                issue.number,
                issue.state,
                _sanitize_text(issue.title),
                _sanitize_text(issue.url),
            )
            for issue in sorted(selected, key=lambda item: item.number)
        )
        summary = (
            "An open issue already tracks this fingerprint."
            if open_exact
            else "A closed issue previously tracked this fingerprint."
        )
        return DuplicateResult(classification, matches, summary)

    normalized_title = _normalize_title(title)
    possible = tuple(
        issue
        for issue in inspection.issues
        if issue.state == "OPEN"
        and _normalize_title(issue.title) == normalized_title
        and check_id.lower() in issue.body.lower()
    )
    if possible:
        matches = tuple(
            DuplicateMatch(
                issue.number,
                issue.state,
                _sanitize_text(issue.title),
                _sanitize_text(issue.url),
            )
            for issue in sorted(possible, key=lambda item: item.number)
        )
        return DuplicateResult(
            "POSSIBLE_DUPLICATE",
            matches,
            "An unmarked open issue has the same title and check ID.",
        )
    return DuplicateResult("CLEAR", (), "No matching issue was found.")


def _issue_body(
    fingerprint: str,
    facts: dict[str, Any],
    impact: str,
    hypotheses: tuple[dict[str, str], ...],
    affected_area: str,
    acceptance_criteria: tuple[str, ...],
) -> str:
    hypothesis_lines = (
        "\n".join(
            f"- [{item['confidence']}] {item['statement']}" for item in hypotheses
        )
        if hypotheses
        else "- No cause hypothesis is supported by the current evidence."
    )
    criteria_lines = "\n".join(f"- [ ] {item}" for item in acceptance_criteria)
    return (
        f"<!-- pdrb-ops-fingerprint: {fingerprint} -->\n"
        "## Observed facts\n\n"
        f"```json\n{json.dumps(facts, ensure_ascii=True, sort_keys=True)}\n```\n\n"
        f"## Impact\n\n{impact}\n\n"
        f"## Hypotheses\n\n{hypothesis_lines}\n\n"
        f"## Likely affected area\n\n{affected_area}\n\n"
        f"## Acceptance criteria\n\n{criteria_lines}\n"
    )


def _candidate(
    check: OperationalCheck, inspection: IssueInspection
) -> IssueCandidate:
    fingerprint = _fingerprint(check)
    title = _sanitize_text(_title(check))
    facts = {
        "check_id": check.check_id,
        "evidence": _safe_evidence(check),
        "required": check.required,
        "status": check.status,
        "summary": _sanitize_text(check.summary),
    }
    hypotheses = (
        (
            {
                "confidence": "unverified",
                "statement": _sanitize_text(check.cause),
            },
        )
        if check.cause
        else ()
    )
    impact = _impact(check)
    affected_area = _affected_area(check)
    acceptance_criteria = _acceptance_criteria(check)
    duplicate = _duplicate_result(
        fingerprint, title, check.check_id, inspection
    )
    approval_ready = duplicate.classification in {"CLEAR", "RECURRENCE"}
    return IssueCandidate(
        fingerprint=fingerprint,
        title=title,
        observed_check_ids=(check.check_id,),
        facts=facts,
        impact=impact,
        hypotheses=hypotheses,
        affected_area=affected_area,
        acceptance_criteria=acceptance_criteria,
        duplicate=duplicate,
        approval_required=True,
        approval_ready=approval_ready,
        issue_body=_issue_body(
            fingerprint,
            facts,
            impact,
            hypotheses,
            affected_area,
            acceptance_criteria,
        ),
    )


def build_discovery_report(
    status: OperationalReport, inspection: IssueInspection
) -> DiscoveryReport:
    actionable = tuple(
        check
        for check in status.checks
        if check.status == FAIL or (check.status == BLOCKED and check.required)
    )
    skipped = tuple(
        {
            "check_id": check.check_id,
            "reason": "optional BLOCKED checks do not create issue proposals",
            "status": check.status,
        }
        for check in status.checks
        if check.status == BLOCKED and not check.required
    )
    proposals = tuple(
        _candidate(check, inspection)
        for check in sorted(actionable, key=lambda item: item.check_id)
    )
    return DiscoveryReport(
        proposals=proposals,
        skipped_checks=skipped,
        duplicate_inspection="PASS" if inspection.available else "BLOCKED",
    )
