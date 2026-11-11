import test from 'node:test';
import assert from 'node:assert/strict';
import worker, { fetchHandler } from '../worker.js';

const env = {
  GITEE_WEBHOOK_SECRET: 'secret', GITHUB_DISPATCH_TOKEN: 'token', GITHUB_DISPATCH_REPO: 'owner/repo',
  GITEE_SOURCES_JSON: JSON.stringify([{ repository: 'private/repo', branches: ['release/v1'] }])
};
const relayEnv = { ...env, RELAY_WEBHOOK_SECRET: 'relay-secret' };

test('Worker entry point keeps ExecutionContext separate from fetch dependencies', async () => {
  const originalFetch = globalThis.fetch;
  let dispatched = false;
  globalThis.fetch = async () => { dispatched = true; return new Response(null, { status: 204 }); };
  try {
    const response = await worker.fetch(new Request('https://worker.example/hooks/gitee', {
      method: 'POST', headers: { 'X-Gitee-Token': 'secret', 'X-Gitee-Event': 'Push Hook' },
      body: JSON.stringify({ repository: { full_name: 'private/repo' }, ref: 'refs/heads/release/v1' }),
    }), env, { waitUntil() {} });
    assert.equal(response.status, 204);
    assert.equal(dispatched, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('rejects an unauthenticated webhook', async () => {
  const response = await fetchHandler(new Request('https://worker.example/hooks/gitee', { method: 'POST', body: '{}' }), env, { fetch: async () => { throw new Error('must not dispatch'); } });
  assert.equal(response.status, 401);
});

test('accepts the relay secret without exposing the Gitee webhook secret', async () => {
  let dispatched = false;
  const response = await fetchHandler(new Request('https://worker.example/hooks/gitee', {
    method: 'POST', headers: { 'X-Relay-Token': 'relay-secret', 'X-Gitee-Token': 'secret', 'X-Gitee-Event': 'Push Hook' },
    body: JSON.stringify({ repository: { full_name: 'private/repo' }, ref: 'refs/heads/release/v1' }),
  }), relayEnv, { fetch: async () => { dispatched = true; return new Response(null, { status: 204 }); } });
  assert.equal(response.status, 204);
  assert.equal(dispatched, true);
});

test('rejects a relay request without the original Gitee token', async () => {
  const response = await fetchHandler(new Request('https://worker.example/hooks/gitee', {
    method: 'POST', headers: { 'X-Relay-Token': 'relay-secret', 'X-Gitee-Event': 'Push Hook' }, body: '{}'
  }), relayEnv, { fetch: async () => { throw new Error('must not dispatch'); } });
  assert.equal(response.status, 401);
});

test('dispatches a verified push event without forwarding source data', async () => {
  let request;
  const response = await fetchHandler(new Request('https://worker.example/hooks/gitee', {
    method: 'POST', headers: { 'X-Gitee-Token': 'secret', 'X-Gitee-Event': 'Push Hook' }, body: JSON.stringify({ repository: { full_name: 'private/repo' }, ref: 'refs/heads/release/v1', head_commit: { id: 'private-sha' } })
  }), env, { fetch: async (url, init) => { request = { url, init }; return new Response(null, { status: 204 }); } });
  assert.equal(response.status, 204);
  assert.equal(request.url, 'https://api.github.com/repos/owner/repo/dispatches');
  assert.equal(request.init.body, JSON.stringify({ event_type: 'gitee-submodule-push' }));
});

test('ignores non-push events', async () => {
  const response = await fetchHandler(new Request('https://worker.example/hooks/gitee', {
    method: 'POST', headers: { 'X-Gitee-Token': 'secret', 'X-Gitee-Event': 'Merge Request Hook' }, body: '{}'
  }), env, { fetch: async () => { throw new Error('must not dispatch'); } });
  assert.equal(response.status, 204);
});

for (const body of [
  { repository: { full_name: 'other/repo' }, ref: 'refs/heads/release/v1' },
  { repository: { full_name: 'private/repo' }, ref: 'refs/heads/other' },
  { repository: { full_name: 'private/repo' }, ref: 'refs/tags/release/v1' },
  { repository: { full_name: 'private/repo' }, ref: 'refs/heads/release/v1', deleted: true },
]) {
  test(`does not dispatch an unconfigured or deleted ref: ${JSON.stringify(body)}`, async () => {
    const response = await fetchHandler(new Request('https://worker.example/hooks/gitee', {
      method: 'POST', headers: { 'X-Gitee-Token': 'secret', 'X-Gitee-Event': 'Push Hook' }, body: JSON.stringify(body)
    }), env, { fetch: async () => { throw new Error('must not dispatch'); } });
    assert.equal(response.status, 204);
  });
}

test('fails closed when webhook configuration is missing', async () => {
  const response = await fetchHandler(new Request('https://worker.example/hooks/gitee', {
    method: 'POST', headers: { 'X-Gitee-Event': 'Push Hook' }, body: '{}'
  }), {}, { fetch: async () => { throw new Error('must not dispatch'); } });
  assert.equal(response.status, 503);
});

test('returns a generic error when dispatch fails', async () => {
  const response = await fetchHandler(new Request('https://worker.example/hooks/gitee', {
    method: 'POST', headers: { 'X-Gitee-Token': 'secret', 'X-Gitee-Event': 'Push Hook' }, body: JSON.stringify({ repository: { full_name: 'private/repo' }, ref: 'refs/heads/release/v1' })
  }), env, { fetch: async () => { throw new Error('private error details'); } });
  assert.equal(response.status, 502);
  assert.doesNotMatch(await response.text(), /private/);
});

test('matches the repository URL rather than the Gitee display name', async () => {
  let sent = false;
  const response = await fetchHandler(new Request('https://worker.example/hooks/gitee', {
    method: 'POST', headers: { 'X-Gitee-Token': 'secret', 'X-Gitee-Event': 'Push Hook' },
    body: JSON.stringify({ repository: { full_name: '团队/显示名称', html_url: 'https://gitee.com/private/repo' }, ref: 'refs/heads/release/v1' })
  }), env, { fetch: async () => { sent = true; return new Response(null, { status: 204 }); } });
  assert.equal(response.status, 204);
  assert.equal(sent, true);
});

test('rejects oversized bodies before dispatch', async () => {
  const response = await fetchHandler(new Request('https://worker.example/hooks/gitee', {
    method: 'POST', headers: { 'X-Gitee-Token': 'secret', 'X-Gitee-Event': 'Push Hook' }, body: ' '.repeat(1048577)
  }), env, { fetch: async () => { throw new Error('must not dispatch'); } });
  assert.equal(response.status, 413);
});
