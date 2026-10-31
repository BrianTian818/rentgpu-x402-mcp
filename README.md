# rentgpu GPU Market MCP

[![MCP Registry](https://img.shields.io/badge/MCP_Registry-link.rentgpu%2Fgpu--market-blue)](https://registry.modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/Code_License-MIT-green.svg)](LICENSE)
[![Data: CC BY 4.0](https://img.shields.io/badge/Data-License-CC_BY_4.0-orange.svg)](https://creativecommons.org/licenses/by/4.0/)

**Live endpoint: `https://rentgpu.link/mcp`** — a read-only MCP server exposing real-time GPU
rental price data, monetized per call with [x402](https://www.x402.org) (USDC on Base mainnet).
No signup, no API key, no credit card — the payment proof *is* the identity.

中文说明见下方的「中文简介」。

## What you get

| Tool | Price | What it returns |
|---|---|---|
| `list_gpu_models` | free | all tracked GPU models |
| `ping` | free | liveness check |
| `gpu_price` | $0.002/call | current lowest quotes for a GPU across 29 platforms |
| `gpu_history` | $0.02/call | daily lowest-price series (30-day window) |
| `market_movers` | $0.005/call | biggest 24h price drops/rises |
| `platform_reliability` | $0.005/call | coverage/uptime-style stability score per platform |
| `recommend_for_task` | $0.01/call | GPU + platform recommendation for a task/budget |

Free HTTP endpoints (no key, CC BY 4.0): `GET https://rentgpu.link/api/prices`,
`GET https://rentgpu.link/api/resolve?gpu=H100&budget=10&hours=8&limit=3`.

## Data coverage

- **29 platforms** (11 of them China-based: AutoDL, 恒源云/gpushare, 矩池云, 优云智算, 算家云, 晨涧云 …),
  **26 GPU models** (RTX 3090/4090/5090 → A100/H100/H200/B200/L40S/MI300X), **140 live quotes**
  (131 of them re-crawled this round; the rest are official list prices, flagged `source=seed`).
- Re-crawled **every 45 minutes** from public price pages / public APIs.
- Ranked strictly by price. No paid placement. Reference-price entries are flagged and excluded
  from "lowest price" ranking.
- Prices are **CNY per GPU per hour** (USD×7.2). Multi-GPU machines are normalized to per-GPU price.

Coverage numbers move as platforms are added — treat https://rentgpu.link/llms.txt as the live
figure and this README as the snapshot it was verified against (2026-10-31).

Full methodology: https://rentgpu.link/sources · machine-readable fact pack: https://rentgpu.link/llms-full.txt

## Quick start (this repo)

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python server.py            # stdio MCP server on the bundled sample data
```

Point an MCP client at it:

```json
{ "mcpServers": { "rentgpu-gpu-market": { "command": "python", "args": ["server.py"] } } }
```

Or talk to the **live hosted endpoint** (streamable HTTP, x402-paid):

```json
{ "mcpServers": { "rentgpu-gpu-market": { "url": "https://rentgpu.link/mcp" } } }
```

A minimal x402-paying demo client is in `client_agent.py` (set `BUYER_KEY` env var to a funded
Base USDC wallet; do **not** commit keys).

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `X402_NETWORK` | `eip155:8453` (Base mainnet in production) | chain for settlement |
| `X402_FACILITATOR` | `https://facilitator.payai.network` | x402 facilitator |
| `PAY_TO` | `0x230cd9...4a63b` | USDC receiving address |
| `MARKET_JSON` | `./data/market.json` (bundled sample) | path to the market data file |

The bundled `data/market.json` is a static sample. The hosted service regenerates it every
45 minutes; self-hosters can wire the same generator against their own crawl.

## Design principles

- **Strictly read-only** — tools only query the market file; no writes, no DB.
- **Discovery is free** — `list_gpu_models` / `ping` cost nothing (an agent can't buy what it can't see).
- **Failed delivery is never charged** — if a tool errors, the x402 wrapper does not settle.

## Data license

Code: MIT. The underlying price data is **CC BY 4.0** — cite
*rentgpu.link (GPU rental price radar), updated YYYY-MM-DD, https://rentgpu.link*.

## 中文简介

rentgpu.link（GPU 算力雷达）是一个中立的 GPU 租用比价站：每 45 分钟自动抓取 29 家算力平台
（含 AutoDL、恒源云、矩池云等 11 家中国平台）的公开价格，覆盖 26 种卡型、140 条报价，永远按价格升序排序，
不接受任何付费排名。本仓库是它的 MCP 服务器源码：所有数据查询通过 x402 协议按次收费
（USDC on Base 主网，分档计价 $0.002–$0.02/次），免注册、免 API Key；卡型列表与 ping 免费。
数据可自由引用（CC BY 4.0），请注明来源 rentgpu.link 与抓取时间。
