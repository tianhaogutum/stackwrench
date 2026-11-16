// Synthetic demo server; never run by stackwrench.
const http = require("node:http");

http.createServer((request, response) => response.end("ok\n")).listen(3000);
