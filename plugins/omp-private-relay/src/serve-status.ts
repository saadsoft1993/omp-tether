// Reading `tailscale serve status --json` for the doctor: is there a route from https://<this machine>:<port> to the
// guest listener, and is Funnel (the public internet) switched on for it?
export type ServeRoute = { state: "ok"; funnel: boolean } | { state: "absent" } | { state: "elsewhere"; target: string } | { state: "unreadable" };

const trimSlash = (s: string): string => s.replace(/\/+$/, "");

export function parseServeStatus(json: string, port: number, target: string): ServeRoute {
	let status: { Web?: Record<string, { Handlers?: Record<string, { Proxy?: string }> }>; AllowFunnel?: Record<string, boolean> };
	try {
		status = JSON.parse(json);
	} catch {
		return { state: "unreadable" };
	}
	if (!status || typeof status !== "object") return { state: "unreadable" };
	for (const [hostPort, web] of Object.entries(status.Web ?? {})) {
		if (!hostPort.endsWith(`:${port}`)) continue;
		const proxy = web?.Handlers?.["/"]?.Proxy;
		if (typeof proxy !== "string") return { state: "elsewhere", target: "(not a proxy)" };
		if (trimSlash(proxy) !== trimSlash(target)) return { state: "elsewhere", target: proxy };
		return { state: "ok", funnel: status.AllowFunnel?.[hostPort] === true };
	}
	return { state: "absent" };
}
