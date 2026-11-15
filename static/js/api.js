// fetch wrapper: JSON in/out, readable errors, DB-down banner.
import { $ } from './util.js';

export class ApiError extends Error {
  constructor(msg, status, data) { super(msg); this.status = status; this.data = data; }
}

export function setDbBanner(down, text) {
  const b = $('#dbBanner');
  if (down) { b.textContent = text || 'Database unavailable. Start MySQL with `docker compose up -d db`. Changes cannot be saved until it is back.'; b.classList.remove('hidden'); }
  else b.classList.add('hidden');
}

export async function api(method, url, body, { raw = false } = {}) {
  const opt = { method, headers: {} };
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) { opt.body = JSON.stringify(body); opt.headers['Content-Type'] = 'application/json'; }
  let res;
  try { res = await fetch(url, opt); }
  catch (e) { throw new ApiError('Cannot reach the Bulk API Runner server. Is `python run.py` still running?', 0); }
  if (raw) return res;
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (res.status === 503 && data && data.db_down) setDbBanner(true);
  if (!res.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => (d.loc ? d.loc.slice(1).join('.') + ': ' : '') + d.msg).join('; ');
    throw new ApiError(msg || `HTTP ${res.status}`, res.status, data);
  }
  return data;
}
export const GET = (u) => api('GET', u);
export const POST = (u, b) => api('POST', u, b ?? {});
export const PUT = (u, b) => api('PUT', u, b);
export const PATCH = (u, b) => api('PATCH', u, b);
export const DEL = (u) => api('DELETE', u);
