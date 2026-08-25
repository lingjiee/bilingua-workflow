"""校验：装配前的硬门槛。

自动化能被信任的唯一理由，是它会自己发现自己错了。这里每条规则都对应
一种模型真实会犯的错，而且都是**人眼扫一遍看不出来**的那种——
数字抄错、脚注编号丢了、术语换了个译法、整段没翻直接回抄英文。

任一条不过，该段标记待重跑；整本有 error 就不发布。
"""

from __future__ import annotations

import pytest

from pipeline.document import Block, parse_markdown
from pipeline.glossary import Sense
from pipeline.verify import Severity, verify_block, verify_chapter


def block(text: str, kind: str = "para"):
    src = f"# C\n\n{text}\n"
    doc = parse_markdown(src, book_slug="t")
    return next(b for b in doc.translatable_blocks() if b.kind == kind)


def rules(findings) -> set[str]:
    return {f.rule for f in findings}


# ---------------------------------------------------------------- 数字


class TestNumbers:
    def test_missing_number_is_flagged(self):
        b = block("Sales grew by 47 percent in 1995.")
        assert "numbers" in rules(verify_block(b, "销售额在某年增长了不少。"))

    def test_all_numbers_present_passes(self):
        b = block("Sales grew by 47 percent in 1995.")
        assert "numbers" not in rules(verify_block(b, "销售额在 1995 年增长了 47%。"))

    def test_no_numbers_in_source_never_flags(self):
        b = block("A sentence without any figures at all.")
        assert "numbers" not in rules(verify_block(b, "一个不含任何数字的句子。"))

    def test_transposed_digits_are_caught(self):
        """74 和 47 都是数字，但不是同一个数字。"""
        b = block("Exactly 47 units shipped.")
        assert "numbers" in rules(verify_block(b, "恰好发运了 74 件。"))

    def test_number_inside_a_word_is_not_required(self):
        """像 COVID-19、A4 这类嵌在词里的数字不强制出现在译文。"""
        b = block("The A4 format is standard.")
        findings = verify_block(b, "A4 是标准幅面。")
        assert "numbers" not in rules(findings)

    def test_hyphenated_currency_does_not_backtrack_to_partial_number(self):
        b = block("The $2,500-a-year program is affordable.")
        assert "numbers" not in rules(verify_block(b, "这个每年 $2,500 的项目负担得起。"))

    @pytest.mark.parametrize(
        ("source", "translation"),
        [
            ("# Chapter 2 Progress", "# 第二章 进展"),
            ("# Section 1 Introduction", "# 第一部分 导论"),
            ("# Chapter 10 Conclusions", "# 第十章 结论"),
        ],
    )
    def test_heading_ordinal_may_use_chinese_numeral(self, source, translation):
        b = block(source, kind="heading")
        assert "numbers" not in rules(verify_block(b, translation))

    def test_prose_number_may_not_be_silently_spelled_as_unrelated_chinese(self):
        b = block("The company sold 2 products.")
        assert "numbers" in rules(verify_block(b, "这家公司销售了第二种产品。"))

    @pytest.mark.parametrize(
        ("source", "translation"),
        [
            ("It is a $1 billion business.", "这是一家价值 10 亿美元的企业。"),
            ("Revenue reached 2.5 million dollars.", "收入达到 250 万美元。"),
            ("Revenue reached 535 million dollars.", "收入达到 5.35 亿美元。"),
            ("Revenue reached 32 million dollars.", "收入达到 3,200 万美元。"),
            ("Output exceeded 800 billion dollars.", "产值超过八千亿美元。"),
        ],
    )
    def test_exact_scale_conversion_is_not_a_missing_number(self, source, translation):
        assert "numbers" not in rules(verify_block(block(source), translation))

    def test_repeated_token_at_two_scales_preserves_both_occurrences(self):
        source = "It cost $1 billion and started at $1 million."
        translation = "研发耗资 10 亿美元，起售价为 100 万美元。"
        assert "numbers" not in rules(verify_block(block(source), translation))

    def test_evidenced_billon_source_typo_still_accepts_exact_conversion(self):
        source = "Operating income was $21 billon."
        translation = "营业利润为 210 亿美元。"
        assert "numbers" not in rules(verify_block(block(source), translation))

    def test_percentage_may_use_exact_chinese_words(self):
        b = block("The company was 100 percent committed.")
        assert "numbers" not in rules(verify_block(b, "公司百分之百地投入其中。"))

    def test_large_currency_may_use_exact_wan_conversion(self):
        b = block("Homes cost $120,000 to $200,000.")
        assert "numbers" not in rules(verify_block(b, "住宅售价为 12 万至 20 万美元。"))

    def test_year_with_collapsed_endnote_may_be_separated_in_translation(self):
        b = block("He told the magazine in 2013.3 Then growth resumed in 2015.")
        zh = "他在 2013 年对杂志说。3 随后增长于 2015 年恢复。"
        assert "numbers" not in rules(verify_block(b, zh))

    def test_exact_hour_may_use_chinese_time_without_zero_minutes(self):
        b = block("The visit is at 2:00 and the scan must finish by 11:30.")
        zh = "就诊安排在下午两点，检查最晚必须在 11 点 30 分前完成。"
        assert "numbers" not in rules(verify_block(b, zh))

    def test_twenty_four_seven_may_be_translated_as_continuous_service(self):
        b = block("The 24/7 television news cycle eclipsed newspapers.")
        zh = "全天候电视新闻周期盖过了报纸。"
        assert "numbers" not in rules(verify_block(b, zh))


# ---------------------------------------------------------------- 标记


class TestMarkup:
    def test_lost_footnote_marker_is_flagged(self):
        b = block("This claim rests on prior work.[12]")
        assert "markup" in rules(verify_block(b, "这个论断建立在既有研究之上。"))

    def test_kept_footnote_marker_passes(self):
        b = block("This claim rests on prior work.[12]")
        assert "markup" not in rules(verify_block(b, "这个论断建立在既有研究之上。[12]"))

    def test_lost_link_target_is_flagged(self):
        b = block("See [the report](https://example.com/r) for details.")
        assert "markup" in rules(verify_block(b, "详见报告。"))

    def test_kept_link_target_passes(self):
        b = block("See [the report](https://example.com/r) for details.")
        assert "markup" not in rules(verify_block(b, "详见[报告](https://example.com/r)。"))

    def test_heading_internal_anchor_is_intentionally_dropped(self):
        b = block("# [Chapter One](#nav.xhtml_nch1)", kind="heading")
        assert "markup" not in rules(verify_block(b, "# 第一章"))

    def test_list_block_must_stay_a_list(self):
        b = block("- alpha\n- beta\n- gamma", kind="list")
        bad = verify_block(b, "甲、乙、丙")
        assert "structure" in rules(bad)

    def test_list_translated_as_list_passes(self):
        b = block("- alpha\n- beta\n- gamma", kind="list")
        good = verify_block(b, "- 甲\n- 乙\n- 丙")
        assert "structure" not in rules(good)


# ---------------------------------------------------------------- 术语


class TestGlossary:
    JOB = Sense(id="j", surface="job", zh="任务", status="approved")

    def test_glossary_term_must_use_approved_translation(self):
        b = block("The customer has a job to get done.")
        f = verify_block(b, "顾客有一件差事要办。", senses=[self.JOB])
        assert "glossary" in rules(f)

    def test_approved_translation_passes(self):
        b = block("The customer has a job to get done.")
        f = verify_block(b, "顾客有一件任务要完成。", senses=[self.JOB])
        assert "glossary" not in rules(f)

    def test_alias_is_for_search_not_an_accepted_build_translation(self):
        s = Sense(id="j", surface="job", zh="任务", aliases_zh=("差事",), status="approved")
        b = block("The job to be done.")
        assert "glossary" in rules(verify_block(b, "这件差事要完成。", senses=[s]))

    def test_term_absent_from_source_is_not_required(self):
        b = block("Nothing about that concept here.")
        assert "glossary" not in rules(verify_block(b, "这里没提那个概念。", senses=[self.JOB]))

    def test_glossary_finding_names_the_term(self):
        b = block("The customer has a job.")
        f = [
            x for x in verify_block(b, "顾客有件差事。", senses=[self.JOB]) if x.rule == "glossary"
        ]
        assert f and "job" in f[0].detail and "任务" in f[0].detail

    def test_fire_metaphor_does_not_match_forest_fires(self):
        sense = Sense(id="fire", surface="fire", zh="解雇", status="approved")
        b = block("Ice cream sales and forest fires are correlated.")
        f = verify_block(b, "冰淇淋销量与森林火灾存在相关性。", senses=[sense])
        assert "glossary" not in rules(f)

    def test_lemon_product_name_is_not_the_defective_car_concept(self):
        sense = Sense(id="lemon", surface="lemon", zh="柠檬车", status="approved")
        b = block("The retailer also stocks Lemon V8.")
        f = verify_block(b, "零售商还销售柠檬 V8。", senses=[sense])
        assert "glossary" not in rules(f)

    def test_chinese_quotes_may_wrap_model_name_before_suffix(self):
        sense = Sense(
            id="model",
            surface="Jobs-As-Progress",
            zh="任务即进展模型",
            status="approved",
        )
        b = block("Jobs-As-Progress is descriptive.")
        f = verify_block(b, "“任务即进展”模型是描述性的。", senses=[sense])
        assert "glossary" not in rules(f)

    def test_model_name_may_omit_redundant_model_suffix_in_prose(self):
        sense = Sense(
            id="model",
            surface="Jobs-As-Progress",
            zh="任务即进展模型",
            status="approved",
        )
        b = block("Jobs-As-Progress is a theory.")
        f = verify_block(b, "“任务即进展”是一种理论。", senses=[sense])
        assert "glossary" not in rules(f)

    def test_book_title_does_not_trigger_jobs_term(self):
        sense = Sense(
            id="jtbd",
            surface="Jobs to be Done",
            zh="待办任务",
            status="approved",
        )
        b = block("The Jobs to be Done Handbook describes an interview method.")
        f = verify_block(b, "《Jobs to be Done Handbook》介绍了一种访谈方法。", senses=[sense])
        assert "glossary" not in rules(f)

    def test_customer_in_named_research_method_is_not_generic_customer(self):
        sense = Sense(id="customer", surface="customer", zh="客户", status="approved")
        b = block("She promotes Customer Case Research instead.")
        f = verify_block(b, "她转而推广 Customer Case Research。", senses=[sense])
        assert "glossary" not in rules(f)

    def test_customer_jobs_inside_named_book_title_is_not_enforced(self):
        senses = [
            Sense(id="c", surface="customer", zh="客户", status="approved"),
            Sense(id="cj", surface="Customer Jobs", zh="客户任务", status="approved"),
        ]
        source = "Summary of Putting Customer Jobs to Work"
        zh = "《Putting Customer Jobs to Work》要点小结"
        assert "glossary" not in rules(verify_block(block(source), zh, senses=senses))

    def test_bibliography_entry_may_remain_english(self):
        senses = [Sense(id="customer", surface="customer", zh="客户", status="approved")]
        b = block(
            "Ulwick, A. W. (2005). What customers want: Using outcome-driven "
            "innovation to create breakthrough products and services. McGraw-Hill."
        )
        f = verify_block(b, b.text, senses=senses)
        assert "residual_en" not in rules(f)
        assert "glossary" not in rules(f)

    @pytest.mark.parametrize(
        "source",
        [
            "Bob Moesta, “Bob Moesta on Jobs-to-be-Done,” interview by Des Traynor.",
            "Anthony W. Ulwick, “Turn Customer Input into Innovation,” "
            "*Harvard Business Review* (January 2002).",
            "Anthony W. Ulwick and Lance A. Bettencourt, "
            "“Giving Customers a Fair Hearing,” *MIT Sloan Management Review* "
            "(Spring 2008).",
        ],
    )
    def test_author_first_citation_title_does_not_trigger_glossary(self, source):
        senses = [
            self.JOB,
            Sense(id="customer", surface="customer", zh="客户", status="approved"),
        ]
        findings = verify_block(block(source), source, senses=senses)
        assert "glossary" not in rules(findings)
        assert "residual_en" not in rules(findings)

    @pytest.mark.parametrize(
        "source",
        [
            "Hugh Beyer and Karen Holtzblatt, *Contextual Design* "
            "(San Francisco: Morgan Kaufmann, 1998).",
            "Stephen Wunker, Jessica Wattman, and David Farber, “Competition,” "
            "in *Jobs to Be Done* (New York: AMACOM, 2016).",
            "Scott Anthony et al., *The Innovator’s Guide to Growth* "
            "(Boston: Harvard Business Review Press, 2008).",
            "Maxim van de Keuken, “Using Job Stories,” (Utrecht University, 2017).",
        ],
    )
    def test_multi_author_or_italic_title_citation_is_reference(self, source):
        findings = verify_block(block(source), source, senses=[self.JOB])
        assert "glossary" not in rules(findings)
        assert "residual_en" not in rules(findings)

    def test_bulleted_resource_entry_with_period_separator_is_reference(self):
        source = "• Anthony Ulwick. *Jobs to Be Done: Theory to Practice* (2016)"
        findings = verify_block(block(source), source, senses=[self.JOB])
        assert "glossary" not in rules(findings)
        assert "residual_en" not in rules(findings)

    def test_bulleted_et_al_resource_entry_is_reference(self):
        source = "• Alex Author et al. “Integrating Around a Job,” (2016)"
        findings = verify_block(block(source), source, senses=[self.JOB])
        assert "glossary" not in rules(findings)
        assert "residual_en" not in rules(findings)

    @pytest.mark.parametrize(
        "source",
        [
            "Deloitte. “Customer-Centricity: Embedding It into Your Organisation’s DNA” (2014)",
            "IBM. “Needs Statements,” *IBM Enterprise Design Thinking Toolkit* (2018)",
        ],
    )
    def test_organization_authored_resource_is_reference(self, source):
        senses = [
            Sense(id="customer", surface="customer", zh="客户", status="approved"),
            Sense(id="needs", surface="needs", zh="需求", status="approved"),
        ]
        findings = verify_block(block(source), source, senses=senses)
        assert "glossary" not in rules(findings)
        assert "residual_en" not in rules(findings)

    def test_glossary_ignores_markdown_image_target(self):
        source = (
            "### **PLAY** ![Images](the-jobs-to-be-done-playbook/images/arrow1.jpg) "
            "**Run Switch Interviews**"
        )
        zh = "### **实战方法** ![Images](the-jobs-to-be-done-playbook/images/arrow1.jpg) **开展转换访谈**"
        assert "glossary" not in rules(
            verify_block(block(source, kind="heading"), zh, senses=[self.JOB])
        )

    @pytest.mark.parametrize(
        "source",
        [
            "The form you end up with needs to be appropriate to your team.",
            "A developer needs a way to make sense of minimal design.",
            "Of course, they need to be able to use your solution.",
        ],
    )
    def test_needs_as_verb_is_not_customer_needs_term(self, source):
        sense = Sense(id="needs", surface="needs", zh="需求", status="approved")
        assert "glossary" not in rules(
            verify_block(block(source), "这里的 needs 是普通动词。", senses=[sense])
        )

    def test_customer_needs_noun_still_requires_approved_term(self):
        sense = Sense(id="needs", surface="needs", zh="需求", status="approved")
        findings = verify_block(
            block("The team must understand unmet customer needs."),
            "团队必须理解客户尚未满足的要求。",
            senses=[sense],
        )
        assert "glossary" in rules(findings)

    def test_jtbd_plays_as_verb_is_not_named_play(self):
        sense = Sense(id="play", surface="JTBD play", zh="JTBD 实战方法", status="approved")
        source = "JTBD plays a key role in aligning to customer needs."
        assert "glossary" not in rules(
            verify_block(block(source), "待办任务在对齐客户需求方面发挥关键作用。", senses=[sense])
        )

    @pytest.mark.parametrize(
        "source",
        [
            "Steve Jobs declared that experience comes first.",
            "Jobs’s approach seemed revolutionary at the time.",
            "What would you miss about your current job?",
            "Create a Jobs Atlas before proposing a solution.",
            "The Jobs To Be Done Playbook is an excellent resource.",
        ],
    )
    def test_job_surface_ignores_evidenced_names_and_employment(self, source):
        assert "glossary" not in rules(
            verify_block(block(source), "这里是专名或就业语义。", senses=[self.JOB])
        )

    @pytest.mark.parametrize(
        "source",
        [
            "See Ulwick’s full-length book on *Jobs to Be Done* (2016).",
            "In *Jobs to Be Done,* he states that innovation needs a small team.",
            "This summarizes his presentation “Using Jobs to Be Done at CarMax.”",
            "The Jobs To Be Done Playbook helps organizations turn insight into action.",
        ],
    )
    def test_jobs_to_be_done_inside_evidenced_title_is_not_enforced(self, source):
        sense = Sense(id="jtbd", surface="Jobs to Be Done", zh="待办任务", status="approved")
        assert "glossary" not in rules(
            verify_block(block(source), "这里保留英文出版物标题。", senses=[sense])
        )

    @pytest.mark.parametrize(
        "source",
        [
            "He wrote *Who Do You Want Your Customers to Become?*",
            "In their book, *The Customer-Driven Playbook*, the authors explain JTBD.",
        ],
    )
    def test_customer_inside_evidenced_book_title_is_not_enforced(self, source):
        sense = Sense(id="customer", surface="customer", zh="客户", status="approved")
        assert "glossary" not in rules(
            verify_block(block(source), "这里保留英文书名。", senses=[sense])
        )

    def test_generic_manager_job_is_not_the_jtbd_concept(self):
        b = block("We study the dimensions of the job of general managers.")
        f = verify_block(b, "我们研究总经理这份工作的各个维度。", senses=[self.JOB])
        assert "glossary" not in rules(f)

    def test_better_job_means_employment_not_jtbd(self):
        b = block("The training may help them get a better job.")
        f = verify_block(b, "培训可能帮助他们找到更好的工作。", senses=[self.JOB])
        assert "glossary" not in rules(f)

    @pytest.mark.parametrize(
        ("source", "translation"),
        [
            ("The auto industry had been hemorrhaging jobs.", "汽车业一直在流失就业。"),
            ("Tools doctors need to do their job well.", "医生做好工作所需的工具。"),
            ("He had visited many times in the course of his job.", "因工作关系，他来过许多次。"),
            ("The reorganization changed job responsibilities.", "重组改变了岗位职责。"),
            (
                "A proxy for how well the manager is doing his job.",
                "衡量管理者工作表现的替代指标。",
            ),
        ],
    )
    def test_occupational_job_is_not_jtbd(self, source, translation):
        assert "glossary" not in rules(verify_block(block(source), translation, senses=[self.JOB]))

    def test_hiring_a_person_is_literal_employment(self):
        sense = Sense(id="h", surface="hire", zh="雇用", status="approved")
        b = block("They might hire another person to do the books.")
        f = verify_block(b, "他们可能再雇一个人专门记账。", senses=[sense])
        assert "glossary" not in rules(f)

    def test_hiring_a_car_service_is_literal_procurement(self):
        sense = Sense(id="h", surface="hire", zh="雇用", status="approved")
        b = block("They may hire a car service for the full day.")
        f = verify_block(b, "他们可能会包一整天的专车服务。", senses=[sense])
        assert "glossary" not in rules(f)

    def test_consultancy_hired_to_help_is_literal_service_relationship(self):
        sense = Sense(id="h", surface="hire", zh="雇用", status="approved")
        b = block("Take McKinsey & Company, which is hired to help companies worldwide.")
        f = verify_block(b, "以受雇为全球企业提供帮助的麦肯锡为例。", senses=[sense])
        assert "glossary" not in rules(f)

    @pytest.mark.parametrize(
        "source",
        [
            "They predicted the progress of the planets around the earth.",
            "They observed their progress along Aristotle's circles.",
            "The model shows how technological progress interacts with market needs.",
        ],
    )
    def test_astronomical_progress_is_not_customer_progress(self, source):
        sense = Sense(id="p", surface="progress", zh="进展", status="approved")
        f = verify_block(block(source), "他们观察行星运行的轨迹。", senses=[sense])
        assert "glossary" not in rules(f)

    @pytest.mark.parametrize(
        "source",
        [
            "Adding features may push up visible figures.",
            "High prices may push customers away.",
            "What pushes them to change?",
        ],
    )
    def test_push_used_as_an_ordinary_verb_is_not_the_named_force(self, source):
        sense = Sense(id="push", surface="push", zh="推动力", status="approved")
        f = verify_block(block(source), "这句话使用的是普通动词语义。", senses=[sense])
        assert "glossary" not in rules(f)

    def test_push_used_as_named_force_requires_approved_term(self):
        sense = Sense(id="push", surface="push", zh="推动力", status="approved")
        source = "The push of the situation and the pull of the new idea compete."
        f = verify_block(block(source), "处境的压力与新想法的吸引相互竞争。", senses=[sense])
        assert "glossary" in rules(f)

    def test_pull_the_curtain_back_is_not_the_named_force(self):
        sense = Sense(id="pull", surface="pull", zh="吸引力", status="approved")
        source = "Then you pull the curtain back on the larger grill."
        f = verify_block(block(source), "这时你揭开帷幕，展示更大的烤炉。", senses=[sense])
        assert "glossary" not in rules(f)

    def test_struggle_with_used_as_ordinary_verb_is_not_named_concept(self):
        sense = Sense(id="struggle", surface="struggle", zh="挣扎", status="approved")
        source = "Many entrepreneurs struggle with feelings of isolation."
        f = verify_block(block(source), "许多创业者饱受孤立感之苦。", senses=[sense])
        assert "glossary" not in rules(f)

    def test_glossary_term_inside_preserved_endnote_title_is_not_required(self):
        sense = Sense(id="di", surface="disruptive innovation", zh="颠覆性创新", status="approved")
        source = (
            "[1.](#endnote_1) 这里是中文可译的说明。Smith, Jane. "
            "*How Disruptive Innovation Changes Markets*. Boston, 2012."
        )
        zh = (
            "[1.](#endnote_1) 这里是中文说明。Smith, Jane. "
            "*How Disruptive Innovation Changes Markets*. Boston, 2012."
        )
        assert "glossary" not in rules(verify_block(block(source), zh, senses=[sense]))

    def test_glossary_term_inside_nested_bracket_endnote_title_is_not_required(self):
        sense = Sense(id="customer", surface="customer", zh="客户", status="approved")
        source = (
            r"[\[10\]](#part0000_split_013.html__ednref10) Steve Blank, "
            "“First Contact with a Customer,” http://example.test."
        )
        assert "glossary" not in rules(verify_block(block(source), source, senses=[sense]))


# ---------------------------------------------------------------- 译文本身


class TestTranslationSanity:
    def test_empty_translation_is_an_error(self):
        b = block("Some prose here.")
        f = verify_block(b, "")
        assert "empty" in rules(f)
        assert any(x.severity == Severity.ERROR for x in f)

    def test_whitespace_only_translation_is_an_error(self):
        assert "empty" in rules(verify_block(block("Some prose."), "   \n  "))

    def test_untranslated_english_echo_is_flagged(self):
        """模型偶尔直接回抄英文。人眼扫过去像是"还没翻到"，
        但它已经占了那个 id，不校验就永远发现不了。"""
        text = (
            "This is a long English paragraph that the model simply echoed "
            "back instead of translating it into Chinese as instructed."
        )
        b = block(text)
        assert "residual_en" in rules(verify_block(b, text))

    def test_short_latin_translation_is_not_flagged(self):
        """人名、缩写、代码原样保留是合法的，不该误报。"""
        b = block("Ask Christensen.")
        assert "residual_en" not in rules(verify_block(b, "去问 Christensen。"))

    def test_list_of_proper_names_may_remain_unchanged(self):
        source = (
            "- Uber\n- TurboTax\n- Disney\n- Mayo Clinic\n- OnStar\n"
            "- Harvard\n- Match.com\n- OpenTable\n- LinkedIn"
        )
        b = block(source, kind="list")
        assert "residual_en" not in rules(verify_block(b, source))

    def test_mixed_translation_with_terms_is_not_flagged(self):
        b = block("The job to be done framework helps teams decide what to build.")
        zh = "「待办任务（job to be done）」框架帮助团队决定该做什么。"
        assert "residual_en" not in rules(verify_block(b, zh))

    def test_cyrillic_fragment_is_flagged(self):
        b = block("The process produces exactly what it was designed to produce.")
        zh = "这一流程会постро产出它被设计来产出的结果。"
        assert "foreign_script" in rules(verify_block(b, zh))

    def test_suspiciously_short_long_translation_is_flagged(self):
        source = " ".join(["meaningful"] * 50)
        b = block(source)
        assert "suspicious_short" in rules(verify_block(b, "意思。"))

    def test_svg_markup_may_be_preserved_verbatim(self):
        source = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="100%" '
            'height="100%" viewbox="0 0 728 1186">'
        )
        got = rules(verify_block(block(source), source))
        assert "residual_en" not in got
        assert "suspicious_short" not in got

    def test_long_acknowledgement_name_list_is_not_suspiciously_short(self):
        source = (
            ", ".join(f"Person{index} Family{index}" for index in range(30))
            + ", and the rest of the Example team."
        )
        zh = source.replace(", and the rest", "，以及其余")
        assert "suspicious_short" not in rules(verify_block(block(source), zh))

    def test_concise_but_complete_length_is_not_flagged(self):
        source = " ".join(["meaningful"] * 50)
        b = block(source)
        zh = "这是一段长度足以表明模型没有把大部分内容截断或遗漏的完整中文译文。"
        assert "suspicious_short" not in rules(verify_block(b, zh))

    def test_bibliographic_endnote_may_keep_english_title(self):
        source = (
            "[1.](#Introduction.xhtml_endnote_ref_1) Smith, Jane. "
            "“A Long English Article Title.” *Research Review*, October 2015."
        )
        zh = (
            "[1.](#Introduction.xhtml_endnote_ref_1) Smith, Jane. "
            "“A Long English Article Title.” *Research Review*，2015 年 10 月。"
        )
        assert "residual_en" not in rules(verify_block(block(source), zh))

    def test_bibliographic_endnote_without_space_after_link_is_allowed(self):
        source = (
            "[2.](#Chapter_3.xhtml_endnote_ref_12_2)Christensen, Clayton M. "
            "“Marketing Malpractice.” *Harvard Business Review*, December 2005."
        )
        zh = source.replace("December 2005", "2005 年 12 月")
        assert "residual_en" not in rules(verify_block(block(source), zh))

    def test_nested_bracket_endnote_reference_may_preserve_english_title(self):
        source = (
            r"[\[4\]](#part0000_split_010.html__ednref4) Donald Norman’s "
            "Human-Centered Design Considered Harmful, https://example.test."
        )
        zh = source.replace("Donald Norman’s", "Donald Norman 的")
        got = rules(verify_block(block(source), zh))
        assert "residual_en" not in got
        assert "suspicious_short" not in got

    def test_thinking_tag_leak_is_flagged(self):
        b = block("Some prose.")
        assert "tag_leak" in rules(verify_block(b, "<thinking>让我想想</thinking>一些散文。"))

    def test_json_fragment_leak_is_flagged(self):
        b = block("Some prose.")
        assert "tag_leak" in rules(verify_block(b, '{"id": "t/c/§ab", "zh": "一些散文。"}'))


# ---------------------------------------------------------------- 整章


class TestChapterVerification:
    def test_reports_per_block_findings(self):
        doc = parse_markdown("# C\n\nIn 1999 things changed.\n\nMore prose.\n", book_slug="t")
        blocks = [b for b in doc.translatable_blocks() if b.kind == "para"]
        t = {blocks[0].id: "有些事变了。", blocks[1].id: "更多散文。"}
        t.update({b.id: "章" for b in doc.translatable_blocks() if b.kind == "heading"})
        rep = verify_chapter(doc, doc.chapter_slugs()[0], t)
        assert blocks[0].id in {f.block_id for f in rep.findings}

    def test_clean_chapter_passes(self):
        doc = parse_markdown("# C\n\nPlain prose here.\n", book_slug="t")
        t = {b.id: "这里是普通散文。" for b in doc.translatable_blocks()}
        rep = verify_chapter(doc, doc.chapter_slugs()[0], t)
        assert rep.ok

    def test_missing_translation_is_an_error(self):
        doc = parse_markdown("# C\n\nPlain prose here.\n", book_slug="t")
        rep = verify_chapter(doc, doc.chapter_slugs()[0], {})
        assert not rep.ok
        assert rep.error_count > 0

    def test_blocks_to_retry_lists_only_errors(self):
        doc = parse_markdown("# C\n\nIn 1999 things changed.\n", book_slug="t")
        para = next(b for b in doc.translatable_blocks() if b.kind == "para")
        t = {b.id: "章" for b in doc.translatable_blocks()}
        t[para.id] = ""  # error
        rep = verify_chapter(doc, doc.chapter_slugs()[0], t)
        assert para.id in rep.blocks_to_retry

    def test_warnings_do_not_block_publication(self):
        doc = parse_markdown("# C\n\nPlain prose here.\n", book_slug="t")
        t = {b.id: "这里是普通散文。" for b in doc.translatable_blocks()}
        rep = verify_chapter(doc, doc.chapter_slugs()[0], t)
        assert rep.ok

    def test_report_renders_as_markdown(self):
        doc = parse_markdown("# C\n\nIn 1999 things changed.\n", book_slug="t")
        t = {b.id: "有些事变了。" for b in doc.translatable_blocks()}
        rep = verify_chapter(doc, doc.chapter_slugs()[0], t)
        md = rep.to_markdown()
        assert "1999" in md or "numbers" in md


# ---------------------------------------------------------------- 兜底


class TestCandidateHarvest:
    def test_repeated_capitalised_phrase_becomes_a_candidate(self):
        """设计里补 Codex 的那个洞：自动抽取没识别出来的术语，
        既不是新词也不冲突也没分歧，永远进不了人工视野。
        翻译时撞见高频名词短语就记账。"""
        from pipeline.verify import harvest_candidates

        texts = ["The Forces of Progress shape the decision."] * 4
        got = harvest_candidates(texts, known_surfaces=set(), min_count=3)
        assert any("Forces of Progress" in c for c in got)

    def test_known_terms_are_not_harvested(self):
        from pipeline.verify import harvest_candidates

        texts = ["The Forces of Progress shape the decision."] * 4
        got = harvest_candidates(texts, known_surfaces={"forces of progress"}, min_count=3)
        assert not any("Forces of Progress" in c for c in got)

    def test_rare_phrase_is_not_harvested(self):
        from pipeline.verify import harvest_candidates

        got = harvest_candidates(
            ["The Forces of Progress matter."], known_surfaces=set(), min_count=3
        )
        assert got == []


class TestTranslationSplitHarvest:
    def test_frequent_two_camp_translation_is_harvested(self):
        from pipeline.verify import harvest_translation_splits

        aligned = []
        for index in range(20):
            source = f"The customer chooses a solution number {index}."
            translation = "客户选择了合适方案。" if index < 11 else "顾客选择了合适方案。"
            aligned.append((source, translation))
        got = harvest_translation_splits(
            aligned, known_surfaces=set(), min_source_blocks=12, min_variant_blocks=4
        )
        customer = next(item for item in got if item.surface == "customer")
        assert {value for value, _ in customer.variants} == {"客户", "顾客"}

    def test_known_surface_is_not_reharvested(self):
        from pipeline.verify import harvest_translation_splits

        aligned = [
            ("The customer chooses this.", "客户选择它。" if i < 6 else "顾客选择它。")
            for i in range(12)
        ]
        got = harvest_translation_splits(
            aligned,
            known_surfaces={"customer"},
            min_source_blocks=12,
            min_variant_blocks=4,
            known_translations={"customer": ("客户", "顾客")},
        )
        customer = next(item for item in got if item.surface == "customer")
        assert customer.known_surface

    def test_known_surface_keeps_single_outlier_and_more_than_two_variants(self):
        from pipeline.verify import harvest_translation_splits

        translations = ["这是成为目标。"] * 24 + ["这是存在目标。"] + ["这是达成目标。"] * 4
        aligned = [("The goal shapes progress.", zh) for zh in translations]
        got = harvest_translation_splits(
            aligned,
            known_surfaces={"goal"},
            known_translations={"goal": ("成为目标", "存在目标", "达成目标")},
            min_source_blocks=12,
            min_variant_blocks=4,
            min_known_variant_blocks=1,
            max_variants=5,
        )
        goal = next(item for item in got if item.surface == "goal")
        assert goal.known_surface
        assert len(goal.variants) >= 3


def test_mixed_script_inside_english_term_is_rejected():
    b = block("We created Customer Job Theory (JTBD).")
    sense = Sense(id="job", surface="job", zh="任务", status="approved")
    findings = verify_block(
        b,
        "我们创立了 Customer 任务 Theory（JTBD）。",
        senses=[sense],
    )
    assert "mixed_script_term" in rules(findings)


def test_parenthetical_mixed_term_is_rejected_without_glossary_alias():
    b = block("We created Customer Job Theory (JTBD).")
    findings = verify_block(b, "我们创立了客户任务理论（Customer 任务 Theory，JTBD）。")
    assert "mixed_script_term" in rules(findings)


def test_normal_latin_names_joined_by_chinese_are_not_mixed_terms():
    b = block("Dan and Clarity built it together.")
    sense = Sense(id="job", surface="job", zh="任务", status="approved")
    findings = verify_block(b, "Dan 与 Clarity 共同打造了它。", senses=[sense])
    assert "mixed_script_term" not in rules(findings)


def test_parenthetical_chinese_clause_between_names_is_not_a_mixed_term():
    b = block("Dan sold it to Fundable in 2015.")
    findings = verify_block(b, "这笔交易随后完成了（Dan 把它卖给了 Fundable）。")
    assert "mixed_script_term" not in rules(findings)


def test_surface_alias_and_forbidden_translation_are_enforced():
    sense = Sense(
        id="play",
        surface="Jobs to Be Done play",
        surface_aliases=("JTBD play", "PLAY"),
        zh="实战方法",
        forbidden_zh=("打法", "玩法"),
        status="approved",
    )
    source = "**PLAY** **Conduct Jobs Interviews**"
    findings = verify_block(block(source), "**打法** **开展任务访谈**", [sense])
    assert "glossary" in rules(findings)
    assert "glossary.forbidden" in rules(findings)


def test_duplicate_source_translation_inconsistency_is_reported():
    from pipeline.document import Document
    from pipeline.verify import verify_corpus_consistency

    first = block("LEARN MORE ABOUT THIS PLAY")
    second = Block(
        kind=first.kind,
        text=first.text,
        chapter="two",
        id="book/two/§2",
    )
    doc = Document(book_slug="book", blocks=[first, second])
    report = verify_corpus_consistency(
        doc,
        {first.id: "深入了解这一实战方法", second.id: "深入了解这一招"},
    )
    assert not report.ok
    assert report.findings[0].rule == "consistency.duplicate_source"
