import { Eye, Pencil, Play, Power, Square, SquareTerminal } from "lucide-react";
import { type FormEvent, useMemo, useState } from "react";
import { webUrl } from "../app/nav";
import {
	forceKillHubSession,
	getHubLink,
	getHubProjects,
	getHubSessions,
	type HubProject,
	type HubSession,
	type HubStarted,
	killHubSession,
	renameHubSession,
	startHubSession,
	stopHubSession,
} from "../data/hub";
import { useQuery } from "../data/query";
import {
	forceKillNotice,
	killConfirmMatches,
	MAX_NAME_CHARS,
	nameProblem,
	placeLabel,
	sessionControls,
	sessionDisplayName,
} from "../data/session-controls";
import { Badge, Card, EmptyState, PageHeader, QueryView, SearchInput, TableSkeleton, type Tone } from "../ui";
import { ago, Help, type Notice, NoticeLine, useNoun } from "./hub-common";

export interface SessionsRouteProps {
	active: boolean;
}

const REFRESH_MS = 5000;
const MAX_PROJECTS = 60;

function sessionState(s: HubSession): { label: string; tone: Tone } {
	if (s.inputRequired) return { label: "Needs you", tone: "warn" };
	if (s.busy === true) return { label: "Working", tone: "accent" };
	if (s.busy === false) return { label: "Idle", tone: "ok" };
	return { label: "Unknown", tone: "neutral" };
}

const RELAY_LABEL: Record<NonNullable<HubSession["relay"]>, { label: string; tone: Tone }> = {
	private: { label: "Private relay", tone: "ok" },
	public: { label: "Still on the public relay", tone: "warn" },
	unknown: { label: "Relay unknown", tone: "neutral" },
	off: { label: "No private relay", tone: "neutral" },
};

/** Why Open and Watch are off for a session, or null when they are on. */
function openReason(s: HubSession): string | null {
	switch (s.relay ?? "unknown") {
		case "private":
			return null;
		case "public":
			return "Still on the public relay";
		case "off":
			return "No private relay is configured (relay.enabled)";
		default:
			return "The hub cannot tell which relay this session uses";
	}
}

/** Needs you first, then working, then newest. */
function sessionOrder(a: HubSession, b: HubSession): number {
	return (
		Number(b.inputRequired ?? false) - Number(a.inputRequired ?? false) ||
		Number(b.busy === true) - Number(a.busy === true) ||
		(b.startedAt ?? 0) - (a.startedAt ?? 0)
	);
}

function errorText(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

/**
 * The relay's web client is its own origin: open it in a new tab, never inside the dashboard. The tab is
 * opened during the click itself (Safari blocks tabs opened after an await) and cut off from this page
 * before the link is fetched, so the link is current and the opened page can't reach back in here.
 */
async function openSession(instanceId: string, view: boolean): Promise<void> {
	const tab = window.open("about:blank", "_blank");
	if (tab) tab.opener = null;
	try {
		const answer = await getHubLink(instanceId, view);
		// The hub is trusted, but this is where a link leaves the page: only an http(s) address is ever opened.
		const url = webUrl(answer.url);
		if (!url) throw new Error("the hub sent a link that is not an http or https address");
		if (tab) tab.location.replace(url);
		else window.open(url, "_blank", "noopener,noreferrer");
	} catch (error) {
		tab?.close();
		throw error;
	}
}

export function SessionsRoute({ active }: SessionsRouteProps) {
	const noun = useNoun();
	const sessions = useQuery(["hub-sessions"], ({ signal }) => getHubSessions(signal), {
		enabled: active,
		pollMs: REFRESH_MS,
	});
	const [notice, setNotice] = useState<Notice | null>(null);

	const open = (instanceId: string, view: boolean) => {
		setNotice(null);
		openSession(instanceId, view).catch((error: unknown) => {
			setNotice({ tone: "bad", text: `Could not open: ${errorText(error)}` });
			sessions.refetch();
		});
	};

	return (
		<div className="page">
			<PageHeader
				title="Sessions"
				description={
					sessions.data
						? `Every omp session on this ${noun} that shares through Collab. Updated ${new Date(sessions.data.now).toLocaleTimeString()} · refreshes every 5 seconds.`
						: `Every omp session on this ${noun} that shares through Collab.`
				}
			/>
			<Help summary="What can I do here?">
				<p>
					<b>Needs you</b> means it is waiting for your answer; <b>Working</b> means a turn is running;{" "}
					<b>Idle</b> means it is waiting for a prompt.
				</p>
				<p>
					<b>Open &amp; control</b> opens the session in a new tab where you can read it, send prompts, answer its
					questions, approve tools and stop a running turn. <b>Watch</b> opens the same view read-only: you see
					everything but can't type or press anything that changes the session. Use it when you only want to look,
					or to show someone.
				</p>
				<p>
					<b>Stop</b> (shown while it works) interrupts the running turn, like pressing Esc at the {noun}; the
					session stays open. <b>Rename</b> changes the session's name. <b>Kill</b> ends the session's omp process,
					and any subagents it is running stop with it. Its transcript stays on the {noun}: resume it there later
					with <code>omp --resume</code>. Every Stop, Rename, Kill and opened link is written to the hub's audit log.
				</p>
				<p>
					Stop and Rename need the <b>omp-session-control</b> extension, turned on, in sessions that started after
					that. A session that was already running shows "Stop and Rename need omp-session-control…"; Kill works for
					every session.
				</p>
				<p>
					Open and Watch use the private relay on this {noun} (omp-private-relay), only from browsers you paired.
					The conversation is end-to-end encrypted between the session and your browser; the relay only passes
					sealed messages along.
				</p>
				<p>
					<b>Still on the public relay</b>: the session started before the switch to the private relay and still
					shares through omp's public server. Its links are never opened from here, so its Open and Watch buttons
					are off. To fix it, in that session run <code>/collab stop</code>, then <code>/collab</code>; it moves to
					the private relay and the buttons come back within a few seconds.
				</p>
				<p>
					The model, thinking level and plan mode can't be changed from here yet; change them at the {noun}. Each
					link is fetched fresh when you click, so it always points at the session's current room.
				</p>
			</Help>

			<NoticeLine notice={notice} />
			<QueryView
				query={sessions}
				skeleton={<TableSkeleton rows={3} />}
				isEmpty={data => data.sessions.length === 0}
				empty={
					<Card>
						<EmptyState
							title="No live omp sessions"
							hint={
								<>
									Start one below, or run <code>omp</code> on the {noun}.
								</>
							}
						/>
					</Card>
				}
			>
				{data => (
					<div className="hub-sessions">
						{[...data.sessions].sort(sessionOrder).map((s, i) => (
							<SessionCard
								key={s.instanceId}
								session={s}
								controlProblem={data.controlProblem ?? null}
								index={i}
								now={data.now}
								onOpen={open}
								onChanged={sessions.refetch}
								onEnded={setNotice}
							/>
						))}
					</div>
				)}
			</QueryView>

			<StartSession active={active} onStarted={sessions.refetch} />
		</div>
	);
}

function SessionCard({
	session: s,
	controlProblem,
	index,
	now,
	onOpen,
	onChanged,
	onEnded,
}: {
	session: HubSession;
	controlProblem: string | null;
	index: number;
	now: number;
	onOpen: (instanceId: string, view: boolean) => void;
	onChanged: () => void;
	onEnded: (notice: Notice) => void;
}) {
	const state = sessionState(s);
	const relay = RELAY_LABEL[s.relay ?? "unknown"];
	const closed = openReason(s);
	const place = placeLabel(s);
	const facts = [
		s.model?.id,
		s.startedAt ? `started ${ago(s.startedAt, now)}` : null,
		s.participants !== undefined ? `${s.participants} connected` : null,
		s.pid !== undefined ? `pid ${s.pid}` : null,
	].filter(Boolean);
	return (
		<Card
			index={index}
			title={<span className="truncate">{sessionDisplayName(s)}</span>}
			description={(s.cwd ?? "").replace(/^\/(?:Users|home)\/[^/]+/, "~")}
			actions={<Badge tone={state.tone}>{state.label}</Badge>}
		>
			<div className="hub-session-meta">
				<Badge tone={relay.tone}>{relay.label}</Badge>
				{s.relayConnected === false && <Badge tone="bad">Relay offline</Badge>}
				{place && <Badge tone="neutral">{place}</Badge>}
				<span className="micro">{facts.join(" · ")}</span>
			</div>
			{s.relay === "public" && (
				<p className="hub-warn">
					Still on the public relay, so it can't be opened from here. Fix: in that session run{" "}
					<code>/collab stop</code>, then <code>/collab</code>.
				</p>
			)}
			<div className="hub-actions">
				<button
					type="button"
					className="btn"
					data-variant="accent"
					disabled={closed !== null}
					title={closed ?? "Open in a new tab with full control"}
					onClick={() => onOpen(s.instanceId, false)}
				>
					<SquareTerminal size={14} /> Open &amp; control
				</button>
				<button
					type="button"
					className="btn"
					disabled={closed !== null}
					title={closed ?? "Open read-only in a new tab"}
					onClick={() => onOpen(s.instanceId, true)}
				>
					<Eye size={14} /> Watch
				</button>
			</div>
			{closed !== null && s.relay !== "public" && <p className="micro hub-reason">{closed}.</p>}
			<SessionControlsBar session={s} controlProblem={controlProblem} onChanged={onChanged} onEnded={onEnded} />
		</Card>
	);
}

type Panel = "rename" | "kill" | null;

/** Stop, Rename and Kill (with Force kill only after a Kill that did not end the session). */
function SessionControlsBar({
	session: s,
	controlProblem,
	onChanged,
	onEnded,
}: {
	session: HubSession;
	controlProblem: string | null;
	onChanged: () => void;
	onEnded: (notice: Notice) => void;
}) {
	const noun = useNoun();
	const controls = sessionControls(s, controlProblem);
	const name = sessionDisplayName(s);
	const [panel, setPanel] = useState<Panel>(null);
	const [pending, setPending] = useState<string | null>(null);
	const [notice, setNotice] = useState<Notice | null>(null);
	const [newName, setNewName] = useState("");
	const [typed, setTyped] = useState("");
	const [forceOffered, setForceOffered] = useState(false);
	const [forceConfirming, setForceConfirming] = useState(false);
	const sessionId = s.sessionId ?? "";

	const run = async (label: string, action: () => Promise<void>) => {
		setPending(label);
		setNotice(null);
		try {
			await action();
		} catch (error) {
			setNotice({ tone: "bad", text: `${label} failed: ${errorText(error)}` });
		} finally {
			setPending(null);
			onChanged();
		}
	};

	const stop = () =>
		run("Stop", async () => {
			const r = await stopHubSession(sessionId);
			setNotice({
				tone: "ok",
				text:
					r.outcome === "stopped"
						? "Stopped the running turn. The session is still open."
						: "Nothing was running.",
			});
		});

	const rename = (event: FormEvent) => {
		event.preventDefault();
		run("Rename", async () => {
			const r = await renameHubSession(sessionId, newName);
			setPanel(null);
			setNotice({ tone: "ok", text: `Renamed to “${r.name ?? newName.trim()}”.` });
		});
	};

	const kill = (event: FormEvent) => {
		event.preventDefault();
		run("Kill", async () => {
			const r = await killHubSession(sessionId, typed);
			if (r.outcome === "exited") {
				setPanel(null);
				// The card disappears with the session, so the outcome is shown above the list.
				onEnded({
					tone: "ok",
					text: `“${name}” has ended. Its transcript is kept: resume it at the ${noun} with omp --resume.`,
				});
				return;
			}
			setForceOffered(true);
			setNotice({
				tone: "bad",
				text: "It is still running 10 seconds after being asked to end. Wait a little and refresh, or use Force kill.",
			});
		});
	};

	const forceKill = () =>
		run("Force kill", async () => {
			const r = await forceKillHubSession(sessionId, typed);
			setForceConfirming(false);
			const result = forceKillNotice(r, name, noun);
			if (r.outcome === "force_exited" || result.pageLevel) {
				setPanel(null);
				setForceOffered(false);
				onEnded({ tone: result.tone, text: result.text });
			} else {
				setNotice({ tone: result.tone, text: result.text });
			}
		});

	const toggle = (next: Panel) => {
		setNotice(null);
		setForceOffered(false);
		setForceConfirming(false);
		setPanel(panel === next ? null : next);
		if (next === "rename") setNewName(s.sessionName ?? "");
		if (next === "kill") setTyped("");
	};

	const extensionReason = controls.rename.enabled ? null : controls.rename.reason;
	return (
		<div className="hub-controls">
			<div className="hub-actions">
				{controls.showStop && (
					<button
						type="button"
						className="btn"
						disabled={!controls.stop.enabled || pending !== null}
						title={controls.stop.reason ?? "Interrupt the running turn; the session stays open"}
						onClick={stop}
					>
						<Square size={13} /> {pending === "Stop" ? "Stopping…" : "Stop"}
					</button>
				)}
				<button
					type="button"
					className="btn"
					disabled={!controls.rename.enabled || pending !== null}
					title={controls.rename.reason ?? "Change this session's name"}
					aria-expanded={panel === "rename"}
					onClick={() => toggle("rename")}
				>
					<Pencil size={13} /> Rename
				</button>
				<button
					type="button"
					className="btn"
					data-variant="danger"
					disabled={!controls.kill.enabled || pending !== null}
					title={controls.kill.reason ?? "End this session's omp process"}
					aria-expanded={panel === "kill"}
					onClick={() => toggle("kill")}
				>
					<Power size={13} /> Kill
				</button>
			</div>
			{extensionReason && <p className="micro hub-reason">{extensionReason}</p>}
			{panel === "rename" && (
				<form className="hub-inline-form" onSubmit={rename}>
					<label className="micro" htmlFor={`rename-${s.instanceId}`}>
						New name (1–{MAX_NAME_CHARS} characters, no invisible characters)
					</label>
					<div className="hub-inline-row">
						<input
							id={`rename-${s.instanceId}`}
							className="input"
							value={newName}
							maxLength={MAX_NAME_CHARS}
							onChange={event => setNewName(event.target.value)}
						/>
						<button
							type="submit"
							className="btn"
							data-variant="primary"
							disabled={nameProblem(newName) !== null || pending !== null}
							title={nameProblem(newName) ?? undefined}
						>
							{pending === "Rename" ? "Saving…" : "Save"}
						</button>
						<button type="button" className="btn" data-variant="ghost" onClick={() => setPanel(null)}>
							Cancel
						</button>
					</div>
				</form>
			)}
			{panel === "kill" && (
				<form className="hub-inline-form hub-danger" onSubmit={kill}>
					<p>
						Kill ends this session's omp process, and any subagents it is running stop with it. Its transcript
						stays on the {noun}: resume it later with <code>omp --resume</code>.
					</p>
					<label className="micro" htmlFor={`kill-${s.instanceId}`}>
						To confirm, type the session's name <b>{name}</b>
						{s.sessionName ? " (or its project folder's name)" : ""}
					</label>
					<div className="hub-inline-row">
						<input
							id={`kill-${s.instanceId}`}
							className="input"
							value={typed}
							autoComplete="off"
							onChange={event => setTyped(event.target.value)}
						/>
						<button
							type="submit"
							className="btn"
							data-variant="danger"
							disabled={!killConfirmMatches(s, typed) || pending !== null}
						>
							{pending === "Kill" ? "Ending…" : "Kill session"}
						</button>
						<button type="button" className="btn" data-variant="ghost" onClick={() => setPanel(null)}>
							Cancel
						</button>
					</div>
					{forceOffered && !forceConfirming && (
						<button
							type="button"
							className="btn"
							data-variant="danger"
							disabled={pending !== null}
							onClick={() => setForceConfirming(true)}
						>
							Force kill…
						</button>
					)}
					{forceConfirming && (
						<div className="hub-inline-form">
							<p>
								Force kill stops it at once, without letting it finish writing or close its subagents cleanly.
								Only use it when Kill did not work.
							</p>
							<div className="hub-inline-row">
								<button
									type="button"
									className="btn"
									data-variant="danger"
									disabled={!killConfirmMatches(s, typed) || pending !== null}
									onClick={forceKill}
								>
									{pending === "Force kill" ? "Force killing…" : "Yes, force kill"}
								</button>
								<button
									type="button"
									className="btn"
									data-variant="ghost"
									onClick={() => setForceConfirming(false)}
								>
									No
								</button>
							</div>
						</div>
					)}
				</form>
			)}
			<NoticeLine notice={notice} />
		</div>
	);
}

function startedText(project: HubProject, r: HubStarted): string {
	const where = r.workspace ? `${r.backend}: ${r.workspace} › ${r.tab ?? "omp"}` : `a new ${r.backend} pane`;
	return r.instanceId
		? `Started ${project.name} in ${where}. It is listed above: click Open & control to talk to it.`
		: `Started ${project.name} in ${where}; it is not sharing yet and shows up above in a few seconds.`;
}

function StartSession({ active, onStarted }: { active: boolean; onStarted: () => void }) {
	const noun = useNoun();
	const projects = useQuery(["hub-projects"], ({ signal }) => getHubProjects(signal), { enabled: active });
	const [query, setQuery] = useState("");
	const [starting, setStarting] = useState<string | null>(null);
	const [notice, setNotice] = useState<Notice | null>(null);

	const matches = useMemo(() => {
		const q = query.trim().toLowerCase();
		const all = projects.data?.projects ?? [];
		return all.filter(p => !q || p.label.toLowerCase().includes(q)).slice(0, MAX_PROJECTS);
	}, [projects.data, query]);

	const start = async (project: HubProject) => {
		setStarting(project.path);
		setNotice(null);
		try {
			const r = await startHubSession(project.path);
			onStarted();
			setNotice({ tone: "ok", text: startedText(project, r) });
		} catch (error) {
			setNotice({ tone: "bad", text: `Could not start: ${errorText(error)}` });
		} finally {
			setStarting(null);
		}
	};

	if (projects.data?.backend === "off") {
		return (
			<Card title="Start a session" description="Off">
				<p className="micro">
					Start opens a new omp session in a project folder, in a herdr tab or a tmux window. It is off until you
					choose a backend and the folders it may use: set <code>start.backend</code> and <code>start.roots</code>{" "}
					in the hub's configuration (see the README).
				</p>
			</Card>
		);
	}
	const backend = projects.data?.backend ?? "herdr";
	return (
		<Card
			title="Start a session"
			description={projects.data?.projects.some(p => p.last_used) ? "Most recently used projects first" : "Projects"}
			actions={
				<SearchInput value={query} onChange={setQuery} placeholder="Search projects" label="Search projects" />
			}
		>
			<Help summary="What does Start do?">
				<p>
					Starts a new omp session in that project on this {noun}, in{" "}
					{backend === "tmux"
						? "tmux: a new window in the hub's tmux session"
						: "herdr: a new tab called omp in the project's herdr workspace (a workspace named after the project is created when there is none)"}
					. It uses your default model. The only text typed into the new pane is <code>omp</code>. Once the session
					shows up above, click <b>Open &amp; control</b> to talk to it.
				</p>
			</Help>
			<NoticeLine notice={notice} />
			<QueryView
				query={projects}
				skeleton={<TableSkeleton rows={4} />}
				isEmpty={() => matches.length === 0}
				empty={<EmptyState title="No matching project" hint="Folders come from start.roots in the configuration." />}
			>
				{() => (
					<ul className="hub-projects">
						{matches.map(p => (
							<li key={p.path} className="hub-project">
								<div className="stack">
									<span className="hub-project-name truncate">{p.name}</span>
									<span className="micro truncate">
										{p.label}
										{p.last_used ? ` · used ${ago(p.last_used)}` : ""}
									</span>
								</div>
								<button
									type="button"
									className="btn"
									data-size="sm"
									disabled={starting !== null}
									onClick={() => start(p)}
								>
									<Play size={13} /> {starting === p.path ? "Starting…" : "Start"}
								</button>
							</li>
						))}
					</ul>
				)}
			</QueryView>
		</Card>
	);
}
