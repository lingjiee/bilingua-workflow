"""术语库：三层合并、义项建模、冻结快照。

这里的每条测试都对应设计里的一条决定：

D1 义项建模  —— 同一个英文词在不同作者笔下是不同义项，不是互相覆盖的译名
D2 三层骨架  —— global / domain / book，书级优先，但禁止静默覆盖
D3 冻结快照  —— 一次构建绑定一个不可变快照，候选词不参与
"""

from __future__ import annotations

import pytest

from pipeline.glossary import (
    Glossary,
    OverrideWithoutReason,
    Sense,
    freeze,
    merge_layers,
)


def sense(sid, surface, zh, **kw) -> Sense:
    kw.setdefault("status", "approved")
    return Sense(id=sid, surface=surface, zh=zh, **kw)


# ---------------------------------------------------------------- D1 义项

class TestSenseModel:
    def test_same_surface_different_authors_coexist(self):
        """三位作者对 job 的定义不同，这是学术分歧不是翻译问题。
        合并后必须两个义项都在，不能互相覆盖。"""
        g = merge_layers(domain=[
            sense("jtbd.job.christensen", "job", "任务",
                  author="christensen", parent="jtbd.job"),
            sense("jtbd.job.klement", "job", "任务",
                  author="klement", parent="jtbd.job"),
        ])
        senses = g.for_surface("job")
        assert len(senses) == 2
        assert {s.author for s in senses} == {"christensen", "klement"}

    def test_senses_share_a_parent_concept(self):
        g = merge_layers(domain=[
            sense("jtbd.job.a", "job", "任务", author="a", parent="jtbd.job"),
            sense("jtbd.job.b", "job", "任务", author="b", parent="jtbd.job"),
        ])
        assert {s.parent for s in g.for_surface("job")} == {"jtbd.job"}

    def test_everyday_sense_is_separate_from_domain_sense(self):
        """job 的日常义和 JTBD 义是两个条目，parent 不同。"""
        g = merge_layers(
            global_=[sense("gen.job", "job", "工作", parent="general")],
            domain=[sense("jtbd.job", "job", "任务", parent="jtbd.job")],
        )
        parents = {s.parent for s in g.for_surface("job")}
        assert parents == {"general", "jtbd.job"}

    def test_sense_carries_evidence(self):
        s = sense("x", "job", "任务", evidence=("cal/ch02/§7b1e0a33",))
        assert s.evidence == ("cal/ch02/§7b1e0a33",)

    def test_first_use_defaults_to_zh_plus_english(self):
        s = sense("x", "job", "任务")
        assert s.display_first_use == "任务（job）"

    def test_explicit_first_use_wins(self):
        s = sense("x", "job", "任务", first_use="待办任务（job）")
        assert s.display_first_use == "待办任务（job）"


# ---------------------------------------------------------------- D2 三层

class TestLayerMerge:
    def test_book_layer_overrides_domain(self):
        g = merge_layers(
            domain=[sense("t.x", "outcome", "结果")],
            book=[sense("t.x", "outcome", "成效", decision="本书特指可量化指标")],
        )
        assert g.by_id("t.x").zh == "成效"

    def test_domain_layer_overrides_global(self):
        g = merge_layers(
            global_=[sense("t.x", "progress", "进展")],
            domain=[sense("t.x", "progress", "进步", decision="JTBD 语境专指")],
        )
        assert g.by_id("t.x").zh == "进步"

    def test_override_without_reason_is_rejected(self):
        """静默覆盖是三层结构最大的风险——半年后没人知道为什么变了。"""
        with pytest.raises(OverrideWithoutReason) as e:
            merge_layers(
                domain=[sense("t.x", "outcome", "结果")],
                book=[sense("t.x", "outcome", "成效")],  # 没有 decision
            )
        assert "t.x" in str(e.value)

    def test_same_value_override_needs_no_reason(self):
        """值没变就不算覆盖，不该逼人写理由。"""
        g = merge_layers(
            domain=[sense("t.x", "outcome", "结果")],
            book=[sense("t.x", "outcome", "结果")],
        )
        assert g.by_id("t.x").zh == "结果"

    def test_merged_sense_records_its_layer(self):
        g = merge_layers(
            global_=[sense("a", "alpha", "甲")],
            domain=[sense("b", "beta", "乙")],
            book=[sense("c", "gamma", "丙")],
        )
        assert g.by_id("a").layer == "global"
        assert g.by_id("b").layer == "domain"
        assert g.by_id("c").layer == "book"

    def test_override_keeps_overridden_value_for_audit(self):
        g = merge_layers(
            domain=[sense("t.x", "outcome", "结果")],
            book=[sense("t.x", "outcome", "成效", decision="本书特指")],
        )
        assert g.by_id("t.x").overrides == ("结果",)


# ---------------------------------------------------------------- 候选区

class TestCandidates:
    def test_candidates_are_excluded_from_approved(self):
        g = merge_layers(domain=[
            sense("a", "alpha", "甲"),
            Sense(id="b", surface="beta", zh="乙", status="candidate"),
        ])
        assert [s.id for s in g.approved()] == ["a"]

    def test_candidates_never_enter_a_snapshot(self):
        """D3：候选词不参与本次构建，否则同一次构建前后不一致。"""
        g = merge_layers(domain=[
            sense("a", "alpha", "甲"),
            Sense(id="b", surface="beta", zh="乙", status="candidate"),
        ])
        snap = freeze(g, domain="jtbd")
        assert [s.id for s in snap.senses] == ["a"]

    def test_superseded_senses_are_excluded(self):
        g = merge_layers(domain=[
            sense("a", "alpha", "甲"),
            Sense(id="b", surface="beta", zh="乙", status="superseded"),
        ])
        assert [s.id for s in g.approved()] == ["a"]

    def test_candidates_are_still_listed_for_review(self):
        g = merge_layers(domain=[
            Sense(id="b", surface="beta", zh="乙", status="candidate"),
        ])
        assert [s.id for s in g.candidates()] == ["b"]


# ---------------------------------------------------------------- D3 快照

class TestSnapshot:
    def test_version_is_deterministic(self):
        a = freeze(merge_layers(domain=[sense("a", "alpha", "甲")]), domain="d")
        b = freeze(merge_layers(domain=[sense("a", "alpha", "甲")]), domain="d")
        assert a.version == b.version

    def test_version_changes_when_a_translation_changes(self):
        a = freeze(merge_layers(domain=[sense("a", "alpha", "甲")]), domain="d")
        b = freeze(merge_layers(domain=[sense("a", "alpha", "乙")]), domain="d")
        assert a.version != b.version

    def test_version_is_stable_under_source_ordering(self):
        """两个人往表里加词的顺序不同，不该产生不同的快照版本。"""
        a = freeze(merge_layers(domain=[
            sense("a", "alpha", "甲"), sense("b", "beta", "乙")]), domain="d")
        b = freeze(merge_layers(domain=[
            sense("b", "beta", "乙"), sense("a", "alpha", "甲")]), domain="d")
        assert a.version == b.version

    def test_snapshot_is_immutable(self):
        snap = freeze(merge_layers(domain=[sense("a", "alpha", "甲")]), domain="d")
        with pytest.raises(Exception):
            snap.senses.append(sense("b", "beta", "乙"))  # type: ignore[attr-defined]

    def test_snapshot_roundtrips_through_yaml(self, tmp_path):
        snap = freeze(merge_layers(domain=[
            sense("a", "alpha", "甲", aliases_zh=("假名",), evidence=("x/y/§z",))
        ]), domain="d")
        p = tmp_path / "snap.lock.yaml"
        snap.save(p)
        from pipeline.glossary import load_snapshot
        back = load_snapshot(p)
        assert back.version == snap.version
        assert back.senses[0].aliases_zh == ("假名",)

    def test_snapshot_records_domain_and_count(self):
        snap = freeze(merge_layers(domain=[sense("a", "alpha", "甲")]), domain="jtbd")
        assert snap.domain == "jtbd"
        assert len(snap.senses) == 1

    def test_snapshot_can_filter_author_specific_senses(self):
        common = sense("common", "customer", "客户")
        klement = Sense(
            id="klement", surface="job", zh="待办任务",
            author="Author One", status="approved",
        )
        christensen = Sense(
            id="christensen", surface="job", zh="任务",
            author="Author Two", status="approved",
        )
        snap = freeze(
            merge_layers(domain=[common, klement, christensen]),
            domain="jtbd",
            authors=["Author One"],
        )
        assert {item.id for item in snap.senses} == {"common", "klement"}
        assert snap.authors == ("Author One",)

    def test_snapshot_author_filter_roundtrips(self, tmp_path):
        snap = freeze(
            merge_layers(domain=[Sense(
                id="k", surface="job", zh="任务", author="Author One",
                status="approved",
            )]),
            domain="jtbd",
            authors=["Author One"],
        )
        path = tmp_path / "author.lock"
        snap.save(path)
        from pipeline.glossary import load_snapshot
        assert load_snapshot(path).authors == ("Author One",)


# ---------------------------------------------------------------- 命中

class TestTermHits:
    @pytest.fixture
    def g(self) -> Glossary:
        return merge_layers(domain=[
            sense("j", "job", "任务"),
            sense("p", "progress", "进步"),
            sense("s", "struggling moment", "挣扎时刻"),
        ])

    def test_finds_terms_present_in_text(self, g):
        hits = g.hits("The customer has a job to do.")
        assert [s.id for s in hits] == ["j"]

    def test_is_case_insensitive(self, g):
        assert [s.id for s in g.hits("A JOB worth doing.")] == ["j"]

    def test_matches_simple_plural(self, g):
        assert [s.id for s in g.hits("Several jobs at once.")] == ["j"]

    def test_fire_verb_does_not_match_plural_forest_fires(self):
        glossary = merge_layers(domain=[sense("f", "fire", "解雇")])
        assert glossary.hits("Forest fires occur in summer; brush can be on fire.") == []

    def test_fire_verb_matches_conceptual_inflections(self):
        glossary = merge_layers(domain=[sense("f", "fire", "解雇")])
        assert [item.id for item in glossary.hits("The old solution was fired.")] == ["f"]

    def test_does_not_match_inside_another_word(self, g):
        """jobless / progressive 不该命中，否则术语表会被无关条目塞满。"""
        assert g.hits("He was jobless and unhappy.") == []

    def test_matches_multiword_terms(self, g):
        assert [s.id for s in g.hits("At the struggling moment.")] == ["s"]

    def test_returns_all_senses_of_a_hit_surface(self):
        g = merge_layers(domain=[
            sense("a", "job", "任务", author="christensen"),
            sense("b", "job", "任务", author="klement"),
        ])
        assert len(g.hits("a job")) == 2

    def test_no_hits_returns_empty(self, g):
        assert g.hits("Nothing relevant here.") == []

    def test_hits_are_deduplicated(self, g):
        assert len([s for s in g.hits("job job job")]) == 1


# ---------------------------------------------------------------- 加载

class TestLoadLayers:
    def test_loads_three_layers_from_disk(self, tmp_path):
        (tmp_path / "domains").mkdir()
        (tmp_path / "books").mkdir()
        (tmp_path / "global.yaml").write_text(
            "- id: g1\n  surface: Christensen\n  zh: 克里斯坦森\n  status: approved\n",
            encoding="utf-8")
        (tmp_path / "domains" / "jtbd.yaml").write_text(
            "- id: d1\n  surface: job\n  zh: 任务\n  status: approved\n",
            encoding="utf-8")
        (tmp_path / "books" / "cal.yaml").write_text(
            "- id: b1\n  surface: milkshake\n  zh: 奶昔\n  status: approved\n",
            encoding="utf-8")
        from pipeline.glossary import load_layers
        g = load_layers(tmp_path, domain="jtbd", book="cal")
        assert {s.id for s in g.approved()} == {"g1", "d1", "b1"}

    def test_missing_layer_files_are_tolerated(self, tmp_path):
        from pipeline.glossary import load_layers
        g = load_layers(tmp_path, domain="jtbd", book="cal")
        assert g.approved() == []

    def test_empty_yaml_file_is_tolerated(self, tmp_path):
        (tmp_path / "global.yaml").write_text("", encoding="utf-8")
        from pipeline.glossary import load_layers
        g = load_layers(tmp_path, domain="d", book="b")
        assert g.approved() == []
