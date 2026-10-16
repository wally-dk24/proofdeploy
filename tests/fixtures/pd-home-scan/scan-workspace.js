// A workspace scanner in the shape of nx's project discovery
// (the Vendure failure mode, plan 1.2(k)): walk the workspace
// tree pruning only node_modules (and .git); any project.json
// found is a "project". The app itself ships none — so a clean
// scan proves no tool state leaked into the tree; a project.json
// appearing here can only have come from inside the tree, e.g. a
// package-manager cache living under a HOME inside the checkout.
const fs = require("fs");
const path = require("path");

const root = process.cwd();
const found = [];

(function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "node_modules" || entry.name === ".git") continue;
      walk(full);
    } else if (entry.isFile() && entry.name === "project.json") {
      found.push(path.relative(root, full));
    }
  }
})(root);

if (found.length > 0) {
  for (const rel of found) {
    let name = "?";
    try {
      name = JSON.parse(fs.readFileSync(path.join(root, rel), "utf8")).name;
    } catch (e) {
      // keep the default marker
    }
    console.error(
      `scan: project.json outside the declared workspace: ${rel} (project "${name}")`
    );
  }
  process.exit(1);
}
console.log("scan: workspace clean (no project.json outside node_modules)");
