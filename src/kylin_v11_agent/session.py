# -*- coding: utf-8 -*-
from __future__ import annotations
import json, logging, os, tempfile, time, uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TaskRecord:
    task_id: str
    description: str
    tool: str
    argv: List[str]
    returncode: int
    success: bool
    latency_ms: float
    error: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class AgentSession:
    session_id: str
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    task_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    total_latency_ms: float = 0.0
    task_history: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def record_task(self, task: TaskRecord) -> None:
        self.last_active = time.time()
        self.task_count += 1
        if task.success:
            self.success_count += 1
        else:
            self.failed_count += 1
        self.total_latency_ms += task.latency_ms
        self.task_history.append(asdict(task))
        if len(self.task_history) > 200:
            self.task_history = self.task_history[-200:]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AgentSession":
        return AgentSession(**d)


class SessionManager:
    def __init__(self, sessions_dir: str = ""):
        if not sessions_dir:
            base = Path(__file__).resolve().parents[2]
            sessions_dir = str(base / "data" / "sessions")
        self.sessions_dir = Path(sessions_dir)
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning("sessions_dir mkdir failed: %s", e)

    def create(self, agent_id: str = "kylin_v11_agent") -> AgentSession:
        session_id = "kv11_" + uuid.uuid4().hex[:12]
        session = AgentSession(session_id=session_id)
        session.notes.append("created=" + agent_id)
        self._save(session)
        logger.info("session created: %s", session_id)
        return session

    def get(self, session_id: str) -> Optional[AgentSession]:
        path = self.sessions_dir / (session_id + ".json")
        if not path.exists():
            return None
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return AgentSession.from_dict(d)
        except Exception as e:
            logger.warning("session load failed: %s: %s", session_id, e)
            return None

    def update(self, session: AgentSession) -> None:
        session.last_active = time.time()
        self._save(session)

    def list_all(self) -> List[AgentSession]:
        sessions = []
        for p in self.sessions_dir.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                sessions.append(AgentSession.from_dict(d))
            except Exception as e:
                logger.debug("skip corrupt session %s: %s", p.name, e)
        return sorted(sessions, key=lambda s: s.last_active, reverse=True)

    def prune(self, keep: int = 20) -> int:
        sessions = self.list_all()
        removed = 0
        for s in sessions[keep:]:
            p = self.sessions_dir / (s.session_id + ".json")
            try:
                p.unlink()
                removed += 1
            except Exception as e:
                logger.warning("prune failed: %s", e)
        return removed

    def _save(self, session: AgentSession) -> None:
        # R3-5: atomic write via temp file + os.replace. If the process
        # crashes mid-write the previous file is untouched.
        # R1-4: drop indent=2 to keep session files small (5x smaller
        # for sessions with many tasks).
        path = self.sessions_dir / (session.session_id + ".json")
        try:
            text = json.dumps(asdict(session), ensure_ascii=False)
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=session.session_id + ".", suffix=".tmp",
                dir=str(self.sessions_dir),
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning("_save failed for %s: %s", session.session_id, e)