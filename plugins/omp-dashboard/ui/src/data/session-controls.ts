/**
 * Which Sessions-page controls a session offers, and why one is off. Mirrors the hub's own rules
 * (`python/omp_dashboard/control.py`, `kill.py`), which decide in the end.
 */
import type { HubSession } from "./hub";

export const RESTART_FOR_CONTROLS =
	"Stop and Rename need omp-session-control, turned on, in a session that started after that: restart this session once it is.";

export interface ControlState {
	enabled: boolean;
	/** Why it is off (shown as the button's tooltip and under it). */
	reason: string | null;
}

export interface SessionControls {
	/** Stop is only shown while a turn runs. */
	showStop: boolean;
	stop: ControlState;
	rename: ControlState;
	kill: ControlState;
}

const on: ControlState = { enabled: true, reason: null };

export function projectName(s: HubSession): string {
	return (s.cwd ?? "").split("/").pop() ?? "";
}

export function sessionDisplayName(s: HubSession): string {
	return s.sessionName || projectName(s) || "omp session";
}

export function sessionControls(s: HubSession, controlProblem?: string | null): SessionControls {
	const noId: ControlState = { enabled: false, reason: "omp did not report this session's id" };
	const extension: ControlState = !s.sessionId
		? noId
		: s.control
			? on
			: { enabled: false, reason: controlProblem || RESTART_FOR_CONTROLS };
	return {
		showStop: s.busy === true,
		stop: extension,
		rename: extension,
		kill: !s.sessionId
			? noId
			: typeof s.pid === "number"
				? on
				: { enabled: false, reason: "omp did not report this session's process" },
	};
}

/** The Kill confirmation the hub accepts: the session's exact name, or its project folder's name. */
export function killConfirmMatches(s: HubSession, typed: string): boolean {
	const value = typed.trim();
	if (!value) return false;
	return value === (s.sessionName ?? "").trim() || value === projectName(s);
}

/** "in herdr: sms › omp" or "in tmux: omp › api", or null when no Start backend knows the session. */
export function placeLabel(s: HubSession): string | null {
	const place = s.place;
	if (!place?.workspace) return null;
	return place.tab ? `in ${place.backend}: ${place.workspace} › ${place.tab}` : `in ${place.backend}: ${place.workspace}`;
}

export const MAX_NAME_CHARS = 120;

/** Why the hub would refuse this name (same rule as omp-session-control), or null. */
export function nameProblem(raw: string): string | null {
	const name = raw.replace(/ +/g, " ").trim();
	const chars = [...name].length;
	if (chars < 1 || chars > MAX_NAME_CHARS) return `Use 1–${MAX_NAME_CHARS} characters.`;
	if (/[\u0000-\u001f\u007f-\u009f\u2028\u2029\p{Cf}]/u.test(name)) {
		return "Remove control or invisible characters (such as text-direction marks).";
	}
	return null;
}

export interface ForceKillNotice {
	tone: "ok" | "bad";
	text: string;
	/** Shown above the session list: the card may be gone at the next refresh (the session's own process has ended). */
	pageLevel: boolean;
}

/** What to tell the person after a Force kill: the session's own process and the processes it started are reported apart. */
export function forceKillNotice(
	r: { outcome: string; descendants?: number; remaining?: number; parent_running?: boolean },
	name: string,
	noun: string,
): ForceKillNotice {
	if (r.outcome === "force_exited") {
		return {
			tone: "ok",
			text: `“${name}” was force-killed. Its transcript up to that moment is kept: resume it with omp --resume.`,
			pageLevel: true,
		};
	}
	const left = r.remaining ?? 1;
	if (r.parent_running === false) {
		const others = Math.max(0, left);
		return {
			tone: "bad",
			text: `“${name}” itself has ended, but ${others} process${others === 1 ? "" : "es"} it started ${others === 1 ? "is" : "are"} still running. Check at the ${noun} (the session no longer appears in this list).`,
			pageLevel: true,
		};
	}
	const below = Math.max(0, left - 1);
	return {
		tone: "bad",
		text: `“${name}” is still running even after Force kill${below > 0 ? `, and so ${below === 1 ? "is 1 process" : `are ${below} processes`} it started` : ""}; check it at the ${noun}.`,
		pageLevel: false,
	};
}
