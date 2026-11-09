import type { ReactNode } from "react";
import { getHubMachine } from "../data/hub";
import { useQuery } from "../data/query";
import "./hub.css";

/** What the page calls the machine the hub runs on: "Mac" on macOS, "machine" elsewhere (until the hub has answered). */
export function useNoun(): string {
	const machine = useQuery(["hub-machine"], ({ signal }) => getHubMachine(signal), { pollMs: 5 * 60_000 });
	return machine.data?.noun || "machine";
}

/** Plain-language help under a page or card title, collapsed until asked for. */
export function Help({ summary, children }: { summary: string; children: ReactNode }) {
	return (
		<details className="hub-help">
			<summary>{summary}</summary>
			{children}
		</details>
	);
}

/** "5m ago" style age of a millisecond timestamp. */
export function ago(timestamp: number | null | undefined, now = Date.now()): string {
	if (!timestamp) return "";
	const m = Math.floor((now - timestamp) / 60_000);
	if (m < 1) return "just now";
	if (m < 60) return `${m}m ago`;
	if (m < 2880) return `${Math.floor(m / 60)}h ago`;
	return `${Math.floor(m / 1440)}d ago`;
}

/** Inline result line for an action (start, open, test alert). */
export interface Notice {
	tone: "ok" | "bad";
	text: string;
}

export function NoticeLine({ notice }: { notice: Notice | null }) {
	if (!notice) return null;
	return (
		<p className="hub-notice" data-tone={notice.tone} role={notice.tone === "bad" ? "alert" : "status"}>
			{notice.text}
		</p>
	);
}
