// Plan 1.2(n) regression scan (the 1.2(k) pd-home-scan pattern,
// aimed at the checkout root): list the checkout root and refuse
// when ProofDeploy's own tool state is inside the app's tree.
// .pd-tools (the bootstrapped managers / corepack / bun) and
// .venv (the app venv) belong in the per-side home (/pd-home);
// node_modules is the app's own install product and stays.
const fs = require("fs");
const entries = fs.readdirSync(process.cwd()).sort();
console.log("checkout root: " + entries.join(" "));
for (const bad of [".pd-tools", ".venv"]) {
  if (entries.includes(bad)) {
    console.error(`scan: ${bad} found in the checkout root`);
    process.exit(1);
  }
}
console.log("scan: checkout root clean (no .pd-tools, no .venv)");
