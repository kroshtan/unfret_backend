// Pay one Unfret check per call over x402 on Base, in USDC, with the official @x402 packages.
//
// The first POST /v1/check answers 402; the x402 client signs an EIP-3009 transferWithAuthorization of exactly the
// price to Unfret's payTo (the `exact` scheme) and sends the request again with PAYMENT-SIGNATURE. The facilitator
// pays the gas, so the wallet needs USDC and no ETH, and Unfret settles only once the answer is ready: a failed
// check costs nothing. Run: `EVM_PRIVATE_KEY=0x… node x402_base.ts` (Node 22.18 or later).
import { decodePaymentResponseHeader, wrapFetchWithPayment, x402Client } from "@x402/fetch";
import { toClientEvmSigner } from "@x402/evm";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { privateKeyToAccount } from "viem/accounts";

const PRODUCTION = process.env.UNFRET_ENV === "production";
const API = process.env.UNFRET_API_URL ?? (PRODUCTION ? "https://unfret.net" : "https://dev.unfret.net");
const NETWORK = PRODUCTION ? "eip155:8453" : "eip155:84532"; // Base mainnet / Base Sepolia
const USDC = PRODUCTION ? "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913" : "0x036CbD53842c5426634e7929541eC2318f3dCF7e";
const PAGE = process.env.UNFRET_CHECK_URL ?? `${API}/test-shop`;
const TIER = process.env.UNFRET_TIER ?? "quick"; // quick: $0.01, deep: $0.20
const MAX_AMOUNT = BigInt(process.env.MAX_AMOUNT ?? 250_000); // refuse more than $0.25 (USDC has 6 decimals)

const key = process.env.EVM_PRIVATE_KEY;
if (!key) throw new Error("set EVM_PRIVATE_KEY (0x and 64 hex digits)");
const account = privateKeyToAccount(key as `0x${string}`);

const client = new x402Client()
  .register(NETWORK, new ExactEvmScheme(toClientEvmSigner(account)))
  // Pay only Circle's USDC, and never more than MAX_AMOUNT.
  .registerPolicy((_version, reqs) =>
    reqs.filter((r) => r.asset.toLowerCase() === USDC.toLowerCase() && BigInt(r.amount) <= MAX_AMOUNT),
  );
const fetchWithPayment = wrapFetchWithPayment(fetch, client);

console.log(`Paying from ${account.address} in USDC on ${NETWORK}`);
const response = await fetchWithPayment(`${API}/v1/check`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ url: PAGE, tier: TIER }),
  signal: AbortSignal.timeout(100_000),
});

const body = await response.json();
const receipt = response.headers.get("PAYMENT-RESPONSE");
if (receipt) console.log("Payment:", decodePaymentResponseHeader(receipt)); // success, transaction, network, payer
if (!response.ok) {
  // 402 again: the facilitator refused the authorization (e.g. not enough USDC); nothing was paid.
  console.error(`HTTP ${response.status} ${body.error_code}: ${body.detail ?? body.error}`);
  if (body.reason) console.error(`reason: ${body.reason}`);
  process.exit(1);
}
console.log(`${body.action.toUpperCase()} (${body.band}, risk ${body.risk_score.toFixed(2)}) for ${PAGE}`);
for (const flag of body.flags) console.log(`  - ${flag.type}: ${flag.evidence}`);
