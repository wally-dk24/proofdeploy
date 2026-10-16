// Contract seed for the Ghost readiness contract (D87).
//
// Adapted from the WO-8.3 proof seed (tests/test_wo83_sandbox.py,
// GHOST_SEED_JS) in layout only: that seed booted the npm-tarball
// tree (`node index.js` at the repo root); this one boots the git
// monorepo the readiness contract provisions — a temporary Ghost
// from ghost/core, in the contract's own D71 start form
// (`node --conditions=source --import=tsx index.js`) — against the
// checkout's SQLite database, runs the instance's own setup with
// the per-run bootstrap credentials, creates ONE integration
// through the Admin API, and writes the <id>:<secret> Admin API
// key to the contract's declared key file (.pd-auth/admin_key).
// The app container then starts on the same database;
// ProofDeploy's host-side ghost_jwt provider reads the key file
// after READY and mints a fresh JWT per request.
//
// Generic only (rule 2): the seed creates the owner account and
// one integration, nothing else — no posts, members, tiers,
// offers, settings or flags.
const { spawn } = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');

const PORT = 3999;
const BASE = `http://127.0.0.1:${PORT}`;
const CORE_DIR = path.join(__dirname, 'ghost', 'core');

function request(method, urlPath, body, headers) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const req = http.request(
      BASE + urlPath,
      {
        method,
        headers: {
          'Content-Type': 'application/json',
          ...(data ? { 'Content-Length': Buffer.byteLength(data) } : {}),
          ...(headers || {}),
        },
      },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () =>
          resolve({
            status: res.statusCode,
            headers: res.headers,
            text: Buffer.concat(chunks).toString('utf8'),
          })
        );
      }
    );
    req.on('error', reject);
    if (data) req.write(data);
    req.end();
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitUp(child) {
  // "Up" means the app answers like a running Ghost, not the 503
  // maintenance page it serves for every route during boot
  // (migrations). The poll target is the site endpoint, whose
  // read is public by design (permissions: false in Ghost's
  // core/server/api/endpoints/site.js), so it answers 200 with
  // or without auth, before and after setup; only 503 means
  // still booting. Auth enforcement is proven separately
  // against /ghost/api/admin/tags/, whose browse requires
  // permission (permissions: true).
  for (let i = 0; i < 150; i++) {
    if (child.exitCode !== null) {
      throw new Error(`temporary Ghost exited early with ${child.exitCode}`);
    }
    try {
      const res = await request('GET', '/ghost/api/admin/site/');
      if (res.status && res.status !== 503) return;
    } catch (e) {
      /* not up yet */
    }
    await sleep(2000);
  }
  throw new Error('temporary Ghost did not come up in 300 s');
}

async function main() {
  const { ADMIN_EMAIL: email, ADMIN_PASSWORD: password } = process.env;
  if (!email || !password) {
    throw new Error('ADMIN_EMAIL / ADMIN_PASSWORD missing from seed env');
  }
  const env = {
    ...process.env,
    server__host: '127.0.0.1',
    server__port: String(PORT),
  };
  const child = spawn(
    process.execPath,
    ['--conditions=source', '--import=tsx', 'index.js'],
    { cwd: CORE_DIR, env, stdio: 'inherit' }
  );
  try {
    await waitUp(child);
    const setup = await request('POST', '/ghost/api/admin/authentication/setup/', {
      setup: [{ name: 'ProofDeploy', email, password, blogTitle: 'ProofDeploy' }],
    });
    if (setup.status !== 201) {
      throw new Error(`setup failed: ${setup.status} ${setup.text.slice(0, 300)}`);
    }
    const session = await request('POST', '/ghost/api/admin/session/', {
      username: email,
      password,
    });
    if (session.status !== 201 && session.status !== 200) {
      throw new Error(
        `session failed: ${session.status} ${session.text.slice(0, 300)}`
      );
    }
    const cookie = (session.headers['set-cookie'] || [])
      .map((c) => c.split(';')[0])
      .join('; ');
    const created = await request(
      'POST',
      '/ghost/api/admin/integrations/?include=api_keys',
      { integrations: [{ name: 'proofdeploy' }] },
      { Cookie: cookie }
    );
    if (created.status !== 201) {
      throw new Error(
        `integration failed: ${created.status} ${created.text.slice(0, 300)}`
      );
    }
    const integration = JSON.parse(created.text).integrations[0];
    const adminKey = (integration.api_keys || []).find((k) => k.type === 'admin');
    if (!adminKey) {
      throw new Error('no admin api key in integration response');
    }
    // The integrations API returns an admin key's secret as the full
    // "<key id>:<secret>" pair (the form Ghost's own admin shows for
    // copying); write it as returned. If a future Ghost returns the
    // bare secret, pair it with the key id here.
    const keyPair = adminKey.secret.includes(':')
      ? adminKey.secret
      : `${adminKey.id}:${adminKey.secret}`;
    const keyDir = path.join(__dirname, '.pd-auth');
    fs.mkdirSync(keyDir, { recursive: true });
    fs.writeFileSync(path.join(keyDir, 'admin_key'), `${keyPair}\n`);
    console.log('ghost seed: setup complete, admin key written');
  } finally {
    child.kill('SIGTERM');
  }
  // Evidence for the record: the database file the temporary boot
  // resolved and wrote, under the checkout's content data dir. The
  // app side boots the same start form from the same checkout, so
  // this is the file it serves from — the auth proof's outcome is
  // the functional check; this line names the file.
  const dataDir = path.join(CORE_DIR, 'content', 'data');
  let dbFiles = [];
  try {
    dbFiles = fs
      .readdirSync(dataDir)
      .filter((name) => name.includes('.db'))
      .map(
        (name) =>
          `ghost/core/content/data/${name} (${fs.statSync(path.join(dataDir, name)).size} bytes)`
      );
  } catch (e) {
    /* no data dir yet — reported as none found */
  }
  console.log(
    `ghost seed: database files in checkout: ${dbFiles.length ? dbFiles.join(', ') : 'none found'}`
  );
}

main().then(
  () => process.exit(0),
  (err) => {
    console.error('ghost seed failed:', err.message);
    process.exit(1);
  }
);
