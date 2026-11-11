function bytesEqual(left, right) {
  if (left.length !== right.length) return false;
  let result = 0;
  for (let index = 0; index < left.length; index += 1) result |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return result === 0;
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

export async function fetchHandler(request, env, runtime = globalThis) {
  const url = new URL(request.url);
  if (request.method === "GET" && url.pathname === "/health") return jsonResponse({ ok: true });
  if (request.method !== "POST" || url.pathname !== "/hooks/gitee") return new Response("Not found", { status: 404 });

  if (!env.GITEE_WEBHOOK_SECRET || !env.GITHUB_DISPATCH_TOKEN || !env.GITHUB_DISPATCH_REPO || !env.GITEE_SOURCES_JSON) return new Response("Service unavailable", { status: 503 });
  const supplied = request.headers.get("X-Gitee-Token") || "";
  if (!bytesEqual(supplied, env.GITEE_WEBHOOK_SECRET)) return new Response("Unauthorized", { status: 401 });
  if ((request.headers.get("X-Gitee-Event") || "").toLowerCase() !== "push hook") return new Response(null, { status: 204 });

  let payload;
  if (Number(request.headers.get('content-length')) > 1048576) return new Response('Payload too large', { status: 413 });
  const reader = request.body?.getReader();
  const chunks = [];
  let length = 0;
  if (reader) {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > 1048576) { await reader.cancel(); return new Response('Payload too large', { status: 413 }); }
      chunks.push(value);
    }
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  try { payload = JSON.parse(new TextDecoder().decode(bytes)); } catch { return new Response("Bad request", { status: 400 }); }
  let sources;
  try { sources = JSON.parse(env.GITEE_SOURCES_JSON); } catch { return new Response("Configuration error", { status: 500 }); }
  let repository = payload?.repository?.full_name;
  if (payload?.repository?.html_url) {
    try {
      const sourceUrl = new URL(payload.repository.html_url);
      if (sourceUrl.protocol !== 'https:' || sourceUrl.hostname !== 'gitee.com' || sourceUrl.port || sourceUrl.username || sourceUrl.password) return new Response('Invalid source', { status: 400 });
      repository = sourceUrl.pathname.replace(/^\//, '').replace(/\/$/, '').replace(/\.git$/, '');
    } catch { return new Response('Invalid source', { status: 400 }); }
  }
  const branch = typeof payload?.ref === "string" && payload.ref.startsWith("refs/heads/") ? payload.ref.slice("refs/heads/".length) : "";
  const source = Array.isArray(sources) && sources.find(item => item.repository === repository && Array.isArray(item.branches) && item.branches.includes(branch));
  if (!source || payload.deleted === true) return new Response(null, { status: 204 });

  const repo = env.GITHUB_DISPATCH_REPO;
  if (!repo || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) return new Response("Configuration error", { status: 500 });
  let response;
  try { response = await runtime.fetch(`https://api.github.com/repos/${repo}/dispatches`, {
    method: "POST",
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
      "content-type": "application/json",
      "user-agent": "submodule-auto-sync-worker",
    },
    body: JSON.stringify({ event_type: "gitee-submodule-push" }),
  }); } catch { return new Response("Dispatch failed", { status: 502 }); }
  return response.ok ? new Response(null, { status: 204 }) : new Response("Dispatch failed", { status: 502 });
}

export default {
  fetch(request, env) {
    // Cloudflare passes ExecutionContext as the third argument, not a fetch provider.
    return fetchHandler(request, env);
  },
};
