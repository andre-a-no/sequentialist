// SPDX-License-Identifier: Apache-2.0
// Copyright (C) 2026 Novikov Laboratories LLC
/**
 * Assignment of units to the variants of an experiment, identical to
 * `sequentialist.ab.assign` (Python) bit for bit, checked by the shared
 * vectors in `vectors/assignment.json`.
 *
 *   h(domain) = first 8 bytes of SHA-256(UTF-8(domain + "\x1f" + salt + "\x1f" + unit)),
 *               big-endian, shifted right by 11 bits (53 bits)
 *   U(domain) = h(domain) / 2**53
 *
 * A unit takes part if U("traffic") < share(time); its variant is the first
 * i with U("variant") * W < w_1 + ... + w_i.  Overrides come first; units
 * outside [start, end) are not assigned.  The configuration is what
 * `sequentialist ab client-config SPEC` prints (times in seconds since the
 * epoch).
 */
import { sha256, utf8 } from "./sha256.js";

export { sha256, utf8 };

export interface Variant {
  name: string;
  weight: number;
}

export interface TrafficStep {
  /** Seconds since the epoch. */
  from: number;
  share: number;
}

export interface AssignmentConfig {
  salt: string;
  variants: Variant[];
  traffic?: TrafficStep[];
  overrides?: Record<string, string>;
  start?: number | null;
  end?: number | null;
}

export type Reason = "hash" | "override" | "traffic" | "inactive";

export interface Assignment {
  /** null: the unit is not in the experiment now. */
  variant: string | null;
  /** The probability of this outcome, given the configuration and the time. */
  probability: number;
  reason: Reason;
}

const SEPARATOR = new Uint8Array([0x1f]);
const SCALE = 9007199254740992; // 2**53

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.length;
  }
  return out;
}

/** U(domain): a number in [0, 1) that only (domain, salt, unit) determine. */
export function uniform(domain: string, salt: string, unit: string): number {
  const digest = sha256(concat([utf8(domain), SEPARATOR, utf8(salt), SEPARATOR, utf8(unit)]));
  const view = new DataView(digest.buffer);
  const high = view.getUint32(0);
  const low = view.getUint32(4);
  // (high * 2**32 + low) >> 11, exactly: high * 2**21 < 2**53
  const head = high * 2097152 + (low >>> 11);
  return head / SCALE;
}

function share(config: AssignmentConfig, time: number | null | undefined): number {
  const traffic = config.traffic ?? [];
  if (traffic.length === 0) return 1;
  if (time === null || time === undefined) throw new Error("a ramped experiment needs the time of the exposure");
  let current = 0;
  for (const step of traffic) if (time >= step.from) current = step.share;
  return current;
}

/** The variant of `unit` at `time` (seconds since the epoch; needed with a ramp, a start or an end). */
export function assign(config: AssignmentConfig, unit: string, time?: number | null): Assignment {
  const overrides = config.overrides ?? {};
  if (Object.prototype.hasOwnProperty.call(overrides, unit)) {
    return { variant: overrides[unit], probability: 1, reason: "override" };
  }
  const start = config.start ?? null;
  const end = config.end ?? null;
  const known = time !== null && time !== undefined;
  if (known && ((start !== null && (time as number) < start) || (end !== null && (time as number) >= end))) {
    return { variant: null, probability: 1, reason: "inactive" };
  }
  if ((start !== null || end !== null) && !known) {
    throw new Error("an experiment with a start or an end needs the time of the exposure");
  }
  const s = share(config, time);
  if (!(uniform("traffic", config.salt, unit) < s)) {
    return { variant: null, probability: 1 - s, reason: "traffic" };
  }
  let total = 0;
  for (const v of config.variants) total += v.weight;
  const point = uniform("variant", config.salt, unit) * total;
  let cumulative = 0;
  for (const v of config.variants) {
    cumulative += v.weight;
    if (point < cumulative) return { variant: v.name, probability: (s * v.weight) / total, reason: "hash" };
  }
  const last = config.variants[config.variants.length - 1];
  return { variant: last.name, probability: (s * last.weight) / total, reason: "hash" };
}
