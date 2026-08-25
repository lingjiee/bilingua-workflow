"""供应商无关的调用层。

这层存在的唯一理由：换一家中转站不该改流水线。实测两家的差异已经
覆盖了大部分情况——一家只认 anthropic 协议 + bearer 头 + 带前缀的模型名，
另一家 openai 协议 + x-api-key。协议、鉴权头、路径前缀、模型名全部配置化。

用假传输层测，不打真网络。
"""

from __future__ import annotations

import json

import pytest

from pipeline.chunker import chunk_document
from pipeline.client import (
    PermanentError,
    ProviderConfig,
    TransientError,
    TranslationClient,
)
from pipeline.document import parse_markdown

# ------------------------------------------------------------ 假传输层


class FakeTransport:
    """按脚本返回。每次调用记录请求，供断言。"""

    def __init__(self, script):
        self.script = list(script)  # [(status, payload), ...]
        self.calls: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        if not self.script:
            raise AssertionError("传输层被调用的次数超出脚本")
        return self.script.pop(0)


def anthropic_reply(mapping: dict[str, str], usage=None):
    payload = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"translations": [{"id": k, "zh": v} for k, v in mapping.items()]},
                    ensure_ascii=False,
                ),
            }
        ],
        "usage": usage or {"input_tokens": 100, "output_tokens": 50},
    }
    return 200, payload


def openai_reply(mapping: dict[str, str]):
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"translations": [{"id": k, "zh": v} for k, v in mapping.items()]},
                        ensure_ascii=False,
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }
    return 200, payload


def make_chunk():
    doc = parse_markdown("# Chapter One\n\nFirst para.\n\nSecond para.\n", book_slug="t")
    return chunk_document(doc, target_words=10_000)[0]


CFG = ProviderConfig(
    base_url="https://relay.example.com",
    api_key="secret-key-123456",
    model="provider/example-model",
)


# ------------------------------------------------------------ 配置


class TestProviderConfig:
    def test_base_url_is_normalized(self):
        for raw in ("https://x.ai", "https://x.ai/", "https://x.ai/v1", "https://x.ai/v1/"):
            assert ProviderConfig(base_url=raw, api_key="k").base_url == "https://x.ai"

    def test_bearer_auth_header(self):
        h = ProviderConfig(base_url="https://x.ai", api_key="k", auth="bearer").headers()
        assert h["Authorization"] == "Bearer k"
        assert "x-api-key" not in h

    def test_x_api_key_auth_header(self):
        h = ProviderConfig(base_url="https://x.ai", api_key="k", auth="x-api-key").headers()
        assert h["x-api-key"] == "k"
        assert "Authorization" not in h

    def test_anthropic_version_header_only_for_anthropic(self):
        a = ProviderConfig(base_url="https://x.ai", api_key="k", protocol="anthropic")
        o = ProviderConfig(base_url="https://x.ai", api_key="k", protocol="openai")
        assert "anthropic-version" in a.headers()
        assert "anthropic-version" not in o.headers()

    def test_endpoint_per_protocol(self):
        a = ProviderConfig(base_url="https://x.ai", api_key="k", protocol="anthropic")
        o = ProviderConfig(base_url="https://x.ai", api_key="k", protocol="openai")
        assert a.endpoint() == "https://x.ai/v1/messages"
        assert o.endpoint() == "https://x.ai/v1/chat/completions"

    def test_path_prefix_is_configurable(self):
        c = ProviderConfig(base_url="https://x.ai", api_key="k", path_prefix="/api/v2")
        assert c.endpoint() == "https://x.ai/api/v2/messages"

    def test_redacted_never_leaks_the_key(self):
        c = ProviderConfig(base_url="https://relay.example", api_key="test-secret-value")
        blob = json.dumps(c.redacted())
        assert "test-secret-value" not in blob
        assert "test-s" in blob or "***" in blob

    def test_repr_never_leaks_the_key(self):
        c = ProviderConfig(base_url="https://relay.example", api_key="test-secret-value")
        assert "super-secret" not in repr(c)


# ------------------------------------------------------------ 请求构造


class TestRequestShape:
    def test_anthropic_request_carries_model_and_ceiling(self):
        t = FakeTransport([anthropic_reply({})])
        cfg = ProviderConfig(
            base_url="https://x.ai",
            api_key="k",
            model="provider/example-model",
            max_output_tokens=8192,
        )
        c = TranslationClient(cfg, transport=t)
        ch = make_chunk()
        try:
            c.translate_chunk(ch, style_card="S", chapter_card="C", senses=[])
        except Exception:
            pass
        body = t.calls[0]["body"]
        assert body["model"] == "provider/example-model"
        assert body["max_tokens"] <= 8192

    def test_openai_request_uses_messages_array(self):
        t = FakeTransport([openai_reply({})])
        cfg = ProviderConfig(base_url="https://x.ai", api_key="k", protocol="openai", model="gpt-x")
        c = TranslationClient(cfg, transport=t)
        try:
            c.translate_chunk(make_chunk(), style_card="S", chapter_card="C", senses=[])
        except Exception:
            pass
        body = t.calls[0]["body"]
        assert isinstance(body["messages"], list)
        assert body["messages"][0]["role"] == "system"

    def test_payload_contains_only_translatable_block_ids(self):
        ch = make_chunk()
        t = FakeTransport([anthropic_reply({b.id: "译" for b in ch.blocks})])
        c = TranslationClient(CFG, transport=t)
        c.translate_chunk(ch, style_card="S", chapter_card="C", senses=[])
        sent = json.dumps(t.calls[0]["body"], ensure_ascii=False)
        for b in ch.blocks:
            assert b.id in sent

    def test_context_blocks_are_sent_but_not_requested_for_translation(self):
        doc = parse_markdown(
            "# C\n\n" + "\n\n".join(f"Para number {i} here." for i in range(10)), book_slug="t"
        )
        chunks = chunk_document(doc, target_words=8, context_blocks=2)
        ch = next(c for c in chunks if c.prev_context)
        t = FakeTransport([anthropic_reply({b.id: "译" for b in ch.blocks})])
        c = TranslationClient(CFG, transport=t)
        c.translate_chunk(ch, style_card="S", chapter_card="C", senses=[])
        requested = c.requested_ids(ch)
        for b in ch.prev_context:
            assert b.id not in requested, "上下文块不能出现在待译 ID 列表里"

    def test_glossary_senses_are_injected(self):
        from pipeline.glossary import Sense

        s = Sense(id="j", surface="job", zh="任务", status="approved")
        ch = make_chunk()
        t = FakeTransport([anthropic_reply({b.id: "译" for b in ch.blocks})])
        c = TranslationClient(CFG, transport=t)
        c.translate_chunk(ch, style_card="S", chapter_card="C", senses=[s])
        sent = json.dumps(t.calls[0]["body"], ensure_ascii=False)
        assert "任务" in sent and "job" in sent


# ------------------------------------------------------------ 重试


class TestRetry:
    def test_retries_on_502_then_succeeds(self):
        ch = make_chunk()
        ok = anthropic_reply({b.id: "译" for b in ch.blocks})
        t = FakeTransport(
            [
                (502, {"error": {"message": "upstream"}}),
                (502, {"error": {"message": "upstream"}}),
                ok,
            ]
        )
        c = TranslationClient(CFG, transport=t, backoff_base=0)
        res = c.translate_chunk(ch, style_card="S", chapter_card="C", senses=[])
        assert res.attempts == 3
        assert len(res.translations) == len(ch.blocks)

    def test_retries_on_429(self):
        ch = make_chunk()
        ok = anthropic_reply({b.id: "译" for b in ch.blocks})
        t = FakeTransport([(429, {"error": {"message": "rate"}}), ok])
        c = TranslationClient(CFG, transport=t, backoff_base=0)
        assert c.translate_chunk(ch, style_card="S", chapter_card="C", senses=[]).attempts == 2

    def test_does_not_retry_on_403(self):
        """鉴权/权限错误重试多少次都一样，只会拖慢发现问题的速度。"""
        t = FakeTransport([(403, {"error": {"message": "forbidden"}})])
        c = TranslationClient(CFG, transport=t, backoff_base=0)
        with pytest.raises(PermanentError):
            c.translate_chunk(make_chunk(), style_card="S", chapter_card="C", senses=[])
        assert len(t.calls) == 1

    def test_retries_on_blank_gateway_403(self):
        """实测突发请求会收到空 body 403；它是网关抖动，不是权限拒绝。"""
        ch = make_chunk()
        ok = anthropic_reply({b.id: "译" for b in ch.blocks})
        t = FakeTransport([(403, {"_raw": ""}), ok])
        c = TranslationClient(CFG, transport=t, backoff_base=0)
        result = c.translate_chunk(ch, style_card="S", chapter_card="C", senses=[])
        assert result.attempts == 2
        assert len(t.calls) == 2

    def test_does_not_retry_on_400(self):
        t = FakeTransport([(400, {"error": {"message": "bad model"}})])
        c = TranslationClient(CFG, transport=t, backoff_base=0)
        with pytest.raises(PermanentError):
            c.translate_chunk(make_chunk(), style_card="S", chapter_card="C", senses=[])
        assert len(t.calls) == 1

    def test_gives_up_after_max_retries(self):
        t = FakeTransport([(502, {"error": {"message": "x"}})] * 3)
        c = TranslationClient(CFG, transport=t, max_retries=3, backoff_base=0)
        with pytest.raises(TransientError):
            c.translate_chunk(make_chunk(), style_card="S", chapter_card="C", senses=[])
        assert len(t.calls) == 3

    def test_permanent_error_message_reaches_the_caller(self):
        t = FakeTransport(
            [
                (
                    403,
                    {
                        "error": {
                            "message": "Upstream access forbidden, please contact administrator"
                        }
                    },
                )
            ]
        )
        c = TranslationClient(CFG, transport=t, backoff_base=0)
        with pytest.raises(PermanentError) as e:
            c.translate_chunk(make_chunk(), style_card="S", chapter_card="C", senses=[])
        assert "contact administrator" in str(e.value)


# ------------------------------------------------------------ 回包解析


class TestResponseParsing:
    def test_parses_anthropic_reply(self):
        ch = make_chunk()
        want = {b.id: f"译{i}" for i, b in enumerate(ch.blocks)}
        c = TranslationClient(CFG, transport=FakeTransport([anthropic_reply(want)]))
        res = c.translate_chunk(ch, style_card="S", chapter_card="C", senses=[])
        assert res.translations == want

    def test_parses_openai_reply(self):
        ch = make_chunk()
        want = {b.id: f"译{i}" for i, b in enumerate(ch.blocks)}
        cfg = ProviderConfig(base_url="https://x.ai", api_key="k", protocol="openai", model="m")
        c = TranslationClient(cfg, transport=FakeTransport([openai_reply(want)]))
        res = c.translate_chunk(ch, style_card="S", chapter_card="C", senses=[])
        assert res.translations == want

    def test_tolerates_json_wrapped_in_prose(self):
        """模型偶尔会在 JSON 前后加一句话。不该因此整块重跑。"""
        ch = make_chunk()
        want = {b.id: "译" for b in ch.blocks}
        inner = json.dumps(
            {"translations": [{"id": k, "zh": v} for k, v in want.items()]}, ensure_ascii=False
        )
        payload = {
            "content": [{"type": "text", "text": f"好的，这是译文：\n```json\n{inner}\n```\n"}],
            "usage": {},
        }
        c = TranslationClient(CFG, transport=FakeTransport([(200, payload)]))
        assert (
            c.translate_chunk(ch, style_card="S", chapter_card="C", senses=[]).translations == want
        )

    def test_repairs_unescaped_ascii_quotes_inside_translation(self):
        """中转模型偶尔用英文引号包术语，却忘了按 JSON 规则转义。"""
        ch = make_chunk()
        pairs = []
        want = {}
        for index, b in enumerate(ch.blocks):
            zh = f'顾客会"雇用"产品 {index}'
            want[b.id] = zh
            pairs.append(f'{{"id":"{b.id}","zh":"{zh}"}}')
        malformed = '{"translations":[' + ",".join(pairs) + "]}"
        payload = {"content": [{"type": "text", "text": malformed}], "usage": {}}
        client = TranslationClient(CFG, transport=FakeTransport([(200, payload)]))
        result = client.translate_chunk(ch, style_card="S", chapter_card="C", senses=[])
        assert result.translations == want

    def test_missing_ids_are_reported(self):
        ch = make_chunk()
        partial = {ch.blocks[0].id: "译"}
        c = TranslationClient(CFG, transport=FakeTransport([anthropic_reply(partial)]))
        res = c.translate_chunk(ch, style_card="S", chapter_card="C", senses=[])
        assert res.missing_ids
        assert set(res.missing_ids) == {b.id for b in ch.blocks[1:]}

    def test_extra_ids_are_reported(self):
        ch = make_chunk()
        m = {b.id: "译" for b in ch.blocks}
        m["bogus/id/§dead"] = "凭空多出来的"
        c = TranslationClient(CFG, transport=FakeTransport([anthropic_reply(m)]))
        res = c.translate_chunk(ch, style_card="S", chapter_card="C", senses=[])
        assert res.extra_ids == ("bogus/id/§dead",)

    def test_complete_result_reports_clean(self):
        ch = make_chunk()
        m = {b.id: "译" for b in ch.blocks}
        c = TranslationClient(CFG, transport=FakeTransport([anthropic_reply(m)]))
        res = c.translate_chunk(ch, style_card="S", chapter_card="C", senses=[])
        assert res.is_complete

    def test_unparseable_reply_is_transient_not_a_crash(self):
        payload = {"content": [{"type": "text", "text": "抱歉我不明白"}], "usage": {}}
        c = TranslationClient(
            CFG, transport=FakeTransport([(200, payload)] * 3), max_retries=3, backoff_base=0
        )
        with pytest.raises(TransientError):
            c.translate_chunk(make_chunk(), style_card="S", chapter_card="C", senses=[])

    def test_usage_is_captured(self):
        ch = make_chunk()
        m = {b.id: "译" for b in ch.blocks}
        c = TranslationClient(
            CFG,
            transport=FakeTransport(
                [anthropic_reply(m, usage={"input_tokens": 1234, "output_tokens": 567})]
            ),
        )
        res = c.translate_chunk(ch, style_card="S", chapter_card="C", senses=[])
        assert res.usage["input_tokens"] == 1234
        assert res.usage["output_tokens"] == 567
        assert res.usage["client_elapsed_seconds"] >= 0
