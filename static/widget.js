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

    const chatBox = document.createElement("div");
    chatBox.innerHTML = `
      <div id="ai-chat-widget" style="
        position: fixed;
        bottom: 20px;
        right: 20px;
        width: 300px;
        background: white;
        border: 1px solid #ccc;
        border-radius: 10px;
        font-family: Arial;
        box-shadow: 0 4px 10px rgba(0,0,0,0.2);
        overflow: hidden;
      ">
        <div style="background:#000;color:white;padding:10px;">
          ${agencyName}
        </div>
        <div id="chat-messages" style="height:200px;overflow:auto;padding:10px;"></div>
        <input id="chat-input" placeholder="Type message..." style="width:100%;padding:10px;border:none;border-top:1px solid #ccc;" />
      </div>
    `;

    document.body.appendChild(chatBox);

    const input = document.getElementById("chat-input");
    const messages = document.getElementById("chat-messages");

    input.addEventListener("keypress", async function (e) {
      if (e.key === "Enter") {
        const text = input.value;
        messages.innerHTML += `<div><b>You:</b> ${text}</div>`;
        input.value = "";

        const response = await fetch(`${BASE_URL}/chat`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            message: text,
            agency_id: agencyId
          })
        });

        const data = await response.json();
        messages.innerHTML += `<div><b>AI:</b> ${data.reply}</div>`;
        messages.scrollTop = messages.scrollHeight;
      }
    });

  });

})();
