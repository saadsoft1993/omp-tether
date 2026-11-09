import { ExternalLink, Menu } from "lucide-react";
import { useEffect, useState } from "react";
import { MachineBanner } from "./app/MachineBanner";
import { NAV, type Section, sectionFromHash, usageHref } from "./app/nav";
import { PairScreen } from "./app/PairScreen";
import { ThemeToggle } from "./app/ThemeToggle";
import { getHubAbout } from "./data/hub";
import { useUnpaired } from "./data/pairing";
import { useQuery } from "./data/query";
import { AlertsRoute } from "./routes/AlertsRoute";
import { RelayRoute } from "./routes/RelayRoute";
import { SessionsRoute } from "./routes/SessionsRoute";

export default function App() {
	const unpaired = useUnpaired();
	const [section, setSection] = useState<Section>(() => sectionFromHash(location.hash));
	const [menuOpen, setMenuOpen] = useState(false);
	const about = useQuery(["hub-about"], ({ signal }) => getHubAbout(signal), { enabled: !unpaired });
	const usage = usageHref(about.data?.statsUrl);
	useEffect(() => {
		const on = () => setSection(sectionFromHash(location.hash));
		addEventListener("hashchange", on);
		return () => removeEventListener("hashchange", on);
	}, []);
	if (unpaired) return <PairScreen />;
	const go = (id: Section) => {
		location.hash = `#/${id}`;
		setMenuOpen(false);
		window.scrollTo({ top: 0 });
	};
	return (
		<>
			<div className="shell-ambient" />
			<header className="topbar">
				<button
					type="button"
					className="btn topbar-menu"
					data-variant="ghost"
					data-icon="true"
					onClick={() => setMenuOpen(true)}
					aria-label="Open navigation"
				>
					<Menu size={16} />
				</button>
				<div className="topbar-brand">
					<span>omp</span>
					<span className="topbar-slash">/</span>
					<span className="topbar-title">dashboard</span>
				</div>
				<MachineBanner />
				<div className="topbar-spacer" />
				<div className="topbar-actions">
					<ThemeToggle />
				</div>
			</header>
			{menuOpen && <div className="drawer-scrim" onClick={() => setMenuOpen(false)} role="presentation" />}
			<nav className="sidebar" data-open={menuOpen} aria-label="Pages">
				<div className="nav-group">
					<div className="nav-heading">Control</div>
					{NAV.map(item => (
						<button
							key={item.id}
							type="button"
							className="nav-row"
							data-active={item.id === section}
							aria-current={item.id === section ? "page" : undefined}
							onClick={() => go(item.id)}
						>
							<item.icon size={15} />
							<span className="nav-row-label">{item.label}</span>
						</button>
					))}
				</div>
				{usage && (
					<div className="nav-group">
						<div className="nav-heading">Usage</div>
						<a className="nav-row" href={usage} target="_blank" rel="noopener noreferrer">
							<ExternalLink size={15} />
							<span className="nav-row-label">omp stats</span>
						</a>
					</div>
				)}
			</nav>
			<main className="shell-panel">
				<div className="shell-content">
					{section === "sessions" && <SessionsRoute active />}
					{section === "relay" && <RelayRoute active />}
					{section === "alerts" && <AlertsRoute active />}
				</div>
			</main>
		</>
	);
}
