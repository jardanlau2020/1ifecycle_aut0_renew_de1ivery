// cap_core/solve.mjs — Cap hashwx PoW 求解入口（調用 vendored 官方 core）
// 用法: node cap_core/solve.mjs <challenge.json> <solutions.json>
// 入:  POST /challenge 的完整 JSON   出: {token, solutions:[{protocol,nonce}]}
import fs from "node:fs";
import { hashwxReady, hashwxSeed, hashwxHash, hashwxTarget } from "./hashwx.js";

const [chalPath, outPath] = process.argv.slice(2);
if (!chalPath || !outPath) {
  console.error("usage: node solve.mjs <challenge.json> <solutions.json>");
  process.exit(2);
}
const chal = JSON.parse(fs.readFileSync(chalPath, "utf8"));
const list = chal.challenges || [];
if (!Array.isArray(list) || list.length === 0) {
  console.error("bad challenge payload: no challenges[]");
  process.exit(3);
}
const state = await hashwxReady();
const solutions = [];
for (const ch of list) {
  const proto = ch.protocol || "hashwx";
  if (proto !== "hashwx") {
    console.error("unsupported protocol: " + proto);
    process.exit(4);
  }
  const { c, d, n } = ch.payload || {};
  if (!c || typeof d !== "number" || typeof n !== "number") {
    console.error("bad payload: " + JSON.stringify(ch.payload));
    process.exit(5);
  }
  const bytes = new Uint8Array(32);
  for (let i = 0; i < 32; i++) bytes[i] = parseInt(c.slice(2 * i, 2 * i + 2), 16);
  const target = hashwxTarget(d);
  let found = null;
  outer: for (let block = 0n; block < 20000000n; block++) {
    const seed = hashwxSeed(bytes, block);
    const base = block * BigInt(n);
    for (let k = 0; k < n; k++) {
      const nonce = base + BigInt(k);
      if (hashwxHash(state, seed, nonce) <= target) { found = nonce; break outer; }
    }
  }
  if (found === null) {
    console.error(`nonce not found within budget (d=${d}, n=${n})`);
    process.exit(6);
  }
  solutions.push({ protocol: proto, nonce: found.toString() });
}
fs.writeFileSync(outPath, JSON.stringify({ token: chal.token, solutions }));