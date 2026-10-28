"""rentgpu GPU 行情 MCP Server（只读） + x402 按次计费。

设计原则：
- 严格只读：所有工具只查询 market.json，不写任何状态、不碰数据库。
- 发现免费：`list_gpu_models` / `ping` 不收费（agent 看不到的东西不可能买）。
- 数据付费：每一次真正的数据查询都过 x402 的 402 挑战 → 验签 → 结算。
- 交付失败不收费：查不到数据时返回 isError，x402 wrapper 不会结算。

链：Base 主网（eip155:8453），USDC（Circle 官方 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913），exact scheme，x402 v2。
切主网只需把 NETWORK 改成 eip155:8453 并换一个生产 facilitator。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.mcp import create_payment_wrapper
from x402.schemas.config import ResourceConfig

# ---------------------------------------------------------------- 配置

HERE = Path(__file__).parent

# Base Sepolia 测试网。生产切 eip155:8453（Base 主网）。
NETWORK = os.environ.get("X402_NETWORK", "eip155:84532")
FACILITATOR_URL = os.environ.get("X402_FACILITATOR", "https://x402.org/facilitator")


def _load_wallets() -> dict:
    p = HERE / "secrets" / "wallets.json"
    return json.loads(p.read_text(encoding="utf-8"))


# 收款地址：Brian 于 2026-10-20 指定的 Base 收款地址。
# （wallets.json 里的 seller 只是最初跑通链路用的测试地址，正式收款以这里为准。）
# 想临时改走环境变量 PAY_TO 覆盖即可，改完重启服务。
PAY_TO = os.environ.get("PAY_TO") or "0x230cd9cd33589bcf33cd32e5aa4f9e0c3994a63b"

MARKET_JSON = os.environ.get(
    "MARKET_JSON",
    str(HERE / "data" / "market.json"),
)

# 每个工具的定价（USD，按次）
PRICE = {
    "gpu_price": "$0.05",
    "gpu_history": "$0.05",
    "market_movers": "$0.05",
    "platform_reliability": "$0.05",
    "recommend_for_task": "$0.05",
}

# ---------------------------------------------------------------- 数据源（只读）

_market_cache: dict | None = None
_market_mtime: float = 0.0


def market() -> dict:
    """加载行情快照。只读 + 按 mtime 自动重载，绝不写回。

    hourly cron 会重建 market.json，进程常驻时必须跟着刷新，
    否则 agent 花真金白银买到的是昨天的数据。
    """
    global _market_cache, _market_mtime
    try:
        mtime = os.path.getmtime(MARKET_JSON)
    except OSError:
        if _market_cache is not None:
            return _market_cache
        raise
    if _market_cache is None or mtime != _market_mtime:
        with open(MARKET_JSON, encoding="utf-8") as f:
            _market_cache = json.load(f)
        _market_mtime = mtime
    return _market_cache


def _find_gpu(name: str) -> tuple[str, dict] | None:
    """按名字找卡型，支持大小写与部分匹配。"""
    gpus = market()["gpus"]
    q = name.strip().lower()
    if name in gpus:
        return name, gpus[name]
    for k, v in gpus.items():
        if k.lower() == q:
            return k, v
    hits = [(k, v) for k, v in gpus.items() if q in k.lower()]
    return hits[0] if len(hits) == 1 else (hits[0] if hits else None)


# ---------------------------------------------------------------- x402 资源服务器

class RetryFacilitator:
    """给 facilitator 的 settle 加自动重试。

    实测（2026-10-20）：公共 x402 facilitator 广播交易依赖 sepolia.base.org，
    该 RPC 会偶发返回 "Missing or invalid parameters"，导致 settle 报
    invalid_exact_evm_transaction_failed —— 签名和余额都是好的，纯粹是广播失败。
    不重试的话用户会遇到「付了钱却拿不到数据」。

    同一个 payment payload 重复 settle 是安全的：EIP-3009 的 nonce 相同，
    facilitator 侧的 PendingSettlementStore 也会去重，不会双花。
    """

    # 只对这些「瞬时/基础设施类」错误重试；余额不足、签名无效这类直接失败，不浪费额度
    RETRYABLE = ("transaction_failed", "settlement_pending", "unable_to_broadcast")

    def __init__(self, inner, retries: int = 3, delay: float = 3.0):
        self._inner = inner
        self.retries = retries
        self.delay = delay

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def verify(self, payload, requirements):
        return await self._inner.verify(payload, requirements)

    async def settle(self, payload, requirements):
        last = None
        for attempt in range(1, self.retries + 1):
            last = await self._inner.settle(payload, requirements)
            if last.success:
                if attempt > 1:
                    print(f"[x402] settle succeeded on retry #{attempt}")
                return last
            reason = str(getattr(last, "error_reason", "") or "")
            if not any(k in reason for k in self.RETRYABLE):
                return last  # 非瞬时错误，重试无意义
            print(f"[x402] settle attempt {attempt} failed ({reason}), retrying…")
            if attempt < self.retries:
                await asyncio.sleep(self.delay)
        return last


facilitator = RetryFacilitator(
    HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL))
)
resource_server = x402ResourceServer(facilitator)
resource_server.register(NETWORK, ExactEvmServerScheme())
resource_server.initialize()


def accepts_for(tool: str, price: str) -> list:
    """为某个工具生成支付要求。"""
    return resource_server.build_payment_requirements(
        ResourceConfig(
            scheme="exact",
            pay_to=PAY_TO,
            price=price,
            network=NETWORK,
        )
    )


# FastMCP 监听 127.0.0.1 时会自动开启 DNS rebinding 防护，且只放行 localhost。
# 走 nginx 反代后 Host 变成 rentgpu.link，会被 421 "Invalid Host header" 拒掉。
# 这里保留防护（生产建议开启），只是把真实域名加进白名单，而不是关掉它。
ALLOWED_HOSTS = [
    "rentgpu.link", "rentgpu.link:*",
    "www.rentgpu.link", "www.rentgpu.link:*",
    "127.0.0.1", "127.0.0.1:*",
    "localhost", "localhost:*",
]
ALLOWED_ORIGINS = [
    "https://rentgpu.link", "https://www.rentgpu.link",
    "http://127.0.0.1:*", "http://localhost:*",
]

mcp = FastMCP(
    "rentgpu-gpu-market",
    instructions=(
        "只读 GPU 算力租用行情数据源（rentgpu.link）。"
        "列出卡型免费；查询价格/历史/榜单需按次支付 USDC（Base 主网，x402 协议）。"
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=ALLOWED_HOSTS,
        allowed_origins=ALLOWED_ORIGINS,
    ),
)


def paid(tool_name: str):
    """给指定工具挂上 x402 计费。"""
    return create_payment_wrapper(
        resource_server,
        accepts=accepts_for(tool_name, PRICE[tool_name]),
        resource=None,
    )


# ---------------------------------------------------------------- 工具

@mcp.tool(
    name="ping",
    description="健康检查，免费。返回服务状态、收款地址、网络与价目表。",
)
async def ping() -> str:
    return json.dumps(
        {
            "ok": True,
            "service": "rentgpu GPU market MCP (read-only)",
            "network": NETWORK,
            "asset": "USDC",
            "pay_to": PAY_TO,
            "prices_usd": PRICE,
            "note": "tools/list 与 ping 免费；数据类工具按次计费。",
        },
        ensure_ascii=False,
    )


@mcp.tool(
    name="list_gpu_models",
    description="列出所有可查询的 GPU 卡型（免费）。付费查询前先调这个拿准确名称。",
)
async def list_gpu_models() -> str:
    m = market()
    out = [
        {
            "gpu": k,
            "current_low": v["current"]["p"] if v.get("current") else None,
            "days_tracked": v["days"],
        }
        for k, v in m["gpus"].items()
    ]
    out.sort(key=lambda x: x["current_low"] if x["current_low"] is not None else 9e9)
    return json.dumps(
        {
            "count": len(out),
            "window": f'{m["date_from"]} ~ {m["date_to"]} ({m["days_covered"]} 天)',
            "gpus": out,
        },
        ensure_ascii=False,
    )


@mcp.tool(
    name="gpu_price",
    description=(
        "查某张 GPU 现在的全平台最低价（元/小时），含报价平台、30 天最低/最高/均价、"
        "当前价与 30 天最低价的差距。示例: gpu_price(gpu='RTX 4090') → 当前最低 1.24 元/小时（算家云）、市场中位 1.99、30 天最低 1.24、今日 8 家报价。按次计费 $0.05 USDC。"
    ),
)
@paid("gpu_price")
async def gpu_price(gpu: str) -> str:
    hit = _find_gpu(gpu)
    if not hit:
        return json.dumps(
            {"error": f"找不到卡型 {gpu!r}", "hint": "先调 list_gpu_models 拿准确名称"},
            ensure_ascii=False,
        )
    name, v = hit
    return json.dumps(
        {
            "gpu": name,
            "currency": "CNY/hour",
            "current_low": v["current"]["p"],
            "current_low_platform": v["current"]["plat"],
            "as_of": v["current"]["d"],
            "low_30d": v["min30"]["p"],
            "low_30d_date": v["min30"]["d"],
            "low_30d_platform": v["min30"]["plat"],
            "high_30d": v["max30"]["p"],
            "avg_30d": v["avg30"],
            "median_now": v.get("med_now"),
            "above_30d_low_pct": v["vs_min_pct"],
            "change_30d_pct": v["change_pct"],
            "sellers_today": v.get("sellers_today"),
            "source": "rentgpu.link",
        },
        ensure_ascii=False,
    )


@mcp.tool(
    name="gpu_history",
    description=(
        "查某张 GPU 过去 30 天每日价格序列：每日全平台最低价、当日市场中位价、"
        "最低价来自哪个平台、库存状态、当日报价家数。示例: gpu_history(gpu='H100 80GB', limit=7) → 2026-10-20 最低 8.28 元（paperspace）、中位 23.04、有货、9 家报价。按次计费 $0.05 USDC。"
    ),
)
@paid("gpu_history")
async def gpu_history(gpu: str, limit: int = 30) -> str:
    hit = _find_gpu(gpu)
    if not hit:
        return json.dumps(
            {"error": f"找不到卡型 {gpu!r}", "hint": "先调 list_gpu_models 拿准确名称"},
            ensure_ascii=False,
        )
    name, v = hit
    series = v["series"][-max(1, min(limit, 90)) :]
    return json.dumps(
        {
            "gpu": name,
            "currency": "CNY/hour",
            "days": len(series),
            "series": [
                {
                    "date": s["d"],
                    "low": s["p"],
                    "median": s.get("med"),
                    "platform": s.get("plat"),
                    "stock": s.get("stock"),
                    "offers": s.get("n"),
                }
                for s in series
            ],
            "summary": {
                "low_30d": v["min30"],
                "high_30d": v["max30"],
                "avg_30d": v["avg30"],
                "volatility_pct": v.get("volatility_pct"),
            },
            "source": "rentgpu.link",
        },
        ensure_ascii=False,
    )


@mcp.tool(
    name="market_movers",
    description=(
        "近 30 天 GPU 行情涨跌榜：按区间涨跌幅排序的降价/涨价卡型，含当前价与 30 天最低价。示例: market_movers(top=3) → 降价榜 A6000 48GB 2.52 元(-28.6%)、A100 80GB 5.47 元(-21.6%)、RTX 3090 0.80 元(-19.2%)。"
        "按次计费 $0.05 USDC。"
    ),
)
@paid("market_movers")
async def market_movers(top: int = 5) -> str:
    m = market()
    items = [(k, v) for k, v in m["gpus"].items() if v.get("current")]
    ranked = sorted(items, key=lambda kv: kv[1]["change_pct"])
    n = max(1, min(top, 20))

    def pack(lst):
        return [
            {
                "gpu": k,
                "current": v["current"]["p"],
                "change_30d_pct": v["change_pct"],
                "low_30d": v["min30"]["p"],
                "low_30d_date": v["min30"]["d"],
            }
            for k, v in lst
        ]

    return json.dumps(
        {
            "window": f'{m["date_from"]} ~ {m["date_to"]}',
            "fallers": pack(ranked[:n]),
            "risers": pack(list(reversed(ranked))[:n]),
        },
        ensure_ascii=False,
    )


@mcp.tool(
    name="platform_reliability",
    description=(
        "各 GPU 租用平台的数据监测覆盖度评分（A~E）：自动抓取成功率、收录天数、"
        "库存充足率、覆盖卡型数。注意：衡量的是本数据源对该平台的价格监测覆盖度，"
        "不代表平台自身的服务质量。示例: platform_reliability() → 26 家平台评分，如 gpushare 98 分 A 级（抓取成功率 97%、覆盖 7 张卡、28/29 天）。按次计费 $0.05 USDC。"
    ),
)
@paid("platform_reliability")
async def platform_reliability() -> str:
    m = market()
    rows = []
    for pid, v in m["platforms"].items():
        rows.append(
            {
                "platform": pid,
                "score": v.get("score"),
                "grade": v.get("grade"),
                "live_pct": v.get("live_pct"),
                "presence_pct": v.get("presence_pct"),
                "stock_pct": v.get("stock_pct"),
                "cards": v.get("cards"),
                "days_live": v.get("days_live"),
                "days_total": v.get("days_total"),
                "manual": v.get("manual"),
            }
        )
    rows.sort(key=lambda r: (-(r["score"] or -1), -(r["days_live"] or 0)))
    return json.dumps(
        {
            "caveat": "评分 = 本数据源的监测覆盖度，不代表平台自身服务质量。",
            "count": len(rows),
            "platforms": rows,
        },
        ensure_ascii=False,
    )


# ---------- 任务推荐工具：按任务给最便宜方案（差异化，别处没有） ----------
TASKS = {
    "finetune-7b":  {"name": "微调 7B 大模型", "gpus": ["RTX 4090", "RTX 3090", "A100 80GB", "L40S 48GB"], "hours": 8,   "unit": "一轮 LoRA 微调"},
    "llm-inference": {"name": "大模型推理部署", "gpus": ["L40S 48GB", "A100 80GB", "RTX 4090", "L4 24GB"], "hours": 720, "unit": "7×24 常驻一个月"},
    "image-gen":    {"name": "AI 绘图（SDXL/Flux）", "gpus": ["RTX 4090", "RTX 5090", "RTX 3090", "RTX 6000 Ada 48GB"], "hours": 4,  "unit": "集中出图一批"},
    "video-gen":    {"name": "视频生成", "gpus": ["RTX 4090", "RTX 5090", "L40S 48GB", "A100 80GB"], "hours": 6, "unit": "一批短视频"},
    "render-3d":    {"name": "3D 渲染", "gpus": ["RTX 4090", "RTX 3090", "A6000 48GB", "L40S 48GB"], "hours": 12, "unit": "一个中等渲染项目"},
    "student":      {"name": "学生/入门练手", "gpus": ["RTX 3090", "T4 16GB", "RTX 4090", "RTX 4000 Ada 20GB"], "hours": 3, "unit": "周末练手一下午"},
    "train-70b":    {"name": "训练 70B 大模型", "gpus": ["H100 80GB", "A800 80GB", "H800 80GB", "A100 80GB"], "hours": 168, "unit": "一周训练（单卡）"},
    "multi-card":   {"name": "多卡并行/集群", "gpus": ["A800 80GB", "H100 80GB", "A100 80GB", "RTX 4090"], "hours": 72, "unit": "三天任务（单卡）"},
}

# 自然语言关键词 → slug（中英都认）
_TASK_ALIASES = {
    "微调": "finetune-7b", "finetune": "finetune-7b", "fine-tune": "finetune-7b", "7b": "finetune-7b", "13b": "finetune-7b",
    "推理": "llm-inference", "inference": "llm-inference", "api 服务": "llm-inference", "部署": "llm-inference",
    "绘图": "image-gen", "出图": "image-gen", "image": "image-gen", "sdxl": "image-gen", "flux": "image-gen", "comfyui": "image-gen",
    "视频": "video-gen", "video": "video-gen", "wan": "video-gen",
    "渲染": "render-3d", "render": "render-3d", "blender": "render-3d", "octane": "render-3d", "3d": "render-3d",
    "学生": "student", "练手": "student", "student": "student", "课程": "student", "demo": "student",
    "70b": "train-70b", "预训练": "train-70b", "pretrain": "train-70b",
    "多卡": "multi-card", "集群": "multi-card", "multi-card": "multi-card", "cluster": "multi-card",
}


def _resolve_task(task: str):
    q = task.strip().lower()
    if q in TASKS:
        return q
    for slug, t in TASKS.items():
        if slug in q or t["name"].lower() in q:
            return slug
    for kw, slug in _TASK_ALIASES.items():
        if kw in q:
            return slug
    return None


@mcp.tool(
    name="recommend_for_task",
    description=(
        "按任务推荐最便宜的 GPU 租用方案：输入任务（如'微调 7B'/'视频生成'，或英文 finetune-7b/video-gen），"
        "返回推荐卡型、最便宜平台、每小时单价与估算任务总成本。覆盖 8 类任务：微调、推理、绘图、视频、"
        "渲染、学生练手、70B 训练、多卡集群。示例: recommend_for_task(task='微调 7B') → 最便宜卡 + 平台 + 单价 × 8 小时的总成本。"
        "这是别处没有的'按任务问价'能力。按次计费 $0.05 USDC。"
    ),
)
@paid("recommend_for_task")
async def recommend_for_task(task: str) -> str:
    slug = _resolve_task(task)
    if not slug:
        return json.dumps(
            {"error": f"无法识别任务 {task!r}",
             "available_tasks": [{"slug": k, "name": v["name"]} for k, v in TASKS.items()],
             "hint": "支持中英文，如 '微调 7B'、'video-gen'、'训练 70B'"},
            ensure_ascii=False,
        )
    t = TASKS[slug]
    hours = t["hours"]
    options = []
    for gname in t["gpus"]:
        hit = _find_gpu(gname)
        if not hit:
            continue
        name, v = hit
        cur = v.get("current") or {}
        p = cur.get("p")
        if not p or p <= 0:
            continue
        options.append({
            "gpu": name,
            "price_per_hour": p,
            "platform": cur.get("plat"),
            "as_of": cur.get("d"),
            "est_total_cost": round(p * hours, 2),
        })
    options.sort(key=lambda x: x["price_per_hour"])
    if not options:
        return json.dumps(
            {"error": f"任务 {t['name']} 的推荐卡型当前都没有报价", "task": slug},
            ensure_ascii=False,
        )
    best = options[0]
    return json.dumps(
        {
            "task": slug,
            "task_name": t["name"],
            "hours_estimated": hours,
            "scope": t["unit"],
            "currency": "CNY",
            "cheapest": best,
            "alternatives": options[1:],
            "note": "总成本=单卡时价×估算时长；多卡集群任务按卡数倍增，实际以平台下单页为准。",
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8791"))
    print(f"[mcp] network={NETWORK} pay_to={PAY_TO} market={MARKET_JSON}")
    app = mcp.streamable_http_app()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
