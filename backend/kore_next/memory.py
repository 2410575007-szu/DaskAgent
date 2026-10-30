from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from .database import Database
from .util import dumps, loads, now, uid
from .wiki import scan_knowledge

ReflectionModel = Callable[[str], Awaitable[str]]


class MemoryService:
    """管理长期记忆的候选、人工审核、版本修订和归档。"""

    def __init__(self, db: Database, *, max_active: int = 50):
        self.db = db
        self.max_active = max_active

    async def migrate_unreviewed(self) -> None:
        """保留旧数据，但将未经审核的记忆降为候选，避免直接进入模型上下文。"""
        rows = await self.db.fetchall("SELECT data_json FROM memories WHERE status='active'")
        for row in rows:
            item = loads(row["data_json"], {})
            if not item.get("review"):
                item.update(status="candidate", source_ref=item.get("source_ref") or "legacy_memory", updated_at=now())
                item["risk_flags"] = scan_knowledge("\n".join(str(item.get(key) or "") for key in ("title", "summary", "content")))
                await self._save(item)
        raw = await self.db.get_json("core_memory_entries", {})
        if isinstance(raw, dict):
            retained = {}
            for key, value in raw.items():
                if isinstance(value, dict) and value.get("reviewed"):
                    retained[key] = value
                    continue
                content = str(value.get("value", "")) if isinstance(value, dict) else str(value)
                if content.strip():
                    await self.create(
                        title=f"Core memory: {key}", summary="Imported core fact pending review",
                        content=content, reason="legacy_core_memory", source_ref="legacy_core_memory", core_key=str(key),
                    )
            if retained != raw:
                await self.db.set_json("core_memory_entries", retained)

    async def list(self) -> dict[str, Any]:
        rows = await self.db.fetchall("SELECT data_json FROM memories ORDER BY updated_at DESC")
        items = [loads(row["data_json"], {}) for row in rows]
        active = [item for item in items if item.get("status") == "active"]
        archived = [item for item in items if item.get("status") == "archived"]
        candidates = [item for item in items if item.get("status") == "candidate"]
        quarantined = [item for item in items if item.get("status") == "quarantined"]
        superseded = [item for item in items if item.get("status") == "superseded"]
        return {
            "core": await self.render_core(),
            "core_entries": await self.core_entries_detailed(),
            "active": active,
            "archived": archived,
            "candidates": candidates,
            "quarantined": quarantined,
            "superseded": superseded,
            "active_count": len(active),
            "max_active": self.max_active,
            "maintenance": await self.db.get_json("memory_maintenance_state", {"last_run_at": None, "archived": 0}),
        }

    async def create(
        self, *, title: str, summary: str, content: str, reason: str = "agent_tool",
        source_ref: str | None = None, applicability: str = "user", supersedes_id: str | None = None,
        core_key: str | None = None, source_type: str = "manual",
    ) -> dict[str, Any]:
        # 新记忆先作为候选保存；是否成为可注入上下文的事实由审核流程决定。
        stamp = now()
        previous = await self.get(supersedes_id, touch=False) if supersedes_id else None
        if previous and previous["status"] not in {"active", "quarantined"}:
            raise ValueError("Only active or quarantined memories can be revised")
        memory = {
            "id": uid(),
            "title": title.strip()[:120] or "Memory",
            "summary": summary.strip(),
            "content": content.strip(),
            "created_at": stamp,
            "updated_at": stamp,
            "last_accessed_at": stamp,
            "access_count": 0,
            "revision": int(previous.get("revision", 0)) + 1 if previous else 1,
            "lineage_id": previous.get("lineage_id", previous["id"]) if previous else None,
            "supersedes_id": supersedes_id,
            "source_ref": (source_ref or reason).strip(),
            "source_type": source_type,
            "applicability": applicability.strip(),
            "core_key": core_key,
            "risk_flags": scan_knowledge("\n".join((title, summary, content))),
            "status": "candidate",
            "last_update_reason": reason,
            "archive_reason": None,
        }
        memory["lineage_id"] = memory["lineage_id"] or memory["id"]
        await self.db.execute(
            "INSERT INTO memories(id,data_json,status,updated_at) VALUES(?,?,?,?)",
            (memory["id"], dumps(memory), "candidate", stamp),
        )
        return memory

    async def get(self, memory_id: str, *, touch: bool = True) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT data_json FROM memories WHERE id=?", (memory_id,))
        if not row:
            raise KeyError("memory not found")
        memory = loads(row["data_json"], {})
        if touch:
            memory["access_count"] = int(memory.get("access_count", 0)) + 1
            memory["last_accessed_at"] = now()
            await self._save(memory)
        return memory

    async def update(
        self, memory_id: str, *, title: str | None = None, summary: str | None = None, content: str | None = None,
        reason: str = "agent_tool", source_ref: str | None = None, source_type: str | None = None,
    ) -> dict[str, Any]:
        memory = await self.get(memory_id, touch=False)
        if memory["status"] in {"active", "quarantined"}:
            return await self.create(
                title=title if title is not None else memory["title"],
                summary=summary if summary is not None else memory["summary"],
                content=content if content is not None else memory["content"],
                reason=reason, source_ref=source_ref or reason, applicability=memory.get("applicability", "user"),
                supersedes_id=memory_id, core_key=memory.get("core_key"), source_type=source_type or memory.get("source_type", "manual"),
            )
        if memory["status"] != "candidate":
            raise ValueError("Only a candidate or active memory can be updated")
        if title is not None:
            memory["title"] = title.strip()[:120]
        if summary is not None:
            memory["summary"] = summary.strip()
        if content is not None:
            memory["content"] = content.strip()
        memory["revision"] = int(memory.get("revision", 1)) + 1
        memory["updated_at"] = now()
        memory["last_update_reason"] = reason
        if source_ref:
            memory["source_ref"] = source_ref
        if source_type:
            memory["source_type"] = source_type
        memory["risk_flags"] = scan_knowledge("\n".join((memory["title"], memory["summary"], memory["content"])))
        await self._save(memory)
        return memory

    async def approve(self, memory_id: str, *, reviewer: str, checks: dict[str, bool]) -> dict[str, Any]:
        required = {"relevance", "source", "applicability", "sensitive_information", "prompt_injection"}
        if not reviewer.strip() or not required.issubset(checks) or not all(checks[key] is True for key in required):
            raise ValueError("Reviewer and all five review checks are required")
        async with self.db.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT data_json FROM memories WHERE id=?", (memory_id,))).fetchone()
            if not row:
                raise KeyError("memory not found")
            memory = loads(row["data_json"], {})
            if memory["status"] != "candidate":
                raise ValueError("Only a candidate can be approved")
            memory["risk_flags"] = scan_knowledge("\n".join(str(memory.get(key) or "") for key in ("title", "summary", "content")))
            if any(memory["risk_flags"].values()):
                raise ValueError("Candidate contains a secret or instruction pattern; revise it before approval")
            if memory.get("source_type") == "run" and not await (
                await db.execute("SELECT 1 FROM runs WHERE id=?", (memory.get("source_ref"),))
            ).fetchone():
                raise ValueError("Candidate source run cannot be verified")
            previous_id = memory.get("supersedes_id")
            stamp = now()
            if previous_id:
                prior_row = await (await db.execute("SELECT data_json FROM memories WHERE id=?", (previous_id,))).fetchone()
                previous = loads(prior_row["data_json"], {}) if prior_row else None
                if not previous or previous["status"] not in {"active", "quarantined"}:
                    raise ValueError("The replaced memory is no longer current")
                previous.update(status="superseded", replaced_by_id=memory_id, updated_at=stamp)
                await db.execute(
                    "UPDATE memories SET data_json=?,status=?,updated_at=? WHERE id=?",
                    (dumps(previous), "superseded", stamp, previous_id),
                )
            memory.update(status="active", review={"reviewer": reviewer.strip(), "checks": checks, "reviewed_at": stamp}, updated_at=stamp)
            await db.execute(
                "UPDATE memories SET data_json=?,status=?,updated_at=? WHERE id=?",
                (dumps(memory), "active", stamp, memory_id),
            )
            await db.commit()
        if memory.get("core_key"):
            await self.update_core(memory["core_key"], memory["content"])
        await self.maintain()
        return memory

    async def quarantine(self, memory_id: str, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("A quarantine reason is required")
        memory = await self.get(memory_id, touch=False)
        if memory["status"] not in {"candidate", "active"}:
            raise ValueError("Only a candidate or active memory can be quarantined")
        memory.update(status="quarantined", quarantine_reason=reason.strip(), updated_at=now())
        await self._save(memory)
        if memory.get("core_key"):
            await self.remove_core(memory["core_key"])
        return memory

    async def history(self, memory_id: str) -> list[dict[str, Any]]:
        item = await self.get(memory_id, touch=False)
        rows = await self.db.fetchall("SELECT data_json FROM memories ORDER BY updated_at DESC")
        return [
            memory for row in rows if (memory := loads(row["data_json"], {})).get("lineage_id", memory.get("id"))
            == item.get("lineage_id", item["id"])
        ]

    async def archive(self, memory_id: str, reason: str = "agent_tool") -> dict[str, Any]:
        memory = await self.get(memory_id, touch=False)
        if memory["status"] != "active":
            raise ValueError("Only an active memory can be archived")
        memory.update(status="archived", archive_reason=reason, updated_at=now())
        memory["revision"] = int(memory.get("revision", 1)) + 1
        await self._save(memory)
        if memory.get("core_key"):
            await self.remove_core(memory["core_key"])
        return memory

    async def index(self) -> str:
        rows = await self.db.fetchall("SELECT data_json FROM memories WHERE status='active' ORDER BY updated_at DESC")
        lines = []
        for row in rows:
            memory = loads(row["data_json"], {})
            lines.append(f"- {memory['id']}: {memory.get('title', '')} — {memory.get('summary', '')}")
        return "\n".join(lines)

    async def core_entries(self) -> dict[str, str]:
        data = await self.db.get_json("core_memory_entries", {})
        if not isinstance(data, dict):
            return {}
        return {
            str(key): str(value.get("value", "")) if isinstance(value, dict) else str(value)
            for key, value in data.items()
        }

    async def core_entries_detailed(self) -> list[dict[str, Any]]:
        data = await self.db.get_json("core_memory_entries", {})
        if not isinstance(data, dict):
            return []
        result = []
        for key, raw in sorted(data.items()):
            if isinstance(raw, dict):
                result.append({"key": str(key), **raw})
            else:
                result.append({"key": str(key), "value": str(raw), "revision": 1, "updated_at": None})
        return result

    async def render_core(self) -> str:
        entries = await self.core_entries()
        return "\n".join(f"- {key}: {value}" for key, value in sorted(entries.items()))

    async def update_core(self, key: str, value: str) -> dict[str, str]:
        normalized = key.strip().lower().replace(" ", "_")[:80]
        if not normalized:
            raise ValueError("core memory key is required")
        raw = await self.db.get_json("core_memory_entries", {})
        entries = raw if isinstance(raw, dict) else {}
        previous = entries.get(normalized)
        revision = int(previous.get("revision", 0)) + 1 if isinstance(previous, dict) else 1
        entries[normalized] = {"value": value.strip(), "revision": revision, "updated_at": now(), "reviewed": True}
        await self.db.set_json("core_memory_entries", entries)
        return await self.core_entries()

    async def remove_core(self, key: str) -> dict[str, str]:
        raw = await self.db.get_json("core_memory_entries", {})
        entries = raw if isinstance(raw, dict) else {}
        entries.pop(key.strip().lower().replace(" ", "_"), None)
        await self.db.set_json("core_memory_entries", entries)
        return await self.core_entries()

    async def maintain(self, model: ReflectionModel | None = None) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT data_json FROM memories WHERE status='active'")
        memories = [loads(row["data_json"], {}) for row in rows]
        if len(memories) <= self.max_active:
            return []
        memories.sort(key=self._retention_score)
        candidates = memories[: min(len(memories), max(20, len(memories) - self.max_active + 10))]
        if model is None:
            await self.db.set_json("memory_maintenance_state", {
                "last_run_at": now(), "archived": 0, "deferred": len(memories) - self.max_active,
                "reason": "maintenance_model_unavailable",
            })
            return []
        prompt = (
            "Memory capacity is full. Decide which of the listed candidate memories to archive or defer. "
            "Never invent IDs. Prefer defer when uncertain. Return strict JSON: "
            '{"decisions":[{"id":"candidate-id","action":"archive|defer","reason":"..."}]}. '
            f"Archive at most {len(memories) - self.max_active} memories. Archived memories remain recoverable.\n\n"
            + dumps([{
                "id": item["id"], "title": item.get("title"), "summary": item.get("summary"),
                "access_count": item.get("access_count"), "updated_at": item.get("updated_at"),
            } for item in candidates])
        )
        try:
            decisions = self._json_object(await model(prompt)).get("decisions", [])
        except Exception as exc:
            await self.db.set_json("memory_maintenance_state", {
                "last_run_at": now(), "archived": 0, "deferred": len(memories) - self.max_active,
                "reason": f"model_failed: {type(exc).__name__}",
            })
            return []
        candidate_ids = {item["id"] for item in candidates}
        archived = []
        seen: set[str] = set()
        for decision in decisions if isinstance(decisions, list) else []:
            if not isinstance(decision, dict):
                continue
            memory_id = str(decision.get("id") or "")
            if memory_id in seen or memory_id not in candidate_ids:
                continue
            seen.add(memory_id)
            if decision.get("action") == "archive" and len(archived) < len(memories) - self.max_active:
                archived.append(await self.archive(memory_id, f"capacity_maintenance: {str(decision.get('reason') or '')[:200]}"))
        await self.db.set_json("memory_maintenance_state", {
            "last_run_at": now(), "archived": len(archived),
            "deferred": max(0, len(memories) - len(archived) - self.max_active),
        })
        return archived

    @staticmethod
    def should_reflect(user_text: str, assistant_text: str) -> bool:
        joined = f"{user_text}\n{assistant_text}".strip()
        if len(joined) < 12:
            return False
        durable_markers = (
            "prefer",
            "always",
            "remember",
            "my name",
            "i am",
            "decided",
            "constraint",
            "偏好",
            "以后",
            "记住",
            "我的",
            "决定",
            "约束",
            "长期",
        )
        return len(joined) >= 240 or any(marker in joined.lower() for marker in durable_markers)

    async def reflect(self, *, user_text: str, assistant_text: str, model: ReflectionModel, run_id: str | None = None) -> list[dict[str, Any]]:
        prompt = (
            "Extract only stable user preferences, durable facts, important decisions, or reusable knowledge from this run. "
            "Do not store transient task details. Return strict JSON: "
            '{"memories":[{"title":"...","summary":"...","content":"..."}],"core_updates":{"key":"value"}}.\n\n'
            f"USER:\n{user_text[-12000:]}\n\nASSISTANT:\n{assistant_text[-12000:]}"
        )
        raw = await model(prompt)
        payload = self._json_object(raw)
        created: list[dict[str, Any]] = []
        for item in payload.get("memories", [])[:5]:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            if float(item.get("confidence", 1.0)) < 0.6:
                continue
            duplicate = await self._find_duplicate(str(item.get("title", "")), str(item.get("summary", "")))
            if duplicate:
                created.append(
                    await self.update(
                        duplicate["id"],
                        summary=str(item.get("summary", duplicate.get("summary", ""))),
                        content=str(item["content"]),
                        reason="post_run_reflection",
                        source_ref=run_id or "post_run_reflection",
                        source_type="run" if run_id else "manual",
                    )
                )
            else:
                created.append(
                    await self.create(
                        title=str(item.get("title", "Memory")),
                        summary=str(item.get("summary", "")),
                        content=str(item["content"]),
                        reason="post_run_reflection",
                        source_ref=run_id or "post_run_reflection",
                        source_type="run" if run_id else "manual",
                    )
                )
        updates = payload.get("core_updates", {})
        if isinstance(updates, dict):
            for key, value in list(updates.items())[:10]:
                existing = await self._find_core_candidate(str(key))
                if existing:
                    created.append(await self.update(
                        existing["id"], content=str(value), reason="post_run_reflection",
                        source_ref=run_id or "post_run_reflection",
                    ))
                else:
                    current = await self.core_entries()
                    if current.get(str(key).strip().lower().replace(" ", "_")) == str(value):
                        continue
                    previous = await self._find_core_active(str(key))
                    created.append(await self.create(
                        title=f"Core memory: {key}", summary=f"Stable preference or fact: {key}",
                        content=str(value), reason="post_run_reflection", source_ref=run_id or "post_run_reflection",
                        source_type="run" if run_id else "manual", core_key=str(key),
                        supersedes_id=previous["id"] if previous else None,
                    ))
        return created

    async def _find_core_candidate(self, key: str) -> dict[str, Any] | None:
        rows = await self.db.fetchall("SELECT data_json FROM memories WHERE status='candidate'")
        for row in rows:
            item = loads(row["data_json"], {})
            if item.get("core_key") == key:
                return item
        return None

    async def _find_core_active(self, key: str) -> dict[str, Any] | None:
        rows = await self.db.fetchall("SELECT data_json FROM memories WHERE status='active'")
        for row in rows:
            item = loads(row["data_json"], {})
            if item.get("core_key") == key:
                return item
        return None

    async def _find_duplicate(self, title: str, summary: str) -> dict[str, Any] | None:
        target = self._terms(title + " " + summary)
        if not target:
            return None
        rows = await self.db.fetchall(
            "SELECT data_json FROM memories WHERE status IN ('active','candidate') "
            "ORDER BY CASE status WHEN 'candidate' THEN 0 ELSE 1 END, updated_at DESC"
        )
        for row in rows:
            item = loads(row["data_json"], {})
            terms = self._terms(str(item.get("title", "")) + " " + str(item.get("summary", "")))
            if terms and len(target & terms) / max(1, len(target | terms)) >= 0.72:
                return item
        return None

    async def _save(self, memory: dict[str, Any]) -> None:
        await self.db.execute(
            "UPDATE memories SET data_json=?,status=?,updated_at=? WHERE id=?",
            (dumps(memory), memory["status"], memory["updated_at"], memory["id"]),
        )

    @staticmethod
    def _retention_score(memory: dict[str, Any]) -> tuple[float, str]:
        access = int(memory.get("access_count", 0))
        updated = str(memory.get("updated_at", ""))
        try:
            age_days = max(0.0, (datetime.now(UTC) - datetime.fromisoformat(updated.replace("Z", "+00:00"))).total_seconds() / 86400)
        except (TypeError, ValueError):
            age_days = 365.0
        score = math.log1p(access) * 2.0 + max(0.0, 4.0 - age_days / 30.0) + min(2.0, int(memory.get("revision", 1)) / 3)
        return score, updated

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {part.lower() for part in text.replace("，", " ").replace("。", " ").split() if len(part) > 1}

    @staticmethod
    def _json_object(raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            return {}
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
