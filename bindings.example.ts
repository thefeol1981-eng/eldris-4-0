export {};

/**
 * Bindings injected by the Freebuff Sites platform on deploy.
 * Uncomment the section you need and remove the other.
 *
 * Assets:
 *   env.ASSETS.fetch(request) serves the prebuilt frontend dist (via the Worker's ASSETS binding).
 *
 * R2 storage (preview):
 *   env.ELDRIS_FILES is an R2 bucket bound in the Worker's context.
 *   Use it for upload/download, or for persisting user data.
 *
 * D1 database (production):
 *   env.ELDRIS_DB is a D1 SQL database bound in the Worker's context.
 *   Use env.ELDRIS_DB.query(sql) / .prepare(sql).bind(...) for SQL.
 */
// R2 (preview)
// export const ELDRIS_FILES = env.ELDRIS_FILES;
// D1 (production)
// export const ELDRIS_DB = env.ELDRIS_DB;

export interface EldisBindings {
  // Assets: serves frontend/dist from the Worker's ASSETS binding.
  ASSETS: { fetch: (input: RequestInfo, init?: RequestInit) => Promise<Response> };
  // Storage (uncomment in app.js after provisioning)
  // ELDRIS_FILES?: R2Bucket;
  // ELDRIS_DB?: D1Database;
}
