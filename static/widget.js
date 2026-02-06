(function () {
  const agencyId = document.currentScript.getAttribute("data-agency");
  const BASE_URL = "https://luxury-leads-ai.onrender.com";

  async function getAgencyName() {
    try {
      const res = await fetch(`${BASE_URL}/agency/${agencyId}`);
      const data = await res.json();
      return data.name || "AI Assistant";
    } catch {
      return "AI Assistant";
    }
  }

  getAgencyName().then((agencyName) => {

    // ---------- FLOATING BUTTON ----------
    const button = document.createElement("div");
    button.innerHTML = "💬";
    button.style = `
      position: fixed;
      bottom: 20px;
      right: 20px;
      width: 55px;
      height: 55px;
      background: black;
      color: white;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 24px;
      cursor: pointer;
      box-shadow: 0 4px 10px rgba(0,0,0,0.3);
      z-index: 9999;
    `;
    document.body.appendChild(button);

    // ---------- CHAT BOX ----------
    const chatBox = document.createElement("div");
    chatBox.style = `
      position: fixed;
      bottom: 90px;
      right: 20px;
      width: 300px;
      background: white;
      border: 1px solid #ccc;
      border-radius: 10px;
      font-family: Arial;
      box-shadow: 0 4px 10px rgba(0,0,0,0.2);
      overflow: hidden;
      display: none;
      z-index: 9999;
    `;

    chatBox.innerHTML = `
      <div style="background:black;color:white;padding:10px;display:flex;justify-content:space-between;">
        <span>${agencyName}</span>
        <span id="chat-close" style="cursor:pointer;">✖</span>
      </div>
      <div id="chat-messages" style="height:200px;overflow:auto;padding:10px;"></div>
      <input id="chat-input" placeholder="Type message..." style="width:100%;padding:10px;border:none;border-top:1px solid #ccc;" />
    `;

    document.body.appendChild(chatBox);

    const input = chatBox.querySelector("#chat-input");
    const messages = chatBox.querySelector("#chat-messages");
    const closeBtn = chatBox.querySelector("#chat-close");

    // ---------- TOGGLE ----------
    button.onclick = () => {
      chatBox.style.display = "block";
    };

    closeBtn.onclick = () => {
      chatBox.style.display = "none";
    };

    // ---------- CHAT SEND ----------
    input.addEventListener("keypress", async function (e) {
      if (e.key === "Enter") {
        const text = input.value;
        messages.innerHTML += `<div><b>You:</b> ${text}</div>`;
        input.value = "";

        const response = await fetch(`${BASE_URL}/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message: text,
            agency_id: agencyId
          })
        });

        let data;
try {
  data = await response.json();
} catch {
  data = { reply: "Server error" };
}

        messages.innerHTML += `<div><b>AI:</b> ${data.reply || data.error || "No response"}</div>`;
        messages.scrollTop = messages.scrollHeight;
      }
    });

  });

})();
