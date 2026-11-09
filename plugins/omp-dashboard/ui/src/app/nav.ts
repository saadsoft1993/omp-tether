import { BellRing, type LucideIcon, SquareTerminal, Waypoints } from "lucide-react";

export type Section = "sessions" | "relay" | "alerts";

export const NAV: { id: Section; label: string; icon: LucideIcon }[] = [
	{ id: "sessions", label: "Sessions", icon: SquareTerminal },
	{ id: "relay", label: "Relay", icon: Waypoints },
	{ id: "alerts", label: "Alerts", icon: BellRing },
];

export function sectionFromHash(hash: string): Section {
	const id = hash.replace(/^#\/?/, "").split("?")[0];
	return NAV.some(n => n.id === id) ? (id as Section) : "sessions";
}

/** An address a page may be sent to: absolute, http or https, nothing else (no javascript:, data:, file:, blob:). */
export function webUrl(value: unknown): string | null {
	if (typeof value !== "string" || value === "") return null;
	try {
		const url = new URL(value);
		return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
	} catch {
		return null;
	}
}

/** Where the Usage link goes: only an http(s) address the hub reports (stats.url); anything else is not linked. */
export function usageHref(statsUrl: string | null | undefined): string | null {
	return webUrl(statsUrl);
}
