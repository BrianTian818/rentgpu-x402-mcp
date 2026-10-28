"""付费 Agent 客户端：演示 x402 全链路「查询 → 402 → 签名付款 → 拿数据 → 链上到账」。

用法：
    python client_agent.py                 # 跑默认演示
    python client_agent.py --tool gpu_history --args '{"gpu":"RTX 4090"}'

钱包里的 USDC 余额不足时会明确报错（不会静默失败）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from eth_account import Account
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from web3 import Web3

from x402 import x402Client
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.mcp import wrap_mcp_client_with_payment

# 本机有全局 HTTP 代理（127.0.0.1:20203），会把 127.0.0.1 的 MCP 请求也拦掉 → 502。
# 必须排除本地地址，但保留代理用于访问 x402 facilitator 与 Base RPC。
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

HERE = Path(__file__).parent
NETWORK = os.environ.get("X402_NETWORK", "eip155:8453")
# 主网 RPC：mainnet.base.org；若 getLogs 报 413 可换 base-rpc.publicnode.com。
RPC = os.environ.get("BASE_RPC", "https://base-rpc.publicnode.com")
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SERVER = os.environ.get("MCP_URL", "https://rentgpu.link/mcp")

_BAL_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


class _SessionAdapter:
    """x402 的 MCP 封装调用 `call_tool(params_dict)`，而 mcp 1.x 的 ClientSession
    签名是 `call_tool(name=..., arguments=...)`。这层薄适配把两者对齐，
    其余方法（list_tools / initialize 等）原样透传。"""

    def __init__(self, session):
        self._s = session

    async def call_tool(self, params: dict, **kwargs):
        # params["_meta"] 里装着 x402 的支付载荷（x402/payment），
        # 第一次调用为空（触发 402），第二次带着签名重试。必须透传，否则永远付不了款。
        return await self._s.call_tool(
            name=params["name"],
            arguments=params.get("arguments") or {},
            meta=params.get("_meta"),
            **kwargs,
        )

    def __getattr__(self, name):
        return getattr(self._s, name)


def usdc_balance(addr: str) -> float:
    w3 = Web3(Web3.HTTPProvider(RPC))
    c = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=_BAL_ABI)
    return c.functions.balanceOf(Web3.to_checksum_address(addr)).call() / 10**6


def transfers_between(from_addr: str, to_addr: str, blocks: int = 400) -> list[dict]:
    """链上核验：查 USDC Transfer 事件，只取 from_addr → to_addr 的那些。

    余额差值会被 RPC 节点同步延迟带偏（最后一笔刚广播时 buyer 可能还没扣），
    所以最终结论以链上事件为准。
    """
    w3 = Web3(Web3.HTTPProvider(RPC))
    topic = w3.keccak(text="Transfer(address,address,uint256)").hex()
    pad = lambda a: "0x" + "0" * 24 + a[2:].lower()
    latest = w3.eth.block_number
    logs = w3.eth.get_logs(
        {
            "fromBlock": latest - blocks,
            "toBlock": "latest",
            "address": Web3.to_checksum_address(USDC),
            "topics": [topic, pad(from_addr), pad(to_addr)],
        }
    )
    out = []
    for l in logs:
        out.append(
            {
                "block": l["blockNumber"],
                "tx": l["transactionHash"].hex(),
                "amount": int(l["data"].hex(), 16) / 10**6,
            }
        )
    return out


def _fmt(res) -> str:
    """把 MCP 工具结果压成可读文本。"""
    out = []
    for c in getattr(res, "content", []) or []:
        text = getattr(c, "text", None)
        if text:
            out.append(text)
    return "\n".join(out)


def _settlement(res) -> dict | None:
    """x402 v2 的结算回执位置在不同版本里不固定，这里做多路径查找。

    实测（2026-10-26 主网首跑）：结算其实成功了，但回执不在 res.meta 顶层，
    导致误报「本次无结算记录」。所以要把 meta / meta["_meta"] /
    structuredContent / content 里的 JSON 全扫一遍。
    """
    cands = []
    meta = getattr(res, "meta", None) or {}
    if isinstance(meta, dict):
        cands.append(meta)
        if isinstance(meta.get("_meta"), dict):
            cands.append(meta["_meta"])
    sc = getattr(res, "structuredContent", None)
    if isinstance(sc, dict):
        cands.append(sc)
    for c in getattr(res, "content", []) or []:
        t = getattr(c, "text", None)
        if t:
            try:
                j = json.loads(t)
                if isinstance(j, dict):
                    cands.append(j)
            except Exception:
                pass
    for d in cands:
        for key in ("x402/payment-response", "x402/paymentResponse", "payment-response"):
            if d.get(key):
                return d[key]
    return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", default=None, help="只跑指定工具")
    ap.add_argument("--args", default="{}", help="工具参数的 JSON")
    args_ns = ap.parse_args()

    w = json.loads((HERE / "secrets" / "wallets.json").read_text(encoding="utf-8"))
    buyer_key, buyer_addr = w["buyer"]["key"], w["buyer"]["address"]
    # 正式收款方 = Brian 指定的 Base 地址（wallets.json 里的 seller 只是早期联调用的地址）
    seller_addr = os.environ.get("PAYEE") or "0x230cd9cd33589bcf33cd32e5aa4f9e0c3994a63b"

    print("=" * 68)
    print("x402 全链路演示 · rentgpu GPU 行情 MCP（只读）")
    print("=" * 68)
    print(f"网络        : {NETWORK}  (Base 主网)")
    print(f"资产        : USDC {USDC}")
    print(f"买方/付款方 : {buyer_addr}")
    print(f"卖方/收款方 : {seller_addr}   <-- Brian 指定的 Base 地址")
    print(f"MCP 端点    : {SERVER}")

    # 本次演示要花的总价（与 server.py 的 PRICE 保持一致）
    PRICE = {"gpu_price": 0.001, "market_movers": 0.005,
             "gpu_history": 0.01, "platform_reliability": 0.002}
    demo_calls = (
        [(args_ns.tool, json.loads(args_ns.args))]
        if args_ns.tool
        else [("gpu_price", {"gpu": "RTX 4090"}),
              ("market_movers", {"top": 3}),
              ("gpu_history", {"gpu": "H100 80GB", "limit": 7})]
    )
    need = sum(PRICE.get(n, 0.01) for n, _ in demo_calls)

    bal_before = usdc_balance(buyer_addr)
    seller_before = usdc_balance(seller_addr)
    print(f"付款前余额  : buyer {bal_before:.6f} USDC | seller {seller_before:.6f} USDC")
    print(f"本次预计花费: {need:.6f} USDC（{len(demo_calls)} 次调用）")
    if bal_before < need:
        print(f"\n[!] buyer 测试 USDC 不足（有 {bal_before:.6f}，需要 {need:.6f}）。")
        print("    测试网水龙头 24 小时限一次，额度有限：")
        print("    POST https://a2awire.com/api/v1/faucet/drip  {\"address\":..., \"asset\":\"usdc\"}")
        print("    也可以只跑便宜的工具：--tool gpu_price --args '{\"gpu\":\"RTX 4090\"}'")
        return 2

    account = Account.from_key(buyer_key)
    signer = EthAccountSigner(account)
    x402_client = x402Client()
    x402_client.register(NETWORK, ExactEvmScheme(signer))

    paid_tx: list[str] = []

    async with streamable_http_client(SERVER) as (r, w_stream, _):
        async with ClientSession(r, w_stream) as session:
            await session.initialize()  # 底层 MCP 握手
            mcp = wrap_mcp_client_with_payment(
                _SessionAdapter(session),
                x402_client,
                auto_payment=True,
                on_payment_requested=lambda ctx: True,  # agent 自动批准付款
            )

            # ---------- 1) 发现（免费） ----------
            tools = await mcp.list_tools()
            print("\n--- 1. tools/list（免费，agent 必须先看见才可能买）---")
            for t in tools.tools:
                print(f"    - {t.name}: {t.description.split('。')[0][:60]}")

            # ---------- 2) 免费健康检查 ----------
            print("\n--- 2. ping（免费）---")
            res = await mcp.call_tool("ping", {})
            print("   ", _fmt(res)[:300])

            # ---------- 3) 付费查询 ----------
            calls = demo_calls
            for i, (name, arg) in enumerate(calls, start=3):
                print(f"\n--- {i}. {name}{arg}（付费）---")
                res = await mcp.call_tool(name, arg)
                body = _fmt(res)
                print(f"    isError = {getattr(res, 'isError', None)}")
                try:
                    parsed = json.loads(body)
                    print("   ", json.dumps(parsed, ensure_ascii=False)[:420])
                except Exception:
                    print("   ", body[:420])

                st = _settlement(res)
                if st:
                    tx = st.get("transaction") or ""
                    print(f"    结算: success={st.get('success')} network={st.get('network')}")
                    print(f"    tx   : {tx}")
                    if tx:
                        paid_tx.append(tx)
                else:
                    print("    （响应未携带结算回执；服务端 settle 后未写入 _meta，以链上核验为准）")

    # ---------- 4) 链上核验 ----------
    # 最后一笔刚广播时 RPC 可能还没同步，等一下再读，避免余额读数偏小。
    print("\n等待链上确认…")
    await asyncio.sleep(6)

    print("\n" + "=" * 68)
    print("链上核验（以 USDC Transfer 事件为准）")
    print("=" * 68)
    txs = transfers_between(buyer_addr, seller_addr)
    total = sum(t["amount"] for t in txs)
    print(f"buyer → seller 转账笔数 : {len(txs)}")
    for t in txs:
        print(f"    {t['amount']:.6f} USDC  blk {t['block']}  tx {t['tx']}")
        print(f"        https://basescan.org/tx/{t['tx']}")
    print(f"链上合计                : {total:.6f} USDC")

    bal_after = usdc_balance(buyer_addr)
    seller_after = usdc_balance(seller_addr)
    print(f"余额变化  : buyer {bal_before:.6f} → {bal_after:.6f} | "
          f"seller {seller_before:.6f} → {seller_after:.6f}")

    ok = len(txs) >= len(calls) and total > 0
    print("\n结论:",
          f"✅ 全链路跑通 — Agent 查询 → 402 → 付款 → 卖方到账 {total:.6f} USDC"
          if ok else "❌ 链上未查到对应转账，需排查")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
