(function () {
  const agencyId = document.currentScript.getAttribute("data-agency");
  const BASE_URL = "https://luxury-leads-ai.onrender.com";

  async function getAgencyName() {
    try {
      const res = await fetch(`${BASE_URL}/agency/${agencyId}`);
      const data = await res.json();
      return data.name || "Assistant";
    } catch {
      return "Assistant";
    }
  }

  getAgencyName().then((agencyName) => {

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

    const chatBox = document.createElement("div");
    chatBox.style = `
      position: fixed; bottom: 90px; right: 20px;
      width: 340px; height: 420px;
      background: #1e1e1e; color: white;
      border-radius: 14px;
      font-family: Arial;
      box-shadow: 0 5px 20px rgba(0,0,0,0.5);
      display: none; z-index: 9999;
      overflow: hidden;
    `;

    chatBox.innerHTML = `
      <div style="background:#000;padding:12px;display:flex;justify-content:space-between;">
        <span>${agencyName}</span>
        <span id="chat-close" style="cursor:pointer;">✖</span>
      </div>
      <div id="chat-messages" style="height:300px;overflow:auto;padding:12px;"></div>
      <input id="chat-input" placeholder="Type message..." 
        style="width:100%;padding:14px;border:none;background:#111;color:white;">
    `;

    document.body.appendChild(chatBox);

    const input = chatBox.querySelector("#chat-input");
    const messages = chatBox.querySelector("#chat-messages");
    const closeBtn = chatBox.querySelector("#chat-close");

    button.onclick = () => chatBox.style.display = "block";
    closeBtn.onclick = () => chatBox.style.display = "none";

    function addBubble(text, sender) {
      const div = document.createElement("div");
      div.style = `
        margin:8px 0;
        padding:10px 14px;
        border-radius:16px;
        max-width:80%;
        background:${sender==="user" ? "#0084ff" : "#333"};
        align-self:${sender==="user" ? "flex-end" : "flex-start"};
      `;
      div.innerText = text;
      messages.appendChild(div);
      messages.scrollTop = messages.scrollHeight;
    }

    input.addEventListener("keypress", async function (e) {
      if (e.key === "Enter") {
        const text = input.value;
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
