// Builds the dashboard page: src/ -> ../ui-dist (five files) and ../ui-build.json (their SHA-256 pins).
//
//   bun build.ts            build and write
//   bun build.ts --verify   build into a scratch directory and compare with what is committed; exit 1 on any difference
//
// The page is committed, not downloaded: a marketplace install copies the repository tree only, and the hub then serves the page
// without any network. The pins make that safe to check: the hub refuses a page whose files do not match them (python/omp_dashboard/ui.py),
// and `--verify` (make verify-ui) rebuilds from the sources with the Bun version recorded in the pin and compares.
import { createHash } from "node:crypto";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";

const HERE = import.meta.dir;
const ROOT = path.resolve(HERE, "..");
const DIST = path.join(ROOT, "ui-dist");
const PIN = path.join(ROOT, "ui-build.json");
const EXPECTED = ["favicon.svg", "index.css", "index.html", "index.js", "theme.js"] as const;
const sha256 = (data: Uint8Array | string): string => createHash("sha256").update(data).digest("hex");

// theme.js applies the persisted theme before first paint (no flash). It is a file, not an inline script, so the page works under a
// strict Content-Security-Policy (script-src 'self').
const THEME_JS = `(function () {
  try {
    var stored = localStorage.getItem("omp-dashboard-theme");
    var system = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    var theme = stored === "light" || stored === "dark" ? stored : system;
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
  } catch (e) {}
})();
`;

const INDEX_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<title>omp dashboard</title>
<script src="theme.js"></script>
<link rel="stylesheet" href="index.css">
<link rel="icon" type="image/svg+xml" href="favicon.svg">
</head>
<body>
<div id="root"></div>
<script src="index.js" type="module"></script>
</body>
</html>
`;

async function buildInto(outdir: string): Promise<Record<string, Uint8Array>> {
	await fs.rm(outdir, { recursive: true, force: true });
	const result = await Bun.build({
		entrypoints: [path.join(HERE, "src", "index.tsx")],
		outdir,
		minify: true,
		naming: "[dir]/[name].[ext]",
		define: { "process.env.NODE_ENV": JSON.stringify("production") },
	});
	if (!result.success) {
		for (const message of result.logs) console.error(message);
		throw new Error("the bundler failed");
	}
	await Bun.write(path.join(outdir, "theme.js"), THEME_JS);
	await Bun.write(path.join(outdir, "index.html"), INDEX_HTML);
	await fs.copyFile(path.join(HERE, "src", "favicon.svg"), path.join(outdir, "favicon.svg"));
	const names = (await fs.readdir(outdir)).sort();
	if (names.join(",") !== [...EXPECTED].join(",")) throw new Error(`unexpected build output: ${names.join(", ")} (expected ${EXPECTED.join(", ")})`);
	const files: Record<string, Uint8Array> = {};
	for (const name of names) files[name] = new Uint8Array(await fs.readFile(path.join(outdir, name)));
	return files;
}

/** ui-build/1: the SHA-256 and size of every file, and one hash over the set (see python/omp_dashboard/ui.py, tree_digest). */
function pinDocument(files: Record<string, Uint8Array>): string {
	const hashes = Object.fromEntries(Object.keys(files).sort().map(name => [name, sha256(files[name])]));
	const tree = createHash("sha256");
	for (const name of Object.keys(hashes)) tree.update(`${name}\0${hashes[name]}\n`);
	const doc = {
		schema: 1,
		bun: Bun.version,
		files: Object.fromEntries(Object.keys(hashes).map(name => [name, { sha256: hashes[name], size: files[name].length }])),
		treeSha256: tree.digest("hex"),
	};
	return `${JSON.stringify(doc, null, "\t")}\n`;
}

if (Bun.argv.includes("--verify")) {
	const committed = JSON.parse(await Bun.file(PIN).text()) as { bun: string };
	if (committed.bun !== Bun.version) {
		console.error(`verify-ui: the pin was built with Bun ${committed.bun}, this is Bun ${Bun.version}; use the same version (BUN_BIN) so the output is comparable`);
		process.exit(2);
	}
	const scratch = await fs.mkdtemp(path.join(os.tmpdir(), "omp-dashboard-ui-"));
	try {
		const files = await buildInto(path.join(scratch, "dist"));
		const problems: string[] = [];
		for (const name of EXPECTED) {
			const have = await Bun.file(path.join(DIST, name)).bytes().catch(() => null);
			if (!have || sha256(have) !== sha256(files[name])) problems.push(`${name} differs from a fresh build`);
		}
		for (const name of await fs.readdir(DIST)) if (!(EXPECTED as readonly string[]).includes(name)) problems.push(`${name} is not part of the build`);
		if ((await Bun.file(PIN).text()) !== pinDocument(files)) problems.push("ui-build.json differs from a fresh build");
		if (problems.length > 0) {
			for (const problem of problems) console.error(`verify-ui: ${problem}`);
			process.exit(1);
		}
		console.log(`verify-ui: ui-dist matches a fresh build with Bun ${Bun.version} (${EXPECTED.map(n => `${n} ${sha256(files[n]).slice(0, 12)}`).join(", ")})`);
	} finally {
		await fs.rm(scratch, { recursive: true, force: true });
	}
} else {
	const files = await buildInto(DIST);
	await Bun.write(PIN, pinDocument(files));
	for (const name of EXPECTED) console.log(name.padEnd(12), String(files[name].length).padStart(8), sha256(files[name]).slice(0, 16));
}
