// Trivial fixture server: readiness only.
const http = require("http");
const port = Number(process.env.PORT || 3000);
http
  .createServer((req, res) => {
    if (req.url === "/health") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: true }));
    } else {
      res.writeHead(404);
      res.end();
    }
  })
  .listen(port);
