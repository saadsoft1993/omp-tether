/** The local copy of omp's `projectBankSegment`, pinned with vectors made by omp's own function (v18.18.3 and the source tree it was cut from agree). */
import { describe, expect, test } from "bun:test";
import { projectBankSegment } from "../bank_id";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

const VECTORS: [string, string][] = [
	["/home/alice/projects/demo/api", "api-i6vpjra2mruk"],
	["/home/alice/projects/demo", "demo-vixuttqm7yrf"],
	["/home/bob/Work Stuff/my app (v2)", "my-app-v2-3pqt7lvi2gkm6"],
	[`/srv/${"x".repeat(80)}`, "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx-3q2dgkci9yfji"],
	["/", "default-c8rgibd8h0ws"],
	["/tmp/ünïcode/日本語", "default-2dg5t06dwqxpd"],
	["/a/-weird--name-/", "weird--name-d7ac25d2gpvk"],
	[`/mnt/data/${"a-".repeat(40)}end`, "a-a-a-a-a-a-a-a-a-a-a-a-a-a-a-a-a-a-a-a-a-a-a-a-a-2-1c00zaphfo7d"],
];

describe("projectBankSegment", () => {
	test.each(VECTORS)("%s", (folder, bank) => {
		expect(projectBankSegment(folder)).toBe(bank);
	});

	test("a bank name is at most 64 characters, made of letters, digits, hyphens and underscores", () => {
		for (const [folder] of VECTORS) {
			const bank = projectBankSegment(folder);
			expect(bank.length).toBeLessThanOrEqual(64);
			expect(bank).toMatch(/^[A-Za-z0-9_-]+$/);
		}
	});
});
