// Pay one Unfret check per call over x402 on Solana, in USDC (or wSOL), with the official @x402 packages.
//
// The first POST /v1/check answers 402; the x402 client signs a TransferChecked of the price to Unfret's payTo
// (Unfret is the fee payer and adds its signature, so you pay only the price) and sends the request again with
// PAYMENT-SIGNATURE. Run: `SOLANA_KEYPAIR=~/.config/solana/id.json node x402_solana.ts` (Node 22.18 or later).
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { createKeyPairSignerFromBytes, getBase58Encoder } from "@solana/kit";
import { decodePaymentResponseHeader, wrapFetchWithPayment, x402Client } from "@x402/fetch";
import { ExactSvmScheme } from "@x402/svm/exact/client";

const PRODUCTION = process.env.UNFRET_ENV === "production";
const API = process.env.UNFRET_API_URL ?? (PRODUCTION ? "https://unfret.net" : "https://dev.unfret.net");
const NETWORK = PRODUCTION ? "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp" : "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1";
const MINTS: Record<string, string> = {
  usdc: PRODUCTION ? "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v" : "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
  wsol: "So11111111111111111111111111111111111111112", // wrapped SOL: wrap some first (README)
};
const ASSET = process.env.SOLANA_ASSET ?? "usdc";
const PAGE = process.env.UNFRET_CHECK_URL ?? `${API}/test-shop`;
const TIER = process.env.UNFRET_TIER ?? "quick"; // quick: $0.01, deep: $0.20
// Refuse to pay more than this, in the asset's base units (USDC: 6 decimals, wSOL: 9).
const MAX_AMOUNT = BigInt(process.env.MAX_AMOUNT ?? (ASSET === "usdc" ? 250_000 : 3_000_000));

// The payer: a base58 secret key (SOLANA_PRIVATE_KEY) or a solana-keygen JSON file (SOLANA_KEYPAIR).
const secret = process.env.SOLANA_PRIVATE_KEY
  ? getBase58Encoder().encode(process.env.SOLANA_PRIVATE_KEY)
  : new Uint8Array(
      JSON.parse(readFileSync((process.env.SOLANA_KEYPAIR ?? "~/.config/solana/id.json").replace(/^~/, homedir()), "utf8")),
    );
const signer = await createKeyPairSignerFromBytes(secret);

const client = new x402Client()
  .register(NETWORK, new ExactSvmScheme(signer, { rpcUrl: process.env.SOLANA_RPC_URL })) // default: public RPC
  // Pay only in the chosen asset, and never more than MAX_AMOUNT.
  .registerPolicy((_version, reqs) => reqs.filter((r) => r.asset === MINTS[ASSET] && BigInt(r.amount) <= MAX_AMOUNT));
const fetchWithPayment = wrapFetchWithPayment(fetch, client);

console.log(`Paying from ${signer.address} in ${ASSET.toUpperCase()} on ${NETWORK}`);
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
  // 402 again: the payment was refused (`error`, and `reason` when its simulation failed); nothing was paid.
  console.error(`HTTP ${response.status} ${body.error_code}: ${body.detail ?? body.error}`);
  if (body.reason) console.error(`reason: ${body.reason}`);
  process.exit(1);
}
console.log(`${body.action.toUpperCase()} (${body.band}, risk ${body.risk_score.toFixed(2)}) for ${PAGE}`);
for (const flag of body.flags) console.log(`  - ${flag.type}: ${flag.evidence}`);
