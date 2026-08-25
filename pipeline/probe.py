#!/usr/bin/env python3
"""probe — 摸清一个 API 端点到底支持什么，再决定流水线怎么写。

第三方中转站差异极大：接口形状（Anthropic / OpenAI）、鉴权头、模型名前缀、
是否支持 Batch、是否认 prompt caching，全都不一样。靠问用户不如直接试。

顺序很重要：**先取模型清单，再用真实模型名试形状**。用猜的模型名试形状，
拿回来的 400 分不清是"形状不对"还是"模型名不对"。

使用 requests 作为传输层。该中转站会拦截 Python urllib 的请求指纹，
而 requests 已通过模型清单和真实消息端点验证。

    python3 pipeline/probe.py

读 ../.env。**永不打印 key。**
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 60
_HTTP_SESSION = requests.Session()


# ----------------------------------------------------------------- env


def load_env() -> dict[str, str]:
    env_path = ROOT / ".env"
    if not env_path.exists():
        sys.exit(
            f"没有 {env_path}\n"
            f"先 cp {ROOT}/.env.example {env_path}，填好 TRANSLATE_BASE_URL 和 TRANSLATE_API_KEY。"
        )
    env: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip("'\"")
    for required in ("TRANSLATE_BASE_URL", "TRANSLATE_API_KEY"):
        if not env.get(required):
            sys.exit(f".env 里 {required} 是空的")
    return env


def normalize_base(raw: str) -> str:
    """用户填的地址可能带 /v1、可能不带，可能有尾斜杠。统一成不带 /v1 的根。"""
    base = raw.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def mask(key: str) -> str:
    """key 只以这种形式出现在输出里。"""
    if len(key) <= 12:
        return "*" * len(key)
    return f"{key[:6]}…{key[-4:]}  ({len(key)} 字符)"


# ----------------------------------------------------------------- http


def call(url: str, headers: dict[str, str], body: dict | None = None, method: str = "GET"):
    """返回 (status, parsed)。网络层失败返回 (None, 错误字符串)。"""
    try:
        response = _HTTP_SESSION.request(
            method,
            url,
            headers=headers,
            json=body,
            timeout=(10, TIMEOUT),
        )
    except requests.RequestException as exc:
        return None, f"{type(exc).__name__}: {exc}"

    try:
        payload = response.json()
    except requests.exceptions.JSONDecodeError:
        payload = response.text[:500]
    return response.status_code, payload


def err_text(payload) -> str:
    if isinstance(payload, dict):
        e = payload.get("error")
        if isinstance(e, dict):
            return str(e.get("message") or e.get("type") or e)[:200]
        if e:
            return str(e)[:200]
        return str(payload)[:200]
    return str(payload)[:200]


AUTH_STYLES = {
    "bearer": lambda k: {"Authorization": f"Bearer {k}"},
    "x-api-key": lambda k: {"x-api-key": k},
}


# ----------------------------------------------------------------- 1 models


def probe_models(base: str, key: str) -> tuple[list[str], str, str]:
    """先摸模型清单。顺带确定 /v1 前缀和鉴权头。"""
    print("\n[1/4] 模型清单与鉴权头")
    for prefix in ("/v1", ""):
        for auth, mk in AUTH_STYLES.items():
            url = f"{base}{prefix}/models"
            status, payload = call(url, {**mk(key), "content-type": "application/json"})
            if status == 200 and isinstance(payload, dict):
                items = payload.get("data") or payload.get("models") or []
                ids = [str(m.get("id")) for m in items if isinstance(m, dict) and m.get("id")]
                if ids:
                    print(f"  ✓ {url}  +  {auth}  → {len(ids)} 个模型")
                    return ids, prefix, auth
            print(f"  · {url}  +  {auth}  → {status or 'ERR'}  {err_text(payload)[:80]}")
    print("  ✗ 取不到模型清单")
    return [], "/v1", "bearer"


def pick_models(ids: list[str]) -> dict[str, str]:
    """从清单里挑出翻译要用的两档模型。"""
    claude = [m for m in ids if "claude" in m.lower()]

    def first(*needles: str) -> str:
        for n in needles:
            for m in claude:
                if n in m.lower():
                    return m
        return ""

    return {
        "translate": first("opus-5", "opus-4.8", "opus", "sonnet-5", "sonnet"),
        "cheap": first("haiku", "sonnet-5", "sonnet"),
        "all_claude": claude,
    }


# ----------------------------------------------------------------- 2 shape


def probe_shape(base: str, key: str, prefix: str, model: str) -> dict:
    """用真实模型名试两种协议、两种鉴权头。"""
    print(f"\n[2/4] 接口协议（用真实模型名 {model}）")
    if not model:
        print("  ✗ 没有可用的 Claude 模型，跳过")
        return {}

    attempts = []
    for auth, mk in AUTH_STYLES.items():
        attempts.append(
            (
                "anthropic",
                auth,
                f"{base}{prefix}/messages",
                {**mk(key), "anthropic-version": "2023-06-01", "content-type": "application/json"},
                {
                    "model": model,
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
        )
    for auth, mk in AUTH_STYLES.items():
        attempts.append(
            (
                "openai",
                auth,
                f"{base}{prefix}/chat/completions",
                {**mk(key), "content-type": "application/json"},
                {
                    "model": model,
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
        )

    winner = {}
    for shape, auth, url, headers, body in attempts:
        status, payload = call(url, headers, body, "POST")
        ok = status == 200
        mark = "✓" if ok else "·"
        print(
            f"  {mark} {shape:9s} + {auth:9s} → {status or 'ERR'}"
            f"  {'' if ok else err_text(payload)[:110]}"
        )
        if ok and not winner:
            winner = {
                "shape": shape,
                "auth": auth,
                "url": url,
                "headers": headers,
                "sample": payload,
            }
    if not winner:
        print("  ✗ 四种组合都没通")
    return winner


def extract_text(shape: str, payload) -> str:
    if not isinstance(payload, dict):
        return ""
    if shape == "anthropic":
        for b in payload.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "text":
                return str(b.get("text", ""))[:80]
    else:
        ch = (payload.get("choices") or [{}])[0]
        return str((ch.get("message") or {}).get("content", ""))[:80]
    return ""


# ----------------------------------------------------------------- 3 batch


def probe_batch(base: str, key: str, prefix: str, auth: str, shape: str) -> bool:
    """D5 整个建立在 Batch API 上。中转站多半不支持——必须查实。"""
    print("\n[3/4] Batch API（设计 D5 依赖：五折 + 天然断点续传）")
    headers = {
        **AUTH_STYLES[auth](key),
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    paths = [f"{prefix}/messages/batches", f"{prefix}/batches"]
    for p in paths:
        status, payload = call(f"{base}{p}", headers)
        if status == 200:
            print(f"  ✓ {p} 可用 —— D5 成立")
            return True
        print(f"  · {p} → {status or 'ERR'}  {err_text(payload)[:80]}")
    print("    → D5 要改：并发同步调用 + 自建断点续传，且没有五折")
    return False


# ----------------------------------------------------------------- 4 cache


def probe_caching(base: str, key: str, prefix: str, auth: str, shape: str, model: str) -> bool:
    """D3 的成本论证一半靠 prompt caching。一致性论证不受影响。"""
    print("\n[4/4] Prompt caching（设计 D3 的成本论证依赖）")
    if shape != "anthropic":
        print("  · OpenAI 协议没有 cache_control 字段 → 成本论证减弱，一致性论证不变")
        return False
    headers = {
        **AUTH_STYLES[auth](key),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    filler = (
        "这是一段用来撑过 prompt cache 最小长度门槛的填充文本，"
        "内容本身没有意义，只为凑够 token 数。"
    ) * 90
    body = {
        "model": model,
        "max_tokens": 16,
        "system": [{"type": "text", "text": filler, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "ping"}],
    }
    # 连打两次：第一次写缓存，第二次才可能命中
    last_usage = {}
    for i in (1, 2):
        status, payload = call(f"{base}{prefix}/messages", headers, body, "POST")
        if status != 200:
            print(f"  ✗ 第 {i} 次带 cache_control 的请求 → {status}  {err_text(payload)[:110]}")
            print("    → 不认这个字段。一致性论证仍成立，成本论证减弱")
            return False
        last_usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
        print(f"  · 第 {i} 次 usage: {last_usage}")
    hit = int(last_usage.get("cache_read_input_tokens") or 0)
    if hit > 0:
        print(f"  ✓ 第二次命中缓存 {hit} tokens —— D3 成本论证成立")
        return True
    print("  ~ 接受了 cache_control 但第二次没有 cache_read —— 按不省钱估算")
    return False


# ----------------------------------------------------------------- main


def main() -> int:
    env = load_env()
    raw_base = env["TRANSLATE_BASE_URL"]
    base = normalize_base(raw_base)
    key = env["TRANSLATE_API_KEY"]

    print("=" * 70)
    print(f"填入的 base : {raw_base}")
    print(f"规范化为    : {base}")
    print(f"api_key     : {mask(key)}")
    print("=" * 70)

    ids, prefix, auth = probe_models(base, key)
    picks = pick_models(ids)
    if picks["all_claude"]:
        print("  Claude 系模型：")
        for m in picks["all_claude"]:
            tag = ""
            if m == picks["translate"]:
                tag = "   ← 翻译主力"
            elif m == picks["cheap"]:
                tag = "   ← 廉价档（术语扫描/校验）"
            print(f"    {m}{tag}")

    model = env.get("TRANSLATE_MODEL") or picks["translate"]
    winner = probe_shape(base, key, prefix, model)
    if not winner:
        print("\n结论：协议没探通，后面没意义。")
        return 1

    shape, auth = winner["shape"], winner["auth"]
    print(f"    回包内容：{extract_text(shape, winner['sample'])!r}")

    has_batch = probe_batch(base, key, prefix, auth, shape)
    has_cache = probe_caching(base, key, prefix, auth, shape, model)

    print("\n" + "=" * 70)
    print("结论 —— 这几行就是流水线的配置")
    print("=" * 70)
    print(f"  TRANSLATE_BASE_URL   {base}")
    print(f"  路径前缀             {prefix}")
    print(f"  协议                 {shape}")
    print(f"  鉴权头               {auth}")
    print(f"  TRANSLATE_MODEL      {model}")
    print(f"  廉价档               {picks['cheap'] or '（无）'}")
    print(f"  Batch API            {'支持' if has_batch else '不支持 → D5 改写'}")
    print(f"  prompt cache         {'支持' if has_cache else '不支持 → D3 成本论证减弱'}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
