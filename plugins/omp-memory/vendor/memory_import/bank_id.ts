/**
 * The bank id omp gives a project folder: `<sanitized basename>-<Bun.hash(folder).toString(36)>`, clamped to 64
 * characters. A local copy of omp's `projectBankSegment` (packages/coding-agent/src/mnemopi/config.ts, MIT License,
 * Copyright (c) 2025 Mario Zechner, (c) 2025-2026 Can Bölük, (c) 2026 Stencil Labs, Inc.), because that function is in
 * the coding agent and not in the published `@oh-my-pi/pi-mnemopi` package the importer links against.
 *
 * `doctor` proves it against the banks that already exist: for every bank it recomputes the name from the folder the
 * bank's own rows record and reports any bank whose name differs. test/bank-id.test.ts pins fixed vectors.
 */
import path from "node:path";

const MAX_BANK_NAME = 64;

function limitBankName(name: string): string {
	if (name.length <= MAX_BANK_NAME) return name;
	const hash = Bun.hash(name).toString(36);
	const prefixLength = Math.max(1, MAX_BANK_NAME - 1 - hash.length);
	const prefix = name.slice(0, prefixLength).replace(/-+$/g, "") || "bank";
	return `${prefix}-${hash}`;
}

function sanitizeBankName(value: string | undefined): string | undefined {
	const raw = value?.trim();
	if (!raw) return undefined;
	const sanitized = raw.replace(/[^a-zA-Z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
	return sanitized ? limitBankName(sanitized) : undefined;
}

/** The bank directory name of an absolute project folder (the realpath, or the path omp was started in). */
export function projectBankSegment(projectRoot: string): string {
	const project = sanitizeBankName(path.basename(projectRoot)) ?? "default";
	return limitBankName(`${project}-${Bun.hash(projectRoot).toString(36)}`);
}
