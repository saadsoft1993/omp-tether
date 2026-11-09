import { describe, expect, it } from "bun:test";
import type { HubSession } from "../src/data/hub";
import {
	forceKillNotice,
	killConfirmMatches,
	nameProblem,
	placeLabel,
	RESTART_FOR_CONTROLS,
	sessionControls,
	sessionDisplayName,
} from "../src/data/session-controls";

const base: HubSession = {
	instanceId: "abcd1234",
	sessionId: "01a1b65b-93f3-7000-8000-000000000001",
	pid: 4242,
	cwd: "/work/projects/sms",
	sessionName: "Fix login bug",
	busy: true,
	control: true,
};

describe("session controls", () => {
	it("offers Stop only while a turn runs, and Rename and Kill always", () => {
		expect(sessionControls(base)).toEqual({
			showStop: true,
			stop: { enabled: true, reason: null },
			rename: { enabled: true, reason: null },
			kill: { enabled: true, reason: null },
		});
		expect(sessionControls({ ...base, busy: false }).showStop).toBe(false);
	});

	it("explains the restart when the session has no extension yet, but still allows Kill", () => {
		const c = sessionControls({ ...base, control: false });
		expect(c.stop).toEqual({ enabled: false, reason: RESTART_FOR_CONTROLS });
		expect(c.rename.enabled).toBe(false);
		expect(c.kill.enabled).toBe(true);
	});

	it("shows the hub's reason when the control directory is not safe", () => {
		const c = sessionControls({ ...base, control: false }, "/x/control is not private to this account; Stop and Rename stay off");
		expect(c.stop.reason).toContain("is not private");
		expect(c.rename.enabled).toBe(false);
	});

	it("turns everything off without a session id, and Kill off without a pid", () => {
		const c = sessionControls({ ...base, sessionId: undefined });
		expect([c.stop.enabled, c.rename.enabled, c.kill.enabled]).toEqual([false, false, false]);
		expect(sessionControls({ ...base, pid: undefined }).kill.enabled).toBe(false);
	});

	it("accepts the exact name or the project folder as Kill confirmation", () => {
		expect(killConfirmMatches(base, "Fix login bug")).toBe(true);
		expect(killConfirmMatches(base, "  sms ")).toBe(true);
		for (const typed of ["", "fix login bug", "Fix", "/work/projects/sms"]) {
			expect(killConfirmMatches(base, typed)).toBe(false);
		}
		expect(killConfirmMatches({ ...base, sessionName: undefined }, "")).toBe(false);
	});

	it("names sessions and says where they run", () => {
		expect(sessionDisplayName({ ...base, sessionName: undefined })).toBe("sms");
		expect(placeLabel({ ...base, place: { backend: "herdr", workspace: "sms", tab: "omp", pane: "w1:p2" } })).toBe("in herdr: sms › omp");
		expect(placeLabel({ ...base, place: { backend: "tmux", workspace: "omp", tab: "api", pane: "%3" } })).toBe("in tmux: omp › api");
		expect(placeLabel({ ...base, place: { backend: "herdr", workspace: "sms", tab: null, pane: null } })).toBe("in herdr: sms");
		expect(placeLabel({ ...base, place: null })).toBeNull();
	});
});

describe("rename validation", () => {
	it("matches the extension: 1-120 characters, no control or invisible characters", () => {
		expect(nameProblem("Fix  login ")).toBeNull();
		for (const bad of ["", "   ", "x".repeat(121), "a\nb", "prod\u202eved", "a\u200bb", "x\u2066y"]) {
			expect(nameProblem(bad)).not.toBeNull();
		}
	});
});

describe("Force kill notices", () => {
	it("says apart whether the session itself or only what it started is still running", () => {
		const done = forceKillNotice({ outcome: "force_exited", remaining: 0 }, "work", "mini");
		expect(done.tone).toBe("ok");
		const partial = forceKillNotice({ outcome: "force_still_running", remaining: 2, descendants: 3, parent_running: false }, "work", "mini");
		expect(partial.tone).toBe("bad");
		expect(partial.pageLevel).toBe(true);
		expect(partial.text).toContain("itself has ended");
		expect(partial.text).toContain("2 processes");
		const parent = forceKillNotice({ outcome: "force_still_running", remaining: 3, descendants: 2, parent_running: true }, "work", "mini");
		expect(parent.pageLevel).toBe(false);
		expect(parent.text).toContain("still running even after Force kill");
		expect(parent.text).toContain("2 processes");
	});
});
