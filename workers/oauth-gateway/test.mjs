// Functional test of the OAuth gateway (Node 20 globals, no network).
// Run: npm test  (or: node test.mjs)
import worker from "./src/index.js";
import { createHash } from "node:crypto";

const env = {
  OAUTH_CLIENT_ID: "sdmc-test",
  OAUTH_CLIENT_SECRET: "test-secret",
  JWT_SECRET: "test-jwt-secret-1234567890",
  ORIGIN_BASE: "https://origin.segal.to",
  SF_CLIENT_ID: "sf-id",
  SF_CLIENT_SECRET: "sf-secret",
};
const BASE = "https://flights.segal.to";
let pass = 0, fail = 0;
const ok = (name, cond, extra = "") => {
  console.log((cond ? "PASS " : "FAIL ") + name, extra);
  cond ? pass++ : fail++;
};
const J = async (r) => JSON.parse(await r.text());

// capture origin-bound fetches
let lastOrigin = null;
globalThis.fetch = async (url, init) => {
  lastOrigin = { url, init };
  return new Response(JSON.stringify({ ok: true }),
    { status: 200, headers: { "Content-Type": "application/json", "Mcp-Session-Id": "sess-1" } });
};

// 1. metadata
let r = await worker.fetch(new Request(`${BASE}/.well-known/oauth-authorization-server`), env);
let meta = await J(r);
ok("metadata", r.status === 200 && meta.token_endpoint === `${BASE}/oauth/token`);
r = await worker.fetch(new Request(`${BASE}/.well-known/oauth-protected-resource`), env);
ok("protected-resource", (await J(r)).resource === `${BASE}/mcp`);

// 2. client_credentials happy path
const form = (o) => new Request(`${BASE}/oauth/token`,
  { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(o) });
r = await worker.fetch(form({ grant_type: "client_credentials", client_id: "sdmc-test",
  client_secret: "test-secret" }), env);
let tok = await J(r);
ok("client-credentials", r.status === 200 && tok.access_token.split(".").length === 3 &&
  tok.token_type === "Bearer" && !!tok.refresh_token);
const ACCESS = tok.access_token;

// 3. wrong secret
r = await worker.fetch(form({ grant_type: "client_credentials", client_id: "sdmc-test",
  client_secret: "nope" }), env);
ok("bad-secret-401", r.status === 401);

// 4. /mcp without token -> 401 + resource_metadata hint
r = await worker.fetch(new Request(`${BASE}/mcp`, { method: "POST", body: "{}",
  headers: { "Content-Type": "application/json" } }), env);
ok("mcp-noauth-401", r.status === 401 &&
  (r.headers.get("www-authenticate") || "").includes("oauth-protected-resource"));

// 5. /mcp with token -> proxied, service token attached, session id returned
r = await worker.fetch(new Request(`${BASE}/mcp?x=1`, { method: "POST", body: "{}",
  headers: { "Content-Type": "application/json", "Authorization": `Bearer ${ACCESS}`,
    "Mcp-Session-Id": "abc" } }), env);
ok("mcp-proxy", r.status === 200 && r.headers.get("mcp-session-id") === "sess-1" &&
  lastOrigin.url === "https://origin.segal.to/mcp?x=1" &&
  lastOrigin.init.headers.get("CF-Access-Client-Id") === "sf-id" &&
  lastOrigin.init.headers.get("CF-Access-Client-Secret") === "sf-secret" &&
  lastOrigin.init.headers.get("Mcp-Session-Id") === "abc");

// 6. tampered token rejected
r = await worker.fetch(new Request(`${BASE}/mcp`, { method: "POST", body: "{}",
  headers: { "Authorization": `Bearer ${ACCESS.slice(0, -2)}xx` } }), env);
ok("mcp-tampered-401", r.status === 401);

// 7. authorize code + PKCE roundtrip
const verifier = "verifier-1234567890";
const challenge = createHash("sha256").update(verifier).digest("base64url");
const redir = "http://localhost:9999/cb";
r = await worker.fetch(new Request(
  `${BASE}/oauth/authorize?response_type=code&client_id=sdmc-test&redirect_uri=${encodeURIComponent(redir)}&code_challenge=${challenge}&code_challenge_method=S256&state=s1`), env);
ok("authorize-page", r.status === 200 && (await r.text()).includes("Approve"));
r = await worker.fetch(new Request(`${BASE}/oauth/authorize`,
  { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ approve: "1", client_id: "sdmc-test", redirect_uri: redir,
      code_challenge: challenge, code_challenge_method: "S256", state: "s1" }) }), env);
const loc = r.headers.get("location") || "";
const code = new URL(loc).searchParams.get("code");
ok("approve-302", r.status === 302 && !!code && loc.startsWith(redir));

// 8. code exchange
r = await worker.fetch(form({ grant_type: "authorization_code", code,
  redirect_uri: redir, code_verifier: verifier, client_id: "sdmc-test",
  client_secret: "test-secret" }), env);
tok = await J(r);
ok("code-exchange", r.status === 200 && tok.access_token.split(".").length === 3);

// 9. refresh flow
r = await worker.fetch(form({ grant_type: "refresh_token", refresh_token: tok.refresh_token,
  client_id: "sdmc-test", client_secret: "test-secret" }), env);
ok("refresh", r.status === 200 && (await J(r)).access_token.split(".").length === 3);

// 10. bad redirect rejected
r = await worker.fetch(new Request(
  `${BASE}/oauth/authorize?response_type=code&client_id=sdmc-test&redirect_uri=${encodeURIComponent("http://evil.com/cb")}`), env);
ok("bad-redirect-400", r.status === 400);

// 11. DCR echoes the single client
r = await worker.fetch(new Request(`${BASE}/oauth/register`,
  { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ redirect_uris: ["http://localhost:1"] }) }), env);
const dcr = await J(r);
ok("dcr", r.status === 201 && dcr.client_id === "sdmc-test");

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
