/* Help desk chat widget (hybrid bot + staff).
   Self-contained: injects its own styles + DOM. Include with:
     <script src="chat-widget.js" defer></script>
   Base-path aware (works under "/" or "/app"). */
(function () {
  "use strict";

  // ---- base path detection (mirror of pages' u()) ----
  var BASE = (function () {
    var p = window.location.pathname;
    var routes = ["/index.html", "/submission.html", "/admin", "/admin.html",
                  "/review", "/review.html", "/result-preview", "/result-preview.html",
                  "/result", "/result.html"];
    for (var i = 0; i < routes.length; i++) {
      if (p === routes[i] || p.endsWith(routes[i])) return p.slice(0, p.length - routes[i].length);
    }
    if (p.endsWith("/")) return p.slice(0, -1);
    return p;
  })();
  function u(path) { return BASE + path; }
  function g(path) { return u(path) + (path.indexOf("?") === -1 ? "?" : "&") + "_=" + Date.now(); }

  var STORAGE_KEY = "helpdesk_ticket_id";
  var ticketId = null;
  var pollTimer = null;
  var lastCount = 0;
  var currentStatus = null;

  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }

  // ---- styles ----
  // ---- styles (size-critical props use !important so host-page button/input
  //      rules like button{width:100%;padding:12px} cannot bleed in) ----
  var css = [
    ".hd-fab{position:fixed!important;right:20px!important;bottom:20px!important;width:56px!important;height:56px!important;min-width:0!important;max-width:none!important;border-radius:50%!important;background:#3b82f6!important;color:#fff!important;border:none!important;cursor:pointer;font-size:24px!important;line-height:1!important;padding:0!important;margin:0!important;box-shadow:0 6px 20px rgba(0,0,0,.4);z-index:2147483000!important;}",
    ".hd-fab:hover{background:#2563eb!important;}",
    ".hd-panel{position:fixed!important;right:20px!important;bottom:86px!important;width:340px!important;max-width:calc(100vw - 40px)!important;height:460px!important;max-height:calc(100vh - 120px)!important;background:#1e293b!important;border:1px solid #334155!important;border-radius:14px!important;box-shadow:0 10px 40px rgba(0,0,0,.5);display:none;flex-direction:column;overflow:hidden;z-index:2147483000!important;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;margin:0!important;padding:0!important;}",
    ".hd-panel.open{display:flex!important;}",
    ".hd-head{background:#0f172a;padding:12px 14px!important;border-bottom:1px solid #334155;display:flex!important;justify-content:space-between;align-items:center;flex:0 0 auto!important;}",
    ".hd-head .t{color:#e2e8f0;font-weight:700;font-size:15px!important;}",
    ".hd-head .s{color:#94a3b8;font-size:11px!important;margin-top:2px;}",
    ".hd-close{background:transparent!important;border:none!important;color:#94a3b8!important;font-size:18px!important;line-height:1!important;cursor:pointer;width:28px!important;height:28px!important;min-width:0!important;padding:0!important;margin:0!important;flex:0 0 auto!important;}",
    ".hd-body{flex:1 1 auto!important;overflow-y:auto;padding:14px!important;display:flex!important;flex-direction:column;gap:10px;background:#1e293b;min-height:0!important;}",
    ".hd-msg{max-width:85%!important;padding:8px 11px!important;border-radius:12px;font-size:13px!important;line-height:1.45!important;white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere;margin:0!important;}",
    ".hd-user{align-self:flex-end;background:#3b82f6;color:#fff;border-bottom-right-radius:4px;}",
    ".hd-bot{align-self:flex-start;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-bottom-left-radius:4px;}",
    ".hd-staff{align-self:flex-start;background:#064e3b;color:#a7f3d0;border:1px solid #10b981;border-bottom-left-radius:4px;}",
    ".hd-who{font-size:10px!important;color:#64748b;margin-bottom:3px;}",
    ".hd-foot{border-top:1px solid #334155;padding:10px!important;background:#0f172a;flex:0 0 auto!important;}",
    ".hd-row{display:flex!important;gap:8px!important;align-items:stretch;width:100%!important;}",
    ".hd-row input{flex:1 1 auto!important;min-width:0!important;width:auto!important;max-width:none!important;padding:9px 11px!important;margin:0!important;border:1px solid #334155!important;border-radius:8px!important;background:#1e293b!important;color:#e2e8f0!important;font-size:13px!important;height:auto!important;}",
    ".hd-row input:focus{outline:none!important;border-color:#3b82f6!important;}",
    ".hd-send{flex:0 0 auto!important;width:auto!important;min-width:0!important;max-width:none!important;background:#3b82f6!important;color:#fff!important;border:none!important;border-radius:8px!important;padding:9px 16px!important;margin:0!important;font-size:13px!important;font-weight:600!important;cursor:pointer;height:auto!important;}",
    ".hd-send:hover{background:#2563eb!important;}",
    ".hd-esc{display:block!important;margin:8px 0 0!important;width:100%!important;max-width:none!important;background:transparent!important;border:1px solid #475569!important;color:#94a3b8!important;border-radius:8px!important;padding:8px!important;font-size:12px!important;font-weight:400!important;cursor:pointer;}",
    ".hd-esc:hover{background:#334155!important;color:#e2e8f0!important;}"
  ].join("");
  var style = document.createElement("style"); style.textContent = css; document.head.appendChild(style);

  // ---- DOM ----
  var fab = document.createElement("button");
  fab.className = "hd-fab"; fab.title = "Help / สอบถาม"; fab.innerHTML = "💬";

  var panel = document.createElement("div");
  panel.className = "hd-panel";
  panel.innerHTML =
    '<div class="hd-head"><div><div class="t">Help desk</div><div class="s" id="hd-status">ถามได้เลยครับ</div></div>'
    + '<button class="hd-close" title="ปิด">×</button></div>'
    + '<div class="hd-body" id="hd-body"></div>'
    + '<div class="hd-foot">'
    + '<div class="hd-row"><input id="hd-input" type="text" placeholder="พิมพ์คำถาม..." autocomplete="off" />'
    + '<button class="hd-send" id="hd-send">ส่ง</button></div>'
    + '<button class="hd-esc" id="hd-esc">ติดต่อเจ้าหน้าที่</button>'
    + '</div>';

  document.body.appendChild(fab);
  document.body.appendChild(panel);
  fab.id = "hd-fab-el";
  // Scope the box-sizing reset to our own nodes without a wrapper that would
  // break position:fixed — tag both roots so "#hd-root *" style can match.
  var rootStyle = document.createElement("style");
  rootStyle.textContent = ".hd-fab,.hd-fab *,.hd-panel,.hd-panel *{box-sizing:border-box;}";
  document.head.appendChild(rootStyle);

  var bodyEl = panel.querySelector("#hd-body");
  var inputEl = panel.querySelector("#hd-input");
  var statusEl = panel.querySelector("#hd-status");

  function addMsg(sender, text) {
    var wrap = document.createElement("div");
    var cls = sender === "user" ? "hd-user" : (sender === "staff" ? "hd-staff" : "hd-bot");
    var who = sender === "user" ? "คุณ" : (sender === "staff" ? "เจ้าหน้าที่" : "ผู้ช่วย");
    wrap.className = "hd-msg " + cls;
    wrap.innerHTML = '<div class="hd-who">' + esc(who) + "</div>" + esc(text);
    bodyEl.appendChild(wrap);
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }

  function setStatus(s) {
    var map = {
      open: "พร้อมช่วยเหลือ",
      awaiting_staff: "⏳ ส่งต่อเจ้าหน้าที่แล้ว — รอตอบกลับ",
      answered: "✓ เจ้าหน้าที่ตอบแล้ว",
      closed: "✅ ตอบคำถามเรียบร้อย (ปิดเรื่อง)"
    };
    statusEl.textContent = map[s] || "พร้อมช่วยเหลือ";
  }

  async function ensureTicket() {
    if (ticketId) return ticketId;
    var saved = sessionStorage.getItem(STORAGE_KEY);
    if (saved) {
      // verify it still exists
      try {
        var r = await fetch(g("/api/chat/messages?ticket=" + saved));
        if (r.ok) { ticketId = Number(saved); return ticketId; }
      } catch (e) {}
    }
    var res = await fetch(u("/api/chat/start"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ submissionRef: new URLSearchParams(location.search).get("id") || null })
    });
    var data = await res.json();
    ticketId = data.ticketId;
    sessionStorage.setItem(STORAGE_KEY, ticketId);
    return ticketId;
  }

  async function refresh() {
    if (!ticketId) return;
    try {
      var res = await fetch(g("/api/chat/messages?ticket=" + ticketId));
      if (!res.ok) return;
      var data = await res.json();
      if (data.messages.length !== lastCount) {
        bodyEl.innerHTML = "";
        data.messages.forEach(function (m) { addMsg(m.sender, m.text); });
        lastCount = data.messages.length;
      }
      currentStatus = data.status;
      setStatus(data.status);
    } catch (e) {}
  }

  function newTicket() {
    // Force a fresh ticket for a new question after one was closed.
    ticketId = null; lastCount = 0; currentStatus = null;
    sessionStorage.removeItem(STORAGE_KEY);
  }

  async function send(text, escalate) {
    text = (text || "").trim();
    if (!text && !escalate) return;
    // A closed ticket is resolved — a new question starts a new ticket.
    if (currentStatus === "closed" && !escalate) { newTicket(); }
    await ensureTicket();
    if (text) { addMsg("user", text); lastCount++; }
    try {
      var res = await fetch(u("/api/chat/send"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticketId: ticketId, text: text || "(ขอติดต่อเจ้าหน้าที่)", escalate: !!escalate })
      });
      var data = await res.json();
      if (data.ok) {
        addMsg("bot", data.reply);
        lastCount++;
        currentStatus = data.status;
        setStatus(data.status);
        // If the bot closed it (resolved), sync the full thread (shows the
        // follow-up note). If it needs a human, the status line already says so.
        refresh();
      }
    } catch (e) {
      addMsg("bot", "เกิดข้อผิดพลาดในการเชื่อมต่อ กรุณาลองใหม่");
    }
  }

  function openPanel() {
    panel.classList.add("open");
    ensureTicket().then(function () {
      refresh();
      if (lastCount === 0) {
        addMsg("bot", "สวัสดีครับ 👋 ผมช่วยตอบคำถามเกี่ยวกับการยื่นเอกสารได้ ลองถามได้เลย เช่น 'ต้องแนบเอกสารอะไรบ้าง' หรือ 'เช็กผลยังไง'");
      }
    });
    if (!pollTimer) pollTimer = setInterval(refresh, 5000);
  }
  function closePanel() {
    panel.classList.remove("open");
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  fab.addEventListener("click", function () {
    panel.classList.contains("open") ? closePanel() : openPanel();
  });
  panel.querySelector(".hd-close").addEventListener("click", closePanel);
  panel.querySelector("#hd-send").addEventListener("click", function () {
    var t = inputEl.value; inputEl.value = ""; send(t, false);
  });
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { var t = inputEl.value; inputEl.value = ""; send(t, false); }
  });
  panel.querySelector("#hd-esc").addEventListener("click", function () {
    send("ขอติดต่อเจ้าหน้าที่", true);
  });
})();
