import { BellRing } from "lucide-react";
import { useState } from "react";
import { getHubAlerts, sendHubTestAlert } from "../data/hub";
import { useQuery } from "../data/query";
import { Card, EmptyState, PageHeader, QueryView, TableSkeleton } from "../ui";
import { ago, Help, type Notice, NoticeLine } from "./hub-common";

export interface AlertsRouteProps {
	active: boolean;
}

export function AlertsRoute({ active }: AlertsRouteProps) {
	const alerts = useQuery(["hub-alerts"], ({ signal }) => getHubAlerts(signal), { enabled: active, pollMs: 30_000 });
	const [notice, setNotice] = useState<Notice | null>(null);
	const [sending, setSending] = useState(false);

	const test = async () => {
		setSending(true);
		setNotice(null);
		try {
			await sendHubTestAlert();
			setNotice({ tone: "ok", text: "Test alert sent: it should reach your phone in a few seconds." });
			alerts.refetch();
		} catch (error) {
			setNotice({ tone: "bad", text: `Could not send: ${error instanceof Error ? error.message : String(error)}` });
		} finally {
			setSending(false);
		}
	};

	return (
		<div className="page">
			<PageHeader
				title="Phone alerts"
				description="A push to your phone when a session needs you or finishes a long turn."
			/>
			<Help summary="How do alerts work?">
				<p>
					The hub watches the sessions and sends a push notification through ntfy (a third-party service, or a
					server of your own) when one needs your answer, or finishes a long turn. Alerts carry only the project
					folder, the model and the session's state: no prompt text, no transcript, no links. Anyone who knows the
					topic can read them.
				</p>
				<p>
					On the phone, install the <b>ntfy</b> app and subscribe to your topic. The topic is a secret and is not
					shown here: <code>omp-dashboard config set-ntfy-topic</code> prints it once when it is made.
				</p>
			</Help>
			<QueryView query={alerts} skeleton={<TableSkeleton rows={2} />}>
				{a =>
					!a.enabled ? (
						<Card>
							<EmptyState
								title="Alerts are off"
								hint={
									<>
										Run <code>omp-dashboard config set-ntfy-topic</code> on the machine the hub runs on. Nothing
										is sent anywhere until you do.
									</>
								}
							/>
							{a.last_error && (
								<p className="hub-notice" data-tone="bad">
									{a.last_error}
								</p>
							)}
						</Card>
					) : (
						<Card title="ntfy" description={`Messages go to ${a.server.replace(/\/$/, "")}`}>
							<p className="micro">
								{a.sent} sent since the hub started{a.last_sent_at ? `, last ${ago(a.last_sent_at)}` : ""}.
							</p>
							{a.last_error && (
								<p className="hub-notice" data-tone="bad">
									{a.last_error}
								</p>
							)}
							<div className="hub-actions">
								<button type="button" className="btn" data-variant="accent" disabled={sending} onClick={test}>
									<BellRing size={14} /> {sending ? "Sending…" : "Send test alert"}
								</button>
							</div>
							<NoticeLine notice={notice} />
						</Card>
					)
				}
			</QueryView>
		</div>
	);
}
