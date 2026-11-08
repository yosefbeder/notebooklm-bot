// Minimal Bot API contract used by tests/test_integration.py. No production token is used.
const http = require("http");
const chat = { id: 100, type: "private", first_name: "Test" };
const source = "The heart pumps blood through the human body. The lungs exchange oxygen and carbon dioxide.";
const server = http.createServer(async (req, res) => {
  const path = req.url.split("?")[0];
  const reply = (status, payload) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(payload));
  };
  const state = (await pm.state.get("telegram.requests")) || [];
  if (path === "/__requests") return reply(200, state);
  if (path === "/__reset") {
    await pm.state.set("telegram.requests", []);
    await pm.state.set("telegram.failure", null);
    return reply(200, { ok: true });
  }
  if (path === "/__fail-once") {
    await pm.state.set("telegram.failure", "sendMessage");
    return reply(200, { ok: true });
  }
  if (path.startsWith("/file/bot")) {
    state.push({ method: "download" });
    await pm.state.set("telegram.requests", state);
    res.writeHead(200, { "Content-Type": "text/plain" });
    return res.end(source);
  }
  const method = path.split("/").pop();
  // aiogram uses multipart/form-data even for ordinary method calls.
  const raw = await new Promise(resolve => {
    if (req.readableEnded) return resolve("");
    let value = "";
    req.on("data", chunk => value += chunk.toString());
    req.on("end", () => resolve(value));
  });
  const payload = typeof req.body === "string" ? req.body : raw;
  const body = req.body && typeof req.body === "object" ? req.body : {};
  if ((req.headers["content-type"] || "").includes("application/x-www-form-urlencoded"))
    for (const [key, value] of new URLSearchParams(payload)) body[key] = value;
  for (const match of payload.matchAll(/name="([^"]+)"\r\n\r\n([\s\S]*?)(?=\r\n--)/g))
    body[match[1]] = match[2];
  state.push({ method, body });
  await pm.state.set("telegram.requests", state);
  if ((await pm.state.get("telegram.failure")) === method) {
    await pm.state.set("telegram.failure", null);
    return reply(429, { ok: false, error_code: 429, description: "Too Many Requests",
                        parameters: { retry_after: 1 } });
  }
  let id = (await pm.state.get("telegram.sequence")) || 0;
  id++;
  await pm.state.set("telegram.sequence", id);
  const message = { message_id: id, date: 1700000000, chat, text: body.text || "Test" };
  const parse = value => typeof value === "string" ? JSON.parse(value) : value;
  if (method === "getMe") return reply(200, { ok: true, result: {
    id: 123456, is_bot: true, first_name: "Study", username: "study_test_bot"
  }});
  if (method === "getFile") return reply(200, { ok: true, result: {
    file_id: "source-file", file_unique_id: "source-unique", file_size: source.length,
    file_path: "sources/source.txt"
  }});
  if (method === "sendPoll" || method === "stopPoll") {
    const options = parse(body.options || '[{"text":"Heart"},{"text":"Lungs"}]');
    message.poll = { id: `poll-${id}`, question: body.question || "Question",
      options: options.map((o, i) => ({ text: o.text, voter_count: 0, persistent_id: String(i) })),
      total_voter_count: 0, is_closed: method === "stopPoll", is_anonymous: false,
      type: "quiz", allows_multiple_answers: false, allows_revoting: false, members_only: false };
    return reply(200, { ok: true, result: method === "stopPoll" ? message.poll : message });
  }
  if (method === "sendPhoto") message.photo = [{ file_id: "photo", file_unique_id: "photo",
                                                width: 200, height: 200 }];
  if (method === "sendDocument") message.document = { file_id: "pdf", file_unique_id: "pdf",
                                                     file_name: "study.pdf" };
  if (["sendMessage", "editMessageText", "sendDocument", "sendPhoto", "editMessageReplyMarkup"].includes(method))
    return reply(200, { ok: true, result: message });
  if (method === "answerCallbackQuery") return reply(200, { ok: true, result: true });
  return reply(501, { ok: false, error_code: 501, description: "Unmodeled Bot API method" });
});
server.listen(process.env.PORT || 4010);
