/**
 * SkyDemon MCP OAuth gateway (Cloudflare Worker).
 *
 * What Gemini's "custom connected app" form needs:
 *   app link    -> https://flights.segal.to/mcp
 *   client ID   -> OAUTH_CLIENT_ID secret
 *   client secret -> OAUTH_CLIENT_SECRET secret
 *
 * Endpoints (all paths below live on flights.segal.to via Worker routes):
 *   /.well-known/oauth-protected-resource  public metadata (RFC 9728)
 *   /.well-known/oauth-authorization-server public metadata (RFC 8414)
 *   /oauth/token     client_credentials + authorization_code + refresh_token
 *   /oauth/authorize owner approval page (authorization_code flow, PKCE)
 *   /oauth/register  dynamic client registration (returns the single client)
 *   /mcp             JWT-gated reverse proxy to the Python origin
 *
 * Secrets (wrangler secret put ...):
 *   OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET  -> the Gemini form values
 *   JWT_SECRET        -> HMAC key for our JWTs
 *   ORIGIN_BASE       -> e.g. https://origin.segal.to (tunnel, Access-locked)
 *   SF_CLIENT_ID, SF_CLIENT_SECRET -> Cloudflare Access service token, sent
 *     as CF-Access-Client-Id/Secret to the origin (Access policy on the
 *     origin hostname allows ONLY this service token).
 *
 * Env (wrangler.toml [vars]):
 *   ALLOWED_REDIRECTS -> comma prefixes, default
 *     "https://,http://localhost,http://127.0.0.1"
 */

const ACCESS_TTL = 3600; // 1h
const REFRESH_TTL = 30 * 24 * 3600; // 30d
const CODE_TTL = 600; // 10min

const b64url = {
  enc(bytes) {
    let s = "";
    for (const b of bytes) s += String.fromCharCode(b);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  },
  dec(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    const bin = atob(s + "=".repeat((4 - (s.length % 4)) % 4));
    return Uint8Array.from(bin, (c) => c.charCodeAt(0));
  },
};

async function hmacKey(secret) {
  return crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}

async function signJwt(payload, secret) {
  const head = b64url.enc(new TextEncoder().encode(JSON.stringify({ alg: "HS256", typ: "JWT" })));
  const body = b64url.enc(new TextEncoder().encode(JSON.stringify(payload)));
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", await hmacKey(secret),
    new TextEncoder().encode(`${head}.${body}`)));
  return `${head}.${body}.${b64url.enc(sig)}`;
}

async function verifyJwt(token, secret) {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const ok = await crypto.subtle.verify("HMAC", await hmacKey(secret),
    b64url.dec(parts[2]), new TextEncoder().encode(`${parts[0]}.${parts[1]}`));
  if (!ok) return null;
  try {
    const p = JSON.parse(new TextDecoder().decode(b64url.dec(parts[1])));
    if (p.exp * 1000 < Date.now()) return null;
    return p;
  } catch { return null; }
}

async function sha256B64url(s) {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)));
  return b64url.enc(d);
}

const rnd = (n = 32) => b64url.enc(crypto.getRandomValues(new Uint8Array(n)));

function issuer(req) {
  return `https://${new URL(req.url).hostname}`;
}

function redirectAllowed(url, env) {
  const list = (env.ALLOWED_REDIRECTS ||
    "https://,http://localhost,http://127.0.0.1").split(",").map((s) => s.trim()).filter(Boolean);
  return list.some((p) => url.startsWith(p));
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

function unauthorized(realm) {
  return new Response(JSON.stringify({ error: "unauthorized" }), {
    status: 401,
    headers: {
      "Content-Type": "application/json",
      "WWW-Authenticate": `Bearer resource_metadata="${realm}/.well-known/oauth-protected-resource"`,
    },
  });
}

// ------------------------------------------------------------------ handlers

async function handleToken(req, env) {
  const iss = issuer(req);
  let params;
  const ct = req.headers.get("content-type") || "";
  if (ct.includes("application/x-www-form-urlencoded")) {
    params = Object.fromEntries(new URLSearchParams(await req.text()));
  } else {
    try { params = await req.json(); } catch { return json({ error: "invalid_request" }, 400); }
  }
  // client auth: basic or body (both accepted)
  let cid = params.client_id || "", csec = params.client_secret || "";
  const basic = req.headers.get("authorization") || "";
  if (basic.toLowerCase().startsWith("basic ")) {
    try {
      const [u, p] = atob(basic.slice(6)).split(/:(.*)/s);
      cid = cid || u || ""; csec = csec || p || "";
    } catch { /* fall through to mismatch */ }
  }
  if (cid !== env.OAUTH_CLIENT_ID || csec !== env.OAUTH_CLIENT_SECRET) {
    return json({ error: "invalid_client" }, 401);
  }
  const now = Math.floor(Date.now() / 1000);
  const grant = params.grant_type;

  if (grant === "client_credentials") {
    const access = await signJwt({ iss, aud: "skydemon-mcp", sub: "skydemon-user",
      iat: now, exp: now + ACCESS_TTL, typ: "access" }, env.JWT_SECRET);
    const refresh = await signJwt({ iss, aud: "skydemon-mcp", sub: "skydemon-user",
      iat: now, exp: now + REFRESH_TTL, typ: "refresh", jti: rnd(16) }, env.JWT_SECRET);
    return json({ access_token: access, token_type: "Bearer", expires_in: ACCESS_TTL,
      refresh_token: refresh });
  }

  if (grant === "authorization_code") {
    const claims = await verifyJwt(params.code || "", env.JWT_SECRET);
    if (!claims || claims.typ !== "code" || claims.cid !== cid) {
      return json({ error: "invalid_grant" }, 400);
    }
    if (claims.red !== params.redirect_uri) return json({ error: "invalid_grant" }, 400);
    if (claims.chm === "S256") {
      if (!params.code_verifier || await sha256B64url(params.code_verifier) !== claims.ch) {
        return json({ error: "invalid_grant" }, 400);
      }
    } else if (claims.ch && claims.ch !== params.code_verifier) {
      return json({ error: "invalid_grant" }, 400);
    }
    const access = await signJwt({ iss, aud: "skydemon-mcp", sub: "skydemon-user",
      iat: now, exp: now + ACCESS_TTL, typ: "access" }, env.JWT_SECRET);
    const refresh = await signJwt({ iss, aud: "skydemon-mcp", sub: "skydemon-user",
      iat: now, exp: now + REFRESH_TTL, typ: "refresh", jti: rnd(16) }, env.JWT_SECRET);
    return json({ access_token: access, token_type: "Bearer", expires_in: ACCESS_TTL,
      refresh_token: refresh });
  }

  if (grant === "refresh_token") {
    const claims = await verifyJwt(params.refresh_token || "", env.JWT_SECRET);
    if (!claims || claims.typ !== "refresh") return json({ error: "invalid_grant" }, 400);
    const access = await signJwt({ iss, aud: "skydemon-mcp", sub: "skydemon-user",
      iat: now, exp: now + ACCESS_TTL, typ: "access" }, env.JWT_SECRET);
    const refresh = await signJwt({ iss, aud: "skydemon-mcp", sub: "skydemon-user",
      iat: now, exp: now + REFRESH_TTL, typ: "refresh", jti: rnd(16) }, env.JWT_SECRET);
    return json({ access_token: access, token_type: "Bearer", expires_in: ACCESS_TTL,
      refresh_token: refresh });
  }

  return json({ error: "unsupported_grant_type" }, 400);
}

function approvePage(q) {
  const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  const hidden = ["client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method"]
    .map((k) => `<input type="hidden" name="${k}" value="${esc(q.get(k))}">`).join("");
  return new Response(
    `<!doctype html><html><body style="font-family:sans-serif;max-width:480px;margin:40px auto">` +
    `<h2>Allow SkyDemon MCP access?</h2><p>Client <b>${esc(q.get("client_id"))}</b> ` +
    `wants MCP access. Only approve requests you started yourself.</p>` +
    `<form method="post">${hidden}<input type="hidden" name="approve" value="1">` +
    `<button type="submit">Approve</button></form></body></html>`,
    { headers: { "Content-Type": "text/html" } });
}

async function handleAuthorize(req, env) {
  const iss = issuer(req);
  if (req.method === "GET") {
    const q = new URL(req.url).searchParams;
    if (q.get("response_type") !== "code" || q.get("client_id") !== env.OAUTH_CLIENT_ID) {
      return new Response("bad client/request", { status: 400 });
    }
    if (!q.get("redirect_uri") || !redirectAllowed(q.get("redirect_uri"), env)) {
      return new Response("redirect_uri not allowed", { status: 400 });
    }
    return approvePage(q);
  }
  // POST approval
  const form = Object.fromEntries(new URLSearchParams(await req.text()));
  if (form.approve !== "1" || form.client_id !== env.OAUTH_CLIENT_ID ||
      !form.redirect_uri || !redirectAllowed(form.redirect_uri, env)) {
    return new Response("denied", { status: 403 });
  }
  const now = Math.floor(Date.now() / 1000);
  const code = await signJwt({ iss, aud: "skydemon-mcp", cid: form.client_id,
    red: form.redirect_uri, ch: form.code_challenge || "",
    chm: form.code_challenge_method || "plain",
    iat: now, exp: now + CODE_TTL, typ: "code", jti: rnd(16) }, env.JWT_SECRET);
  const dest = new URL(form.redirect_uri);
  dest.searchParams.set("code", code);
  if (form.state) dest.searchParams.set("state", form.state);
  return Response.redirect(dest.toString(), 302);
}

async function handleMcp(req, env) {
  const iss = issuer(req);
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: {
      "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET,POST,DELETE",
      "Access-Control-Allow-Headers": "Authorization,Content-Type,Accept,Mcp-Session-Id,MCP-Protocol-Version",
      "Access-Control-Expose-Headers": "Mcp-Session-Id,Content-Type" } });
  }
  const auth = req.headers.get("authorization") || "";
  const m = auth.match(/^Bearer\s+(.+)$/i);
  const claims = m ? await verifyJwt(m[1].trim(), env.JWT_SECRET) : null;
  if (!claims || claims.typ !== "access" || claims.aud !== "skydemon-mcp" || claims.iss !== iss) {
    return unauthorized(iss);
  }
  const url = new URL(req.url);
  const fwd = new Headers();
  for (const [k, v] of req.headers) {
    const lk = k.toLowerCase();
    if (["host", "content-length", "authorization", "cf-connecting-ip",
         "cf-ipcountry", "cf-ray", "cf-visitor", "x-forwarded-for", "x-forwarded-proto"].includes(lk)) continue;
    fwd.set(k, v);
  }
  fwd.set("CF-Access-Client-Id", env.SF_CLIENT_ID);
  fwd.set("CF-Access-Client-Secret", env.SF_CLIENT_SECRET);
  const init = { method: req.method, headers: fwd, redirect: "manual" };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = req.body;
    init.duplex = "half";
  }
  const upstream = await fetch(`${env.ORIGIN_BASE.replace(/\/$/, "")}/mcp${url.search}`, init);
  const out = new Headers();
  for (const [k, v] of upstream.headers) {
    const lk = k.toLowerCase();
    if (["content-length", "content-encoding", "transfer-encoding", "connection"].includes(lk)) continue;
    out.set(k, v);
  }
  out.set("Access-Control-Allow-Origin", "*");
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

// ------------------------------------------------------------------ router

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const iss = issuer(req);
    const p = url.pathname;

    if (p === "/.well-known/oauth-protected-resource") {
      return json({ resource: `${iss}/mcp`,
        authorization_servers: [iss],
        scopes_supported: ["mcp:read", "mcp:write"] });
    }
    if (p === "/.well-known/oauth-authorization-server") {
      return json({ issuer: iss,
        authorization_endpoint: `${iss}/oauth/authorize`,
        token_endpoint: `${iss}/oauth/token`,
        registration_endpoint: `${iss}/oauth/register`,
        response_types_supported: ["code"],
        grant_types_supported: ["authorization_code", "client_credentials", "refresh_token"],
        token_endpoint_auth_methods_supported: ["client_secret_basic", "client_secret_post"],
        code_challenge_methods_supported: ["S256", "plain"] });
    }
    if (p === "/oauth/token" && req.method === "POST") return handleToken(req, env);
    if (p === "/oauth/authorize" && (req.method === "GET" || req.method === "POST")) {
      return handleAuthorize(req, env);
    }
    if (p === "/oauth/register" && req.method === "POST") {
      // Single-user server: hand out the one preconfigured client.
      return json({ client_id: env.OAUTH_CLIENT_ID, client_secret: env.OAUTH_CLIENT_SECRET,
        token_endpoint_auth_method: "client_secret_post",
        grant_types: ["authorization_code", "client_credentials", "refresh_token"],
        response_types: ["code"] }, 201);
    }
    if (p === "/mcp" || p.startsWith("/mcp/")) return handleMcp(req, env);
    if (p === "/oauth" || p === "/oauth/") {
      return new Response("SkyDemon MCP OAuth gateway. See /.well-known/oauth-authorization-server",
        { headers: { "Content-Type": "text/plain" } });
    }
    return new Response("not found", { status: 404 });
  },
};
