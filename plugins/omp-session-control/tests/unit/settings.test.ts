import { describe, expect, test } from "bun:test";
import { cleanName, ControlError, DEFAULT_IDLE_TIMEOUT_MS, resolveSettings } from "../../src/omp-session-control";

const base = { env: {}, stored: {}, home: "/home/u" };

describe("resolveSettings", () => {
	test("defaults: directory under HOME, 60 s idle timeout, no warnings", () => {
		expect(resolveSettings(base)).toEqual({
			controlDir: "/home/u/.config/omp-session-control",
			idleTimeoutMs: DEFAULT_IDLE_TIMEOUT_MS,
			debug: false,
			warnings: [],
		});
	});

	test("environment beats the plugin setting, which beats the default", () => {
		const stored = { controlDir: "/srv/stored", idleTimeoutMs: 2000 };
		expect(resolveSettings({ ...base, stored }).controlDir).toBe("/srv/stored");
		expect(resolveSettings({ ...base, stored }).idleTimeoutMs).toBe(2000);
		const env = { OMP_SESSION_CONTROL_DIR: "/srv/env", OMP_SESSION_CONTROL_IDLE_MS: "5000" };
		const r = resolveSettings({ ...base, stored, env });
		expect(r.controlDir).toBe("/srv/env");
		expect(r.idleTimeoutMs).toBe(5000);
	});

	test("a relative or unusable directory turns control off instead of falling back", () => {
		for (const env of [{ OMP_SESSION_CONTROL_DIR: "relative/dir" }, { OMP_SESSION_CONTROL_DIR: "~/x" }]) {
			const r = resolveSettings({ ...base, env });
			expect(r.controlDir).toBe("");
			expect(r.warnings.length).toBe(1);
		}
		expect(resolveSettings({ ...base, stored: { controlDir: "x" } }).controlDir).toBe("");
		expect(resolveSettings({ ...base, home: undefined }).controlDir).toBe("");
		expect(resolveSettings({ ...base, home: "relative" }).controlDir).toBe("");
	});

	test("blank values count as unset", () => {
		const r = resolveSettings({ ...base, env: { OMP_SESSION_CONTROL_DIR: "  " }, stored: { controlDir: "" } });
		expect(r.controlDir).toBe("/home/u/.config/omp-session-control");
	});

	test("an invalid idle timeout is ignored and the next layer is used", () => {
		for (const bad of ["abc", "0", "99", "3600001", "1.5", "-5", "1e3"]) {
			const r = resolveSettings({ ...base, env: { OMP_SESSION_CONTROL_IDLE_MS: bad }, stored: { idleTimeoutMs: 1234 } });
			expect(r.idleTimeoutMs).toBe(1234);
			expect(r.warnings.length).toBe(1);
		}
		const empty = resolveSettings({ ...base, env: { OMP_SESSION_CONTROL_IDLE_MS: "" }, stored: { idleTimeoutMs: 1234 } });
		expect(empty.idleTimeoutMs).toBe(1234);
		expect(empty.warnings).toEqual([]);
		expect(resolveSettings({ ...base, stored: { idleTimeoutMs: "abc" } }).idleTimeoutMs).toBe(DEFAULT_IDLE_TIMEOUT_MS);
		expect(resolveSettings({ ...base, stored: { idleTimeoutMs: "750" } }).idleTimeoutMs).toBe(750);
		expect(resolveSettings({ ...base, stored: { idleTimeoutMs: 100.5 } }).idleTimeoutMs).toBe(DEFAULT_IDLE_TIMEOUT_MS);
	});

	test("the directory is normalised", () => {
		expect(resolveSettings({ ...base, env: { OMP_SESSION_CONTROL_DIR: "/a/b/../c/" } }).controlDir).toBe("/a/c");
	});
});

describe("debug setting", () => {
	test("off by default; on from the environment or the stored setting; anything else is off", () => {
		expect(resolveSettings(base).debug).toBe(false);
		for (const on of ["1", "true", "YES", " on "]) expect(resolveSettings({ ...base, env: { OMP_SESSION_CONTROL_DEBUG: on } }).debug).toBe(true);
		expect(resolveSettings({ ...base, stored: { debugLog: true } }).debug).toBe(true);
		for (const off of ["0", "no", "", "maybe"]) expect(resolveSettings({ ...base, env: { OMP_SESSION_CONTROL_DEBUG: off } }).debug).toBe(false);
	});
});

describe("cleanName", () => {
	test("trims and collapses runs of spaces, keeps non-ASCII text", () => {
		expect(cleanName("  Hub   rename ✓ ")).toBe("Hub rename ✓");
		expect(cleanName("日本語のセッション")).toBe("日本語のセッション");
	});

	test("counts characters, not UTF-16 units", () => {
		expect(cleanName("😀".repeat(120)).length).toBe(240);
		expect(() => cleanName("😀".repeat(121))).toThrow(ControlError);
	});

	test.each([
		["empty", ""],
		["blank", "   "],
		["newline", "a\nb"],
		["tab", "a\tb"],
		["NUL", "a\u0000b"],
		["DEL", "a\u007fb"],
		["C1", "a\u0085b"],
		["line separator", "a\u2028b"],
		["bidi override", "prod\u202eved"],
		["zero-width space", "a\u200bb"],
		["bidi isolate", "x\u2066y"],
		["not a string", 5],
		["null", null],
		["121 characters", "x".repeat(121)],
	])("rejects %s", (_label, value) => {
		expect(() => cleanName(value)).toThrow(ControlError);
		try {
			cleanName(value);
		} catch (error) {
			expect((error as ControlError).code).toBe("invalid_args");
		}
	});

	test("a byte order mark inside a name is rejected as an invisible character", () => {
		expect(() => cleanName("x\ufeffy")).toThrow(ControlError);
	});
});
