"""供应商无关的调用层。

换一家中转站不该改流水线。实测两家的差异就已经覆盖了大部分情况：
一家只认 anthropic 协议、bearer 头、带 `anthropic/` 前缀的模型名、
输出上限被砍到 8192；另一家 openai 协议、x-api-key。所以协议、鉴权头、
路径前缀、模型名、输出上限，全部配置化。

两类错误必须分开：**权限和参数错误重试多少次都一样**，只会拖慢发现
问题的速度；限流和上游抖动才值得退避重试。
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass, field

import requests

from .chunker import Chunk
from .glossary import Sense

__all__ = [
    "ProviderConfig", "TranslationClient", "ChunkResult",
    "PermanentError", "TransientError",
]


class PermanentError(RuntimeError):
    """重试也没用：鉴权、权限、模型名错、请求格式错。"""


class TransientError(RuntimeError):
    """值得重试：限流、上游 5xx、超时、回包解析不出来。"""


# ------------------------------------------------------------------ 配置

def _normalize_base(raw: str) -> str:
    b = (raw or "").strip().rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    return b


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    api_key: str = field(repr=False)
    protocol: str = "anthropic"        # anthropic | openai
    auth: str = "bearer"               # bearer | x-api-key
    path_prefix: str = "/v1"
    model: str = ""
    max_output_tokens: int = 8192
    concurrency: int = 4
    timeout: int = 180
    max_retries: int = 4
    supports_cache: bool = False

    def __post_init__(self):
        object.__setattr__(self, "base_url", _normalize_base(self.base_url))

    def headers(self) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if self.auth == "x-api-key":
            h["x-api-key"] = self.api_key
        else:
            h["Authorization"] = f"Bearer {self.api_key}"
        if self.protocol == "anthropic":
            h["anthropic-version"] = "2023-06-01"
        return h

    def endpoint(self) -> str:
        tail = "/messages" if self.protocol == "anthropic" else "/chat/completions"
        return f"{self.base_url}{self.path_prefix}{tail}"

    def masked_key(self) -> str:
        k = self.api_key or ""
        if len(k) <= 12:
            return "***"
        return f"{k[:6]}…{k[-4:]}"

    def redacted(self) -> dict:
        return {
            "base_url": self.base_url, "api_key": self.masked_key(),
            "protocol": self.protocol, "auth": self.auth,
            "path_prefix": self.path_prefix, "model": self.model,
            "max_output_tokens": self.max_output_tokens,
            "concurrency": self.concurrency, "timeout": self.timeout,
            "max_retries": self.max_retries, "supports_cache": self.supports_cache,
        }

    def __repr__(self) -> str:
        return f"ProviderConfig({self.redacted()})"


# ------------------------------------------------------------------ 结果

@dataclass(frozen=True)
class ChunkResult:
    chunk_id: str
    translations: dict[str, str]
    usage: dict
    attempts: int = 1
    missing_ids: tuple[str, ...] = ()
    extra_ids: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        return not self.missing_ids and not self.extra_ids


# ------------------------------------------------------------------ 传输

_HTTP_LOCAL = threading.local()


def _http_session() -> requests.Session:
    """每个工作线程复用自己的连接池，避免跨线程共享 Session 状态。"""
    session = getattr(_HTTP_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        _HTTP_LOCAL.session = session
    return session

def _http_transport(url: str, headers: dict, body: dict, timeout: int):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        response = _http_session().post(
            url,
            data=data,
            headers=headers,
            timeout=(10, timeout),
        )
    except requests.RequestException as exc:
        raise OSError(f"HTTP transport failed: {exc}") from exc

    try:
        return response.status_code, response.json()
    except requests.exceptions.JSONDecodeError:
        return response.status_code, {"_raw": response.text[:2000]}


# ------------------------------------------------------------------ 解析

_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)


def _extract_json(text: str) -> dict | None:
    """模型偶尔会在 JSON 前后加一句话或套个围栏。不该因此整块重跑。"""
    t = (text or "").strip()
    if not t:
        return None
    for candidate in _json_candidates(t):
        try:
            d = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and isinstance(d.get("translations"), list):
            return d
    # 部分中转模型会在 zh 字符串中输出未转义的 ASCII 引号，例如
    # `"zh":"我们会"雇"一个产品"`。整体不是合法 JSON，但 id/zh 边界仍然
    # 明确；只恢复这两个白名单字段，绝不尝试执行通用的“宽松 JSON”。
    repaired = _extract_translation_pairs(t)
    if repaired:
        return {"translations": repaired}
    return None


def _json_candidates(t: str):
    yield t
    for m in _FENCE.finditer(t):
        yield m.group(1)
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        yield t[i:j + 1]


_TRANSLATION_PAIR = re.compile(
    r'"id"\s*:\s*"(?P<id>[^"\r\n]+)"\s*,\s*'
    r'"zh"\s*:\s*"(?P<zh>.*?)"\s*}'
    r'(?=\s*(?:,|\]))',
    re.S,
)
_JSON_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|[\"\\/bfnrt])")


def _unescape_json_fragment(value: str) -> str:
    simple = {
        '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
        "n": "\n", "r": "\r", "t": "\t",
    }

    def replace(match: re.Match) -> str:
        token = match.group(1)
        if token.startswith("u"):
            return chr(int(token[1:], 16))
        return simple[token]

    return _JSON_ESCAPE.sub(replace, value)


def _extract_translation_pairs(text: str) -> list[dict[str, str]]:
    return [
        {
            "id": _unescape_json_fragment(match.group("id")),
            "zh": _unescape_json_fragment(match.group("zh")),
        }
        for match in _TRANSLATION_PAIR.finditer(text)
    ]


def _reply_text(protocol: str, payload: dict) -> str:
    if protocol == "anthropic":
        parts = [b.get("text", "") for b in payload.get("content") or []
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts)
    choices = payload.get("choices") or [{}]
    return str((choices[0].get("message") or {}).get("content") or "")


def _error_message(payload: dict) -> str:
    e = payload.get("error")
    if isinstance(e, dict):
        return str(e.get("message") or e.get("type") or e)
    if e:
        return str(e)
    return str(payload.get("_raw") or payload)[:300]


# ------------------------------------------------------------------ 客户端

TRANSLATE_SYSTEM = """你是一名专业译者，把英文书翻译成简体中文，供读者精读。

硬性要求：
1. 只输出 JSON，形如 {"translations":[{"id":"...","zh":"..."}]}。不要输出英文原文。
2. 每个请求的段落都必须译，id 原样返回，不增不减。
3. 准确优先，不增译、不省略、不文学化改写。
4. 术语表给出的译名必须使用；未给出的术语保持全书一致。
5. 保留原文里的数字、引文标记、链接、脚注编号、markdown 标记。
6. 上下文段落（prev/next）只供理解，不要翻译它们。
7. 译文中的引号一律使用中文弯引号“”，不要在 JSON 字符串内部输出未转义的 ASCII 双引号。
8. 除人名、机构名、书刊名、产品名和缩写外，不得混入其他语言或无关字符。
"""


class TranslationClient:
    def __init__(self, cfg: ProviderConfig, transport=None,
                 max_retries: int | None = None, backoff_base: float = 1.0):
        self.cfg = cfg
        self._transport = transport or _http_transport
        self.max_retries = max_retries if max_retries is not None else cfg.max_retries
        self.backoff_base = backoff_base

    # -------------------------------------------------- payload

    def requested_ids(self, chunk: Chunk) -> tuple[str, ...]:
        """待译 ID。上下文块绝不出现在这里——混进来会重复翻译并在装配时重段。"""
        return tuple(b.id for b in chunk.blocks)

    def build_payload(self, chunk: Chunk, style_card: str, chapter_card: str,
                      senses: list[Sense]) -> dict:
        return {
            "chunk_id": chunk.id,
            "style_card": style_card,
            "chapter_card": chapter_card,
            "glossary": [
                {"en": s.surface, "zh": s.zh, "first_use": s.display_first_use,
                 "author": s.author, "note": s.decision}
                for s in senses
            ],
            "prev_context_en": [b.text for b in chunk.prev_context],
            "next_context_en": [b.text for b in chunk.next_context],
            "paragraphs": [{"id": b.id, "en": b.text} for b in chunk.blocks],
        }

    def build_body(self, chunk: Chunk, payload: dict) -> dict:
        budget = max(2048, chunk.estimated_output_tokens * 2)
        max_tokens = min(self.cfg.max_output_tokens, budget)
        user = json.dumps(payload, ensure_ascii=False)
        if self.cfg.protocol == "anthropic":
            return {
                "model": self.cfg.model,
                "max_tokens": max_tokens,
                "system": TRANSLATE_SYSTEM,
                "messages": [{"role": "user", "content": user}],
            }
        return {
            "model": self.cfg.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": TRANSLATE_SYSTEM},
                {"role": "user", "content": user},
            ],
        }

    # -------------------------------------------------- call

    def translate_chunk(self, chunk: Chunk, style_card: str, chapter_card: str,
                        senses: list[Sense]) -> ChunkResult:
        overall_started = time.monotonic()
        payload = self.build_payload(chunk, style_card, chapter_card, senses)
        body = self.build_body(chunk, payload)
        wanted = set(self.requested_ids(chunk))

        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                status, resp = self._transport(
                    self.cfg.endpoint(), self.cfg.headers(), body, self.cfg.timeout
                )
            except Exception as e:                        # noqa: BLE001
                last = TransientError(f"传输失败：{type(e).__name__}: {e}")
                self._sleep(attempt)
                continue

            if status == 200:
                reply = _reply_text(self.cfg.protocol, resp)
                parsed = _extract_json(reply)
                if parsed is None:
                    last = TransientError(
                        f"{chunk.id}：回包里找不到可解析的 JSON（第 {attempt} 次；"
                        f"reply_chars={len(reply)}；stop={resp.get('stop_reason', '')}）"
                    )
                    self._sleep(attempt)
                    continue
                got = {
                    str(t.get("id")): str(t.get("zh", ""))
                    for t in parsed["translations"]
                    if isinstance(t, dict) and t.get("id")
                }
                usage = dict(resp.get("usage") or {})
                usage["client_elapsed_seconds"] = round(
                    time.monotonic() - overall_started, 3
                )
                return ChunkResult(
                    chunk_id=chunk.id,
                    translations={k: v for k, v in got.items() if k in wanted},
                    usage=usage,
                    attempts=attempt,
                    missing_ids=tuple(sorted(wanted - got.keys())),
                    extra_ids=tuple(sorted(got.keys() - wanted)),
                )

            msg = _error_message(resp)
            # 这家网关在突发请求时偶尔返回 HTTP 403 + 空 body；同一凭据紧接着
            # 重试即可成功，性质不同于带明确错误信息的权限拒绝。只对白名单形状
            # （无 error 且 _raw 为空）重试，避免掩盖真正的鉴权/权限问题。
            blank_gateway_403 = (
                status == 403
                and not resp.get("error")
                and resp.get("_raw") == ""
            )
            if status == 429 or status >= 500 or blank_gateway_403:
                last = TransientError(f"{chunk.id}：HTTP {status} {msg}")
                self._sleep(attempt)
                continue
            raise PermanentError(f"{chunk.id}：HTTP {status} {msg}")

        raise last or TransientError(f"{chunk.id}：重试 {self.max_retries} 次仍失败")

    def _sleep(self, attempt: int) -> None:
        if self.backoff_base <= 0:
            return
        delay = self.backoff_base * (2 ** (attempt - 1))
        time.sleep(min(delay, 60) * (0.7 + 0.6 * random.random()))
