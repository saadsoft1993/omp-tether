import { useEffect } from "react";
import { getHubMachine } from "../data/hub";
import { useQuery } from "../data/query";
import { machineView } from "./machine";

const REFRESH_MS = 5 * 60_000;

/** Machine-name chip in the topbar: tells two otherwise identical dashboards apart. Renders nothing without a name. */
export function MachineBanner() {
	const machine = useQuery(["hub-machine"], ({ signal }) => getHubMachine(signal), { pollMs: REFRESH_MS });
	const view = machineView(machine.data?.name, machine.data?.noun);
	const name = view?.name;
	useEffect(() => {
		if (!name) return;
		const previous = document.title;
		document.title = `${name} · ${previous.replace(/^.* · /, "")}`;
		return () => {
			document.title = previous;
		};
	}, [name]);
	if (!view) return null;
	return (
		<div
			className="machine-banner"
			role="status"
			aria-label={view.label}
			title={view.label}
			style={{ "--machine-accent": view.accent } as React.CSSProperties}
		>
			<span className="machine-banner-dot" aria-hidden="true" />
			<span className="machine-banner-name">{view.name}</span>
			<span className="machine-banner-scope">{view.scope}</span>
		</div>
	);
}
