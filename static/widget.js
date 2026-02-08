(function () {
  const agencyId = document.currentScript.getAttribute("data-agency");
  const BASE_URL = "https://luxury-leads-ai.onrender.com";

  async function getAgencyInfo() {
    try {
      const res = await fetch(`${BASE_URL}/agency/${agencyId}`);
      const data = await res.json();
      return {
        agency: data.name || "Assistant",
        assistant: data.assistant || "Assistant"
      };
    } catch {
      return { agency: "Assistant", assistant: "Assistant" };
    }
  }

  getAgencyInfo().then((info) => {

    // ---------- FLOAT BUTTON ----------
    const button = document.createElement("div");
    button.innerHTML = "💬";
    button.style = `
      position: fixed; bottom: 20px; right: 20px;
      width: 60px; height: 60px;
      background: #000; color: white;
      border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 26px; cursor: pointer;
      box-shadow: 0 5px 15px rgba(0,0,0,0.4);
      z-index: 9999;
    `;
    document.body.appendChild(button);

    // ---------- CHAT BOX ----------
    const chatBox = document.createElement("div");
    chatBox.style = `
      position: fixed; bottom: 90px; right: 20px;
      width: 360px; height: 480px;
      background: #121212; color: white;
      border-radius: 16px;
      font-family: Arial;
      box-shadow: 0 8px 30px rgba(0,0,0,0.6);
      display: none; z-index: 9999;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    `;

    chatBox.innerHTML = `
      <div style="
        background:#000;padding:14px;
        display:flex;justify-content:space-between;
        font-weight:bold;
      ">
        <span>${info.agency}</span>
        <span id="chat-close" style="cursor:pointer;">✖</span>
      </div>

      <div id="chat-messages" style="
        flex:1;
        overflow:auto;
        padding:14px;
        display:flex;
        flex-direction:column;
        gap:6px;
      "></div>

      <div style="background:#1a1a1a;padding:10px;">
        <input id="chat-input" placeholder="Type message..."
          style="
            width:100%;
            padding:12px;
            border:none;
            border-radius:8px;
            background:#222;
            color:white;
          ">
      </div>
    `;

    document.body.appendChild(chatBox);

    const input = chatBox.querySelector("#chat-input");
    const messages = chatBox.querySelector("#chat-messages");
    const closeBtn = chatBox.querySelector("#chat-close");

    button.onclick = () => chatBox.style.display = "flex";
    closeBtn.onclick = () => chatBox.style.display = "none";

    // ---------- MESSAGE BUBBLE ----------
    function addBubble(text, sender) {
      const wrapper = document.createElement("div");
      wrapper.style = `
        display:flex;
        flex-direction:column;
        align-items:${sender==="user" ? "flex-end" : "flex-start"};
      `;

      const name = document.createElement("small");
      name.innerText = sender==="user" ? "You" : info.assistant;
      name.style = `
        opacity:0.6;
        margin-bottom:2px;
        font-size:11px;
      `;

      const bubble = document.createElement("div");
      bubble.innerText = text;
      bubble.style = `
        padding:10px 14px;
        border-radius:18px;
        max-width:75%;
        font-size:14px;
        line-height:1.4;
        background:${sender==="user" ? "#0084ff" : "#2a2a2a"};
        color:white;
        word-wrap:break-word;
      `;

      wrapper.appendChild(name);
      wrapper.appendChild(bubble);
      messages.appendChild(wrapper);
      messages.scrollTop = messages.scrollHeight;
    }

    // ---------- SEND MESSAGE ----------
    input.addEventListener("keypress", async function (e) {
      if (e.key === "Enter") {
        const text = input.value.trim();
        if (!text) return;

        addBubble(text, "user");
        input.value = "";

        const response = await fetch(`${BASE_URL}/chat`, {
          method: "POST",
          headers: {"Content-Type":"application/json"},
          body: JSON.stringify({message:text, agency_id:agencyId})
        });

        let data;
        try { data = await response.json(); }
        catch { data = {reply:"Server error"}; }

        addBubble(data.reply || "No response", "ai");
      }
    });

  });
})();
