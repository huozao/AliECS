#!/usr/bin/env node
/**
 * 离线生成无副作用测试卡或样例恢复卡 JSON（只输出到 stdout，绝不发往飞书）。
 */
import { buildSideEffectFreeTestCard } from "./core.mjs";

const arg = process.argv[2] || "default";
const card = buildSideEffectFreeTestCard({ variant: arg });
process.stdout.write(`${JSON.stringify(card, null, 2)}\n`);
