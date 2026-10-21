"""④ 운영 서버의 URL 처리: 관제 집계·사건 상세 API와 로컬 화면을 제공한다."""

from pathlib import Path

from sqlalchemy import select

from ess_platform.agents.repository import enqueue_job
from ess_platform.agents.schemas import AnalysisRequest, ApprovalInput
from ess_platform.db import incidents
from ess_platform.http_server import ServiceHandler
from ess_platform.operations.approvals import ApprovalUnavailable, authenticate, decide
from ess_platform.operations.proposals import analysis_for_incident, proposal_detail, run_detail
from ess_platform.operations.queries import incident_detail, state_snapshot

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
STATIC_ROUTES = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/style.css": ("style.css", "text/css"),
}


class OperationsHandler(ServiceHandler):
    """관제 조회와 인증된 가상 대응 승인 API를 제공하며 실제 장비 명령은 받지 않는다."""

    def route(self, method: str, path: str) -> bool:
        """입력: HTTP method·경로. 반환: 조회 경로 처리 여부. 없는 사건에는 404 응답."""
        if method == "POST":
            if not (path.endswith("/approval") or path.endswith("/analysis-jobs")):
                return False
            try:
                actor = authenticate(self.headers.get("Authorization"))
            except ApprovalUnavailable:
                self.respond(503, {"error": "approval_auth_not_configured"})
                return True
            except PermissionError:
                self.respond(403, {"error": "approver_authentication_required"})
                return True
            parts = path.strip("/").split("/")
            if (
                len(parts) == 4
                and parts[:2] == ["api", "action-proposals"]
                and parts[3] == "approval"
            ):
                request = ApprovalInput.model_validate_json(self.read_body())
                code, result = decide(self.database, actor, int(parts[2]), request)
                self.respond(code, result)
                return True
            if (
                len(parts) == 4
                and parts[:2] == ["api", "incidents"]
                and parts[3] == "analysis-jobs"
            ):
                request = AnalysisRequest.model_validate_json(self.read_body())
                with self.database.begin() as conn:
                    incident = (
                        conn.execute(select(incidents).where(incidents.c.id == int(parts[2])))
                        .mappings()
                        .one_or_none()
                    )
                    if incident is None:
                        self.respond(404, {"error": "incident_not_found"})
                        return True
                    mode = "assisted" if incident["response_mode"] == "assisted" else "shadow"
                    try:
                        job_id = enqueue_job(
                            conn,
                            incident["id"],
                            mode,
                            "manual:" + request.request_id,
                            {"reason": request.reason, "requested_by": actor["name"]},
                        )
                    except ValueError:
                        self.respond(409, {"error": "analysis_request_identity_conflict"})
                        return True
                self.respond(200, {"job_id": job_id})
                return True
            return False
        if method != "GET":
            return False
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "incidents"] and parts[3] == "analysis":
            self.respond(200, analysis_for_incident(self.database, int(parts[2])))
            return True
        if len(parts) == 3 and parts[0] == "api" and parts[1] in {"agent-runs", "action-proposals"}:
            result = (run_detail if parts[1] == "agent-runs" else proposal_detail)(
                self.database, int(parts[2])
            )
            self.respond(200 if result else 404, result or {"error": "not_found"})
            return True
        if path == "/api/state":
            self.respond(200, state_snapshot(self.database))
        elif path.startswith("/api/incidents/"):
            result = incident_detail(self.database, int(path.removeprefix("/api/incidents/")))
            self.respond(200 if result else 404, result or {"error": "incident_not_found"})
        elif path in STATIC_ROUTES:
            filename, mime = STATIC_ROUTES[path]
            self.respond(200, (STATIC_DIR / filename).read_bytes(), mime + "; charset=utf-8")
        else:
            return False
        return True
