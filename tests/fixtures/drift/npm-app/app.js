// Minimal app for the drift fixtures: the install refuses before
// this ever runs; it exists so the contract is complete.
const http = require("http");
const port = Number(process.env.PORT || 3000);
http
  .createServer((req, res) => {
    res.writeHead(200, { "content-type": "text/plain" });
    res.end("ok");
  })
  .listen(port);
