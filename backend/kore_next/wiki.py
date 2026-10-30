from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .database import Database
from .util import dumps, loads, now, uid

_SECRET = re.compile(
    r"(?i)(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*[^\s,;]{8,}"
    r"|\bsk-[A-Za-z0-9_-]{16,}\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)
_INJECTION = re.compile(
    r"(?i)(?:ignore|disregard|override)\s+(?:all\s+)?(?:previous|prior|system|developer)\s+instructions"
    r"|(?:忽略|无视|覆盖)(?:之前|以上|所有|系统|开发者)(?:的)?(?:指令|要求|规则)"
    r"|(?:你现在是|you are now)\s*(?:system|developer|管理员)"
)


def scan_knowledge(text: str) -> dict[str, bool]:
    """预先标记敏感内容和提示注入；事实是否可信仍由人工审核决定。"""
    return {
        "sensitive_information": bool(_SECRET.search(text)),
        "prompt_injection": bool(_INJECTION.search(text)),
    }


class WikiService:
    """以不可变版本保存项目知识，审核通过后才进入可检索内容。"""

    def __init__(self, db: Database, workspace: Path) -> None:
        self.db = db
        self.workspace = workspace.resolve()
        self.project_id = str(self.workspace)

    async def _source_exists(self, source_type: str, source_ref: str) -> bool:
        # 文件来源必须留在当前工作区内，防止用相对路径引用外部文件。
        if source_type == "manual":
            return bool(source_ref.strip())
        if source_type == "run":
            return await self.db.fetchone("SELECT 1 FROM runs WHERE id=?", (source_ref,)) is not None
        try:
            path = (self.workspace / source_ref).resolve()
            path.relative_to(self.workspace)
        except ValueError:
            return False
        return path.is_file()

    async def propose(
        self, *, title: str, summary: str, content: str, source_type: str,
        source_ref: str, applicability: str, supersedes_id: str | None = None,
    ) -> dict[str, Any]:
        title, summary, content = title.strip(), summary.strip(), content.strip()
        source_ref, applicability = source_ref.strip(), applicability.strip()
        if not all((title, summary, content, source_ref, applicability)):
            raise ValueError("title, summary, content, source_ref and applicability are required")
        if source_type not in {"run", "file", "manual"}:
            raise ValueError("source_type must be run, file or manual")
        previous = await self.get(supersedes_id) if supersedes_id else None
        if previous and previous["status"] not in {"active", "quarantined"}:
            raise ValueError("Only an active or quarantined entry can be revised")
        stamp = now()
        item = {
            "id": uid(), "project_id": self.project_id,
            "lineage_id": previous["lineage_id"] if previous else None,
            "version": int(previous["version"]) + 1 if previous else 1,
            "supersedes_id": supersedes_id,
            "title": title[:160], "summary": summary[:600], "content": content,
            "source_type": source_type, "source_ref": source_ref,
            "source_verified": await self._source_exists(source_type, source_ref),
            "applicability": applicability[:1000], "status": "candidate",
            "risk_flags": scan_knowledge("\n".join((title, summary, content))),
            "review": None, "created_at": stamp, "updated_at": stamp,
        }
        if not item["lineage_id"]:
            item["lineage_id"] = item["id"]
        await self.db.execute(
            "INSERT INTO wiki_entries(id,project_id,status,data_json,updated_at) VALUES(?,?,?,?,?)",
            (item["id"], self.project_id, "candidate", dumps(item), stamp),
        )
        return item

    async def get(self, entry_id: str | None) -> dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT data_json FROM wiki_entries WHERE id=? AND project_id=?",
            (entry_id, self.project_id),
        )
        if not row:
            raise KeyError("wiki entry not found")
        return loads(row["data_json"], {})

    async def list(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT data_json FROM wiki_entries WHERE project_id=? "
            + ("AND status=? " if status else "")
            + "ORDER BY updated_at DESC LIMIT ?",
            (self.project_id, status, max(1, min(limit, 500))) if status else (self.project_id, max(1, min(limit, 500))),
        )
        return [loads(row["data_json"], {}) for row in rows]

    async def index(self) -> str:
        items = await self.list(status="active", limit=100)
        return "\n".join(f"- {item['id']}: {item['title']} — {item['summary']}" for item in items)[:8000]

    async def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        terms = [term.casefold() for term in re.findall(r"[\w\u4e00-\u9fff]+", query) if term]
        if not terms:
            return []
        matches = []
        for item in await self.list(status="active", limit=500):
            haystack = " ".join(str(item.get(key, "")) for key in ("title", "summary", "content")).casefold()
            score = sum(haystack.count(term) for term in terms)
            if score:
                matches.append((score, item))
        matches.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        return [item for _, item in matches[: max(1, min(limit, 100))]]

    async def approve(self, entry_id: str, *, reviewer: str, checks: dict[str, bool]) -> dict[str, Any]:
        required = {"relevance", "source", "applicability", "sensitive_information", "prompt_injection"}
        if not reviewer.strip() or not required.issubset(checks) or not all(checks[key] is True for key in required):
            raise ValueError("Reviewer and all five review checks are required")
        item = await self.get(entry_id)
        if item["status"] != "candidate":
            raise ValueError("Only a candidate can be approved")
        if any(item["risk_flags"].values()):
            raise ValueError("Candidate contains a secret or instruction pattern; revise it before approval")
        if not await self._source_exists(item["source_type"], item["source_ref"]):
            raise ValueError("Candidate source cannot be verified")
        stamp = now()
        previous_id = item.get("supersedes_id")
        async with self.db.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            current_row = await (await db.execute(
                "SELECT data_json FROM wiki_entries WHERE id=? AND project_id=?",
                (entry_id, self.project_id),
            )).fetchone()
            current = loads(current_row["data_json"], {}) if current_row else None
            if not current or current["status"] != "candidate":
                raise ValueError("Only a candidate can be approved")
            if previous_id:
                cursor = await db.execute(
                    "SELECT data_json FROM wiki_entries WHERE id=? AND project_id=?",
                    (previous_id, self.project_id),
                )
                row = await cursor.fetchone()
                previous = loads(row["data_json"], {}) if row else None
                if not previous or previous["status"] not in {"active", "quarantined"}:
                    raise ValueError("The replaced entry is no longer current")
                previous.update(status="superseded", replaced_by_id=item["id"], updated_at=stamp)
                await db.execute(
                    "UPDATE wiki_entries SET status=?,data_json=?,updated_at=? WHERE id=?",
                    ("superseded", dumps(previous), stamp, previous_id),
                )
            item.update(
                status="active", review={"reviewer": reviewer.strip(), "checks": checks, "reviewed_at": stamp},
                updated_at=stamp,
            )
            await db.execute(
                "UPDATE wiki_entries SET status=?,data_json=?,updated_at=? WHERE id=?",
                ("active", dumps(item), stamp, entry_id),
            )
            await db.commit()
        return item

    async def quarantine(self, entry_id: str, *, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("A quarantine reason is required")
        item = await self.get(entry_id)
        if item["status"] not in {"candidate", "active"}:
            raise ValueError("Only a candidate or active entry can be quarantined")
        item.update(status="quarantined", quarantine_reason=reason.strip(), updated_at=now())
        await self.db.execute(
            "UPDATE wiki_entries SET status=?,data_json=?,updated_at=? WHERE id=?",
            ("quarantined", dumps(item), item["updated_at"], entry_id),
        )
        return item

    async def history(self, entry_id: str) -> list[dict[str, Any]]:
        item = await self.get(entry_id)
        return [entry for entry in await self.list(limit=500) if entry.get("lineage_id") == item["lineage_id"]]
