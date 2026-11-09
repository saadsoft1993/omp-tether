import { getHubRelay } from "../data/hub";
import { useQuery } from "../data/query";
import { Badge, Card, EmptyState, ErrorState, PageHeader, QueryView, Stat, StatGrid, TableSkeleton } from "../ui";
import { Help, useNoun } from "./hub-common";

export interface RelayRouteProps {
	active: boolean;
}

const REFRESH_MS = 15_000;

function uptime(seconds: number | null | undefined): string {
	if (seconds == null) return "–";
	if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
	if (seconds < 172_800) return `${Math.floor(seconds / 3600)}h`;
	return `${Math.floor(seconds / 86_400)}d`;
}

export function RelayRoute({ active }: RelayRouteProps) {
	const noun = useNoun();
	const relay = useQuery(["hub-relay"], ({ signal }) => getHubRelay(signal), { enabled: active, pollMs: REFRESH_MS });

	return (
		<div className="page">
			<PageHeader
				title="Collab relay"
				description={`The small server on this ${noun} that connects your browser to omp sessions. Refreshes every 15 seconds.`}
				actions={
					relay.data?.enabled !== false &&
					relay.data && <Badge tone={relay.data.ok ? "ok" : "bad"}>{relay.data.ok ? "Running" : "Problem"}</Badge>
				}
			/>
			<Help summary="What is this?">
				<p>
					Sessions reach the relay on the {noun} itself; browsers reach it only the way you set up (a tunnel, or
					Tailscale), and only if paired with this dashboard. Unpairing a browser (<code>omp-dashboard revoke</code>)
					also cuts its open session tabs within about 10 seconds.
				</p>
				<p>
					<b>Rooms</b> are sessions currently sharing; <b>viewers</b> are open browser tabs. If it shows "not
					reachable", sessions keep working at the {noun} but you can't open them from here; start the relay again
					(a service manager does that on its own), and open tabs reconnect after a restart.
				</p>
			</Help>
			<QueryView query={relay} skeleton={<TableSkeleton rows={2} />}>
				{data =>
					data.enabled === false ? (
						<Card>
							<EmptyState
								title="No private relay is configured"
								hint={
									<>
										Install <code>omp-private-relay</code> and set <code>relay.enabled</code> in the hub's
										configuration to open sessions from this page (see the README).
									</>
								}
							/>
						</Card>
					) : (
						<>
							{data.error && <ErrorState error={data.error} onRetry={relay.refetch} />}
							{data.rooms != null && (
								<Card>
									<StatGrid>
										<Stat label="Rooms" value={data.rooms} hint="Sessions sharing now" />
										<Stat label="Viewers" value={data.guests ?? 0} hint="Open browser tabs" />
										<Stat label="Up" value={uptime(data.uptime_s)} hint="Since the relay last started" />
										<Stat
											label="Web build"
											value={data.build ? data.build.split(" ")[0] : "–"}
											hint="collab-web the relay serves"
											size="sm"
										/>
									</StatGrid>
								</Card>
							)}
						</>
					)
				}
			</QueryView>
		</div>
	);
}
