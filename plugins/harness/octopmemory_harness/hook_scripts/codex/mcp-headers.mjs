import { readFileSync } from "node:fs";

const { token } = JSON.parse(readFileSync(new URL("../config.json", import.meta.url), "utf8"));
if (typeof token !== "string" || !token.trim()) {
  throw new Error("Octop Memory MCP token is missing");
}
process.stdout.write(JSON.stringify({ Authorization: "Bearer " + token }));
