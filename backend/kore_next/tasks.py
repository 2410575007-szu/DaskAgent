from __future__ import annotations

from typing import Any

from .database import Database
from .util import dumps, loads, now, uid

TASK_STATUSES = {"pending", "active", "blocked", "completed", "cancelled"}
STEP_STATUSES = {"pending", "in_progress", "completed", "blocked", "skipped"}


class TaskService:
    """持久化任务目标、步骤和规格版本，供跨轮次执行与恢复使用。"""

    def __init__(self, db: Database):
        self.db = db

    async def list(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT data_json FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,))
        tasks = [loads(row["data_json"], {}) for row in rows]
        return [
            task
            for task in tasks
            if (status is None or task.get("status") == status)
            and (conversation_id is None or task.get("owner_conversation_id") == conversation_id)
        ]

    async def get(self, task_id: str) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT data_json FROM tasks WHERE id=?", (task_id,))
        if not row:
            raise KeyError("task not found")
        return loads(row["data_json"], {})

    async def create(
        self,
        *,
        title: str,
        goal: str,
        description: str = "",
        steps: list[dict[str, Any]] | None = None,
        constraints: list[str] | None = None,
        success_criteria: list[str] | None = None,
        conversation_id: str | None = None,
        run_id: str | None = None,
        status: str = "active",
    ) -> dict[str, Any]:
        # 任务规格单独记录版本，便于后续判断 Run 绑定的是哪一版目标。
        if status not in TASK_STATUSES:
            raise ValueError("invalid task status")
        stamp = now()
        normalized_steps = []
        for index, step in enumerate(steps or [], 1):
            normalized_steps.append(
                {
                    "id": str(step.get("id") or uid()),
                    "title": str(step.get("title") or f"Step {index}"),
                    "description": str(step.get("description") or ""),
                    "status": str(step.get("status") or "pending"),
                    "result": step.get("result"),
                    "updated_at": stamp,
                }
            )
        task = {
            "id": uid(),
            "title": title.strip()[:160] or "Task",
            "description": description.strip(),
            "goal": goal.strip(),
            "status": status,
            "priority": "normal",
            "constraints": [str(value).strip() for value in (constraints or []) if str(value).strip()],
            "success_criteria": [str(value).strip() for value in (success_criteria or []) if str(value).strip()],
            "state": [],
            "key_facts": [],
            "steps": normalized_steps,
            "owner_conversation_id": conversation_id,
            "run_ids": [run_id] if run_id else [],
            "created_at": stamp,
            "updated_at": stamp,
            "completed_at": stamp if status == "completed" else None,
            "revision": 1,
            "spec_revision": 1,
            "spec_history": [],
            "spec_bindings": {run_id: 1} if run_id else {},
        }
        await self.db.execute("INSERT INTO tasks(id,data_json,updated_at) VALUES(?,?,?)", (task["id"], dumps(task), stamp))
        return task

    async def update(self, task_id: str, changes: dict[str, Any], *, run_id: str | None = None) -> dict[str, Any]:
        task = await self.get(task_id)
        for key in ("goal", "constraints", "success_criteria"):
            if key in changes and changes[key] != task.get(key, [] if key != "goal" else ""):
                raise ValueError("Task Spec changes require explicit user revision")
        for key in ("title", "description", "priority", "state", "key_facts"):
            if key in changes:
                task[key] = changes[key]
        if "status" in changes:
            status = str(changes["status"])
            if status not in TASK_STATUSES:
                raise ValueError("invalid task status")
            task["status"] = status
            task["completed_at"] = now() if status == "completed" else None
        if "steps" in changes:
            task["steps"] = self._merge_steps(task.get("steps", []), changes["steps"])
        if run_id and run_id not in task.get("run_ids", []):
            task.setdefault("run_ids", []).append(run_id)
        if run_id:
            task.setdefault("spec_bindings", {}).setdefault(run_id, int(task.get("spec_revision", 1)))
        task["revision"] = int(task.get("revision", 1)) + 1
        task["updated_at"] = now()
        await self._save(task)
        return task

    async def revise_spec(self, task_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        task = await self.get(task_id)
        allowed = {"goal", "constraints", "success_criteria"}
        if not changes or set(changes) - allowed:
            raise ValueError("Only goal, constraints and success_criteria may be revised")
        snapshot = {"revision": task.get("spec_revision", 1), "goal": task.get("goal", ""),
                    "constraints": task.get("constraints", []), "success_criteria": task.get("success_criteria", []),
                    "replaced_at": now()}
        for key, value in changes.items():
            if key != "goal":
                if not isinstance(value, list):
                    raise ValueError(f"{key} must be a list")
                task[key] = [str(item).strip() for item in value if str(item).strip()]
            else:
                task[key] = str(value).strip()
        task.setdefault("spec_history", []).append(snapshot)
        task["spec_revision"] = int(task.get("spec_revision", 1)) + 1
        task["revision"] = int(task.get("revision", 1)) + 1
        task["updated_at"] = now()
        await self._save(task)
        return task

    async def decide(self, task_id: str, accepted: bool) -> dict[str, Any]:
        task = await self.get(task_id)
        if task.get("status") != "pending":
            raise ValueError("task plan is already resolved")
        task["status"] = "active" if accepted else "cancelled"
        task["updated_at"] = now()
        task["revision"] = int(task.get("revision", 1)) + 1
        await self._save(task)
        return task

    async def active_context(self, conversation_id: str | None, *, run_id: str | None = None) -> str:
        tasks = await self.list(limit=100)
        active = [
            task
            for task in tasks
            if task.get("status") in {"active", "blocked"} and (conversation_id is None or task.get("owner_conversation_id") == conversation_id)
        ]
        if not active:
            return ""
        lines = []
        for task in active[:3]:
            changed = False
            if run_id and run_id not in task.get("run_ids", []):
                task.setdefault("run_ids", []).append(run_id)
                changed = True
            if run_id and run_id not in task.get("spec_bindings", {}):
                task.setdefault("spec_bindings", {})[run_id] = int(task.get("spec_revision", 1))
                changed = True
            if changed:
                task["updated_at"] = now()
                await self._save(task)
            bound_revision = task.get("spec_bindings", {}).get(run_id, task.get("spec_revision", 1)) if run_id else task.get("spec_revision", 1)
            spec = task
            if bound_revision != task.get("spec_revision", 1):
                spec = next((entry for entry in task.get("spec_history", []) if entry["revision"] == bound_revision), task)
            lines.append(f"Task {task['id']}: {task['title']} [{task['status']}]\nGoal: {spec.get('goal', '')}")
            if run_id:
                lines.append(f"Bound run: {run_id}; Task Spec revision: {bound_revision}")
            for value in spec.get("constraints", []):
                lines.append(f"  Constraint: {value}")
            for value in spec.get("success_criteria", []):
                lines.append(f"  Success criterion: {value}")
            for value in task.get("key_facts", []):
                lines.append(f"  Task fact: {value}")
            for step in task.get("steps", []):
                lines.append(f"  - {step['id']}: {step['title']} [{step['status']}] {step.get('result') or ''}")
        return "\n".join(lines)

    async def _save(self, task: dict[str, Any]) -> None:
        await self.db.execute(
            "UPDATE tasks SET data_json=?,updated_at=? WHERE id=?",
            (dumps(task), task["updated_at"], task["id"]),
        )

    @staticmethod
    def _merge_steps(existing: list[dict[str, Any]], incoming: Any) -> list[dict[str, Any]]:
        if not isinstance(incoming, list):
            raise ValueError("steps must be a list")
        by_id = {str(step.get("id")): dict(step) for step in existing}
        result = list(existing)
        for index, update in enumerate(incoming, 1):
            if not isinstance(update, dict):
                raise ValueError("each step must be an object")
            step_id = str(update.get("id") or "")
            if step_id and step_id in by_id:
                target = by_id[step_id]
                if "status" in update and update["status"] not in STEP_STATUSES:
                    raise ValueError("invalid step status")
                target.update({key: value for key, value in update.items() if key in {"title", "description", "status", "result"}})
                target["updated_at"] = now()
                for position, item in enumerate(result):
                    if item.get("id") == step_id:
                        result[position] = target
                        break
            else:
                status = str(update.get("status") or "pending")
                if status not in STEP_STATUSES:
                    raise ValueError("invalid step status")
                result.append(
                    {
                        "id": step_id or uid(),
                        "title": str(update.get("title") or f"Step {index}"),
                        "description": str(update.get("description") or ""),
                        "status": status,
                        "result": update.get("result"),
                        "updated_at": now(),
                    }
                )
        return result
