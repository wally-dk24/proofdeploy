// The fixture app: a plain HTTP server (runs under node or bun).
const http = require("http");
const port = Number(process.env.PORT || 3000);
const server = http.createServer((req, res) => {
  if (req.url === "/health") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
    return;
  }
  res.writeHead(200, { "content-type": "text/plain" });
  res.end("checkout-scan\n");
});
server.listen(port, "0.0.0.0");
