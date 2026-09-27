/* integration-rot docs + waitlist worker.
 * Served at navigatorslab.com/Integrationrot*.
 * Deploy: python3 worker/deploy.py  (injects docs/index.html, binds WAITLIST KV)
 */
addEventListener("fetch", (event) => {
  event.respondWith(handle(event.request).catch((e) => json({ ok: false, error: "server error" }, 500)));
});

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

async function handle(req) {
  const url = new URL(req.url);
  const path = url.pathname;

  if (path === "/Integrationrot/waitlist" && req.method === "POST") {
    let body;
    try {
      body = await req.json();
    } catch (e) {
      return json({ ok: false, error: "invalid JSON" }, 400);
    }
    const email = String(body.email || "").trim().toLowerCase().slice(0, 254);
    const useCase = String(body.use_case || "").trim().slice(0, 500);
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email)) {
      return json({ ok: false, error: "invalid email" }, 400);
    }
    const key = "email:" + email;
    if (await WAITLIST.get(key)) {
      return json({ ok: true, duplicate: true });
    }
    await WAITLIST.put(
      key,
      JSON.stringify({
        email: email,
        use_case: useCase,
        ts: new Date().toISOString(),
        ua: (req.headers.get("user-agent") || "").slice(0, 200),
      })
    );
    const n = (parseInt((await WAITLIST.get("count")) || "0", 10) || 0) + 1;
    await WAITLIST.put("count", String(n));
    return json({ ok: true, count: n });
  }

  if (path === "/Integrationrot/waitlist/count" && req.method === "GET") {
    const n = parseInt((await WAITLIST.get("count")) || "0", 10) || 0;
    return json({ count: n });
  }

  if (path === "/Integrationrot" || path === "/Integrationrot/") {
    return new Response(HTML, {
      headers: { "content-type": "text/html; charset=utf-8" },
    });
  }

  return new Response("not found", { status: 404 });
}

const HTML = __HTML__;
