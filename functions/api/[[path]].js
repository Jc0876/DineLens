export async function onRequest(context) {
  const { request, params, env } = context;
  const backend = env.BACKEND_URL;
  if (!backend) {
    return new Response(
      JSON.stringify({
        error: "backend_not_configured",
        detail: "Set the BACKEND_URL environment variable in the Pages project settings.",
      }),
      { status: 500, headers: { "content-type": "application/json; charset=utf-8" } }
    );
  }

  const path = Array.isArray(params.path) ? params.path.join("/") : (params.path || "");
  const url = new URL(request.url);
  const target = new URL(path + url.search, backend.replace(/\/+$/, "") + "/");

  const proxied = new Request(target.toString(), request);
  if (env.CF_ACCESS_CLIENT_ID && env.CF_ACCESS_CLIENT_SECRET) {
    proxied.headers.set("CF-Access-Client-Id", env.CF_ACCESS_CLIENT_ID);
    proxied.headers.set("CF-Access-Client-Secret", env.CF_ACCESS_CLIENT_SECRET);
  }

  try {
    const resp = await fetch(proxied);
    return new Response(resp.body, resp);
  } catch (err) {
    return new Response(
      JSON.stringify({ error: "backend_offline", detail: String(err) }),
      { status: 503, headers: { "content-type": "application/json; charset=utf-8" } }
    );
  }
}
