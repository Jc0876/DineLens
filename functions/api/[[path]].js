const BACKEND = "https://dinelens.ccwu.cc";

export async function onRequest(context) {
  const { request, params } = context;
  const path = Array.isArray(params.path) ? params.path.join("/") : (params.path || "");
  const url = new URL(request.url);
  const target = new URL(path + url.search, BACKEND + "/");
  try {
    const resp = await fetch(target.toString(), request);
    return new Response(resp.body, resp);
  } catch (err) {
    return new Response(
      JSON.stringify({ error: "backend_offline", detail: String(err) }),
      { status: 503, headers: { "content-type": "application/json; charset=utf-8" } }
    );
  }
}
