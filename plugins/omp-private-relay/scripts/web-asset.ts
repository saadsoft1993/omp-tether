// Release helper for the web client asset (used by scripts/release-web.sh and CI; users never run it).
//
//   web-asset.ts pack   --dist DIR --tag vX.Y.Z --out DIR      write omp-private-relay-web-<minor>.tar (adds NOTICE)
//   web-asset.ts record --asset FILE --tag vX.Y.Z --bun X.Y.Z --release-tag TAG [--repository owner/repo]
//                                                              pin that asset in web-builds.json
//   web-asset.ts verify --asset FILE --tag vX.Y.Z              check an asset against web-builds.json (exit 1 when not equal)
import * as fs from "node:fs";
import * as path from "node:path";
import { pluginRoot } from "../src/paths";
import { loadWebBuilds, packTar, readTree, sha256, treeHash, unpackTar, verifyAsset, type WebBuilds, webBuildsFile } from "../src/web-asset";

function option(name: string): string | undefined {
	const argv = Bun.argv.slice(3);
	const i = argv.indexOf(`--${name}`);
	return i >= 0 ? argv[i + 1] : undefined;
}

function need(name: string): string {
	const v = option(name);
	if (!v) {
		console.error(`missing --${name}`);
		process.exit(2);
	}
	return v;
}

function minorOfTag(tag: string): string {
	const m = /^v(\d+)\.(\d+)\.\d+$/.exec(tag);
	if (!m) {
		console.error("--tag must look like v18.18.0");
		process.exit(2);
	}
	return `${m[1]}.${m[2]}`;
}

const command = Bun.argv[2];

if (command === "pack") {
	const files = readTree(need("dist"));
	files.set("NOTICE", fs.readFileSync(path.join(pluginRoot(), "NOTICE")));
	const out = path.join(need("out"), `omp-private-relay-web-${minorOfTag(need("tag"))}.tar`);
	fs.mkdirSync(path.dirname(out), { recursive: true });
	const tar = packTar(files);
	fs.writeFileSync(out, tar);
	console.log(JSON.stringify({ asset: out, sha256: sha256(tar), treeSha256: treeHash(files), bytes: tar.byteLength, files: files.size }));
} else if (command === "record") {
	const data = fs.readFileSync(need("asset"));
	const files = unpackTar(data);
	const tag = need("tag");
	const builds: WebBuilds = fs.existsSync(webBuildsFile())
		? loadWebBuilds()
		: { schema: 1, repository: option("repository") ?? "saadsoft1993/omp-tether", releaseTag: need("release-tag"), builds: {} };
	builds.releaseTag = need("release-tag");
	if (option("repository")) builds.repository = option("repository") as string;
	builds.builds[minorOfTag(tag)] = {
		ompTag: tag,
		bun: need("bun"),
		asset: path.basename(need("asset")),
		sha256: sha256(data),
		treeSha256: treeHash(files),
		noticeSha256: sha256(files.get("NOTICE") as Buffer),
		bytes: data.byteLength,
		files: files.size,
	};
	const sorted: WebBuilds = { ...builds, builds: Object.fromEntries(Object.entries(builds.builds).sort(([a], [b]) => a.localeCompare(b, "en", { numeric: true }))) };
	fs.writeFileSync(webBuildsFile(), `${JSON.stringify(sorted, null, "\t")}\n`);
	console.log(`pinned ${path.basename(need("asset"))} for omp ${minorOfTag(tag)}`);
} else if (command === "verify") {
	const data = fs.readFileSync(need("asset"));
	const build = loadWebBuilds().builds[minorOfTag(need("tag"))];
	let ok = false;
	try {
		// the same complete check `init` makes before it installs: archive hash, content hash and NOTICE
		ok = build !== undefined && (verifyAsset(data, build), true);
	} catch {
		ok = false;
	}
	console.log(ok ? "asset matches web-builds.json" : "asset does NOT match web-builds.json");
	process.exit(ok ? 0 : 1);
} else {
	console.error("usage: web-asset.ts pack|record|verify (see the header of this file)");
	process.exit(2);
}
