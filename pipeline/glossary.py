"""术语库：三层合并、义项建模、冻结快照。

三条设计决定的载体：

**义项，不是词→词映射。** 同一个英文词在不同作者笔下可能是不同概念。
`job` 的日常义、以及三位 JTBD 作者各自的定义，是四个条目挂在同一个
上位概念（parent）下，不是一个词条互相覆盖。词→词映射只能二选一：
要么抹平分歧，要么让分歧伪装成翻译不统一。

**三层，但禁止静默覆盖。** global / domain / book，书级优先。改变上层
译名必须写理由（decision），否则半年后没人知道为什么变了。被覆盖的
旧值保留在 overrides 里备查。

**冻结快照。** 一次构建绑定一个不可变快照。候选词只进候选区，不参与
本次构建——否则同一次构建的前几章和后几章会用不同的术语表。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

__all__ = [
    "Sense", "Glossary", "Snapshot", "OverrideWithoutReason",
    "merge_layers", "freeze", "load_layers", "load_snapshot",
]

APPROVED = "approved"
CANDIDATE = "candidate"
SUPERSEDED = "superseded"


class OverrideWithoutReason(ValueError):
    """上层改了下层的译名却没说为什么。"""


def _tup(v) -> tuple:
    if v is None:
        return ()
    if isinstance(v, (list, tuple)):
        return tuple(v)
    return (v,)


@dataclass(frozen=True)
class Sense:
    """一个义项。不是"一个词"——同一个词可以有多个义项。"""

    id: str
    surface: str                       # 英文词形
    zh: str                            # 统一显示译名
    parent: str = ""                   # 上位概念，三家共享，是"在争同一件事"的载体
    author: str = ""                   # 该义项属于哪位作者的用法
    first_use: str = ""                # 每章首现写法，留空则自动生成
    aliases_zh: tuple[str, ...] = ()   # 通行译名，供检索对齐
    definition_zh: str = ""            # 按本作者的定义记录，不与他家合并
    evidence: tuple[str, ...] = ()     # 定义性段落的段落 ID
    decision: str = ""                 # 为什么这么定 / 为什么覆盖上层
    status: str = CANDIDATE            # candidate | approved | superseded
    layer: str = ""                    # global | domain | book，合并时写入
    overrides: tuple[str, ...] = ()    # 被本条覆盖掉的旧译名，备查

    @property
    def display_first_use(self) -> str:
        return self.first_use or f"{self.zh}（{self.surface}）"

    def to_dict(self) -> dict:
        d = {
            "id": self.id, "surface": self.surface, "zh": self.zh,
            "parent": self.parent, "author": self.author,
            "first_use": self.first_use, "aliases_zh": list(self.aliases_zh),
            "definition_zh": self.definition_zh, "evidence": list(self.evidence),
            "decision": self.decision, "status": self.status,
            "layer": self.layer, "overrides": list(self.overrides),
        }
        return {k: v for k, v in d.items() if v not in ("", [], ())}

    @classmethod
    def from_dict(cls, d: dict) -> "Sense":
        return cls(
            id=str(d["id"]), surface=str(d.get("surface", "")),
            zh=str(d.get("zh", "")), parent=str(d.get("parent", "")),
            author=str(d.get("author", "")), first_use=str(d.get("first_use", "")),
            aliases_zh=_tup(d.get("aliases_zh")),
            definition_zh=str(d.get("definition_zh", "")),
            evidence=_tup(d.get("evidence")),
            decision=str(d.get("decision", "")),
            status=str(d.get("status", CANDIDATE)),
            layer=str(d.get("layer", "")),
            overrides=_tup(d.get("overrides")),
        )


# ------------------------------------------------------------------ 匹配

def _term_pattern(surface: str) -> re.Pattern:
    """词边界匹配，容忍简单复数。

    `jobless` 不该命中 `job`——否则术语表会被无关条目塞满，而注入给模型的
    上下文里每多一条无关术语都是在稀释真正相关的那几条。
    """
    core = re.escape(surface.strip())
    core = core.replace(r"\ ", r"\s+")
    # hire/fire 在术语表中是动词隐喻，按动词屈折；其他条目只容忍简单复数。
    suffix = (
        r"(?:d|s|ing)?"
        if surface.strip().casefold() in {"hire", "fire"}
        else r"(?:s|es)?"
    )
    return re.compile(rf"\b{core}{suffix}\b", re.IGNORECASE)


@dataclass
class Glossary:
    senses: list[Sense] = field(default_factory=list)

    def by_id(self, sid: str) -> Sense:
        for s in self.senses:
            if s.id == sid:
                return s
        raise KeyError(sid)

    def for_surface(self, surface: str) -> list[Sense]:
        low = surface.strip().lower()
        return [s for s in self.senses if s.surface.strip().lower() == low]

    def approved(self) -> list[Sense]:
        return [s for s in self.senses if s.status == APPROVED]

    def candidates(self) -> list[Sense]:
        return [s for s in self.senses if s.status == CANDIDATE]

    def hits(self, text: str) -> list[Sense]:
        """文本命中了哪些义项。只注入命中的条目，不是整张表。"""
        out: list[Sense] = []
        seen: set[str] = set()
        for s in self.approved():
            if s.id in seen or not s.surface:
                continue
            searchable = text
            if s.surface.strip().casefold() == "fire":
                # 同一书中既有“解雇方案”的概念隐喻，也有森林火灾的字面义。
                searchable = re.sub(
                    r"\bforest\s+fires?\b|\bon\s+fire\b",
                    "",
                    searchable,
                    flags=re.IGNORECASE,
                )
            if s.surface.strip().casefold() == "lemon":
                # Lemon V8 是产品口味名，不是阿克洛夫“柠檬车”概念。
                searchable = re.sub(
                    r"\bLemon\s+V8\b", "", searchable, flags=re.IGNORECASE
                )
            if _term_pattern(s.surface).search(searchable):
                out.append(s)
                seen.add(s.id)
        return out


# ------------------------------------------------------------------ 合并

def merge_layers(
    global_: list[Sense] | None = None,
    domain: list[Sense] | None = None,
    book: list[Sense] | None = None,
) -> Glossary:
    """按 global < domain < book 的优先级合并。覆盖必须带理由。"""
    merged: dict[str, Sense] = {}
    order: list[str] = []

    for layer_name, layer in (
        ("global", global_ or []), ("domain", domain or []), ("book", book or [])
    ):
        for s in layer:
            incoming = replace(s, layer=layer_name)
            prev = merged.get(s.id)
            if prev is None:
                merged[s.id] = incoming
                order.append(s.id)
                continue
            if prev.zh == incoming.zh:
                # 值没变就不算覆盖，不该逼人写理由
                merged[s.id] = replace(incoming, overrides=prev.overrides)
                continue
            if not incoming.decision:
                raise OverrideWithoutReason(
                    f"{s.id}：{layer_name} 层把 “{prev.zh}” 改成 “{incoming.zh}”"
                    f"却没有写 decision。静默覆盖会让半年后的你无法解释这个改动。"
                )
            merged[s.id] = replace(
                incoming, overrides=prev.overrides + (prev.zh,)
            )
    return Glossary(senses=[merged[i] for i in order])


# ------------------------------------------------------------------ 快照

@dataclass(frozen=True)
class Snapshot:
    version: str
    domain: str
    senses: tuple[Sense, ...]
    authors: tuple[str, ...] = ()

    def by_id(self, sid: str) -> Sense:
        for s in self.senses:
            if s.id == sid:
                return s
        raise KeyError(sid)

    def hits(self, text: str) -> list[Sense]:
        return Glossary(senses=list(self.senses)).hits(text)

    def to_dict(self) -> dict:
        data = {
            "version": self.version,
            "domain": self.domain,
            "senses": [s.to_dict() for s in self.senses],
        }
        if self.authors:
            data["authors"] = list(self.authors)
        return data

    def save(self, path) -> None:
        import yaml
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# 术语快照 —— 冻结产物，不要手改。\n"
            f"# version {self.version}\n"
            f"# 改术语请改 glossary/ 下的层文件，然后重新 freeze。\n"
        )
        p.write_text(
            header + yaml.safe_dump(self.to_dict(), allow_unicode=True,
                                    sort_keys=False),
            encoding="utf-8",
        )


def _fingerprint(senses: list[Sense]) -> str:
    """内容指纹。按 id 排序，所以两个人加词顺序不同不会产生不同版本。"""
    payload = [
        {
            "id": s.id, "surface": s.surface, "zh": s.zh, "parent": s.parent,
            "author": s.author, "first_use": s.first_use,
            "aliases_zh": sorted(s.aliases_zh), "status": s.status,
        }
        for s in sorted(senses, key=lambda x: x.id)
    ]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def freeze(
    glossary: Glossary,
    domain: str = "",
    authors: list[str] | tuple[str, ...] | None = None,
) -> Snapshot:
    """冻结成不可变快照。只收 approved——候选词不参与本次构建。"""
    approved = glossary.approved()
    selected_authors = tuple(dict.fromkeys(authors or ()))
    if authors is not None:
        allowed = set(selected_authors)
        # author 为空表示跨作者通用项；总是保留。
        approved = [sense for sense in approved if not sense.author or sense.author in allowed]
    return Snapshot(
        version=_fingerprint(approved),
        domain=domain,
        senses=tuple(sorted(approved, key=lambda s: s.id)),
        authors=selected_authors,
    )


def load_snapshot(path) -> Snapshot:
    import yaml
    d = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Snapshot(
        version=str(d.get("version", "")),
        domain=str(d.get("domain", "")),
        senses=tuple(Sense.from_dict(x) for x in d.get("senses", [])),
        authors=tuple(str(value) for value in d.get("authors", [])),
    )


# ------------------------------------------------------------------ 加载

def _read_layer(path: Path) -> list[Sense]:
    if not path.exists():
        return []
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not data:
        return []
    if isinstance(data, dict):          # 容忍 {senses: [...]} 包一层
        data = data.get("senses") or []
    return [Sense.from_dict(d) for d in data if isinstance(d, dict)]


def load_layers(root, domain: str, book: str) -> Glossary:
    """从 glossary/ 目录读三层。缺文件、空文件都容忍——刚起步时全局层
    本来就该是空的。"""
    r = Path(root)
    return merge_layers(
        global_=_read_layer(r / "global.yaml"),
        domain=_read_layer(r / "domains" / f"{domain}.yaml"),
        book=_read_layer(r / "books" / f"{book}.yaml"),
    )
